"""An in-process fake of the parts of the Paperless-ngx 3.x API that paperless-drop uses.

The response shapes were captured from a real 3.1.3 server: paginated /api/tasks/ with lowercase
statuses and result_data / related_document_ids, SHA-256 checksums, and duplicates that are
*accepted* rather than rejected.

Behaviour is selected by file name: bad*.pdf -> task failure, reject*.pdf -> HTTP 400 on upload,
slow*.pdf -> "started" for 2 polls first.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "secret-token"


class FakePaperless:
    def __init__(self, tags=("inbox",)):
        self.tags = {name: i + 1 for i, name in enumerate(tags)}
        self.uploads: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.docs: dict[int, dict] = {}
        self.polls: dict[str, int] = {}
        self.next_doc = 100
        self.fail_with: int | None = None  # force this HTTP status on every request
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Version", "3.1.3")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                if fake.fail_with:
                    self._send(fake.fail_with, {"detail": "forced"})
                    return False
                if self.headers.get("Authorization") != f"Token {TOKEN}":
                    self._send(401, {"detail": "bad token"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path == "/api/tags/":
                    wanted = q.get("name__iexact", [None])[0]
                    rows = [{"id": i, "name": n} for n, i in fake.tags.items()
                            if wanted is None or n.lower() == wanted.lower()]
                    return self._send(200, {"count": len(rows), "results": rows})
                if url.path == "/api/tasks/":
                    task = fake.tasks.get(q["task_id"][0])
                    if not task:
                        return self._send(200, {"count": 0, "next": None, "results": []})
                    n = fake.polls[task["task_id"]] = fake.polls.get(task["task_id"], 0) + 1
                    if task["file"].startswith("slow") and n <= 2:
                        row = {"task_id": task["task_id"], "status": "started", "result_data": None,
                               "related_document_ids": []}
                        return self._send(200, {"count": 1, "next": None, "results": [row]})
                    return self._send(200, {"count": 1, "next": None, "results": [task["final"]]})
                if url.path == "/api/documents/":
                    wanted = q.get("checksum__iexact", [None])[0]
                    rows = [{"id": d["id"], "title": d["title"]} for d in fake.docs.values()
                            if wanted is None or d["checksum"].lower() == wanted.lower()]
                    return self._send(200, {"count": len(rows), "next": None, "results": rows[:1]})
                if url.path.startswith("/api/documents/"):
                    doc_id = int(url.path.strip("/").split("/")[-1])
                    return self._send(200, fake.docs.get(doc_id, {"id": doc_id, "title": "Fake Title"}))
                self._send(404, {})

            def do_POST(self):
                if not self._auth():
                    return
                length = int(self.headers["Content-Length"])
                body = self.rfile.read(length)
                msg = BytesParser().parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields, filename, content = [], None, b""
                for part in msg.get_payload():
                    name = part.get_param("name", header="content-disposition")
                    if part.get_filename():
                        # Real clients send raw UTF-8, which email's header parser mangles.
                        filename = re.search(rb'filename="([^"]*)"', body).group(1).decode("utf-8")
                        content = part.get_payload(decode=True)
                    else:
                        fields.append((name, part.get_payload(decode=True).decode()))
                if filename.startswith("reject"):
                    return self._send(400, {"document": ["File type not supported"]})
                task_id = str(uuid.uuid4())
                doc_id = fake.next_doc = fake.next_doc + 1
                if filename.startswith("bad"):
                    final = {"task_id": task_id, "status": "failure", "result_data": "Unable to parse file",
                             "related_document_ids": []}
                else:
                    final = {"task_id": task_id, "status": "success", "result_data": {"document_id": doc_id},
                             "related_document_ids": [doc_id]}
                    fake.docs[doc_id] = {"id": doc_id, "title": "Fake Title",
                                         "checksum": hashlib.sha256(content).hexdigest()}
                fake.tasks[task_id] = {"task_id": task_id, "file": filename, "final": final}
                fake.uploads.append({"filename": filename, "fields": fields, "content": content})
                self._send(200, task_id)

        return Handler
