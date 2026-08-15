from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import delete

from astrbot.core import logger
from astrbot.core.knowledge_base.kb_helper import KBHelper
from astrbot.core.knowledge_base.models import KBMedia, KBDocument
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider

from .config import PluginSettings
from .scanner import SourceDocument
from .sync_plan import build_document_sync_plan


@dataclass(frozen=True)
class KnowledgeBaseSyncStats:
    kb_name: str
    file_count: int
    imported_count: int
    deleted_count: int


class KnowledgeBaseGateway:
    """Translate repository sync operations into AstrBot KB API calls."""

    def __init__(self, context: Any, settings: PluginSettings) -> None:
        self.context = context
        self.settings = settings

    async def get_existing(self, kb_name: str) -> KBHelper | None:
        return await self.context.kb_manager.get_kb_by_name(kb_name)

    async def get_or_create(self, kb_name: str) -> KBHelper:
        kb_manager = self.context.kb_manager
        configured_embedding_id, configured_rerank_id = (
            await self._validate_configured_provider_ids()
        )
        helper = await kb_manager.get_kb_by_name(kb_name)
        if helper is None:
            helper = await kb_manager.create_kb(
                kb_name=kb_name,
                description="由 repo_kbs_sync 自动创建",
                emoji="📚",
                embedding_provider_id=await self._select_embedding_provider_id(),
                rerank_provider_id=await self._select_rerank_provider_id(),
                chunk_size=self.settings.chunk_size,
                chunk_overlap=self.settings.chunk_overlap,
            )
        else:
            if (
                configured_embedding_id
                and helper.kb.embedding_provider_id != configured_embedding_id
            ):
                logger.warning(
                    "repo_kbs_sync will not replace the embedding provider of "
                    "existing KB %s automatically; current=%s configured=%s",
                    kb_name,
                    helper.kb.embedding_provider_id,
                    configured_embedding_id,
                )
            if (
                configured_rerank_id
                and helper.kb.rerank_provider_id != configured_rerank_id
            ):
                logger.warning(
                    "repo_kbs_sync will not replace the rerank provider of "
                    "existing KB %s automatically; current=%s configured=%s",
                    kb_name,
                    helper.kb.rerank_provider_id,
                    configured_rerank_id,
                )
        return await self._ensure_chunk_settings(helper)

    async def synchronize(
        self,
        helper: KBHelper,
        kb_name: str,
        current_documents: dict[str, SourceDocument],
        managed_document_names: tuple[str, ...],
        changed_source_paths: set[str],
        full_sync: bool,
    ) -> KnowledgeBaseSyncStats:
        if not hasattr(helper, "vec_db"):
            await helper.initialize()

        existing_documents = await self._list_all_documents(helper)
        documents_by_name: dict[str, list[KBDocument]] = {}
        for document in existing_documents:
            documents_by_name.setdefault(document.doc_name, []).append(document)

        plan = build_document_sync_plan(
            current_documents=current_documents,
            existing_document_names=documents_by_name,
            managed_document_names=managed_document_names,
            changed_source_paths=changed_source_paths,
            full_sync=full_sync,
        )

        deleted_count = 0
        for document_name in plan.delete_names:
            for document in documents_by_name.get(document_name, []):
                await self._delete_document_with_media(helper, document)
                deleted_count += 1
                logger.info(
                    "repo_kbs_sync deleted document: kb=%s file=%s doc_id=%s",
                    kb_name,
                    document_name,
                    document.doc_id,
                )

        imported_count = 0
        for source_document in plan.upload_documents:
            content = source_document.read_for_upload(self.settings.preprocess_mdx)
            file_type = source_document.upload_file_type

            async def progress_callback(stage: str, current: int, total: int) -> None:
                logger.info(
                    "repo_kbs_sync progress: kb=%s file=%s stage=%s progress=%s/%s",
                    kb_name,
                    source_document.document_name,
                    stage,
                    current,
                    total,
                )

            uploaded = await helper.upload_document(
                file_name=source_document.document_name,
                file_content=content,
                file_type=file_type,
                chunk_size=self.settings.chunk_size,
                chunk_overlap=self.settings.chunk_overlap,
                batch_size=self.settings.embedding_batch_size,
                tasks_limit=self.settings.embedding_tasks_limit,
                max_retries=self.settings.embedding_max_retries,
                progress_callback=progress_callback,
            )
            imported_count += 1
            logger.info(
                "repo_kbs_sync imported document: kb=%s file=%s chunks=%s",
                kb_name,
                source_document.document_name,
                getattr(uploaded, "chunk_count", "unknown"),
            )

        return KnowledgeBaseSyncStats(
            kb_name=kb_name,
            file_count=len(current_documents),
            imported_count=imported_count,
            deleted_count=deleted_count,
        )

    async def _ensure_chunk_settings(self, helper: KBHelper) -> KBHelper:
        kb = helper.kb
        desired_size = self.settings.chunk_size
        desired_overlap = self.settings.chunk_overlap
        if kb.chunk_size == desired_size and kb.chunk_overlap == desired_overlap:
            return helper

        manager = self.context.kb_manager
        updated = await manager.update_kb(
            kb_id=kb.kb_id,
            kb_name=kb.kb_name,
            description=kb.description,
            emoji=kb.emoji,
            embedding_provider_id=kb.embedding_provider_id,
            rerank_provider_id=kb.rerank_provider_id,
            chunk_size=desired_size,
            chunk_overlap=desired_overlap,
            top_k_dense=kb.top_k_dense,
            top_k_sparse=kb.top_k_sparse,
            top_m_final=kb.top_m_final,
        )
        if updated is None:
            raise RuntimeError(f"更新知识库 {kb.kb_name} 的分块配置失败。")
        if (
            updated.kb.chunk_size != desired_size
            or updated.kb.chunk_overlap != desired_overlap
        ):
            raise RuntimeError(f"更新知识库 {kb.kb_name} 的分块配置未生效。")
        return updated

    async def _select_embedding_provider_id(self) -> str:
        if self.settings.embedding_provider_id:
            await self._validate_configured_embedding_provider(
                self.settings.embedding_provider_id
            )
            return self.settings.embedding_provider_id

        provider_manager = self.context.kb_manager.provider_manager
        providers = getattr(provider_manager, "embedding_provider_insts", [])
        if not providers:
            raise ValueError("当前没有可用的嵌入模型，无法自动创建知识库。")
        provider = providers[0]
        provider_id = provider.meta().id
        resolved = await provider_manager.get_provider_by_id(provider_id)
        if not isinstance(resolved, EmbeddingProvider):
            raise ValueError(f"嵌入模型 {provider_id} 不可用，无法自动创建知识库。")
        return provider_id

    async def _select_rerank_provider_id(self) -> str | None:
        if self.settings.rerank_provider_id:
            await self._validate_configured_rerank_provider(
                self.settings.rerank_provider_id
            )
            return self.settings.rerank_provider_id

        provider_manager = self.context.kb_manager.provider_manager
        providers = getattr(provider_manager, "rerank_provider_insts", [])
        if not providers:
            return None
        provider = providers[0]
        provider_id = provider.meta().id
        resolved = await provider_manager.get_provider_by_id(provider_id)
        return provider_id if isinstance(resolved, RerankProvider) else None

    async def _validate_configured_provider_ids(
        self,
    ) -> tuple[str | None, str | None]:
        embedding_id = self.settings.embedding_provider_id
        rerank_id = self.settings.rerank_provider_id
        if embedding_id:
            await self._validate_configured_embedding_provider(embedding_id)
        if rerank_id:
            await self._validate_configured_rerank_provider(rerank_id)
        return embedding_id, rerank_id

    async def _validate_configured_embedding_provider(self, provider_id: str) -> None:
        provider_manager = self.context.kb_manager.provider_manager
        provider = await provider_manager.get_provider_by_id(provider_id)
        if not isinstance(provider, EmbeddingProvider):
            raise ValueError(
                f"配置的 embedding_provider_id={provider_id} 不存在，"
                "或它不是 Embedding Provider。"
            )

    async def _validate_configured_rerank_provider(self, provider_id: str) -> None:
        provider_manager = self.context.kb_manager.provider_manager
        provider = await provider_manager.get_provider_by_id(provider_id)
        if not isinstance(provider, RerankProvider):
            raise ValueError(
                f"配置的 rerank_provider_id={provider_id} 不存在，"
                "或它不是 Rerank Provider。"
            )

    async def _list_all_documents(self, helper: KBHelper) -> list[KBDocument]:
        documents: list[KBDocument] = []
        offset = 0
        limit = 200
        while True:
            batch = await helper.list_documents(offset=offset, limit=limit)
            documents.extend(batch)
            if len(batch) < limit:
                return documents
            offset += len(batch)

    async def _delete_document_with_media(
        self,
        helper: KBHelper,
        document: KBDocument,
    ) -> None:
        media_items = []
        list_media = getattr(helper.kb_db, "list_media_by_doc", None)
        if list_media is not None:
            media_items = await list_media(document.doc_id)
        media_paths = [
            Path(media.file_path)
            for media in media_items
            if getattr(media, "file_path", None)
        ]

        await self._delete_media_records(helper, document.doc_id)
        await helper.delete_document(document.doc_id)

        for media_path in media_paths:
            if not _is_inside(media_path, helper.kb_medias_dir):
                logger.warning(
                    "repo_kbs_sync skipped unsafe media path during cleanup: %s",
                    media_path,
                )
                continue
            try:
                if media_path.exists():
                    media_path.unlink()
            except OSError as exc:
                logger.warning(
                    "repo_kbs_sync failed to remove media file %s: %s",
                    media_path,
                    exc,
                )

        media_directory = helper.kb_medias_dir / document.doc_id
        if media_directory.exists() and _is_inside(
            media_directory,
            helper.kb_medias_dir,
        ):
            try:
                shutil.rmtree(media_directory)
            except OSError as exc:
                logger.warning(
                    "repo_kbs_sync failed to remove media directory %s: %s",
                    media_directory,
                    exc,
                )

    async def _delete_media_records(self, helper: KBHelper, doc_id: str) -> None:
        async with helper.kb_db.get_db() as session:
            await session.execute(delete(KBMedia).where(KBMedia.doc_id == doc_id))
            await session.commit()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
