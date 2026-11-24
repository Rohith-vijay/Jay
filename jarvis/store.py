"""SQLite-backed long-term memory, schedule, and the activity log patterns are learned from."""
from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (      -- what happened to tasks, for learning habits
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- added | done | rescheduled | deleted
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (      -- each planned work block once its time arrives: done, missed or moved
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    minutes INTEGER,
    status TEXT NOT NULL DEFAULT 'open',  -- open | done | missed | moved | dropped
    nudged INTEGER NOT NULL DEFAULT 0,
    resolved TEXT
);
CREATE TABLE IF NOT EXISTS log (         -- conversation history, reviewed to learn patterns
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- The user's own notes and documents, cut into passages and searchable by keyword.
"""
PASSAGE_CHARS = 1200
KNOWLEDGE_TABLE = ("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5("
                   "source, passage, page UNINDEXED, tokenize = 'porter unicode61 remove_diacritics 2')")
STOPWORDS = frozenset("""a an the is are was were be been of in on at to for from by with and or but not what which who 
whom whose when where why how do does did can could should would will shall may might this that these those 
it its as about into over under than then so if you your me my i we they them he she explain tell define 
describe give list state write short note notes""".split())


def split_passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Cut text into passages of about `size` characters, breaking at paragraph ends where possible."""
    passages, current = [], ""
    for para in (p.strip() for p in text.replace("\r\n", "\n").split("\n\n")):
        if not para:
            continue
        while len(para) > size:                      # one huge paragraph: cut it at a space
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                passages.append(current)
                current = ""
            passages.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + len(para) + 2 > size:
            passages.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        passages.append(current)
    return passages
# Columns added after the first version; applied to existing databases on open.
TASK_COLUMNS = {"start": "TEXT", "minutes": "INTEGER", "priority": "TEXT NOT NULL DEFAULT 'normal'",
                "done_at": "TEXT", "repeat": "TEXT"}
PRIORITIES = ("low", "normal", "high")
REPEATS = ("daily", "weekdays", "weekly", "monthly")
UNSET = object()


def next_occurrence(when: str, repeat: str, after: datetime | None = None) -> str:
    """The next time a repeating item comes round, strictly after `after` (default: now)."""
    after = after or datetime.now()
    t = datetime.fromisoformat(when)
    while t <= after:
        if repeat == "daily":
            t += timedelta(days=1)
        elif repeat == "weekdays":
            t += timedelta(days=1)
            while t.weekday() >= 5:
                t += timedelta(days=1)
        elif repeat == "weekly":
            t += timedelta(weeks=1)
        elif repeat == "monthly":
            month = t.month % 12 + 1
            year = t.year + (t.month == 12)
            for day in (t.day, 30, 29, 28):          # 31 Jan -> 28/29 Feb, and so on
                try:
                    t = t.replace(year=year, month=month, day=day)
                    break
                except ValueError:
                    continue
        else:
            raise ValueError(f"repeat must be one of {REPEATS}")
    return t.isoformat(timespec="minutes")


def _now() -> str:
    return datetime.now().isoformat(timespec="minutes")


def _when(value: str | None) -> str | None:
    return datetime.fromisoformat(value).isoformat(timespec="minutes") if value else None


def describe_task(t) -> str:
    bits = [f"#{t['id']}", "[done]" if t["done"] else "", t["title"]]
    if t["priority"] != "normal":
        bits.append(f"({t['priority']} priority)")
    if t["start"]:
        bits.append(f"planned {t['start']}")
    if t["minutes"]:
        bits.append(f"~{t['minutes']} min")
    if t["due"]:
        bits.append(f"deadline {t['due']}")
    if "repeat" in t.keys() and t["repeat"]:
        bits.append(f"repeats {t['repeat']}")
    return " ".join(b for b in bits if b)


class Store:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(tasks)")}
            for name, kind in TASK_COLUMNS.items():
                if name not in have:
                    self._db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            self._upgrade_knowledge()
            self._db.commit()

    def _upgrade_knowledge(self) -> None:
        """Older databases have the knowledge table without word stems or page numbers: rebuild it."""
        row = self._db.execute("SELECT sql FROM sqlite_master WHERE name = 'knowledge'").fetchone()
        if row and "porter" in (row["sql"] or ""):
            return
        old = self._db.execute("SELECT source, passage FROM knowledge").fetchall() if row else []
        if row:
            self._db.execute("DROP TABLE knowledge")
        self._db.execute(KNOWLEDGE_TABLE)
        self._db.executemany("INSERT INTO knowledge (source, passage, page) VALUES (?, ?, '')",
                             [(r["source"], r["passage"]) for r in old])

    def _run(self, sql: str, args: tuple = ()) -> tuple[list[sqlite3.Row], int, int]:
        """Returns (rows, last inserted id, rows changed)."""
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows, cur.lastrowid, cur.rowcount

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._run(sql, args)[0]

    # ---- facts -----------------------------------------------------------
    def add_fact(self, fact: str, category: str = "general") -> int:
        return self._run("INSERT INTO facts (category, fact, created) VALUES (?, ?, ?)",
                         (category, fact.strip(), _now()))[1]

    def all_facts(self) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM facts ORDER BY id")

    def search_facts(self, query: str) -> list[sqlite3.Row]:
        words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
        clause = " OR ".join("lower(fact) LIKE ? OR lower(category) LIKE ?" for _ in words)
        args = tuple(x for w in words for x in (f"%{w}%", f"%{w}%"))
        return self._rows(f"SELECT * FROM facts WHERE {clause} ORDER BY id", args)

    def delete_fact(self, fact_id: int) -> bool:
        return self._run("DELETE FROM facts WHERE id = ?", (fact_id,))[2] > 0

    # ---- tasks / schedule -------------------------------------------------
    def add_task(self, title: str, due: str | None = None, start: str | None = None,
                 minutes: int | None = None, priority: str = "normal", repeat: str | None = None) -> int:
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if repeat and repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {REPEATS}")
        if repeat and not (start or due):
            raise ValueError("A repeating item needs a time (start or due).")
        task_id = self._run(
            "INSERT INTO tasks (title, due, start, minutes, priority, repeat, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), _when(due), _when(start), minutes, priority, repeat or None, _now()))[1]
        self.log_event("added", title.strip())
        return task_id

    def get_task(self, task_id: int):
        rows = self._rows("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else None

    def update_task(self, task_id: int, title=UNSET, due=UNSET, start=UNSET, minutes=UNSET, priority=UNSET,
                    repeat=UNSET) -> bool:
        """Change any fields of a task. Pass None for due/start/minutes/repeat to clear them."""
        task = self.get_task(task_id)
        if task is None:
            return False
        if priority is not UNSET and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if repeat not in (UNSET, None) and repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {REPEATS}")
        changes = {}
        if repeat is not UNSET:
            changes["repeat"] = repeat
        if title is not UNSET:
            changes["title"] = title.strip()
        if due is not UNSET:
            changes["due"] = _when(due)
        if start is not UNSET:
            changes["start"] = _when(start)
        if minutes is not UNSET:
            changes["minutes"] = minutes
        if priority is not UNSET:
            changes["priority"] = priority
        if not changes:
            return True
        moved = [k for k in ("due", "start") if k in changes and changes[k] != task[k]]
        if "start" in moved:
            self.resolve_blocks(task_id, "moved")                # a re-planned block isn't a missed one
        if moved:
            changes["notified"] = 0   # remind again at the new time
            # Giving a task its first time is planning; only changing an existing time is postponing.
            if any(task[k] and changes[k] and changes[k] > task[k] for k in moved):
                self.log_event("rescheduled", task["title"])
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._run(f"UPDATE tasks SET {sets} WHERE id = ?", (*changes.values(), task_id))
        return True

    def delete_task(self, task_id: int) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        self.resolve_blocks(task_id, "dropped")
        self._run("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.log_event("deleted", task["title"])
        return True

    def list_tasks(self, include_done: bool = False) -> list[sqlite3.Row]:
        where = "" if include_done else "WHERE done = 0"
        return self._rows(
            f"SELECT * FROM tasks {where} ORDER BY COALESCE(start, due) IS NULL, COALESCE(start, due), id")

    def _roll_forward(self, task) -> None:
        """Move a repeating item to its next occurrence and re-arm its reminder."""
        anchor = task["start"] or task["due"]
        nxt = datetime.fromisoformat(next_occurrence(anchor, task["repeat"]))
        shift = nxt - datetime.fromisoformat(anchor)
        moved = {k: (datetime.fromisoformat(task[k]) + shift).isoformat(timespec="minutes")
                 for k in ("start", "due") if task[k]}
        sets = ", ".join(f"{k} = ?" for k in moved)
        self._run(f"UPDATE tasks SET {sets}, notified = 0 WHERE id = ?", (*moved.values(), task["id"]))

    def complete_task(self, task_id: int) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        finished_block = self.resolve_blocks(task_id, "done")     # this occurrence was already announced
        if task["repeat"]:
            if not finished_block:             # done ahead of time: skip to the next one
                self._roll_forward(task)       # (a routine is never finished, just done for this time)
        else:
            self._run("UPDATE tasks SET done = 1, done_at = ? WHERE id = ?", (_now(), task_id))
        self.log_event("done", task["title"])
        return True

    # ---- planned blocks (v1: done / missed tracking) --------------------------
    def open_block(self, task) -> None:
        """A planned block's time has come: start tracking whether it gets done."""
        if not task["start"] or not task["minutes"]:
            return                                  # deadlines and plain reminders are not blocks (a block has a time AND a length)
        have = self._rows("SELECT id FROM blocks WHERE task_id = ? AND start = ? AND status = 'open'",
                          (task["id"], task["start"]))
        if not have:
            self._run("INSERT INTO blocks (task_id, title, start, minutes) VALUES (?, ?, ?, ?)",
                      (task["id"], task["title"], task["start"], task["minutes"]))

    def open_blocks(self) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM blocks WHERE status = 'open' ORDER BY start")

    def resolve_blocks(self, task_id: int, status: str) -> int:
        return self._run("UPDATE blocks SET status = ?, resolved = ? WHERE task_id = ? AND status = 'open'",
                         (status, _now(), task_id))[2]

    def resolve_block(self, block_id: int, status: str) -> None:
        self._run("UPDATE blocks SET status = ?, resolved = ? WHERE id = ?", (status, _now(), block_id))

    def mark_verified(self, task_id: int, proof: str) -> None:
        """The work is proven (e.g. a LeetCode solve): count the block as done even if it had been marked missed."""
        task = self.get_task(task_id)
        if task is None:
            return
        day = (task["start"] or task["due"] or _now())[:10]
        self._run("UPDATE blocks SET status = 'done', resolved = ? WHERE task_id = ? AND status IN ('open', 'missed') "
                  "AND substr(start, 1, 10) = ?", (_now(), task_id, day))
        self.complete_task(task_id)
        self.log_event("verified", f"{task['title']}: {proof}")

    def pop_due_reminders(self) -> list[sqlite3.Row]:
        """Tasks whose planned start (or, without one, deadline) has arrived and not yet announced."""
        rows = self._rows(
            "SELECT * FROM tasks WHERE done = 0 AND notified = 0 "
            "AND COALESCE(start, due) IS NOT NULL AND COALESCE(start, due) <= ?",
            (datetime.now().isoformat(timespec="seconds"),))   # seconds, so timers fire on time
        for r in rows:
            self.open_block(r)                                  # remember this occurrence before it rolls on
            if r["repeat"]:
                self._roll_forward(r)
            else:
                self._run("UPDATE tasks SET notified = 1 WHERE id = ?", (r["id"],))
        return rows

    def search_log(self, query: str, limit: int = 12) -> list[sqlite3.Row]:
        """Past conversation lines mentioning any of the query's words, newest first."""
        words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
        clause = " OR ".join("lower(text) LIKE ?" for _ in words)
        return self._rows(f"SELECT ts, role, text FROM log WHERE {clause} ORDER BY id DESC LIMIT ?",
                          (*(f"%{w}%" for w in words), limit))

    # ---- activity log -----------------------------------------------------
    def log_event(self, kind: str, detail: str) -> None:
        self._run("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (_now(), kind, detail))

    def log_message(self, role: str, text: str) -> None:
        self._run("INSERT INTO log (ts, role, text) VALUES (?, ?, ?)", (_now(), role, text))

    def unreviewed_log(self, limit_chars: int = 6000) -> tuple[str, int]:
        """Conversation not yet reviewed for patterns: (text, id of the last row included)."""
        rows = self._rows("SELECT * FROM log WHERE reviewed = 0 ORDER BY id")
        lines, used, last = [], 0, 0
        for r in rows:
            line = f"{r['ts']} {r['role']}: {' '.join(r['text'].split())[:400]}"
            if used + len(line) > limit_chars:
                break
            lines.append(line)
            used += len(line)
            last = r["id"]
        return "\n".join(lines), last

    def mark_reviewed(self, up_to_id: int) -> None:
        self._run("UPDATE log SET reviewed = 1 WHERE id <= ?", (up_to_id,))

    def get_meta(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))

    def recent_conversation(self, exchanges: int = 6, clip: int = 300) -> list[tuple[str, str]]:
        """The last few things said, so a new session can pick up where the last one ended."""
        rows = self._rows("SELECT role, text FROM log ORDER BY id DESC LIMIT ?", (exchanges * 2,))
        return [(r["role"], " ".join(r["text"].split())[:clip]) for r in reversed(rows)]

    # ---- knowledge base ---------------------------------------------------
    def add_knowledge(self, source: str, text: str = "", pages: list[tuple[str, str]] | None = None) -> int:
        """Store a note or document under `source`, replacing any earlier copy. `pages` is a list of
        (page label, text) so passages can be cited by page; otherwise `text` is stored without pages.
        Returns passages stored."""
        parts = pages if pages is not None else [("", text)]
        rows = [(source, passage, label) for label, body in parts for passage in split_passages(body)]
        with self._lock:
            self._db.execute("DELETE FROM knowledge WHERE source = ?", (source,))
            self._db.executemany("INSERT INTO knowledge (source, passage, page) VALUES (?, ?, ?)", rows)
            self._db.commit()
        return len(rows)

    def search_knowledge(self, query: str, limit: int = 5, source: str | None = None) -> list[sqlite3.Row]:
        """The best passages for a question: stemmed word matching ranked by BM25, with the source
        name counting a little, optionally limited to sources whose name contains `source`."""
        words = [w for w in "".join(c if c.isalnum() else " " for c in query.lower()).split()
                 if len(w) > 1 and w not in STOPWORDS] or                 [w for w in "".join(c if c.isalnum() else " " for c in query.lower()).split() if len(w) > 1]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        where, args = "knowledge MATCH ?", [match]
        if source:
            where += " AND lower(source) LIKE ?"
            args.append(f"%{source.lower()}%")
        return self._rows(
            f"SELECT source, passage, page FROM knowledge WHERE {where} "
            "ORDER BY bm25(knowledge, 2.0, 1.0, 0.0) LIMIT ?", (*args, limit))

    def knowledge_sources(self) -> list[sqlite3.Row]:
        return self._rows("SELECT source, COUNT(*) AS passages FROM knowledge GROUP BY source ORDER BY source")

    def remove_knowledge(self, source: str) -> bool:
        return self._run("DELETE FROM knowledge WHERE source = ?", (source,))[2] > 0

    def work_patterns(self, days: int = 30) -> str:
        """Plain-language numbers about how the user actually works, from the logs."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="minutes")
        done = self._rows("SELECT * FROM tasks WHERE done = 1 AND done_at >= ?", (since,))
        lines = [f"Last {days} days:"]
        if not done:
            lines.append("- No completed tasks recorded yet.")
        else:
            with_deadline = [t for t in done if t["due"]]
            late = [t for t in with_deadline if t["done_at"] > t["due"]]
            lines.append(f"- {len(done)} tasks completed; {len(with_deadline)} had deadlines, {len(late)} of those finished late.")
            if late:
                mins = [(datetime.fromisoformat(t["done_at"]) - datetime.fromisoformat(t["due"])).total_seconds() / 60
                        for t in late]
                lines.append(f"- Late tasks ran over by {round(sum(mins) / len(mins))} minutes on average.")
            def part(hour: int) -> str:
                return ("morning (5-12)" if 5 <= hour < 12 else "afternoon (12-17)" if hour < 17 and hour >= 12
                        else "evening (17-22)" if 17 <= hour < 22 else "night (22-5)")
            parts = Counter(part(datetime.fromisoformat(t["done_at"]).hour) for t in done)
            lines.append("- Tasks get finished mostly in the: " +
                         ", ".join(f"{p} x{n}" for p, n in parts.most_common()))
            timed = [t for t in done if t["minutes"] and t["start"]]
            if timed:
                ratio = [((datetime.fromisoformat(t["done_at"]) - datetime.fromisoformat(t["start"])).total_seconds() / 60)
                         / t["minutes"] for t in timed if t["done_at"] > t["start"]]
                if ratio:
                    lines.append(f"- Tasks take about {sum(ratio) / len(ratio):.1f}x the estimated time "
                                 f"(from {len(ratio)} timed tasks).")
            days_done = sorted({datetime.fromisoformat(t["done_at"]).date() for t in done})
            weekdays = Counter(datetime.fromisoformat(t["done_at"]).strftime("%A") for t in done)
            lines.append("- Busiest days for finishing things: " +
                         ", ".join(f"{d} x{n}" for d, n in weekdays.most_common(3)))
            streak, today = 0, datetime.now().date()
            while today - timedelta(days=streak) in days_done or (streak == 0 and today - timedelta(days=1) in days_done):
                streak += 1
                if streak > 400:
                    break
            if streak:
                lines.append(f"- Current streak: something finished on {streak} day(s) in a row.")
        moved = Counter(e["detail"] for e in self._rows(
            "SELECT detail FROM events WHERE kind = 'rescheduled' AND ts >= ?", (since,)))
        if moved:
            lines.append("- Most often postponed: " + ", ".join(f"{t} x{n}" for t, n in moved.most_common(5)))
        hours = Counter(datetime.fromisoformat(r["ts"]).hour for r in self._rows(
            "SELECT ts FROM log WHERE role = 'user' AND ts >= ?", (since,)))
        if hours:
            lines.append("- The user is usually active around: " +
                         ", ".join(f"{h:02d}:00 (x{n})" for h, n in hours.most_common(4)))
        overdue = self._rows(
            "SELECT * FROM tasks WHERE done = 0 AND due IS NOT NULL AND due < ?", (_now(),))
        lines.append(f"- Open tasks: {len(self.list_tasks())}, of which {len(overdue)} are past their deadline.")
        return "\n".join(lines)
