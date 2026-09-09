"""Background jobs with progress, for work too slow to hold a request open.

Importing twenty channels takes minutes. Doing that inside one HTTP request
gives the browser nothing to show but a spinning tab, and the user no way to
tell "working" from "hung" — which is exactly the failure this exists to fix.

**Progress lives in memory, not in the database.** That is deliberate. A job
cannot outlive the process that runs it, so persisting its progress would only
create rows describing work that can never resume. The durable record of what
happened is still written to :class:`~channel_lens.models.JobRun` as now, and
this registry holds only the live view: what it's on, how far through, and the
per-item results the UI shows at the end.

It also sidesteps a real hazard: the schema is created with ``create_all``,
which does not ALTER existing tables, so adding columns here would silently
do nothing on an install that already has a database.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..db import session_scope

log = logging.getLogger(__name__)

#: Finished jobs are kept this long so a page refresh can still collect the
#: result. Beyond that they are only clutter.
_KEEP_FINISHED = 60 * 30


@dataclass
class Job:
    """Live state of one background job."""

    id: str
    name: str
    #: "running", "ok", "error", or "cancelled".
    status: str = "running"
    total: int = 0
    done: int = 0
    #: What is being worked on right now, for the progress line.
    current: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    #: Whatever the worker chose to return. Shape is the worker's business.
    result: dict[str, Any] | None = None
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def cancelled(self) -> bool:
        """Workers check this between items to stop at a clean boundary."""
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def advance(self, label: str = "") -> None:
        """Mark one item finished and name the next one."""
        self.done += 1
        self.current = label

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "fraction": round(self.done / self.total, 4) if self.total else 0.0,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "running": self.status == "running",
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancelled,
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _prune() -> None:
    now = datetime.now(timezone.utc)
    stale = [
        job_id for job_id, job in _jobs.items()
        if job.finished_at and (now - job.finished_at).total_seconds() > _KEEP_FINISHED
    ]
    for job_id in stale:
        _jobs.pop(job_id, None)


def start(name: str, total: int, worker: Callable[[Job, Any], dict[str, Any]]) -> Job:
    """Run ``worker`` on a background thread and return the job immediately.

    The worker is handed the :class:`Job` (to report progress and check for
    cancellation) and a fresh SQLAlchemy session of its own — never the request
    session, which is closed the moment the response is sent.
    """
    job = Job(id=uuid.uuid4().hex[:12], name=name, total=total)
    with _lock:
        _prune()
        _jobs[job.id] = job

    def run() -> None:
        try:
            with session_scope() as session:
                job.result = worker(job, session)
            job.status = "cancelled" if job.cancelled else "ok"
        except Exception as exc:  # noqa: BLE001 - a job must never kill the app
            log.exception("Background job %s (%s) failed", job.id, name)
            job.status = "error"
            job.error = str(exc).split("\n", 1)[0][:400] or type(exc).__name__
        finally:
            job.finished_at = datetime.now(timezone.utc)
            job.current = ""

    threading.Thread(target=run, name=f"job-{name}-{job.id}", daemon=True).start()
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def active(name: str | None = None) -> Job | None:
    """The running job, optionally filtered by name.

    Lets a page that is reopened mid-import re-attach to the job already in
    flight instead of showing nothing.
    """
    for job in _jobs.values():
        if job.status == "running" and (name is None or job.name == name):
            return job
    return None


def reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
