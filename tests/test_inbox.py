import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from paperless_drop.inbox import Inbox
from paperless_drop.sender import Outcome, Status


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.clock = Clock()
        self.inbox = Inbox(self.tmp / "inbox", retention_days=30, settle_seconds=3, clock=self.clock)
        self.inbox.ensure()

    def put(self, name, data=b"x", age=100):
        p = self.inbox.root / name
        p.write_bytes(data)
        os.utime(p, (self.clock.t - age, self.clock.t - age))
        return p

    def test_needs_two_scans_to_settle(self):
        p = self.put("a.pdf")
        self.assertEqual(self.inbox.settled_files(), [])
        self.assertEqual(self.inbox.settled_files(), [p])

    def test_growing_file_waits(self):
        p = self.put("a.pdf")
        self.inbox.settled_files()
        p.write_bytes(b"xxxx")
        os.utime(p, (self.clock.t - 100, self.clock.t - 100))
        self.assertEqual(self.inbox.settled_files(), [])
        self.assertEqual(self.inbox.settled_files(), [p])

    def test_fresh_file_waits_for_age(self):
        p = self.put("a.pdf", age=0)
        self.inbox.settled_files()
        self.assertEqual(self.inbox.settled_files(), [])
        self.clock.t += 5
        self.assertEqual(self.inbox.settled_files(), [p])

    def test_ignores_partial_hidden_dirs_and_symlinks(self):
        self.put("a.pdf.crdownload")
        self.put("b.part")
        self.put(".hidden.pdf")
        (self.inbox.root / "folder").mkdir()
        (self.inbox.root / "link.pdf").symlink_to(self.tmp)
        self.inbox.settled_files()
        self.assertEqual(self.inbox.settled_files(), [])

    def test_oldest_first(self):
        new, old = self.put("new.pdf", age=50), self.put("old.pdf", age=500)
        self.inbox.settled_files()
        self.assertEqual(self.inbox.settled_files(), [old, new])

    def test_claim_is_exclusive(self):
        p = self.put("a.pdf")
        self.assertIsNotNone(self.inbox.claim(p))
        self.assertIsNone(self.inbox.claim(p))

    def test_claim_name_collision(self):
        a = self.inbox.claim(self.put("a.pdf", b"1"))
        b = self.inbox.claim(self.put("a.pdf", b"2"))
        self.assertNotEqual(a, b)
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"1", b"2"))

    def test_archive_success_goes_to_dated_folder(self):
        claimed = self.inbox.claim(self.put("a.pdf"))
        dest = self.inbox.archive(claimed, Outcome(claimed, Status.ADDED))
        day = dt.date.fromtimestamp(self.clock.t).isoformat()
        self.assertEqual(dest, self.inbox.sent / day / "a.pdf")
        self.assertTrue(dest.exists())

    def test_archive_failure_writes_error_note(self):
        claimed = self.inbox.claim(self.put("a.pdf"))
        dest = self.inbox.archive(claimed, Outcome(claimed, Status.FAILED, message="no good"))
        self.assertEqual(dest.parent, self.inbox.failed)
        self.assertIn("no good", (self.inbox.failed / "a.pdf.error.txt").read_text())

    def test_recover_returns_stranded_files(self):
        self.inbox.claim(self.put("a.pdf"))
        self.assertEqual(self.inbox.recover(), 1)
        self.assertTrue((self.inbox.root / "a.pdf").exists())

    def test_prune_only_old_dated_folders(self):
        today = dt.date.fromtimestamp(self.clock.t)
        old = self.inbox.sent / (today - dt.timedelta(days=31)).isoformat()
        edge = self.inbox.sent / (today - dt.timedelta(days=30)).isoformat()
        stranger = self.inbox.sent / "my-notes"
        for d in (old, edge, stranger):
            d.mkdir()
            (d / "f.pdf").write_bytes(b"x")
        removed = self.inbox.prune()
        self.assertEqual(removed, [old])
        self.assertTrue(edge.exists() and stranger.exists())

    def test_prune_disabled_with_zero(self):
        self.inbox.retention_days = 0
        old = self.inbox.sent / "2000-01-01"
        old.mkdir()
        self.assertEqual(self.inbox.prune(), [])
        self.assertTrue(old.exists())

    def test_contains(self):
        self.assertTrue(self.inbox.contains(self.inbox.root / "a.pdf"))
        self.assertFalse(self.inbox.contains(self.tmp / "a.pdf"))
        self.assertFalse(self.inbox.contains(self.inbox.sent / "a.pdf"))


if __name__ == "__main__":
    unittest.main()
