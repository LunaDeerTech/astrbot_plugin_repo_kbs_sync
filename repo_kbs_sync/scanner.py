from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import SyncRule
from .mdx import preprocess_mdx_bytes


@dataclass(frozen=True)
class SourceDocument:
    """A repository file selected for one knowledge-base mapping."""

    source_path: PurePosixPath
    filesystem_path: Path
    extension: str

    @property
    def document_name(self) -> str:
        # Keep the repository-relative name as the stable KB document key.
        # AstrBot's markitdown parser infers the format from the file_name
        # suffix (not file_type) and has no ".mdx" converter, so MDX is
        # uploaded under a ".md" name after conversion.  source_path keeps
        # the ".mdx" suffix so rename/delete tracking stays unambiguous.
        if self.is_mdx:
            return self.source_path.with_suffix(".md").as_posix()
        return self.source_path.as_posix()

    @property
    def is_mdx(self) -> bool:
        return self.extension == ".mdx"

    @property
    def upload_file_type(self) -> str:
        return "md" if self.is_mdx else self.extension.removeprefix(".")

    def read_for_upload(self, preprocess_mdx: bool) -> bytes:
        content = self.filesystem_path.read_bytes()
        if self.is_mdx and preprocess_mdx:
            return preprocess_mdx_bytes(content)
        return content


def scan_repository(
    repository_root: Path,
    rules: Iterable[SyncRule],
    allowed_file_types: Iterable[str],
    ignore_paths: Iterable[PurePosixPath],
) -> dict[str, dict[str, SourceDocument]]:
    """Recursively collect allowed files for every matching mapping rule."""

    normalized_extensions = {extension.lower() for extension in allowed_file_types}
    normalized_ignores = tuple(ignore_paths)
    documents_by_kb: dict[str, dict[str, SourceDocument]] = {}
    enabled_rules = tuple(rule for rule in rules if rule.enabled)

    for filesystem_path in sorted(repository_root.rglob("*")):
        if not filesystem_path.is_file() or filesystem_path.is_symlink():
            continue
        try:
            relative_path = filesystem_path.relative_to(repository_root)
        except ValueError:
            continue
        if ".git" in relative_path.parts:
            continue

        relative = PurePosixPath(relative_path.as_posix())
        extension = filesystem_path.suffix.lower()
        if extension not in normalized_extensions:
            continue
        if _is_ignored(relative, normalized_ignores):
            continue

        document = SourceDocument(
            source_path=relative,
            filesystem_path=filesystem_path,
            extension=extension,
        )
        for rule in enabled_rules:
            if rule.matches(relative):
                documents_by_kb.setdefault(rule.kb_name, {})[
                    document.document_name
                ] = document

    return documents_by_kb


def _is_ignored(
    relative_path: PurePosixPath,
    ignore_paths: Iterable[PurePosixPath],
) -> bool:
    return any(
        ignore_path == PurePosixPath(".")
        or relative_path == ignore_path
        or ignore_path in relative_path.parents
        for ignore_path in ignore_paths
    )
