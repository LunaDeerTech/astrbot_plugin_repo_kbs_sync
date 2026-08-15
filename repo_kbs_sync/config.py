from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlparse


DEFAULT_ALLOWED_FILE_TYPES = (".md", ".mdx")
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_TASKS_LIMIT = 1
DEFAULT_EMBEDDING_MAX_RETRIES = 7
DEFAULT_AUTO_SYNC_INTERVAL_HOURS = 24


class ConfigError(ValueError):
    """Raised when the plugin configuration cannot be used safely."""


@dataclass(frozen=True)
class SyncRule:
    """One repository path mapped to one AstrBot knowledge base."""

    path: PurePosixPath
    kb_name: str
    enabled: bool = True

    def matches(self, relative_path: PurePosixPath) -> bool:
        if self.path == PurePosixPath("."):
            return True
        return relative_path == self.path or self.path in relative_path.parents

    def as_fingerprint_value(self) -> dict[str, Any]:
        return {
            "path": self.path.as_posix(),
            "kb_name": self.kb_name,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PluginSettings:
    repository_url: str
    branch: str | None
    git_proxy: str | None
    sync_rules: tuple[SyncRule, ...]
    embedding_provider_id: str | None
    rerank_provider_id: str | None
    allowed_file_types: tuple[str, ...]
    ignore_paths: tuple[PurePosixPath, ...]
    preprocess_mdx: bool
    chunk_size: int
    chunk_overlap: int
    embedding_batch_size: int
    embedding_tasks_limit: int
    embedding_max_retries: int
    auto_sync_enabled: bool
    auto_sync_interval_hours: int
    notify_owner_enabled: bool
    notify_group_enabled: bool
    notify_group_id: str | None

    @classmethod
    def from_mapping(cls, raw_config: Mapping[str, Any] | None) -> "PluginSettings":
        config = raw_config or {}
        repository_url = _read_string(
            config,
            "repository_url",
            fallback_key="remote_repo_url",
        )
        if not repository_url:
            raise ConfigError("请在插件配置中填写 repository_url。")

        branch = _optional_string(config.get("branch", config.get("remote_branch")))
        git_proxy = _parse_git_proxy(config.get("git_proxy"))
        sync_rules = _parse_sync_rules(config.get("sync_rules", []))
        if not sync_rules:
            raise ConfigError("请至少配置一条启用中的 sync_rules 路径映射。")

        embedding_provider_id = _optional_string(
            config.get("embedding_provider_id")
        )
        rerank_provider_id = _optional_string(config.get("rerank_provider_id"))
        allowed_file_types = _parse_extensions(
            config.get("allowed_file_types", DEFAULT_ALLOWED_FILE_TYPES)
        )
        ignore_paths = _parse_paths(config.get("ignore_paths", []), "ignore_paths")

        return cls(
            repository_url=repository_url,
            branch=branch,
            git_proxy=git_proxy,
            sync_rules=tuple(sync_rules),
            embedding_provider_id=embedding_provider_id,
            rerank_provider_id=rerank_provider_id,
            allowed_file_types=tuple(allowed_file_types),
            ignore_paths=tuple(ignore_paths),
            preprocess_mdx=_as_bool(config.get("preprocess_mdx", True), True),
            chunk_size=_positive_int(
                config.get("chunk_size", DEFAULT_CHUNK_SIZE),
                "chunk_size",
                DEFAULT_CHUNK_SIZE,
            ),
            chunk_overlap=_positive_or_zero_int(
                config.get("chunk_overlap", DEFAULT_CHUNK_OVERLAP),
                "chunk_overlap",
                DEFAULT_CHUNK_OVERLAP,
            ),
            embedding_batch_size=_positive_int(
                config.get("embedding_batch_size", DEFAULT_EMBEDDING_BATCH_SIZE),
                "embedding_batch_size",
                DEFAULT_EMBEDDING_BATCH_SIZE,
            ),
            embedding_tasks_limit=_positive_int(
                config.get("embedding_tasks_limit", DEFAULT_EMBEDDING_TASKS_LIMIT),
                "embedding_tasks_limit",
                DEFAULT_EMBEDDING_TASKS_LIMIT,
            ),
            embedding_max_retries=_positive_int(
                config.get("embedding_max_retries", DEFAULT_EMBEDDING_MAX_RETRIES),
                "embedding_max_retries",
                DEFAULT_EMBEDDING_MAX_RETRIES,
            ),
            auto_sync_enabled=_as_bool(config.get("auto_sync_enabled", True), True),
            auto_sync_interval_hours=_positive_int(
                config.get(
                    "auto_sync_interval_hours",
                    DEFAULT_AUTO_SYNC_INTERVAL_HOURS,
                ),
                "auto_sync_interval_hours",
                DEFAULT_AUTO_SYNC_INTERVAL_HOURS,
            ),
            notify_owner_enabled=_as_bool(
                config.get("notify_owner_enabled", False), False
            ),
            notify_group_enabled=_as_bool(
                config.get("notify_group_enabled", False), False
            ),
            notify_group_id=_optional_string(config.get("notify_group_id")),
        )

    @property
    def enabled_rules(self) -> tuple[SyncRule, ...]:
        return tuple(rule for rule in self.sync_rules if rule.enabled)

    @property
    def configured_kb_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(rule.kb_name for rule in self.enabled_rules))

    def fingerprint(self, normalized_repository_url: str, branch: str) -> str:
        """Return a stable value that invalidates incremental sync when needed."""

        payload = {
            "repository_url": normalized_repository_url,
            "branch": branch,
            "allowed_file_types": sorted(self.allowed_file_types),
            "ignore_paths": sorted(path.as_posix() for path in self.ignore_paths),
            "preprocess_mdx": self.preprocess_mdx,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "sync_rules": sorted(
                (rule.as_fingerprint_value() for rule in self.enabled_rules),
                key=lambda item: (item["kb_name"], item["path"]),
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def normalize_relative_path(raw_path: str, field_name: str = "path") -> PurePosixPath:
    if not isinstance(raw_path, str):
        raise ConfigError(f"{field_name} 必须是字符串。")

    cleaned = raw_path.strip().replace("\\", "/")
    if "\x00" in cleaned:
        raise ConfigError(f"{field_name} 不能包含 NUL 字符。")
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned == ".":
        return PurePosixPath(".")

    normalized = PurePosixPath(cleaned)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ConfigError(f"{field_name} 不能是绝对路径或包含 '..'：{raw_path}")
    return normalized


def _parse_sync_rules(raw_rules: Any) -> list[SyncRule]:
    if not isinstance(raw_rules, list):
        raise ConfigError("sync_rules 必须是列表。")

    rules: list[SyncRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, Mapping):
            raise ConfigError(f"sync_rules 第 {index} 项必须是对象。")
        if not _as_bool(raw_rule.get("enabled", True), True):
            continue

        raw_path = raw_rule.get("path", raw_rule.get("repo_path", "."))
        path = normalize_relative_path(raw_path, f"sync_rules[{index}].path")
        kb_name = _read_string(
            raw_rule,
            "kb_name",
            fallback_key="knowledge_base",
        )
        if not kb_name:
            kb_name = _optional_string(raw_rule.get("new_kb_name")) or ""
        if not kb_name:
            raise ConfigError(f"sync_rules 第 {index} 项缺少 kb_name。")
        if len(kb_name) > 100:
            raise ConfigError(f"sync_rules 第 {index} 项的 kb_name 不能超过 100 个字符。")

        rules.append(SyncRule(path=path, kb_name=kb_name, enabled=True))

    return rules


def _parse_extensions(raw_extensions: Any) -> list[str]:
    if isinstance(raw_extensions, str):
        values = raw_extensions.replace("，", ",").split(",")
    elif isinstance(raw_extensions, (list, tuple, set)):
        values = list(raw_extensions)
    else:
        raise ConfigError("allowed_file_types 必须是文件后缀列表。")

    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        extension = value.strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = f".{extension}"
        if extension == "." or "/" in extension or "\\" in extension:
            raise ConfigError(f"非法文件后缀：{value}")
        if extension not in result:
            result.append(extension)

    if not result:
        raise ConfigError("allowed_file_types 不能为空。")
    return result


def _parse_paths(raw_paths: Any, field_name: str) -> list[PurePosixPath]:
    if not isinstance(raw_paths, list):
        raise ConfigError(f"{field_name} 必须是列表。")
    result: list[PurePosixPath] = []
    for index, raw_path in enumerate(raw_paths, start=1):
        path = normalize_relative_path(raw_path, f"{field_name}[{index}]")
        if path not in result:
            result.append(path)
    return result


def _read_string(
    config: Mapping[str, Any],
    key: str,
    fallback_key: str | None = None,
) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        if fallback_key:
            value = config.get(fallback_key)
    return value.strip() if isinstance(value, str) else ""


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_git_proxy(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError("git_proxy 必须是字符串。")

    proxy = value.strip()
    if not proxy:
        return None
    if "\x00" in proxy or any(char.isspace() for char in proxy):
        raise ConfigError("git_proxy 不能包含空白字符或 NUL 字符。")

    try:
        parsed = urlparse(proxy)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ConfigError("git_proxy 必须是有效的代理 URL。") from exc

    allowed_schemes = {
        "http",
        "https",
        "socks4",
        "socks4a",
        "socks5",
        "socks5h",
    }
    if parsed.scheme.lower() not in allowed_schemes or not hostname:
        raise ConfigError(
            "git_proxy 必须是带协议和主机的代理 URL，例如 "
            "http://127.0.0.1:7890。"
        )
    return proxy


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "是"}:
            return True
        if normalized in {"false", "0", "no", "off", "否"}:
            return False
    return default


def _positive_int(value: Any, name: str, default: int) -> int:
    parsed = _coerce_int(value, name, default)
    if parsed <= 0:
        raise ConfigError(f"{name} 必须大于 0。")
    return parsed


def _positive_or_zero_int(value: Any, name: str, default: int) -> int:
    parsed = _coerce_int(value, name, default)
    if parsed < 0:
        raise ConfigError(f"{name} 不能小于 0。")
    return parsed


def _coerce_int(value: Any, name: str, default: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是整数。")
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数。") from exc
