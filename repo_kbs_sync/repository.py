from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class GitRepositoryError(RuntimeError):
    """Raised when a remote repository operation fails."""


@dataclass(frozen=True)
class RemoteRepository:
    source_url: str
    normalized_url: str
    configured_branch: str | None = None
    branch_hint: str | None = None

    @classmethod
    def from_config(
        cls,
        source_url: str,
        configured_branch: str | None = None,
    ) -> "RemoteRepository":
        if not isinstance(source_url, str) or not source_url.strip():
            raise GitRepositoryError("repository_url 不能为空。")

        raw_url = source_url.strip()
        if "\x00" in raw_url:
            raise GitRepositoryError("repository_url 不能包含 NUL 字符。")

        normalized_url, branch_hint = _normalize_repository_url(raw_url)
        branch = _normalize_branch(configured_branch) if configured_branch else None
        return cls(
            source_url=raw_url,
            normalized_url=normalized_url,
            configured_branch=branch,
            branch_hint=branch_hint,
        )

    def display_url(self) -> str:
        return _redact_url(self.normalized_url)


class GitClient:
    """Async, argument-list-only wrapper around the system Git executable."""

    async def run(
        self,
        args: list[str],
        cwd: Path | None = None,
        remote_url: str | None = None,
    ) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if remote_url:
                detail = detail.replace(remote_url, _redact_url(remote_url))
            command = "git " + " ".join(_redact_argument(arg) for arg in args)
            raise GitRepositoryError(
                f"{command} 执行失败：{detail or '未知 Git 错误'}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def resolve_branch(self, repository: RemoteRepository) -> str:
        if repository.configured_branch:
            return repository.configured_branch

        if repository.branch_hint:
            try:
                branches = await self.list_branches(repository)
            except GitRepositoryError:
                branches = []
            matching = [
                branch
                for branch in branches
                if repository.branch_hint == branch
                or repository.branch_hint.startswith(f"{branch}/")
            ]
            if matching:
                return max(matching, key=len)
            return _normalize_branch(repository.branch_hint.split("/", 1)[0])

        output = await self.run(
            ["ls-remote", "--symref", repository.normalized_url, "HEAD"],
            remote_url=repository.normalized_url,
        )
        for line in output.splitlines():
            if line.startswith("ref:") and "\tHEAD" in line:
                reference = line.split()[1]
                if reference.startswith("refs/heads/"):
                    return _normalize_branch(reference.removeprefix("refs/heads/"))
        raise GitRepositoryError("无法解析远程仓库默认分支，请手动填写 branch。")

    async def list_branches(self, repository: RemoteRepository) -> list[str]:
        output = await self.run(
            ["ls-remote", "--heads", repository.normalized_url],
            remote_url=repository.normalized_url,
        )
        branches: list[str] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                branches.append(parts[1].removeprefix("refs/heads/"))
        return branches

    async def remote_head(self, repository: RemoteRepository, branch: str) -> str:
        branch = _normalize_branch(branch)
        output = await self.run(
            [
                "ls-remote",
                repository.normalized_url,
                f"refs/heads/{branch}",
            ],
            remote_url=repository.normalized_url,
        )
        for line in output.splitlines():
            parts = line.split()
            if parts and re.fullmatch(r"[0-9a-fA-F]{40}", parts[0]):
                return parts[0]
        raise GitRepositoryError(f"无法获取远程仓库分支 {branch} 的最新提交。")

    async def clone(
        self,
        repository: RemoteRepository,
        branch: str,
        destination: Path,
    ) -> None:
        _normalize_branch(branch)
        await self.run(
            [
                "clone",
                "--branch",
                branch,
                "--single-branch",
                "--no-tags",
                "--no-recurse-submodules",
                repository.normalized_url,
                str(destination),
            ],
            remote_url=repository.normalized_url,
        )

    async def changed_paths(
        self,
        repository_directory: Path,
        previous_head: str,
        current_head: str,
    ) -> set[str]:
        if previous_head == current_head:
            return set()

        output = await self.run(
            [
                "diff",
                "--name-status",
                "--find-renames",
                "-z",
                previous_head,
                current_head,
            ],
            cwd=repository_directory,
        )
        return _parse_changed_paths(output)


def _normalize_repository_url(raw_url: str) -> tuple[str, str | None]:
    # Git's SCP-like SSH syntax (git@github.com:owner/repo.git) has no URL
    # scheme and is nevertheless a valid remote.
    if re.match(r"^[^/\s@]+@[^/\s:]+:.+", raw_url):
        return raw_url.rstrip("/"), None

    parsed = urlparse(raw_url)
    allowed_schemes = {"http", "https", "ssh", "git", "file"}
    if parsed.scheme not in allowed_schemes:
        raise GitRepositoryError(
            "repository_url 必须使用 http、https、ssh、git 或 file 协议，"
            "也可以使用 git@host:path 格式。"
        )
    if parsed.scheme == "file":
        if not parsed.path:
            raise GitRepositoryError("file 协议仓库地址缺少路径。")
        return parsed.geturl().rstrip("/"), None
    if not parsed.netloc:
        raise GitRepositoryError("repository_url 必须包含主机名。")

    parts = [part for part in parsed.path.split("/") if part]
    branch_hint: str | None = None
    for marker in ("tree", "blob"):
        if marker not in parts:
            continue
        marker_index = parts.index(marker)
        if marker_index == 0 or marker_index + 1 >= len(parts):
            raise GitRepositoryError("repository_url 中的 tree/blob 路径不完整。")
        repository_parts = parts[:marker_index]
        if repository_parts and repository_parts[-1] == "-":
            repository_parts.pop()
        branch_hint = "/".join(parts[marker_index + 1 :])
        parts = repository_parts
        break

    if not parts:
        raise GitRepositoryError("repository_url 缺少仓库路径。")
    normalized_path = "/" + "/".join(parts)
    normalized = parsed._replace(
        path=normalized_path,
        params="",
        query="",
        fragment="",
    ).geturl()
    return normalized.rstrip("/"), branch_hint


def _normalize_branch(branch: str) -> str:
    if not isinstance(branch, str) or not branch.strip():
        raise GitRepositoryError("branch 不能为空。")
    normalized = branch.strip()
    if (
        normalized.startswith("-")
        or "\x00" in normalized
        or any(char.isspace() for char in normalized)
        or ".." in normalized
        or normalized.endswith("/")
    ):
        raise GitRepositoryError(f"非法 Git 分支名：{branch}")
    return normalized


def _parse_changed_paths(output: str) -> set[str]:
    tokens = output.split("\x00")
    changed: set[str] = set()
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        if status.startswith(("R", "C")):
            # For rename/copy, only the new path needs to be uploaded.  The
            # old path is removed by comparing the managed manifest to the
            # current snapshot.
            if index < len(tokens):
                index += 1  # old path
            if index < len(tokens) and tokens[index]:
                changed.add(tokens[index])
            index += 1
            continue
        if index < len(tokens) and tokens[index]:
            changed.add(tokens[index])
        index += 1
    return changed


def _redact_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.password is None:
        return value
    username = parsed.username or "user"
    netloc = f"{username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _redact_argument(argument: str) -> str:
    if "@" in argument and "://" in argument:
        return _redact_url(argument)
    return argument
