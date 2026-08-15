import unittest

from repo_kbs_sync.config import ConfigError, PluginSettings


class ConfigTests(unittest.TestCase):
    def make_config(self):
        return {
            "repository_url": "https://example.com/docs.git",
            "branch": "main",
            "sync_rules": [
                {
                    "__template_key": "path_to_kb",
                    "path": "docs\\api",
                    "kb_name": "开发文档",
                    "enabled": True,
                },
                {"path": "disabled", "kb_name": "Nope", "enabled": False},
            ],
        }

    def test_template_rule_is_normalized(self):
        config = self.make_config()
        config.update(
            {
                "embedding_provider_id": "embedding-one",
                "rerank_provider_id": "rerank-one",
                "notify_owner_enabled": True,
                "notify_group_enabled": True,
                "notify_group_id": "123456",
            }
        )
        settings = PluginSettings.from_mapping(config)
        self.assertEqual(settings.configured_kb_names, ("开发文档",))
        self.assertEqual(settings.enabled_rules[0].path.as_posix(), "docs/api")
        self.assertEqual(settings.allowed_file_types, (".md", ".mdx"))
        self.assertEqual(settings.embedding_provider_id, "embedding-one")
        self.assertEqual(settings.rerank_provider_id, "rerank-one")
        self.assertTrue(settings.notify_owner_enabled)
        self.assertTrue(settings.notify_group_enabled)
        self.assertEqual(settings.notify_group_id, "123456")

    def test_fingerprint_changes_when_mapping_changes(self):
        first = PluginSettings.from_mapping(self.make_config())
        changed_config = self.make_config()
        changed_config["sync_rules"][0]["kb_name"] = "另一个知识库"
        second = PluginSettings.from_mapping(changed_config)
        self.assertNotEqual(
            first.fingerprint("https://example.com/docs.git", "main"),
            second.fingerprint("https://example.com/docs.git", "main"),
        )

    def test_invalid_paths_are_rejected(self):
        config = self.make_config()
        config["sync_rules"][0]["path"] = "../outside"
        with self.assertRaises(ConfigError):
            PluginSettings.from_mapping(config)

    def test_empty_rules_are_rejected(self):
        config = self.make_config()
        config["sync_rules"] = []
        with self.assertRaises(ConfigError):
            PluginSettings.from_mapping(config)
