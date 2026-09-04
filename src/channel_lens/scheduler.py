"""Background tracker scheduling.

The tracker exists to build history over time, which only works if it actually
runs. It is deliberately in-process rather than a system cron job: this is an
app you open, and a scheduled task that polls YouTube while the app is closed
would spend quota you didn't ask it to spend.

Every cycle gets its own session and its own client, and any exception is
caught and recorded. A background job that can kill the web server is worse
than no background job.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .db import session_scope
from .services import tracker
from .youtube.client import YouTubeClient
from .youtube.quota import QuotaLedger

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
JOB_ID = "tracker-poll"


def _poll() -> None:
    config = get_settings(refresh=True)
    if not config.tracker_enabled or not config.youtube_api_key:
        return
    try:
        with session_scope() as session:
            client = YouTubeClient(
                config.youtube_api_key, QuotaLedger(config.daily_quota_budget), session
            )
            try:
                job = tracker.run_poll_cycle(
                    client, session,
                    shorts_max_seconds=config.shorts_max_seconds,
                    max_video_age_days=config.tracker_max_video_age_days,
                )
                log.info("Tracker cycle %s: %s", job.status, job.detail or job.error)
            finally:
                client.close()
    except Exception:  # noqa: BLE001 - a failed poll must not stop the scheduler
        log.exception("Tracker poll failed")


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    config = get_settings()
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _poll,
        "interval",
        hours=max(1, config.tracker_interval_hours),
        id=JOB_ID,
        # A laptop that was asleep shouldn't wake to a queue of missed polls;
        # one catch-up cycle is all that's ever useful.
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    log.info("Tracker scheduled every %s hours", config.tracker_interval_hours)
    return _scheduler


def reschedule(hours: int) -> None:
    if _scheduler is not None:
        _scheduler.reschedule_job(JOB_ID, trigger="interval", hours=max(1, hours))


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
