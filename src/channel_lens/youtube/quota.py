"""Data API quota accounting.

The YouTube Data API gives a project 10,000 units a day and resets at midnight
US/Pacific. Costs are wildly uneven: ``videos.list`` is 1 unit for up to 50
videos, while ``search.list`` is 100 units for up to 50 results — a hundred
times more expensive for strictly less information. Almost every design choice
in this app follows from that ratio, so the ledger is a first-class object
rather than a counter tucked inside the HTTP client.

Three rules:

1. **Estimate before spending.** An operation declares its cost up front and is
   refused if the budget can't cover it. Discovering exhaustion from a 403 is
   too late — by then the partial work is already paid for.
2. **Prefer the cheap endpoint.** Resolving a channel's uploads through
   ``playlistItems.list`` costs 1 unit per 50 videos; the same thing through
   ``search.list`` costs 100. They are never interchangeable here.
3. **Reconcile on conflict.** If Google says the quota is gone while our ledger
   disagrees, Google wins and the ledger is marked full for the day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import QuotaUsage

#: YouTube's quota day boundary. Not local midnight, and not UTC midnight.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")

#: Documented unit cost per endpoint. A "call" here is one HTTP request, which
#: may return up to 50 items — the cost is per request, not per item, which is
#: why batching ids 50 at a time matters so much.
ENDPOINT_COSTS: dict[str, int] = {
    "search.list": 100,
    "videos.list": 1,
    "channels.list": 1,
    "playlistItems.list": 1,
    "playlists.list": 1,
    "commentThreads.list": 1,
    "captions.list": 50,
    "videoCategories.list": 1,
}

#: Requests are capped at 50 ids or results each by the API itself.
MAX_BATCH = 50


def cost_of(endpoint: str, calls: int = 1) -> int:
    """Units for ``calls`` requests to ``endpoint``.

    >>> cost_of("videos.list", 4)
    4
    >>> cost_of("search.list", 3)
    300
    """
    return ENDPOINT_COSTS.get(endpoint, 1) * calls


def calls_for_items(item_count: int) -> int:
    """Number of requests needed to fetch ``item_count`` items, 50 at a time.

    >>> calls_for_items(0), calls_for_items(1), calls_for_items(50), calls_for_items(51)
    (0, 1, 1, 2)
    """
    if item_count <= 0:
        return 0
    return (item_count + MAX_BATCH - 1) // MAX_BATCH


def quota_day(now: datetime | None = None) -> date:
    """The Pacific calendar day that ``now`` falls in.

    >>> from datetime import datetime, timezone
    >>> # 06:00 UTC on the 2nd is still 23:00 Pacific on the 1st.
    >>> quota_day(datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc))
    datetime.date(2026, 3, 1)
    >>> quota_day(datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc))
    datetime.date(2026, 3, 2)
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(QUOTA_TZ).date()


def next_reset(now: datetime | None = None) -> datetime:
    """UTC instant of the next quota reset.

    >>> from datetime import datetime, timezone
    >>> r = next_reset(datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc))
    >>> r > datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    True
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(QUOTA_TZ)
    tomorrow = (local + timedelta(days=1)).date()
    reset_local = datetime.combine(tomorrow, datetime.min.time(), tzinfo=QUOTA_TZ)
    return reset_local.astimezone(timezone.utc)


@dataclass
class QuotaStatus:
    """A snapshot of today's spend, shaped for direct display."""

    day: date
    used: int
    budget: int
    resets_at: datetime
    by_endpoint: dict[str, int]

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def fraction_used(self) -> float:
        return min(1.0, self.used / self.budget) if self.budget else 1.0

    def human_reset(self, now: datetime | None = None) -> str:
        """Time until reset, phrased the way a person would say it.

        >>> from datetime import datetime, timedelta, timezone
        >>> now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        >>> s = QuotaStatus(date(2026, 3, 2), 0, 9000, now + timedelta(hours=4, minutes=30), {})
        >>> s.human_reset(now)
        '4h 30m'
        >>> s = QuotaStatus(date(2026, 3, 2), 0, 9000, now + timedelta(minutes=7), {})
        >>> s.human_reset(now)
        '7m'
        """
        now = now or datetime.now(timezone.utc)
        delta = self.resets_at - now
        total_minutes = max(0, int(delta.total_seconds() // 60))
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "used": self.used,
            "budget": self.budget,
            "remaining": self.remaining,
            "fraction_used": round(self.fraction_used, 4),
            "resets_at": self.resets_at.isoformat(),
            "resets_in": self.human_reset(),
            "by_endpoint": self.by_endpoint,
        }


class QuotaLedger:
    """Reads and writes :class:`QuotaUsage` rows for the current Pacific day.

    Deliberately holds no session of its own: it is handed one per operation so
    that quota writes commit in the same transaction as the work they paid for.
    A crash between "spent the unit" and "recorded the unit" would otherwise
    make the ledger optimistic, which is the one direction it must never drift.
    """

    def __init__(self, budget: int) -> None:
        self.budget = budget

    def status(self, session: Session, now: datetime | None = None) -> QuotaStatus:
        day = quota_day(now)
        rows = session.scalars(
            select(QuotaUsage).where(QuotaUsage.day == day)
        ).all()
        by_endpoint = {r.endpoint: r.units for r in rows}
        return QuotaStatus(
            day=day,
            used=sum(by_endpoint.values()),
            budget=self.budget,
            resets_at=next_reset(now),
            by_endpoint=by_endpoint,
        )

    def used_today(self, session: Session, now: datetime | None = None) -> int:
        return self.status(session, now).used

    def can_afford(self, session: Session, units: int, now: datetime | None = None) -> bool:
        return self.used_today(session, now) + units <= self.budget

    def check(self, session: Session, units: int, what: str = "This request",
              now: datetime | None = None) -> None:
        """Raise :class:`QuotaExceeded` if ``units`` won't fit in today's budget."""
        from .errors import QuotaExceeded

        status = self.status(session, now)
        if status.used + units > self.budget:
            raise QuotaExceeded(
                f"{what} needs {units:,} quota units but only {status.remaining:,} "
                f"of today's {self.budget:,} remain.",
                hint=(
                    f"Quota resets in {status.human_reset(now)}. Narrow the date range or "
                    f"the number of channels, or raise the daily budget in Settings if your "
                    f"Google Cloud project has a higher allowance."
                ),
                used=status.used,
                budget=self.budget,
                requested=units,
                resets_at=status.resets_at.isoformat(),
            )

    def record(self, session: Session, endpoint: str, calls: int = 1,
               now: datetime | None = None) -> int:
        """Book ``calls`` requests to ``endpoint``. Returns the units charged.

        Uses read-then-write rather than an upsert because SQLite is
        single-writer here and the surrounding transaction already serialises
        concurrent tracker and request activity.
        """
        day = quota_day(now)
        units = cost_of(endpoint, calls)
        row = session.scalar(
            select(QuotaUsage).where(
                QuotaUsage.day == day, QuotaUsage.endpoint == endpoint
            )
        )
        if row is None:
            row = QuotaUsage(day=day, endpoint=endpoint, calls=0, units=0)
            session.add(row)
        row.calls += calls
        row.units += units
        session.flush()
        return units

    def mark_exhausted(self, session: Session, now: datetime | None = None) -> None:
        """Force the ledger to full after Google reported ``quotaExceeded``.

        Google is the authority. Our count can only ever be an underestimate —
        the same key may be in use by another tool — so on disagreement we
        record the shortfall and stop trying for the day rather than hammering
        an endpoint that will only keep returning 403.
        """
        status = self.status(session, now)
        shortfall = max(0, self.budget - status.used)
        if shortfall:
            self.record_units(session, "reconciliation", shortfall, now)

    def record_units(self, session: Session, endpoint: str, units: int,
                     now: datetime | None = None) -> None:
        """Book a raw unit count against ``endpoint``, bypassing the cost table."""
        day = quota_day(now)
        row = session.scalar(
            select(QuotaUsage).where(
                QuotaUsage.day == day, QuotaUsage.endpoint == endpoint
            )
        )
        if row is None:
            row = QuotaUsage(day=day, endpoint=endpoint, calls=0, units=0)
            session.add(row)
        row.units += units
        session.flush()
