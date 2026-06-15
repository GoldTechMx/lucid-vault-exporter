"""Job model + log plumbing for the web UI.

A `Job` carries live progress (phase/done/total/detail), timing for ETA, a bounded log ring
buffer for the event console, a result/error, and its own `Control` (pause/cancel). The
`JobRegistry` tracks all jobs and the single active one. `JobLogHandler` routes app log records
into the active job's buffer; `RedactionFilter`/`scrub_text` keep secrets (tokens, client
secret) out of those lines.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .control import Control

_MAX_LOG = 1000  # keep the console buffer's tail bounded


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


class RedactionFilter(logging.Filter):
    """Masks known secrets in log records so the event console never shows a token."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            if isinstance(record.msg, str):
                record.msg = scrub_text(record.msg, self._secrets)
            if record.args:
                record.args = tuple(
                    scrub_text(a, self._secrets) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


@dataclass
class Job:
    id: str
    command: str
    status: str = "running"  # running | done | error | cancelled
    logs: list[str] = field(default_factory=list)
    log_dropped: int = 0  # cumulative lines trimmed off the front (so `since` stays absolute)
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    phase: str = "starting"
    done: int = 0
    detail: str = ""
    started: float = 0.0  # monotonic
    phase_started: float = 0.0  # monotonic; resets each phase for per-phase ETA
    control: Control = field(default_factory=Control)

    def progress(self, phase: str, done: int, total: int | None, detail: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.phase_started = time.monotonic()
        self.done = done
        self.total = total or 0
        self.detail = detail


class JobRegistry:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.active: Job | None = None

    def create(self, command: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], command=command)
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)


class JobLogHandler(logging.Handler):
    """Routes app log records into the active job's bounded buffer (secrets already scrubbed)."""

    def __init__(self, registry: JobRegistry) -> None:
        super().__init__()
        self._reg = registry

    def emit(self, record: logging.LogRecord) -> None:
        job = self._reg.active
        if job is None:
            return
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a formatting error must never crash logging
            return
        job.logs.append(line)
        overflow = len(job.logs) - _MAX_LOG
        if overflow > 0:
            del job.logs[:overflow]
            job.log_dropped += overflow
