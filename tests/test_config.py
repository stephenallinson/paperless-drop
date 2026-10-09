import os
import tempfile
import unittest
from pathlib import Path

from paperless_drop.config import ConfigError, load


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.cfg_path = self.tmp / "config.toml"
        self.env = {"XDG_CONFIG_HOME": str(self.tmp)}

    def write(self, text):
        self.cfg_path.write_text(text)

    def test_defaults_and_trailing_slash(self):
        self.write('url = "https://docs.example.com/"\n')
        cfg = load(self.cfg_path, {"PAPERLESS_DROP_TOKEN": "t"})
        self.assertEqual(cfg.url, "https://docs.example.com")
        self.assertEqual((cfg.tags, cfg.wait, cfg.retention_days), ([], True, 30))
        self.assertEqual(cfg.inbox_path, Path.home() / "Paperless-Inbox")
        self.assertIn("environment", cfg.token_source)

    def test_token_file_must_be_private(self):
        self.write('url = "http://x"\n')
        token = self.tmp / "token"
        token.write_text("abc\n")
        token.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "chmod 600"):
            load(self.cfg_path, {})
        token.chmod(0o600)
        self.assertEqual(load(self.cfg_path, {}).token, "abc")

    def test_explicit_token_file_missing(self):
        self.write(f'url = "http://x"\ntoken_file = "{self.tmp}/nope"\n')
        with self.assertRaisesRegex(ConfigError, "Cannot read token file"):
            load(self.cfg_path, {})

    def test_env_overrides(self):
        self.write('url = "http://from-file"\n')
        cfg = load(self.cfg_path, {"PAPERLESS_DROP_URL": "http://from-env", "PAPERLESS_DROP_TOKEN": "t"})
        self.assertEqual(cfg.url, "http://from-env")

    def test_validation(self):
        self.write('url = "docs.example.com"\n')
        with self.assertRaisesRegex(ConfigError, "http"):
            load(self.cfg_path, {"PAPERLESS_DROP_TOKEN": "t"})
        self.write('url = "http://x"\ntags = "inbox"\n')
        with self.assertRaisesRegex(ConfigError, "tags"):
            load(self.cfg_path, {"PAPERLESS_DROP_TOKEN": "t"})
        self.write("url = = broken")
        with self.assertRaisesRegex(ConfigError, "parse"):
            load(self.cfg_path, {})

    def test_missing_explicit_config(self):
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            load(self.tmp / "nope.toml", {})

    def test_token_not_in_repr(self):
        self.write('url = "http://x"\n')
        self.assertNotIn("supersecret", repr(load(self.cfg_path, {"PAPERLESS_DROP_TOKEN": "supersecret"})))


if __name__ == "__main__":
    unittest.main()
