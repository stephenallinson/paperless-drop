"""Desktop notifications via notify-send. Never raises: notifying must not break uploads."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, Sequence

from .sender import Outcome, Status

log = logging.getLogger(__name__)

# $1 urgency, $2 icon, $3 summary, $4 body, $5 URL. Detached, so the CLI can exit while the
# notification is still on screen and clicking keeps working. "default" is the action a click on
# the notification body invokes; "open" is the visible button.
_WITH_ACTION = (
    'a=$(notify-send -a paperless-drop -u "$1" -i "$2" -t 20000 -A default=Open -A open=Open "$3" "$4") '
    '|| exit; case "$a" in open|default) xdg-open "$5";; esac'
)


class Notifier:
    def __init__(self, base_url: str = "", enabled: bool = True,
                 popen: Callable | None = None):
        self.base_url = base_url.rstrip("/")
        self.enabled = enabled
        self._popen = popen

    def document_url(self, doc_id: int) -> str:
        return f"{self.base_url}/documents/{doc_id}/details"

    def _send(self, urgency: str, icon: str, summary: str, body: str = "", url: str | None = None) -> None:
        if not self.enabled or not shutil.which("notify-send"):
            return
        if url:
            argv = ["sh", "-c", _WITH_ACTION, "_", urgency, icon, summary, body, url]
        else:
            argv = ["notify-send", "-a", "paperless-drop", "-u", urgency, "-i", icon, summary, body]
        try:
            (self._popen or subprocess.Popen)(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log.warning("could not send notification: %s", exc)

    def outcome(self, o: Outcome) -> None:
        name = o.path.name
        if o.status is Status.ADDED:
            label = f"{o.title} (#{o.document_id})" if o.document_id else name
            url = self.document_url(o.document_id) if o.document_id else None
            self._send("normal", "document-send", "Added to Paperless", label, url)
        elif o.status is Status.DUPLICATE:
            url = self.document_url(o.document_id) if o.document_id else None
            label = f"{o.title} (#{o.document_id})" if o.title and o.document_id else name
            self._send("normal", "dialog-information", "Already in Paperless", label, url)
        elif o.status is Status.QUEUED:
            self._send("normal", "document-send", "Uploaded to Paperless", f"{name}\n{o.message}")
        else:
            self._send("critical", "dialog-error", "Paperless upload failed", f"{name}\n{o.message}")

    def summary(self, outcomes: Sequence[Outcome]) -> None:
        if len(outcomes) == 1:
            return self.outcome(outcomes[0])
        count = {s: sum(o.status is s for o in outcomes) for s in Status}
        parts = [f"{n} {s.value}" for s, n in count.items() if n]
        failed = [f"{o.path.name}: {o.message}" for o in outcomes if not o.ok]
        bad = bool(failed)
        self._send("critical" if bad else "normal", "dialog-error" if bad else "document-send",
                   f"Paperless: {len(outcomes)} files ({', '.join(parts)})", "\n".join(failed))

    def problem(self, message: str) -> None:
        self._send("critical", "dialog-warning", "Paperless upload problem", message)
