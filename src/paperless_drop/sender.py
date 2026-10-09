"""Upload one file and report what happened."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from .api import ApiError, PaperlessClient, ServiceProblem

# Deliberately generous: the server is the authority (office formats need Tika, for example).
SUPPORTED_EXTENSIONS = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".avif", ".heic",
    ".txt", ".md", ".csv", ".rtf", ".eml",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
})


class Status(str, Enum):
    ADDED = "added"
    DUPLICATE = "duplicate"
    QUEUED = "queued"  # uploaded, but the result was not (or could not be) confirmed
    FAILED = "failed"


@dataclass
class Outcome:
    path: Path
    status: Status
    document_id: int | None = None
    title: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not Status.FAILED


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def send_file(client: PaperlessClient, path: Path, tag_ids: Sequence[int] = (),
              title: str | None = None, wait: bool = True, task_timeout: float = 300,
              force: bool = False) -> Outcome:
    """Upload `path`. Raises ServiceProblem when the failure is not the file's fault.

    Paperless 3 consumes duplicates happily, so identical files are detected here, by checksum,
    before uploading. `force` skips that check.
    """
    if not path.is_file():
        return Outcome(path, Status.FAILED, message="Not a file or does not exist")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return Outcome(path, Status.FAILED, message=f"Unsupported file type {path.suffix or '(none)'}")
    if path.stat().st_size == 0:
        return Outcome(path, Status.FAILED, message="File is empty")

    if not force:
        try:
            existing = client.find_by_checksum(sha256_file(path))
        except ServiceProblem:
            raise
        except (ApiError, OSError):
            existing = None  # the check is an optimisation: fall through to the upload
        if existing:
            return Outcome(path, Status.DUPLICATE, existing.get("id"), existing.get("title"),
                           "Already in Paperless")

    try:
        task_id = client.upload(path, title=title, tag_ids=tag_ids)
    except ServiceProblem:
        raise
    except ApiError as exc:
        return Outcome(path, Status.FAILED, message=str(exc))
    except OSError as exc:
        return Outcome(path, Status.FAILED, message=f"Cannot read file: {exc.strerror}")

    if not wait:
        return Outcome(path, Status.QUEUED, message="Uploaded; processing in Paperless")

    result = client.wait_for_task(task_id, timeout=task_timeout)
    if result is None:
        return Outcome(path, Status.QUEUED, message="Uploaded; Paperless is still processing it")
    if result.state == "failure":
        return Outcome(path, Status.FAILED, message=result.message)
    if result.state == "duplicate":
        return Outcome(path, Status.DUPLICATE, result.document_id, message="Already in Paperless")

    doc_title = None
    if result.document_id is not None:
        try:
            doc_title = client.get_document(result.document_id).get("title")
        except ApiError:
            pass
    return Outcome(path, Status.ADDED, result.document_id, doc_title, "Added to Paperless")
