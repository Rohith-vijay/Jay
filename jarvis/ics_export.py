"""Jay's schedule as an .ics calendar file, to import into Google Calendar, Outlook or a phone.

Jay keeps its own schedule (the plan, study blocks, deadlines). This writes it out in the standard
calendar format so it can also be seen next to everything else. It is rewritten whenever the schedule
changes: data/jay_schedule.ics. Import it once; to refresh, import it again (Google Calendar: Settings,
Import & export). Times are written as local ("floating") times, which is how they were planned.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .store import Store

RULES = {"daily": "FREQ=DAILY", "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
         "weekly": "FREQ=WEEKLY", "monthly": "FREQ=MONTHLY"}


def _stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build(store: Store) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jay//Schedule//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Jay schedule"]
    for task in store.list_tasks():
        anchor = task["start"] or task["due"]
        if not anchor:
            continue
        begin = datetime.fromisoformat(anchor)
        minutes = task["minutes"] or 30
        end = begin + timedelta(minutes=minutes)
        lines += ["BEGIN:VEVENT", f"UID:jay-task-{task['id']}@jay.local", f"DTSTAMP:{_stamp(datetime.fromisoformat(task['created']))}",
                  f"DTSTART:{_stamp(begin)}", f"DTEND:{_stamp(end)}", f"SUMMARY:{_escape(task['title'])}"]
        if task["repeat"] in RULES:
            lines.append(f"RRULE:{RULES[task['repeat']]}")
        if task["priority"] == "high":
            lines.append("PRIORITY:1")
        if task["due"] and task["start"]:
            lines.append(f"DESCRIPTION:Deadline {_escape(task['due'].replace('T', ' '))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:                       # lines over 75 characters are folded, as the format requires
        while len(line.encode("utf-8")) > 74:
            folded.append(line[:74])
            line = " " + line[74:]
        folded.append(line)
    return "\r\n".join(folded) + "\r\n"


def write(store: Store, data_dir: Path) -> Path:
    path = data_dir / "jay_schedule.ics"
    text = build(store)
    try:
        if path.exists() and path.read_bytes() == text.encode("utf-8"):
            return path                      # unchanged: leave the file alone
    except OSError:
        pass
    path.write_text(text, encoding="utf-8", newline="")
    return path
