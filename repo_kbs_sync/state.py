from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


STATE_VERSION = 1


@dataclass(frozen=True)
class SyncState:
    repository_url: str | None = None
    branch: str | None = None
    remote_head: str | None = None
    config_fingerprint: str | None = None
    managed_documents: dict[str, tuple[str, ...]] | None = None

    @property
    def has_snapshot(self) -> bool:
        return bool(
            self.repository_url
            and self.branch
            and self.remote_head
            and self.config_fingerprint
            and self.managed_documents is not None
        )

    def matches(
        self,
        repository_url: str,
        branch: str,
        remote_head: str,
        config_fingerprint: str,
    ) -> bool:
        return (
            self.has_snapshot
            and self.repository_url == repository_url
            and self.branch == branch
            and self.remote_head == remote_head
            and self.config_fingerprint == config_fingerprint
        )

    def managed_for(self, kb_name: str) -> tuple[str, ...]:
        if self.managed_documents is None:
            return ()
        return self.managed_documents.get(kb_name, ())

    def to_json(self) -> str:
        payload = {
            "version": STATE_VERSION,
            "repository_url": self.repository_url,
            "branch": self.branch,
            "remote_head": self.remote_head,
            "config_fingerprint": self.config_fingerprint,
            "managed_documents": {
                kb_name: sorted(names)
                for kb_name, names in (self.managed_documents or {}).items()
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_value(cls, value: Any) -> "SyncState":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return cls()
        if not isinstance(value, dict):
            return cls()
        if value.get("version") != STATE_VERSION:
            return cls()

        raw_manifest = value.get("managed_documents")
        manifest: dict[str, tuple[str, ...]] | None
        if raw_manifest is None:
            manifest = None
        elif isinstance(raw_manifest, dict):
            manifest = {
                str(kb_name): tuple(
                    sorted(
                        {
                            item.strip()
                            for item in names
                            if isinstance(item, str) and item.strip()
                        }
                    )
                )
                for kb_name, names in raw_manifest.items()
                if isinstance(names, (list, tuple, set))
            }
        else:
            manifest = None

        return cls(
            repository_url=_optional_string(value.get("repository_url")),
            branch=_optional_string(value.get("branch")),
            remote_head=_optional_string(value.get("remote_head")),
            config_fingerprint=_optional_string(value.get("config_fingerprint")),
            managed_documents=manifest,
        )


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
