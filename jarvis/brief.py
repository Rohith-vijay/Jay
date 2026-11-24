"""Jay v0: the daily plan, gathered from the schedule and the calendar feed and sent to the phone."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .store import Store, describe_task

PUSH_PROMPT = (
    "(Automatic morning plan from the app; the user has not said anything.) Write today's plan "
    "as a short phone notification from the data below. Plain text only, no markdown, at most "
    "90 words. Put things in time order, say what to do first, and call out anything overdue or "
    "clashing. If the data is empty, say the day is clear and suggest the best use of it from the "
    "user's goals. Do not call tools except get_weather once, and only if their city is known.\n\n"
    "TODAY ({today:%A %d %B})\n{data}")


EVENING_PROMPT = (
    "(Automatic evening check-in from the app; the user has not said anything.) The user has just sat down "
    "at the laptop after the day. Write a short phone notification from the data below: plain text, no markdown, "
    "at most 100 words. Say what got done today (and how it was confirmed, when there is proof), what is still "
    "open, what slipped and where it moved, and what tomorrow starts with. If something cannot be checked "
    "automatically, ask about it in ONE short question at the end. Do not call tools; use only the data.\n\n"
    "TODAY ({today:%A %d %B})\n{data}")


def gather_evening(store: Store, proofs: list[str] | None = None, notes: str = "") -> str:
    """What the evening summary is built from: done, open, proven, changed, and tomorrow."""
    today = date.today()
    day = today.isoformat()
    done = [r["title"] for r in store._rows(
        "SELECT title FROM blocks WHERE status = 'done' AND substr(resolved, 1, 10) = ?", (day,))]
    done += [r["title"] for r in store._rows(
        "SELECT title FROM tasks WHERE done = 1 AND substr(done_at, 1, 10) = ?", (day,)) if r["title"] not in done]
    open_today = [t["title"] for t in store.list_tasks()
                  if (t["start"] or t["due"] or "").startswith(day) and not t["done"]]
    tomorrow = (today + timedelta(days=1)).isoformat()
    next_up = [describe_task(t) for t in store.list_tasks() if (t["start"] or t["due"] or "").startswith(tomorrow)][:4]
    parts = []
    parts.append("Done today:\n" + ("\n".join(f"- {t}" for t in dict.fromkeys(done)) or "- nothing recorded yet"))
    if proofs:
        parts.append("Confirmed by evidence:\n" + "\n".join(f"- {p}" for p in proofs))
    parts.append("Still open today:\n" + ("\n".join(f"- {t}" for t in open_today) or "- nothing"))
    if notes.strip():
        parts.append("What changed in the plan:\n" + notes.strip())
    parts.append("Tomorrow:\n" + ("\n".join(f"- {t}" for t in next_up) or "- nothing planned yet"))
    return "\n\n".join(parts)


def day_items(store: Store, today: date | None = None) -> tuple[list[str], list[str]]:
    """(today's items in time order, overdue items) from the task list."""
    today = today or date.today()
    now = datetime.now()
    due_today, overdue = [], []
    for t in store.list_tasks():
        anchor = t["start"] or t["due"]
        if not anchor:
            continue
        if anchor[:10] == today.isoformat():
            due_today.append((anchor, describe_task(t)))
        elif t["due"] and t["due"] < now.isoformat(timespec="minutes") and anchor[:10] < today.isoformat():
            overdue.append(describe_task(t))
    return [text for _, text in sorted(due_today)], overdue


def calendar_today() -> list[str]:
    """Today's events from the iCal feeds (empty when none are set up or reachable)."""
    from .tools.mailbox import feed_events, feed_urls

    if not feed_urls():
        return []
    today = f"{date.today():%a %d %b}"
    return [line for line in feed_events(1).splitlines() if line.startswith(today)]


def gather(store: Store, calendar: list[str] | None = None) -> str:
    items, overdue = day_items(store)
    events = calendar_today() if calendar is None else calendar
    parts = []
    if events:
        parts.append("Calendar:\n" + "\n".join(f"- {e}" for e in events))
    if items:
        parts.append("Schedule:\n" + "\n".join(f"- {i}" for i in items))
    if overdue:
        parts.append("Overdue:\n" + "\n".join(f"- {i}" for i in overdue))
    return "\n\n".join(parts) or "(nothing scheduled)"


def plain_plan(data: str) -> str:
    """The same information without a brain, for when none can be reached."""
    return "Today's plan\n" + data
