import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperless_drop import cli
from tests.fake_paperless import TOKEN, FakePaperless


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaperless(tags=("inbox",))
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.inbox = self.tmp / "inbox"
        self.config = self.tmp / "config.toml"
        self.config.write_text(f'url = "{self.fake.url}"\nnotify = false\n[inbox]\npath = "{self.inbox}"\n')
        # Never read or touch the real home directory (installed integrations, tokens, ...).
        env = mock.patch.dict(os.environ, {"PAPERLESS_DROP_TOKEN": TOKEN, "HOME": str(self.tmp),
                                           "XDG_CONFIG_HOME": str(self.tmp / "xdg")})
        env.start()
        self.addCleanup(env.stop)
        notify = mock.patch("paperless_drop.notify.subprocess.Popen")
        self.popen = notify.start()
        self.addCleanup(notify.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *argv])
        return code, out.getvalue(), err.getvalue()

    def make(self, name, directory=None):
        p = (directory or self.tmp) / name
        p.write_bytes(b"x")
        return p

    def test_send_success(self):
        code, out, _ = self.run_cli("send", str(self.make("a.pdf")))
        self.assertEqual(code, 0)
        self.assertIn("document #101", out)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("send", str(self.make("bad.pdf")))[0], 1)
        self.assertEqual(self.run_cli("send", str(self.make("a.pdf")))[0], 0)
        code, out, _ = self.run_cli("send", str(self.make("copy.pdf")))
        self.assertEqual(code, 0)
        self.assertIn("already in Paperless", out)
        self.fake.fail_with = 503
        code, _, err = self.run_cli("send", str(self.make("a.pdf")))
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_force_flag(self):
        self.run_cli("send", str(self.make("a.pdf")))
        self.run_cli("send", "--force", str(self.make("copy.pdf")))
        self.assertEqual(len(self.fake.uploads), 2)

    def test_title_requires_single_file(self):
        code, _, err = self.run_cli("send", "--title", "x", str(self.make("a.pdf")), str(self.make("b.pdf")))
        self.assertEqual(code, 2)

    def test_extra_tag_and_missing_tag(self):
        self.assertEqual(self.run_cli("send", "--tag", "inbox", str(self.make("a.pdf")))[0], 0)
        self.assertEqual([v for k, v in self.fake.uploads[0]["fields"] if k == "tags"], ["1"])
        code, _, err = self.run_cli("send", "--tag", "nope", str(self.make("b.pdf")))
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        self.assertEqual(len(self.fake.uploads), 1)

    def test_sending_a_file_from_the_inbox_archives_it(self):
        self.inbox.mkdir()
        path = self.make("a.pdf", self.inbox)
        self.assertEqual(self.run_cli("send", str(path))[0], 0)
        self.assertFalse(path.exists())
        self.assertEqual(len(list((self.inbox / "sent").rglob("a.pdf"))), 1)

    def test_failed_inbox_file_goes_to_failed(self):
        self.inbox.mkdir()
        self.run_cli("send", str(self.make("bad.pdf", self.inbox)))
        self.assertTrue((self.inbox / "failed" / "bad.pdf.error.txt").exists())

    def test_outage_leaves_inbox_file_in_place(self):
        self.inbox.mkdir()
        path = self.make("a.pdf", self.inbox)
        self.fake.fail_with = 502
        self.assertEqual(self.run_cli("send", str(path))[0], 2)
        self.assertTrue(path.exists())

    def test_watch_once(self):
        self.inbox.mkdir()
        self.make("a.pdf", self.inbox)
        os.utime(self.inbox / "a.pdf", (1, 1))
        with mock.patch("paperless_drop.watcher.time.sleep"):
            code, out, _ = self.run_cli("watch", "--once")
        self.assertEqual(code, 0)
        self.assertIn("a.pdf", out)

    def test_install_skips_watcher_without_a_working_config(self):
        self.config.write_text("")  # no URL
        with mock.patch("paperless_drop.cli.Installer") as installer:
            code, _, err = self.run_cli("install", "--watcher")
        self.assertEqual(code, 1)
        self.assertIn("paperless-drop init", err)
        installer.return_value.install_watcher.assert_not_called()

    def test_install_watcher_creates_inbox(self):
        with mock.patch("paperless_drop.cli.Installer") as installer:
            self.assertEqual(self.run_cli("install", "--watcher")[0], 0)
        installer.return_value.install_watcher.assert_called_once()
        self.assertTrue(self.inbox.is_dir())

    def test_doctor(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("3.1.3", out)

    def test_doctor_bad_token(self):
        with mock.patch.dict(os.environ, {"PAPERLESS_DROP_TOKEN": "wrong"}):
            code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("rejected the API token", out)

    def test_config_error_reaches_a_notification(self):
        self.config.write_text("")
        code, _, err = self.run_cli("send", str(self.make("a.pdf")))
        self.assertEqual(code, 2)
        self.assertTrue(self.popen.called)  # no terminal when launched from a file manager


if __name__ == "__main__":
    unittest.main()
