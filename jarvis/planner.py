"""Jay v1: tracks whether planned blocks get done, re-slots what was missed, and writes the week's summary.

All of this is plain bookkeeping on the local database: it needs no AI brain, so it keeps
working when the free brains are slow or rate-limited. The brain is only used to phrase the
weekly summary, and a plain version is sent if none can be reached.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from .store import Store

DAY_START, DAY_END = (8, 0), (22, 30)      # when work can be planned
DEFAULT_MINUTES = 30
NUDGE_AFTER = timedelta(minutes=10)        # a block's end + this: ask whether it got done
GRACE = timedelta(minutes=45)              # a block's end + this: it counts as missed
SOON_REPEATS = ("daily", "weekdays")       # next time round is soon, so a missed one is not made up


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _ceil(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0)
    return t + timedelta(minutes=(-(t.minute % 15)) % 15)


def busy_intervals(store: Store, ignore_task: int | None = None) -> list[tuple[datetime, datetime]]:
    spans = []
    for t in store.list_tasks():
        if t["start"] and t["id"] != ignore_task:
            begin = _parse(t["start"])
            spans.append((begin, begin + timedelta(minutes=t["minutes"] or DEFAULT_MINUTES)))
    return spans


def next_free_slot(store: Store, minutes: int | None, after: datetime | None = None,
                   ignore_task: int | None = None, days: int = 14) -> datetime:
    """The earliest quarter-hour at or after `after` where `minutes` fit inside the working day
    without touching anything else that is planned (a 5-minute gap is kept between blocks)."""
    minutes = minutes or DEFAULT_MINUTES
    spans = busy_intervals(store, ignore_task)
    t = _ceil(after or datetime.now() + timedelta(minutes=10))
    limit = t + timedelta(days=days)
    gap = timedelta(minutes=5)
    while t < limit:
        day_open = t.replace(hour=DAY_START[0], minute=DAY_START[1])
        day_close = t.replace(hour=DAY_END[0], minute=DAY_END[1])
        if t < day_open:
            t = day_open
        end = t + timedelta(minutes=minutes)
        if end > day_close:
            t = (t + timedelta(days=1)).replace(hour=DAY_START[0], minute=DAY_START[1])
            continue
        clash = next((b for a, b in spans if a - gap < end and t < b + gap), None)
        if clash is None:
            return t
        t = _ceil(clash + gap)
    return _ceil(after or datetime.now())


def when_words(t: datetime) -> str:
    today = date.today()
    day = "today" if t.date() == today else "tomorrow" if t.date() == today + timedelta(days=1) else f"{t:%A}"
    clock = f"{t:%I:%M %p}".lstrip("0")
    return f"{day} at {clock}"


EVENING_START = (19, 30)          # where a block that slipped is carried to (the evening you sit down to study)


def _carry_slot(store: Store, minutes: int, now: datetime, task_id: int | None = None) -> datetime:
    """Tomorrow evening: the first free slot from 19:30 on the next day."""
    start = (now + timedelta(days=1)).replace(hour=EVENING_START[0], minute=EVENING_START[1], second=0, microsecond=0)
    return next_free_slot(store, minutes, start, ignore_task=task_id)


def sweep(store: Store, now: datetime | None = None) -> dict:
    """Look at every block whose time has come, quietly. A block that was not done is marked missed and,
    at most ONCE, carried to tomorrow evening; if the same thing is already planned for tomorrow it is
    merged into that one, and a second miss lets it go. Nothing here messages anyone: what happened is
    returned as notes for the next summary.

    (An earlier version moved a missed block to the next free slot every time, so one LeetCode item was
    re-planned and re-announced every 45 minutes all day. That loop is what this replaces.)"""
    now = now or datetime.now()
    notes: list[str] = []
    for block in store.open_blocks():
        task = store.get_task(block["task_id"])
        if task is None or (task["done"] and not task["repeat"]):
            store.resolve_block(block["id"], "done" if task and task["done"] else "dropped")
            continue
        minutes = block["minutes"] or DEFAULT_MINUTES
        end = _parse(block["start"]) + timedelta(minutes=minutes)
        if now < end + GRACE:
            continue
        store.resolve_block(block["id"], "missed")
        store.log_event("missed", block["title"])
        title = block["title"]
        if task["repeat"]:
            if task["repeat"] in SOON_REPEATS:
                notes.append(f"'{title}' was not done ({_parse(block['start']):%a}); it comes round again.")
            else:
                slot = _carry_slot(store, minutes, now)
                store.add_task(f"Make-up: {title}", start=slot.isoformat(timespec="minutes"),
                               minutes=block["minutes"], priority=task["priority"])
                notes.append(f"'{title}' was not done; a make-up is set for {when_words(slot)}.")
            continue
        carried = int(store.get_meta(f"carry:{task['id']}") or 0)
        twin = next((t for t in store.list_tasks() if t["id"] != task["id"] and t["title"] == task["title"]
                     and t["start"] and _parse(t["start"]).date() > _parse(block["start"]).date()), None)
        if twin is not None:
            store.delete_task(task["id"])
            notes.append(f"'{title}' was not done on {_parse(block['start']):%a}; it is merged into the one on "
                         f"{_parse(twin['start']):%a}.")
        elif carried >= 1:
            store.delete_task(task["id"])
            notes.append(f"'{title}' was missed twice, so I've let it go (it can be re-added any time).")
        else:
            slot = _carry_slot(store, minutes, now, task["id"])
            store.update_task(task["id"], start=slot.isoformat(timespec="minutes"))
            store.set_meta(f"carry:{task['id']}", "1")
            risk = ""
            if task["due"] and slot > _parse(task["due"]):
                risk = f" That is after its deadline ({_parse(task['due']):%a %d %b}), so it needs a decision."
            notes.append(f"'{title}' was not done; carried to {when_words(slot)}.{risk}")
    return {"nudge": [], "missed": notes}


# ------------------------------------------------------------------------------- weekly summary
def week_stats(store: Store, days: int = 7, today: date | None = None) -> dict:
    today = today or date.today()
    since = (datetime.combine(today, datetime.min.time()) - timedelta(days=days - 1)).isoformat(timespec="minutes")
    rows = store._rows("SELECT * FROM blocks WHERE start >= ? AND status IN ('done','missed','moved')", (since,))
    done = [r for r in rows if r["status"] == "done"]
    missed = [r for r in rows if r["status"] == "missed"]
    moved = [r for r in rows if r["status"] == "moved"]
    counted = len(done) + len(missed)
    finished = store._rows("SELECT done_at FROM tasks WHERE done = 1 AND done_at >= ?", (since,))
    active = {r["resolved"][:10] for r in done if r["resolved"]} | {r["done_at"][:10] for r in finished}

    def slot(r) -> str:
        t = _parse(r["start"])
        return f"{t:%A} {'morning' if t.hour < 12 else 'afternoon' if t.hour < 17 else 'evening'}"

    return {
        "days": days, "done": len(done), "missed": len(missed), "moved": len(moved),
        "adherence": round(100 * len(done) / counted) if counted else None,
        "missed_titles": Counter(r["title"] for r in missed).most_common(5),
        "done_titles": Counter(r["title"] for r in done).most_common(5),
        "missed_slots": Counter(slot(r) for r in missed).most_common(3),
        "active_days": len(active),
        "tasks_finished": len(finished),
        "open_overdue": len(store._rows("SELECT id FROM tasks WHERE done = 0 AND due IS NOT NULL AND due < ?",
                                        (datetime.now().isoformat(timespec="minutes"),))),
    }


def describe_stats(st: dict) -> str:
    share = f" ({st['adherence']}% of blocks done)." if st["adherence"] is not None else "."
    lines = [f"Last {st['days']} days: {st['done']} planned blocks done, {st['missed']} missed, "
             f"{st['moved']} re-planned{share}",
             f"Active on {st['active_days']} of {st['days']} days; {st['tasks_finished']} tasks finished; "
             f"{st['open_overdue']} overdue."]
    if st["done_titles"]:
        lines.append("Done most: " + ", ".join(f"{t} x{n}" for t, n in st["done_titles"]))
    if st["missed_titles"]:
        lines.append("Missed most: " + ", ".join(f"{t} x{n}" for t, n in st["missed_titles"]))
    if st["missed_slots"]:
        lines.append("Slots missed most: " + ", ".join(f"{s} x{n}" for s, n in st["missed_slots"]))
    return "\n".join(lines)


def learn_from_week(store: Store, st: dict) -> list[str]:
    """Turn a week's evidence into lasting insights (once per pattern), so planning adapts."""
    saved = []
    have = [f["fact"] for f in store.all_facts()]
    for slot, n in st["missed_slots"]:
        if n >= 2 and not any(f"planned for {slot} get missed" in h for h in have):
            fact = f"Blocks planned for {slot} get missed (missed {n} times in one week): plan lighter there or move them."
            store.add_fact(fact, "insight")
            saved.append(fact)
    if st["adherence"] is not None and st["adherence"] >= 85 and st["done"] >= 5:
        fact = "A week with most planned blocks kept was recorded: that load is the realistic baseline for planning."
        if fact not in have:
            store.add_fact(fact, "pattern")
            saved.append(fact)
    return saved


WEEKLY_PROMPT = (
    "(Automatic weekly summary from the app; the user has not said anything.) Write the week's "
    "summary as a short phone notification from the measurements below. Plain text, no markdown, "
    "at most 110 words: how the week went in one honest sentence, what went well, what slipped "
    "and the likely reason, and ONE concrete change you will make to next week's plan. Do not "
    "call tools; use only these figures and your memories of the user. Never guess causes the "
    "figures do not show. If no blocks were recorded yet, say tracking has just started, give no "
    "verdict, and say what you will track.\n\nWEEK\n{stats}")
