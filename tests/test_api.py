import tempfile
import unittest
from pathlib import Path

from paperless_drop.api import (ApiError, AuthError, PaperlessClient, ServiceUnavailable,
                                TagNotFound, parse_task)
from tests.fake_paperless import TOKEN, FakePaperless


def pdf(directory: str, name: str) -> Path:
    path = Path(directory) / name
    path.write_bytes(b"%PDF-1.4 fake")
    return path


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaperless(tags=("Inbox", "Tax"))
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = PaperlessClient(self.fake.url, TOKEN, timeout=5)

    def test_auth_and_version(self):
        self.client.check_auth()
        self.assertEqual(self.client.version, "3.1.3")

    def test_bad_token(self):
        with self.assertRaises(AuthError):
            PaperlessClient(self.fake.url, "nope").check_auth()

    def test_server_error_is_service_problem(self):
        self.fake.fail_with = 503
        with self.assertRaises(ServiceUnavailable):
            self.client.check_auth()

    def test_unreachable(self):
        with self.assertRaises(ServiceUnavailable):
            PaperlessClient("http://127.0.0.1:9", TOKEN, timeout=2).check_auth()

    def test_tags_case_insensitive(self):
        self.assertEqual(self.client.resolve_tags(["inbox", "TAX", "inbox"]), [1, 2])
        with self.assertRaises(TagNotFound):
            self.client.find_tag("missing")

    def test_upload_sends_file_title_and_repeated_tags(self):
        path = pdf(self.tmp.name, "invoice é.pdf")
        task = self.client.upload(path, title="My title", tag_ids=[1, 2])
        up = self.fake.uploads[0]
        self.assertEqual(up["filename"], "invoice é.pdf")
        self.assertEqual(up["content"], b"%PDF-1.4 fake")
        self.assertIn(("title", "My title"), up["fields"])
        self.assertEqual([v for k, v in up["fields"] if k == "tags"], ["1", "2"])
        result = self.client.wait_for_task(task, sleep=lambda s: None)
        self.assertEqual((result.state, result.document_id), ("success", 101))

    def test_find_by_checksum(self):
        path = pdf(self.tmp.name, "a.pdf")
        self.assertIsNone(self.client.find_by_checksum("0" * 64))
        self.client.upload(path)
        import hashlib
        found = self.client.find_by_checksum(hashlib.sha256(path.read_bytes()).hexdigest().upper())
        self.assertEqual(found["id"], 101)

    def test_pending_then_success(self):
        task = self.client.upload(pdf(self.tmp.name, "slow.pdf"))
        sleeps = []
        result = self.client.wait_for_task(task, sleep=sleeps.append)
        self.assertEqual(result.state, "success")
        self.assertEqual(len(sleeps), 2)

    def test_timeout_returns_none(self):
        task = self.client.upload(pdf(self.tmp.name, "slow.pdf"))
        clock = iter(range(0, 1000, 100))
        self.assertIsNone(self.client.wait_for_task(task, timeout=50, sleep=lambda s: None,
                                                    monotonic=lambda: next(clock)))

    def test_upload_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.client.upload(pdf(self.tmp.name, "reject.pdf"))
        self.assertNotIsInstance(ctx.exception, ServiceUnavailable)
        self.assertEqual(ctx.exception.status, 400)

    def test_parse_task_paperless_3(self):
        ok = parse_task({"status": "success", "result_data": {"document_id": 20}, "related_document_ids": [20]})
        self.assertEqual((ok.state, ok.document_id), ("success", 20))
        only_related = parse_task({"status": "success", "result_data": None, "related_document_ids": [5]})
        self.assertEqual(only_related.document_id, 5)
        for status in ("pending", "started"):
            self.assertEqual(parse_task({"status": status}).state, "pending")

    def test_parse_task_real_3_1_3_failure(self):
        """Captured from a real server after uploading a corrupt PDF."""
        task = {"status": "failure", "related_document_ids": [], "result_data": {
            "error_type": "ConsumerError", "traceback": "...",
            "error_message": "bad.pdf: Error occurred while consuming document bad.pdf: InputFileError: "}}
        result = parse_task(task)
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.message, "Paperless could not read the file (damaged, or not really a PDF/image)")

    def test_parse_task_failure_shapes(self):
        for data in ("boom", {"error_message": "boom"}, {"error": "boom"}):
            result = parse_task({"status": "failure", "result_data": data})
            self.assertEqual((result.state, result.message), ("failure", "boom"))
        self.assertEqual(parse_task({"status": "failure"}).state, "failure")  # no payload at all

    def test_parse_task_paperless_2_still_works(self):
        ok = parse_task({"status": "SUCCESS", "related_document": "12"})
        self.assertEqual((ok.state, ok.document_id), ("success", 12))
        dup = parse_task({"status": "FAILURE", "result": "x: It is a duplicate of Foo (#42)."})
        self.assertEqual((dup.state, dup.document_id), ("duplicate", 42))


if __name__ == "__main__":
    unittest.main()
