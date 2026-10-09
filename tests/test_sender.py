import tempfile
import unittest
from pathlib import Path

from paperless_drop.api import PaperlessClient, ServiceUnavailable
from paperless_drop.sender import Status, send_file
from tests.fake_paperless import TOKEN, FakePaperless


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaperless()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.client = PaperlessClient(self.fake.url, TOKEN, timeout=5)

    def make(self, name, data=b"x"):
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_added(self):
        o = send_file(self.client, self.make("a.pdf"))
        self.assertEqual((o.status, o.document_id, o.title), (Status.ADDED, 101, "Fake Title"))

    def test_identical_file_is_detected_before_uploading(self):
        first = send_file(self.client, self.make("a.pdf", b"same bytes"))
        again = send_file(self.client, self.make("copy of a.pdf", b"same bytes").rename(self.tmp / "b.pdf"))
        self.assertEqual((again.status, again.document_id, again.title),
                         (Status.DUPLICATE, first.document_id, "Fake Title"))
        self.assertTrue(again.ok)
        self.assertEqual(len(self.fake.uploads), 1)

    def test_force_uploads_a_duplicate(self):
        send_file(self.client, self.make("a.pdf", b"same bytes"))
        forced = send_file(self.client, self.make("b.pdf", b"same bytes"), force=True)
        self.assertEqual(forced.status, Status.ADDED)
        self.assertEqual(len(self.fake.uploads), 2)

    def test_different_content_is_not_a_duplicate(self):
        send_file(self.client, self.make("a.pdf", b"one"))
        self.assertEqual(send_file(self.client, self.make("b.pdf", b"two")).status, Status.ADDED)

    def test_task_failure(self):
        o = send_file(self.client, self.make("bad.pdf"))
        self.assertEqual(o.status, Status.FAILED)
        self.assertIn("Unable to parse", o.message)

    def test_server_rejection_is_file_failure(self):
        self.assertEqual(send_file(self.client, self.make("reject.pdf")).status, Status.FAILED)

    def test_local_validation_does_not_hit_server(self):
        self.assertEqual(send_file(self.client, self.make("a.exe")).status, Status.FAILED)
        self.assertEqual(send_file(self.client, self.make("empty.pdf", b"")).status, Status.FAILED)
        self.assertEqual(send_file(self.client, self.tmp / "missing.pdf").status, Status.FAILED)
        self.assertEqual(self.fake.uploads, [])

    def test_no_wait_is_queued(self):
        self.assertEqual(send_file(self.client, self.make("a.pdf"), wait=False).status, Status.QUEUED)

    def test_service_problem_propagates(self):
        self.fake.fail_with = 502
        with self.assertRaises(ServiceUnavailable):
            send_file(self.client, self.make("a.pdf"))


if __name__ == "__main__":
    unittest.main()
