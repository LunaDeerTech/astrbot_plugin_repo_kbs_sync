from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain

from .repo_kbs_sync.config import ConfigError, PluginSettings
from .repo_kbs_sync.knowledge_base import (
    KnowledgeBaseGateway,
    KnowledgeBaseSyncStats,
)
from .repo_kbs_sync.repository import GitClient, GitRepositoryError, RemoteRepository
from .repo_kbs_sync.scanner import scan_repository
from .repo_kbs_sync.state import SyncState


STATE_KEY = "repo_kbs_sync:state"
NEXT_CHECK_AT_KEY = "repo_kbs_sync:next_check_at"
AUTO_LOOP_SLEEP_SECONDS = 60


@dataclass(frozen=True)
class RepositorySyncResult:
    repository_url: str
    display_repository_url: str
    branch: str
    remote_head: str
    sync_mode: str
    source_file_count: int
    mapped_file_count: int
    target_stats: tuple[KnowledgeBaseSyncStats, ...]
    config_fingerprint: str
    managed_documents: dict[str, tuple[str, ...]]


class Main(star.Star):
    """Synchronize one Git repository into multiple AstrBot knowledge bases."""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._git = GitClient()
        self._sync_lock = asyncio.Lock()
        self._manual_sync_pending = False
        self._auto_sync_task: asyncio.Task | None = None
        self._stopping = False

    async def initialize(self) -> None:
        self._stopping = False

    async def terminate(self) -> None:
        self._stopping = True
        await self._cancel_auto_sync_task()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self._stopping:
            self._restart_auto_sync_task()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repo_kbs_sync")
    async def sync_repository(self, event: AstrMessageEvent):
        if self._sync_lock.locked() or self._manual_sync_pending:
            yield event.plain_result("仓库知识库同步任务正在进行中，请稍后再试。")
            return

        self._manual_sync_pending = True
        try:
            async with self._sync_lock:
                yield event.plain_result("开始同步仓库到多个知识库，请稍候。")
                try:
                    settings = PluginSettings.from_mapping(self.config)
                    await self._send_notifications(
                        self._format_sync_start_notification(settings)
                    )
                    result = await self._sync_repository()
                    await self._record_state(result)
                    await self._schedule_next_auto_check_after_run()
                    await self._send_notifications(
                        self._format_sync_success_message(result)
                    )
                except Exception as exc:
                    logger.error("repo_kbs_sync manual sync failed: %s", exc, exc_info=True)
                    yield event.plain_result(f"仓库知识库同步失败：{exc}")
                    return

            yield event.plain_result(self._format_sync_success_message(result))
        finally:
            self._manual_sync_pending = False

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repo_kbs_sync_providers")
    async def list_knowledge_base_providers(self, event: AstrMessageEvent):
        provider_manager = self.context.kb_manager.provider_manager
        lines = ["可用于 repo_kbs_sync 配置的知识库模型提供商："]

        embedding_providers = getattr(
            provider_manager,
            "embedding_provider_insts",
            [],
        )
        lines.append("Embedding Providers:")
        lines.extend(
            f"- {self._format_provider_item(provider)}"
            for provider in embedding_providers
        )
        if not embedding_providers:
            lines.append("- （未找到）")

        rerank_providers = getattr(provider_manager, "rerank_provider_insts", [])
        lines.append("Rerank Providers:")
        lines.extend(
            f"- {self._format_provider_item(provider)}"
            for provider in rerank_providers
        )
        if not rerank_providers:
            lines.append("- （未找到，可留空）")

        lines.append("请将每行的 ID 填入插件配置 embedding_provider_id 或 rerank_provider_id。")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _format_provider_item(provider: Any) -> str:
        try:
            metadata = provider.meta()
            provider_id = getattr(metadata, "id", "unknown")
            model = getattr(metadata, "model", None)
            provider_type = getattr(metadata, "type", None)
            suffix = f"，model={model}" if model else ""
            type_suffix = f"，type={provider_type}" if provider_type else ""
            return f"id={provider_id}{type_suffix}{suffix}"
        except Exception as exc:
            return f"（无法读取提供商信息：{exc}）"

    def _restart_auto_sync_task(self) -> None:
        if self._auto_sync_task and not self._auto_sync_task.done():
            self._auto_sync_task.cancel()
        self._auto_sync_task = asyncio.create_task(
            self._auto_sync_loop(),
            name="repo_kbs_sync:auto_sync",
        )

    async def _cancel_auto_sync_task(self) -> None:
        task = self._auto_sync_task
        self._auto_sync_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _auto_sync_loop(self) -> None:
        while not self._stopping:
            try:
                settings = PluginSettings.from_mapping(self.config)
            except ConfigError:
                await asyncio.sleep(AUTO_LOOP_SLEEP_SECONDS)
                continue

            if not settings.auto_sync_enabled:
                await self._clear_auto_sync_state()
                await asyncio.sleep(AUTO_LOOP_SLEEP_SECONDS)
                continue

            next_check_at = await self._ensure_next_check_at(settings)
            delay = max(
                0.0,
                (next_check_at - datetime.now(timezone.utc)).total_seconds(),
            )
            if delay > 0:
                await asyncio.sleep(min(delay, AUTO_LOOP_SLEEP_SECONDS))
                continue

            if self._sync_lock.locked() or self._manual_sync_pending:
                logger.info("repo_kbs_sync auto-check skipped because sync is running")
                await self._schedule_next_auto_check_after_run(settings)
                continue

            try:
                should_sync = await self._has_sync_changes(settings)
            except Exception as exc:
                logger.error("repo_kbs_sync auto-check failed: %s", exc, exc_info=True)
                await self._schedule_next_auto_check_after_run(settings)
                continue

            if not should_sync:
                logger.info("repo_kbs_sync auto-check found no changes")
                await self._schedule_next_auto_check_after_run(settings)
                continue

            await self._send_notifications(
                self._format_sync_start_notification(settings)
            )
            async with self._sync_lock:
                try:
                    result = await self._sync_repository()
                    await self._record_state(result)
                    logger.info(self._format_sync_success_message(result))
                    await self._send_notifications(
                        self._format_sync_success_message(result)
                    )
                except Exception as exc:
                    logger.error("repo_kbs_sync auto-sync failed: %s", exc, exc_info=True)
                finally:
                    await self._schedule_next_auto_check_after_run(settings)

    async def _sync_repository(self) -> RepositorySyncResult:
        settings = PluginSettings.from_mapping(self.config)
        repository = RemoteRepository.from_config(
            settings.repository_url,
            settings.branch,
        )
        branch = await self._git.resolve_branch(repository)
        remote_head = await self._git.remote_head(repository, branch)
        config_fingerprint = settings.fingerprint(repository.normalized_url, branch)
        previous_state = await self._load_state()

        full_sync = not previous_state.matches(
            repository.normalized_url,
            branch,
            remote_head,
            config_fingerprint,
        )
        changed_paths: set[str] = set()

        with tempfile.TemporaryDirectory(prefix="repo_kbs_sync_") as temp_directory:
            repository_directory = Path(temp_directory) / "repository"
            await self._git.clone(repository, branch, repository_directory)

            if not full_sync:
                try:
                    changed_paths = await self._git.changed_paths(
                        repository_directory,
                        previous_state.remote_head or "",
                        remote_head,
                    )
                except GitRepositoryError as exc:
                    logger.warning(
                        "repo_kbs_sync could not calculate Git diff; using full sync: %s",
                        exc,
                    )
                    full_sync = True

            documents_by_kb = scan_repository(
                repository_directory,
                settings.enabled_rules,
                settings.allowed_file_types,
                settings.ignore_paths,
            )
            target_names = set(settings.configured_kb_names)
            previous_names = set(previous_state.managed_documents or {})
            all_names = sorted(target_names | previous_names)

            gateway = KnowledgeBaseGateway(self.context, settings)
            helpers: dict[str, Any] = {}
            for kb_name in sorted(target_names):
                helpers[kb_name] = await gateway.get_or_create(kb_name)
            for kb_name in sorted(previous_names - target_names):
                helpers[kb_name] = await gateway.get_existing(kb_name)

            target_stats: list[KnowledgeBaseSyncStats] = []
            managed_after: dict[str, tuple[str, ...]] = {}
            for kb_name in all_names:
                helper = helpers.get(kb_name)
                current_documents = documents_by_kb.get(kb_name, {})
                if helper is None:
                    # A removed/renamed KB no longer needs cleanup.  For a
                    # currently configured KB get_or_create above would have
                    # raised, so silently skipping here would hide a real bug.
                    if kb_name in target_names:
                        raise RuntimeError(f"无法获取目标知识库 {kb_name}。")
                    logger.warning(
                        "repo_kbs_sync skipped cleanup because old KB is missing: %s",
                        kb_name,
                    )
                    continue

                stats = await gateway.synchronize(
                    helper=helper,
                    kb_name=kb_name,
                    current_documents=current_documents,
                    managed_document_names=previous_state.managed_for(kb_name),
                    changed_source_paths=changed_paths,
                    full_sync=full_sync,
                )
                target_stats.append(stats)
                if kb_name in target_names:
                    managed_after[kb_name] = tuple(sorted(current_documents))

        unique_sources = {
            source_document.source_path.as_posix()
            for documents in documents_by_kb.values()
            for source_document in documents.values()
        }
        return RepositorySyncResult(
            repository_url=repository.normalized_url,
            display_repository_url=repository.display_url(),
            branch=branch,
            remote_head=remote_head,
            sync_mode="full" if full_sync else "incremental",
            source_file_count=len(unique_sources),
            mapped_file_count=sum(len(documents) for documents in documents_by_kb.values()),
            target_stats=tuple(target_stats),
            config_fingerprint=config_fingerprint,
            managed_documents=managed_after,
        )

    async def _has_sync_changes(self, settings: PluginSettings) -> bool:
        repository = RemoteRepository.from_config(
            settings.repository_url,
            settings.branch,
        )
        branch = await self._git.resolve_branch(repository)
        remote_head = await self._git.remote_head(repository, branch)
        fingerprint = settings.fingerprint(repository.normalized_url, branch)
        state = await self._load_state()
        return not state.matches(
            repository.normalized_url,
            branch,
            remote_head,
            fingerprint,
        )

    async def _load_state(self) -> SyncState:
        raw_state = await self.get_kv_data(STATE_KEY, None)
        return SyncState.from_value(raw_state)

    async def _record_state(self, result: RepositorySyncResult) -> None:
        state = SyncState(
            repository_url=result.repository_url,
            branch=result.branch,
            remote_head=result.remote_head,
            config_fingerprint=result.config_fingerprint,
            managed_documents=result.managed_documents,
        )
        await self.put_kv_data(STATE_KEY, state.to_json())

    async def _ensure_next_check_at(self, settings: PluginSettings) -> datetime:
        raw_value = await self.get_kv_data(NEXT_CHECK_AT_KEY, None)
        if isinstance(raw_value, str) and raw_value.strip():
            try:
                parsed = datetime.fromisoformat(raw_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                logger.warning("repo_kbs_sync discarded invalid next_check_at value")
                await self.delete_kv_data(NEXT_CHECK_AT_KEY)

        next_check = datetime.now(timezone.utc) + timedelta(
            hours=settings.auto_sync_interval_hours
        )
        await self.put_kv_data(NEXT_CHECK_AT_KEY, next_check.isoformat())
        return next_check

    async def _schedule_next_auto_check_after_run(
        self,
        settings: PluginSettings | None = None,
    ) -> None:
        if settings is None:
            try:
                settings = PluginSettings.from_mapping(self.config)
            except ConfigError:
                await self._clear_auto_sync_state()
                return
        if not settings.auto_sync_enabled:
            await self._clear_auto_sync_state()
            return
        next_check = datetime.now(timezone.utc) + timedelta(
            hours=settings.auto_sync_interval_hours
        )
        await self.put_kv_data(NEXT_CHECK_AT_KEY, next_check.isoformat())

    async def _clear_auto_sync_state(self) -> None:
        await self.delete_kv_data(NEXT_CHECK_AT_KEY)

    async def _send_notifications(self, text: str) -> None:
        tasks = []
        try:
            settings = PluginSettings.from_mapping(self.config)
        except ConfigError:
            return

        if settings.notify_owner_enabled:
            global_config = self.context.get_config()
            admin_ids = global_config.get("admins_id", [])
            if isinstance(admin_ids, list):
                tasks.extend(
                    self._send_private_notification(str(admin_id).strip(), text)
                    for admin_id in admin_ids
                    if str(admin_id).strip()
                )

        if settings.notify_group_enabled and settings.notify_group_id:
            tasks.append(
                self._send_group_notification(settings.notify_group_id, text)
            )

        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("repo_kbs_sync notification failed: %s", result)

    async def _send_private_notification(self, user_id: str, text: str) -> None:
        await self.context.send_message(
            self._notification_session("FriendMessage", user_id),
            MessageChain([Plain(text)]),
        )

    async def _send_group_notification(self, group_id: str, text: str) -> None:
        await self.context.send_message(
            self._notification_session("GroupMessage", group_id),
            MessageChain([Plain(text)]),
        )

    def _notification_session(self, message_type: str, session_id: str) -> str:
        """Build a current AstrBot unified message origin for aiocqhttp."""

        platform_manager = getattr(self.context, "platform_manager", None)
        if platform_manager is None:
            raise RuntimeError("AstrBot 平台管理器不可用，无法发送通知。")

        get_insts = getattr(platform_manager, "get_insts", None)
        platforms = (
            get_insts()
            if callable(get_insts)
            else getattr(platform_manager, "platform_insts", [])
        )
        for platform in platforms or []:
            try:
                metadata = platform.meta()
            except Exception:
                continue
            platform_name = getattr(metadata, "name", None)
            platform_id = getattr(metadata, "id", None)
            if platform_name == "aiocqhttp" or platform_id == "aiocqhttp":
                if isinstance(platform_id, str) and platform_id.strip():
                    return f"{platform_id.strip()}:{message_type}:{session_id}"

        raise RuntimeError("未找到可用的 aiocqhttp 平台，无法发送通知。")

    def _format_sync_start_notification(self, settings: PluginSettings) -> str:
        try:
            display_url = RemoteRepository.from_config(
                settings.repository_url,
                settings.branch,
            ).display_url()
        except GitRepositoryError:
            display_url = settings.repository_url
        mappings = "、".join(
            f"{rule.path.as_posix()} → {rule.kb_name}"
            for rule in settings.enabled_rules
        )
        return (
            "检测到仓库变化，开始同步到知识库。"
            f"\n仓库：{display_url}"
            f"\n路径映射：{mappings}"
        )

    def _format_sync_success_message(self, result: RepositorySyncResult) -> str:
        mode = "全量同步" if result.sync_mode == "full" else "差异同步"
        lines = [
            f"仓库知识库同步完成（{mode}）。",
            f"仓库：{result.display_repository_url}",
            f"分支：{result.branch}",
            f"递归匹配到源文件：{result.source_file_count} 个",
            f"映射到知识库的文档：{result.mapped_file_count} 个",
        ]
        for stats in result.target_stats:
            lines.append(
                f"- {stats.kb_name}：当前 {stats.file_count} 个，"
                f"新增/更新 {stats.imported_count} 个，删除 {stats.deleted_count} 个"
            )
        return "\n".join(lines)
