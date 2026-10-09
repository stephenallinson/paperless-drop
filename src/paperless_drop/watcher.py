"""Long-running inbox watcher (the systemd user service runs this)."""
from __future__ import annotations

import logging
import signal
import time
from typing import Callable

from .api import PaperlessClient, ServiceProblem
from .config import Config
from .inbox import Inbox
from .notify import Notifier
from .sender import Outcome, send_file

log = logging.getLogger(__name__)

PRUNE_EVERY = 24 * 3600
BACKOFF_START, BACKOFF_MAX = 60.0, 600.0


class Watcher:
    def __init__(self, cfg: Config, client: PaperlessClient, inbox: Inbox, notifier: Notifier,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.client, self.inbox, self.notifier = cfg, client, inbox, notifier
        self._clock, self._sleep = clock, sleep
        self._blocked_until = 0.0
        self._backoff = BACKOFF_START
        self._outage_notified = False
        self._last_prune = float("-inf")
        self._stop = False

    def _problem(self, exc: Exception) -> None:
        log.warning("Paperless unavailable, will retry in %.0fs: %s", self._backoff, exc)
        self._blocked_until = self._clock() + self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)
        if not self._outage_notified:
            self._outage_notified = True
            self.notifier.problem(f"{exc}\nFiles stay in the inbox and will be retried.")

    def _recovered(self) -> None:
        self._backoff = BACKOFF_START
        self._outage_notified = False

    def run_once(self) -> list[Outcome]:
        now = self._clock()
        if now < self._blocked_until:
            return []
        if now - self._last_prune >= PRUNE_EVERY:
            self._last_prune = now
            for folder in self.inbox.prune():
                log.info("pruned %s", folder)

        files = self.inbox.settled_files()
        if not files:
            return []
        try:
            tag_ids = self.client.resolve_tags(self.cfg.tags) if self.cfg.tags else []
        except ServiceProblem as exc:
            self._problem(exc)
            return []

        outcomes: list[Outcome] = []
        for path in files:
            claimed = self.inbox.claim(path)
            if claimed is None:
                continue
            try:
                outcome = send_file(self.client, claimed, tag_ids, wait=self.cfg.wait)
            except ServiceProblem as exc:
                self.inbox.unclaim(claimed)
                self._problem(exc)
                break
            self._recovered()
            dest = self.inbox.archive(claimed, outcome)
            log.info("%s: %s -> %s", outcome.status.value, path.name, dest)
            self.notifier.outcome(outcome)
            outcomes.append(outcome)
        return outcomes

    def once(self) -> list[Outcome]:
        """Process what is in the inbox now, waiting out the settle period first."""
        self.inbox.settled_files()
        self._sleep(self.cfg.settle_seconds)
        return self.run_once()

    def run(self) -> None:
        def stop(signum, frame):
            self._stop = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.inbox.ensure()
        stranded = self.inbox.recover()
        if stranded:
            log.info("returned %d stranded file(s) to the inbox", stranded)
        log.info("watching %s -> %s", self.inbox.root, self.cfg.url)
        while not self._stop:
            try:
                self.run_once()
            except Exception:  # keep the service alive; the next pass retries
                log.exception("unexpected error while processing the inbox")
            self._sleep(self.cfg.poll_seconds)
