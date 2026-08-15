import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repo_kbs_sync.config import PluginSettings
from repo_kbs_sync.scanner import scan_repository
from repo_kbs_sync.sync_plan import build_document_sync_plan


class ScannerAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.settings = PluginSettings.from_mapping(
            {
                "repository_url": "https://example.com/docs.git",
                "sync_rules": [
                    {"path": "guides", "kb_name": "Guides"},
                    {"path": "guides/api", "kb_name": "API"},
                ],
                "ignore_paths": ["guides/draft"],
            }
        )

    def test_recursive_scan_maps_overlapping_rules(self):
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "guides/api").mkdir(parents=True)
            (root / "guides/draft").mkdir()
            (root / "guides/intro.md").write_text("intro", encoding="utf-8")
            (root / "guides/api/auth.mdx").write_text("auth", encoding="utf-8")
            (root / "guides/draft/secret.md").write_text("secret", encoding="utf-8")
            (root / "guides/nope.txt").write_text("nope", encoding="utf-8")

            result = scan_repository(
                root,
                self.settings.enabled_rules,
                self.settings.allowed_file_types,
                self.settings.ignore_paths,
            )

        self.assertEqual(sorted(result["Guides"]), ["guides/api/auth.md", "guides/intro.md"])
        self.assertEqual(sorted(result["API"]), ["guides/api/auth.md"])

    def test_mdx_document_name_uses_md_suffix(self):
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "guides").mkdir()
            (root / "guides/intro.md").write_text("intro", encoding="utf-8")
            (root / "guides/api.mdx").write_text("api", encoding="utf-8")
            source = scan_repository(
                root,
                [self.settings.enabled_rules[0]],
                self.settings.allowed_file_types,
                [],
            )["Guides"]

        by_name = {doc.document_name: doc for doc in source.values()}
        self.assertEqual(set(by_name), {"guides/intro.md", "guides/api.md"})
        self.assertEqual(by_name["guides/api.md"].source_path.as_posix(), "guides/api.mdx")

    def test_first_run_does_not_delete_unmanaged_documents(self):
        with TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "guides/guide.md"
            path.parent.mkdir()
            path.write_text("guide", encoding="utf-8")
            source = scan_repository(
                Path(raw_root),
                [self.settings.enabled_rules[0]],
                self.settings.allowed_file_types,
                [],
            )["Guides"]

        plan = build_document_sync_plan(
            current_documents=source,
            existing_document_names={"manual-upload.md"},
            managed_document_names=(),
            changed_source_paths=(),
            full_sync=True,
        )
        self.assertEqual(plan.delete_names, ())
        self.assertEqual(
            [doc.document_name for doc in plan.upload_documents],
            ["guides/guide.md"],
        )

    def test_incremental_plan_replaces_changed_and_removes_managed(self):
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "guides").mkdir()
            (root / "guides/new.md").write_text("new", encoding="utf-8")
            source = scan_repository(
                root,
                [self.settings.enabled_rules[0]],
                self.settings.allowed_file_types,
                [],
            )["Guides"]

        plan = build_document_sync_plan(
            current_documents=source,
            existing_document_names={"guides/changed.md", "guides/old.md"},
            managed_document_names=("guides/changed.md", "guides/old.md"),
            changed_source_paths=("guides/changed.md", "guides/new.md"),
            full_sync=False,
        )
        self.assertEqual(
            plan.delete_names,
            ("guides/changed.md", "guides/old.md"),
        )
        self.assertEqual([doc.document_name for doc in plan.upload_documents], ["guides/new.md"])
