"""Human-readable schedules for routines, and time parsing for tasks.

  daily 00:00            every day at midnight
  weekdays 08:30         Monday to Friday
  weekly sun 18:00       one or more days: weekly mon,wed,fri 09:00
  monthly 1 09:00        day of month
  every 30m | 2h | 1d    fixed interval from the last run
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def now() -> datetime:
    return datetime.now().astimezone()


def parse_time(text: str) -> datetime:
    """ISO date/time from the model or a file. Naive times are local."""
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


def _hhmm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad time {text!r}; use HH:MM")
    return int(m.group(1)), int(m.group(2))


def next_run(schedule: str, after: datetime) -> datetime:
    """The first time strictly after `after` that matches the schedule."""
    parts = schedule.lower().split()
    if not parts:
        raise ValueError("empty schedule")
    kind = parts[0]
    if kind == "every":
        m = re.fullmatch(r"(\d+)\s*([mhd])", "".join(parts[1:]))
        if not m:
            raise ValueError("use e.g. 'every 30m', 'every 2h', 'every 1d'")
        units = {"m": "minutes", "h": "hours", "d": "days"}
        return after + timedelta(**{units[m.group(2)]: int(m.group(1))})

    if kind in ("daily", "weekdays") and len(parts) == 2:
        days, (h, mi) = (set(range(7)) if kind == "daily" else set(range(5))), _hhmm(parts[1])
        dom = None
    elif kind == "weekly" and len(parts) == 3:
        try:
            days = {DAYS.index(d[:3]) for d in parts[1].split(",")}
        except ValueError:
            raise ValueError(f"bad day list {parts[1]!r}; use mon,tue,...") from None
        (h, mi), dom = _hhmm(parts[2]), None
    elif kind == "monthly" and len(parts) == 3:
        days, dom, (h, mi) = set(range(7)), int(parts[1]), _hhmm(parts[2])
        if not 1 <= dom <= 31:
            raise ValueError("day of month must be 1-31")
    else:
        raise ValueError(f"unknown schedule {schedule!r}; see: daily HH:MM, weekdays HH:MM, "
                         "weekly mon,wed HH:MM, monthly D HH:MM, every 30m")

    candidate = after.replace(hour=h, minute=mi, second=0, microsecond=0)
    for _ in range(400):
        if candidate > after and candidate.weekday() in days and (dom is None or candidate.day == dom):
            return candidate
        candidate = (candidate + timedelta(days=1)).replace(hour=h, minute=mi)
    raise ValueError(f"schedule {schedule!r} never matches")
