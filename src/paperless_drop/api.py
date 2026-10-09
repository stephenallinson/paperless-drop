"""Minimal Paperless-ngx API client (standard library only)."""
from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


class ApiError(Exception):
    """The server rejected a request. For uploads this is the file's fault."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ServiceProblem(ApiError):
    """Not the file's fault (server down, bad token, bad config). Retry later."""


class ServiceUnavailable(ServiceProblem):
    pass


class AuthError(ServiceProblem):
    pass


class TagNotFound(ServiceProblem):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def encode_multipart(fields: Sequence[tuple[str, str]], file_field: str, filename: str,
                     content: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    safe_name = filename.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{safe_name}"\r\nContent-Type: {content_type}\r\n\r\n').encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass
class TaskResult:
    state: str  # success | duplicate | failure | pending
    document_id: int | None = None
    message: str = ""


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_document_id(task: dict) -> int | None:
    """Paperless 3.x: result_data.document_id / related_document_ids. 2.x: related_document."""
    data = task.get("result_data")
    if isinstance(data, dict) and _as_int(data.get("document_id")) is not None:
        return _as_int(data["document_id"])
    ids = task.get("related_document_ids")
    if isinstance(ids, list) and ids:
        return _as_int(ids[0])
    return _as_int(task.get("related_document"))


def _tidy_error(message: str) -> str:
    """Paperless 3.1.3 failures look like 'f.pdf: Error occurred while consuming document f.pdf:
    InputFileError: ' (filename twice, empty detail)."""
    message = re.sub(r"^(.+?): Error occurred while consuming document \1:\s*", "", message.strip())
    if message.startswith("InputFileError"):
        detail = message.partition(":")[2].strip()
        return "Paperless could not read the file (damaged, or not really a PDF/image)" + (
            f": {detail}" if detail else "")
    return message.rstrip(": ").strip() or message


def _task_message(task: dict) -> str:
    """Best-effort error text. Failure payload seen on 3.1.3: result_data.error_message."""
    data = task.get("result_data")
    if isinstance(data, str) and data:
        return _tidy_error(data)
    if isinstance(data, dict) and data:
        for key in ("error_message", "error", "message", "detail"):
            if data.get(key):
                return _tidy_error(str(data[key]))
        return json.dumps(data)
    return str(task.get("result") or task.get("status_display") or "")


def parse_task(task: dict) -> TaskResult:
    status = str(task.get("status", "")).lower()
    if status == "success":
        return TaskResult("success", _task_document_id(task), _task_message(task))
    if status in ("failure", "failed", "revoked"):
        message = _task_message(task) or f"task {status}"
        if "duplicate" in message.lower():  # Paperless 2.x rejected duplicates
            match = re.search(r"#(\d+)", message)
            return TaskResult("duplicate", int(match.group(1)) if match else None, message)
        return TaskResult("failure", None, message)
    return TaskResult("pending")


class PaperlessClient:
    def __init__(self, url: str, token: str, timeout: float = 30, upload_timeout: float = 300):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.version: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str | None = None, timeout: float | None = None):
        headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                self.version = resp.headers.get("X-Version") or self.version
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:500]
            exc.close()
            self.version = exc.headers.get("X-Version") or self.version
            if exc.code in (401, 403):
                raise AuthError(f"Paperless rejected the API token (HTTP {exc.code})", exc.code) from exc
            if 300 <= exc.code < 400:
                raise ServiceProblem(
                    f"Paperless redirected to {exc.headers.get('Location')}. "
                    "Set `url` in the config to that address.", exc.code) from exc
            if exc.code >= 500 or exc.code == 429:
                raise ServiceUnavailable(f"Paperless server error (HTTP {exc.code})", exc.code) from exc
            raise ApiError(f"HTTP {exc.code}: {detail or exc.reason}", exc.code) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServiceUnavailable(f"Cannot reach {self.url}: {reason}") from exc

    def _json(self, method: str, path: str, **kwargs):
        raw = self._request(method, path, **kwargs)
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ServiceProblem(f"{self.url} did not return JSON; is the URL a Paperless server?") from exc

    def check_auth(self) -> None:
        self._json("GET", "/api/tags/?page_size=1")

    def find_tag(self, name: str) -> int:
        query = urllib.parse.urlencode({"name__iexact": name, "page_size": 5})
        data = self._json("GET", f"/api/tags/?{query}") or {}
        for tag in data.get("results", []):
            if str(tag.get("name", "")).lower() == name.lower():
                return int(tag["id"])
        raise TagNotFound(f"Tag {name!r} does not exist in Paperless. Create it there first.")

    def resolve_tags(self, names: Iterable[str]) -> list[int]:
        return [self.find_tag(n) for n in dict.fromkeys(names)]

    def upload(self, path: Path, title: str | None = None, tag_ids: Sequence[int] = ()) -> str:
        fields: list[tuple[str, str]] = []
        if title:
            fields.append(("title", title))
        fields.extend(("tags", str(i)) for i in tag_ids)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body, header = encode_multipart(fields, "document", path.name, path.read_bytes(), ctype)
        result = self._json("POST", "/api/documents/post_document/", body=body,
                            content_type=header, timeout=self.upload_timeout)
        if isinstance(result, str) and result:
            return result
        raise ServiceProblem(f"Unexpected upload response: {result!r}")

    def get_task(self, task_id: str) -> dict | None:
        data = self._json("GET", "/api/tasks/?" + urllib.parse.urlencode({"task_id": task_id}))
        if isinstance(data, dict):
            data = data.get("results", [])
        return data[0] if data else None

    def find_by_checksum(self, sha256: str) -> dict | None:
        """An existing document whose original file has this SHA-256 (Paperless >= 3)."""
        query = urllib.parse.urlencode({"checksum__iexact": sha256, "fields": "id,title", "page_size": 1})
        data = self._json("GET", f"/api/documents/?{query}") or {}
        results = data.get("results") or []
        return results[0] if results else None

    def get_document(self, doc_id: int) -> dict:
        return self._json("GET", f"/api/documents/{doc_id}/")

    def wait_for_task(self, task_id: str, timeout: float = 300,
                      sleep: Callable[[float], None] = time.sleep,
                      monotonic: Callable[[], float] = time.monotonic) -> TaskResult | None:
        """Poll until the task finishes. Returns None if it is still running at timeout
        (or the server became unreachable); the upload itself already succeeded."""
        deadline = monotonic() + timeout
        delay, errors = 1.0, 0
        while True:
            try:
                task = self.get_task(task_id)
                errors = 0
            except ApiError:
                task, errors = None, errors + 1
                if errors >= 5:
                    return None
            if task:
                result = parse_task(task)
                if result.state != "pending":
                    return result
            if monotonic() >= deadline:
                return None
            sleep(delay)
            delay = min(delay * 1.5, 5.0)
