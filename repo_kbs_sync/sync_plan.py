from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .scanner import SourceDocument


@dataclass(frozen=True)
class DocumentSyncPlan:
    """The minimal delete/upload work for one knowledge base."""

    delete_names: tuple[str, ...]
    upload_documents: tuple[SourceDocument, ...]


def build_document_sync_plan(
    current_documents: dict[str, SourceDocument],
    existing_document_names: Iterable[str],
    managed_document_names: Iterable[str],
    changed_source_paths: Iterable[str],
    full_sync: bool,
) -> DocumentSyncPlan:
    current_names = set(current_documents)
    existing_names = set(existing_document_names)
    managed_names = set(managed_document_names)
    changed_paths = {str(path) for path in changed_source_paths}

    if full_sync:
        upsert_names = set(current_names)
    else:
        upsert_names = {
            name
            for name, document in current_documents.items()
            if document.source_path.as_posix() in changed_paths
        }

    # A document newly assigned to a KB must be uploaded even when its Git
    # path did not appear in the diff (for example after adding a new rule).
    upsert_names.update(current_names - existing_names)

    # Only documents previously managed by this plugin are eligible for
    # deletion. This prevents a first run from deleting unrelated documents
    # that a user manually uploaded to the same knowledge base.
    delete_names = (managed_names - current_names) | (upsert_names & existing_names)
    uploads = tuple(
        current_documents[name]
        for name in sorted(upsert_names)
        if name in current_documents
    )
    return DocumentSyncPlan(
        delete_names=tuple(sorted(delete_names)),
        upload_documents=uploads,
    )
