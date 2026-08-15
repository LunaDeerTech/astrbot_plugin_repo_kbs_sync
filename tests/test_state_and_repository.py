import unittest

from repo_kbs_sync.repository import RemoteRepository, _parse_changed_paths
from repo_kbs_sync.state import SyncState


class StateAndRepositoryTests(unittest.TestCase):
    def test_state_round_trip(self):
        original = SyncState(
            repository_url="https://example.com/docs.git",
            branch="main",
            remote_head="a" * 40,
            config_fingerprint="fingerprint",
            managed_documents={"Docs": ("guides/a.md",)},
        )
        restored = SyncState.from_value(original.to_json())
        self.assertEqual(restored, original)
        self.assertTrue(
            restored.matches(
                "https://example.com/docs.git",
                "main",
                "a" * 40,
                "fingerprint",
            )
        )

    def test_github_tree_url_is_normalized(self):
        repository = RemoteRepository.from_config(
            "https://github.com/org/docs/tree/main/reference"
        )
        self.assertEqual(repository.normalized_url, "https://github.com/org/docs")
        self.assertEqual(repository.branch_hint, "main/reference")

    def test_git_diff_nul_parser_uses_new_rename_path(self):
        output = "M\x00guides/a.md\x00R100\x00guides/old.md\x00guides/new.md\x00D\x00guides/deleted.md\x00"
        self.assertEqual(
            _parse_changed_paths(output),
            {"guides/a.md", "guides/new.md", "guides/deleted.md"},
        )
