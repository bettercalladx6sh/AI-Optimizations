"""Analysis windows: which hours an analysis covers.

Production windows follow the calendar:
    30d  the current month to date, from the 1st at 00:00 UTC (even when fewer than 30 days
         have passed, e.g. on 10 November it covers 1-10 November)
    90d  the current month and the two before it, to date (e.g. 1 September - 10 November)
Testing windows are rolling: 1h, 1d, 3d, 7d back from now.

Every window ends at the last settled hour (see metrics.SETTLE_DELAY). The nominal duration
(30 days, 90 days, ...) is also how long an instance must stay unchanged before it can be
evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.cloud.aws.ec2.metrics import settled_end


@dataclass(frozen=True)
class AnalysisWindow:
    """How far back an analysis looks, and whether it is a production or testing run."""

    key: str
    duration: timedelta
    purpose: str
    calendar_months: int = 0

    def bounds(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """Return (start, end): from the 1st of the month for calendar windows, else rolling."""
        end = settled_end(now)
        if self.calendar_months:
            return month_start(end, self.calendar_months - 1), end
        return end - self.duration, end

    @property
    def description(self) -> str:
        if self.calendar_months == 1:
            return "month to date"
        if self.calendar_months:
            return f"last {self.calendar_months} calendar months to date"
        return f"rolling {self.key}"


def month_start(moment: datetime, months_back: int = 0) -> datetime:
    """00:00 UTC on the 1st of the month `months_back` months before `moment`'s month."""
    index = moment.year * 12 + (moment.month - 1) - months_back
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


# Production uses calendar windows (30d, 90d); testing uses rolling ones.
ANALYSIS_WINDOWS = {
    window.key: window
    for window in (
        AnalysisWindow("1h", timedelta(hours=1), "testing"),
        AnalysisWindow("1d", timedelta(days=1), "testing"),
        AnalysisWindow("3d", timedelta(days=3), "testing"),
        AnalysisWindow("7d", timedelta(days=7), "testing"),
        AnalysisWindow("30d", timedelta(days=30), "production", calendar_months=1),
        AnalysisWindow("90d", timedelta(days=90), "production", calendar_months=3),
    )
}
DEFAULT_WINDOW = "30d"