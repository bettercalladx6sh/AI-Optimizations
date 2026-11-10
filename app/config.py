"""Analysis configuration: the windows (durations) every analysis, evidence file and
recommendation is based on, and the evidence and sizing thresholds.

Analysis windows
    Each window is selected by its key on the command line (python -m app.reasoning 7d).
    A window is one of:
        rolling    "hours" or "days" back from the last settled hour
        calendar   "months": the current calendar month and the months before it, from the
                   1st at 00:00 UTC to date (on 10 November, 1 month = 1-10 November)
    The analysis covers exactly that span; nothing else assumes a fixed number of days.

Evaluation readiness
    An instance is evaluated only when its current configuration covers the whole window:
    after a launch or a significant configuration change, it waits until the window no
    longer contains any hour from before the change (rolling: the window's length after the
    change; calendar: until the window starts after the change).

Add, remove or edit windows below; keys must be short lowercase names (letters, digits).
"""

from __future__ import annotations

# ------------------------------------------------------------------ analysis windows

ANALYSIS_WINDOWS: dict[str, dict[str, object]] = {
    # testing: rolling windows
    "1h": {"purpose": "testing", "hours": 1},
    "1d": {"purpose": "testing", "days": 1},
    "3d": {"purpose": "testing", "days": 3},
    "7d": {"purpose": "testing", "days": 7},
    # production: calendar windows, month to date
    "30d": {"purpose": "production", "months": 1},
    "90d": {"purpose": "production", "months": 3},
}
DEFAULT_WINDOW = "30d"

# ------------------------------------------------------------------ metric collection

SETTLE_DELAY_MINUTES = 15          # an hour's average is final this long after the hour ends

# ------------------------------------------------------------------ evidence thresholds

MIN_COVERAGE_PERCENT = 90.0        # each metric must have datapoints for this share of the window hours
MIN_EVIDENCE_HOURS = 1             # and at least this many hours, whatever the window
LIMITED_HISTORY_HOURS = 24         # flag a metric with fewer hours than this (or than the window, if shorter)

# ------------------------------------------------------------------ sizing

CPU_SAFETY_FACTOR = 1.25           # required capacity = p95 utilization x safety factor
MEMORY_SAFETY_FACTOR = 1.25
