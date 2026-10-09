import os
import shutil
import tempfile
import unittest
from pathlib import Path

from paperless_drop.api import PaperlessClient
from paperless_drop.config import Config
from paperless_drop.inbox import Inbox
from paperless_drop.watcher import Watcher
from tests.fake_paperless import TOKEN, FakePaperless
from tests.test_inbox import Clock


class FakeNotifier:
    def __init__(self):
        self.outcomes, self.problems = [], []

    def outcome(self, o):
        self.outcomes.append(o)

    def problem(self, msg):
        self.problems.append(msg)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaperless(tags=("inbox",))
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.clock = Clock()
        self.inbox = Inbox(self.tmp / "inbox", 30, 3, clock=self.clock)
        self.inbox.ensure()
        self.notifier = FakeNotifier()
        self.cfg = Config(url=self.fake.url, token=TOKEN, inbox_path=self.inbox.root)
        self.watcher = Watcher(self.cfg, PaperlessClient(self.fake.url, TOKEN, timeout=5),
                               self.inbox, self.notifier, clock=self.clock, sleep=lambda s: None)

    def put(self, name):
        p = self.inbox.root / name
        p.write_bytes(b"x")
        os.utime(p, (self.clock.t - 100, self.clock.t - 100))

    def pump(self):
        self.watcher.run_once()
        return self.watcher.run_once()

    def test_uploads_and_archives(self):
        self.put("a.pdf")
        self.assertEqual(self.watcher.run_once(), [])  # first scan only records the file
        out = self.watcher.run_once()
        self.assertEqual([o.document_id for o in out], [101])
        self.assertFalse((self.inbox.root / "a.pdf").exists())
        self.assertEqual(len(list(self.inbox.sent.rglob("a.pdf"))), 1)
        self.assertEqual(len(self.notifier.outcomes), 1)

    def test_failed_file_is_parked_not_retried(self):
        self.put("bad.pdf")
        self.pump()
        self.assertTrue((self.inbox.failed / "bad.pdf").exists())
        self.assertTrue((self.inbox.failed / "bad.pdf.error.txt").exists())
        self.pump()
        self.assertEqual(len(self.fake.uploads), 1)

    def test_duplicate_goes_to_sent_without_uploading(self):
        self.put("a.pdf")
        self.pump()
        self.put("b.pdf")  # same content as a.pdf
        self.pump()
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(len(list(self.inbox.sent.rglob("b.pdf"))), 1)
        self.assertEqual([o.status.value for o in self.notifier.outcomes], ["added", "duplicate"])

    def test_outage_keeps_file_notifies_once_and_recovers(self):
        self.fake.fail_with = 503
        self.put("a.pdf")
        self.pump()
        self.assertTrue((self.inbox.root / "a.pdf").exists())  # still there for retry
        self.assertEqual(len(self.notifier.problems), 1)
        self.assertEqual(self.watcher.run_once(), [])  # backing off: no attempt
        self.clock.t += 61
        self.watcher.run_once()  # second failure, no second notification
        self.assertEqual(len(self.notifier.problems), 1)
        self.fake.fail_with = None
        self.clock.t += 200
        out = self.watcher.run_once()
        self.assertEqual(len(out), 1)
        self.assertEqual(len(self.fake.uploads), 1)  # uploaded exactly once overall

    def test_missing_tag_blocks_instead_of_dropping_files(self):
        self.cfg.tags = ["nonexistent"]
        self.put("a.pdf")
        self.pump()
        self.assertTrue((self.inbox.root / "a.pdf").exists())
        self.assertEqual(len(self.notifier.problems), 1)

    def test_configured_tags_are_sent(self):
        self.cfg.tags = ["inbox"]
        self.put("a.pdf")
        self.pump()
        self.assertEqual([v for k, v in self.fake.uploads[0]["fields"] if k == "tags"], ["1"])

    def test_once_processes_without_prior_scan(self):
        self.put("a.pdf")
        self.assertEqual(len(self.watcher.once()), 1)


if __name__ == "__main__":
    unittest.main()
