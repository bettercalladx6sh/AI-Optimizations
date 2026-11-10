"""Analysis windows: which hours an analysis covers, built from app/config.py.

    rolling    the last N hours or days, back from the last settled hour
    calendar   the current month and the (N - 1) months before it, from the 1st 00:00 UTC to
               date; on 10 November a 1-month window covers 1-10 November (9 days, not 30)

Every window ends at the last settled hour (see timeseries.settled_end). Labels, readiness and
evidence always use the real span of the window at run time, never a nominal number of days.
Shared by every provider (EC2, Azure VMs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app import config
from app.cloud.display import format_duration
from app.cloud.timeseries import PERIOD, SETTLE_DELAY, floor_hour, settled_end

PURPOSES = ("production", "testing")
KEY_PATTERN = re.compile(r"^[a-z0-9]{1,16}$")


@dataclass(frozen=True)
class AnalysisWindow:
    """How far back an analysis looks, and whether it is a production or testing run."""

    key: str
    purpose: str
    duration: timedelta | None = None      # rolling windows
    calendar_months: int = 0               # calendar windows

    def bounds(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """(start, end): from the 1st of the month for calendar windows, else rolling."""
        end = settled_end(now)
        if self.calendar_months:
            return month_start(end, self.calendar_months - 1), end
        return end - self.duration, end

    @property
    def description(self) -> str:
        """e.g. 'month to date', 'last 3 calendar months to date', 'rolling 7 days'."""
        if self.calendar_months == 1:
            return "month to date"
        if self.calendar_months:
            return f"last {self.calendar_months} calendar months to date"
        return f"rolling {format_duration(self.duration)}"

    def label(self, start: datetime, end: datetime) -> str:
        """e.g. '30d · production · month to date · 9 days 8 h (224 hours)' or '7d · testing · rolling 7 days (168 hours)'."""
        return span_label(self.key, self.purpose, self.description, int((end - start) / PERIOD))

    def ready_at(self, since: datetime) -> datetime:
        """First moment the window no longer contains any hour from before `since`."""
        if self.calendar_months:
            first = month_start(since)
            if first < since:
                first = month_start(first, -1)                  # next month's 1st
            covered_from = month_start(first, -(self.calendar_months - 1))
        else:
            covered_from = _ceil_hour(since + self.duration)
        return covered_from + SETTLE_DELAY


@dataclass(frozen=True)
class Readiness:
    """Whether an instance's current configuration covers the whole window, so it can be evaluated."""

    snapshot: Any                 # the provider's instance snapshot (has instance_id and name)
    reason: str
    since: datetime | None
    ready_at: datetime | None
    checked_at: datetime

    @property
    def ready(self) -> bool:
        return self.ready_at is None or self.checked_at >= self.ready_at


def readiness_of(
    snapshot: Any,
    launched_at: datetime | None,
    changed_at: datetime | None,
    change_reason: str,
    window: AnalysisWindow,
    now: datetime,
) -> Readiness:
    """Ready once the window no longer contains hours from before the later of launch and last change."""
    events = [(moment, reason) for moment, reason in ((launched_at, "launched"), (changed_at, change_reason)) if moment]
    if not events:
        return Readiness(snapshot, "stable", None, None, now)
    since, reason = max(events)
    return Readiness(snapshot, reason, since, window.ready_at(since), now)


def span_label(key: str, purpose: str, description: str, hours: int) -> str:
    """Window label with its real span; calendar windows also show the span in days."""
    count = f"{hours} hour{'s' if hours != 1 else ''}"
    if description.startswith("rolling"):
        return f"{key} · {purpose} · {description} ({count})"
    return f"{key} · {purpose} · {description} · {format_duration(timedelta(hours=hours))} ({count})"


def month_start(moment: datetime, months_back: int = 0) -> datetime:
    """00:00 UTC on the 1st of the month `months_back` months before `moment`'s month (negative = after)."""
    index = moment.year * 12 + (moment.month - 1) - months_back
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


def _ceil_hour(moment: datetime) -> datetime:
    floored = floor_hour(moment)
    return floored if floored == moment else floored + PERIOD


def _build(key: str, settings: dict[str, object]) -> AnalysisWindow:
    """Validate one entry of config.ANALYSIS_WINDOWS and turn it into an AnalysisWindow."""
    where = f"app/config.py ANALYSIS_WINDOWS['{key}']"
    if not KEY_PATTERN.match(key):
        raise ValueError(f"{where}: key must be 1-16 lowercase letters or digits")
    purpose = settings.get("purpose")
    if purpose not in PURPOSES:
        raise ValueError(f"{where}: purpose must be one of {PURPOSES}")
    units = {name: settings[name] for name in ("hours", "days", "months") if name in settings}
    unknown = set(settings) - {"purpose", "hours", "days", "months"}
    if unknown or len(units) != 1:
        raise ValueError(f"{where}: give exactly one of hours, days or months (and purpose)")
    (unit, amount), = units.items()
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise ValueError(f"{where}: {unit} must be a whole number of 1 or more")
    if unit == "months":
        return AnalysisWindow(key, purpose, calendar_months=amount)
    return AnalysisWindow(key, purpose, duration=timedelta(**{unit: amount}))


ANALYSIS_WINDOWS: dict[str, AnalysisWindow] = {
    key: _build(key, settings) for key, settings in config.ANALYSIS_WINDOWS.items()
}
if config.DEFAULT_WINDOW not in ANALYSIS_WINDOWS:
    raise ValueError(f"app/config.py DEFAULT_WINDOW '{config.DEFAULT_WINDOW}' is not in ANALYSIS_WINDOWS")
DEFAULT_WINDOW = config.DEFAULT_WINDOW
