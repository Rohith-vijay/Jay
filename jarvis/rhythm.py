"""When Jay speaks up: two moments a day, when you are at the laptop, not a ping every hour.

  * Morning: the first time you are at the laptop between 05:00 and 12:00.
  * Evening: after you are home and at the laptop, at or after 19:30 on a college day (a class, CRT or
    college event in your calendar, or a weekday when there is no calendar), or from 17:30 on other days.

"At the laptop" means the keyboard or mouse was used in the last two minutes (Windows' own idle timer), so
nothing is sent to a closed or idle laptop and it all waits until you sit down.
Planned blocks no longer ring individually; they are all in those two summaries.
"""
from __future__ import annotations

import re
from datetime import date, datetime

MORNING_FROM, MORNING_UNTIL = (5, 0), (12, 0)
COLLEGE_WORDS = re.compile(r"(?i)\b(college|crt|class|lab|exam)\b")


def idle_seconds() -> float:
    """Seconds since the last key or mouse movement (Windows' GetLastInputInfo). 0 if it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except Exception:
        return 0.0


def calendar_lines(day: date, feed_text: str) -> list[str]:
    """The lines of a calendar listing that fall on `day` (the format feed_events produces)."""
    prefix = f"{day:%a %d %b}"
    return [line for line in feed_text.splitlines() if line.startswith(prefix)]


def college_day(day: date, lines: list[str] | None) -> bool:
    """True if `day` is a college day: the calendar says so, or (with no calendar) it is a weekday."""
    if lines:
        return any(COLLEGE_WORDS.search(line) for line in lines)
    return day.weekday() < 5


def evening_start(is_college: bool, college_at: str = "19:30", other_at: str = "17:30") -> tuple[int, int]:
    text = college_at if is_college else other_at
    hour, minute = text.split(":")
    return int(hour), int(minute)


def due(now: datetime, last_morning: str, last_evening: str, idle: float, is_college: bool,
        college_at: str = "19:30", other_at: str = "17:30") -> tuple[str | None, bool]:
    """Which summary is due right now: ("morning" | "evening" | None, skip_morning).

    skip_morning is True when the morning window has passed without one, so a "morning" summary is not
    sent at lunchtime (the day just starts with the evening one)."""
    today = now.date().isoformat()
    at_laptop = idle < 120
    hhmm = (now.hour, now.minute)
    skip = last_morning != today and hhmm >= MORNING_UNTIL
    if last_morning != today and MORNING_FROM <= hhmm < MORNING_UNTIL and at_laptop:
        return "morning", False
    if last_evening != today and hhmm >= evening_start(is_college, college_at, other_at) and at_laptop:
        return "evening", skip
    return None, skip
