"""A content desk: Jay drafts videos and posts, you approve them, and it reminds you when each is due.

    draft  ->  approved  ->  due  ->  posted        (or rejected at any point before posting)

Jay writes drafts (title, hook, script, caption, hashtags, when to post and why) and puts them in
the queue. Each draft is sent to your phone with Approve and Reject buttons. Only you approve: there
is no tool for Jay to approve its own drafts, and revising an approved draft sends it back to draft.
When an approved draft's time comes, Jay messages your phone with everything ready to post (the
caption and hashtags to paste, and the video file if one is attached). Nothing is published for you.
Posting through the platforms' APIs is a separate step that would build on this same queue.

Also here: routines, recurring jobs Jay runs by itself ("every Monday 9:00, analyse my channels and
draft this week's content"). Their results come to your phone, and anything needing a yes asks there.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from .store import Store
from .tools.registry import Registry

PLATFORMS = ("youtube", "instagram", "both")
KINDS = ("long video", "short", "reel", "carousel", "photo post", "story", "community post")
STATUSES = ("draft", "approved", "due", "posted", "rejected")
EDITABLE = ("platform", "kind", "title", "hook", "script", "caption", "hashtags", "description", "visual_notes",
            "fact_notes", "scheduled_at", "media_path", "why_now")
SCHEMA = """CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    hook TEXT DEFAULT '', script TEXT DEFAULT '', caption TEXT DEFAULT '', hashtags TEXT DEFAULT '',
    description TEXT DEFAULT '', visual_notes TEXT DEFAULT '', fact_notes TEXT DEFAULT '', why_now TEXT DEFAULT '',
    scheduled_at TEXT DEFAULT '', media_path TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
    reminded INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL, thumb_path TEXT DEFAULT '',
    approved_hash TEXT DEFAULT '', posted_url TEXT DEFAULT '')"""
ROUTINE_SCHEMA = """CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, prompt TEXT NOT NULL, days TEXT NOT NULL,
    at TEXT NOT NULL, last_run TEXT, created TEXT NOT NULL)"""
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HASH_FIELDS = ("platform", "kind", "title", "hook", "script", "caption", "hashtags", "description")


def text_hash(item) -> str:
    """Fingerprint of everything the audience reads, taken when the draft is approved and checked again at posting."""
    return hashlib.sha256(json.dumps([item[f] for f in HASH_FIELDS]).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_when(text: str) -> str:
    """'2026-11-21 19:00' / '2026-11-21T19:00' -> '2026-11-21 19:00'. '' stays ''. Raises ValueError."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("T", " ")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Give the time as YYYY-MM-DD HH:MM in the user's local time.") from None


class ContentQueue:
    def __init__(self, store: Store):
        self.store = store
        store._run(SCHEMA)
        for column in ("thumb_path", "approved_hash", "posted_url"):          # databases made before these existed
            try:
                store._run(f"ALTER TABLE content ADD COLUMN {column} TEXT DEFAULT ''")
            except Exception:
                pass
        self.on_new = None            # called with the new draft's id, so it can be sent to the phone

    def add(self, platform: str, kind: str, title: str, **fields) -> int:
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of: {', '.join(PLATFORMS)}")
        if not title.strip():
            raise ValueError("A draft needs a title.")
        clean = {k: (parse_when(v) if k == "scheduled_at" else str(v)) for k, v in fields.items() if k in EDITABLE and v}
        columns = ["platform", "kind", "title", *clean, "created", "updated"]
        values = [platform, kind, title.strip(), *clean.values(), _now(), _now()]
        item_id = self.store._run(f"INSERT INTO content ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                  tuple(values))[1]
        if self.on_new:
            try:
                self.on_new(item_id)
            except Exception:
                pass
        return item_id

    def get(self, item_id: int):
        rows = self.store._rows("SELECT * FROM content WHERE id = ?", (item_id,))
        return rows[0] if rows else None

    def list(self, status: str | None = None, limit: int = 30):
        if status:
            return self.store._rows("SELECT * FROM content WHERE status = ? ORDER BY COALESCE(NULLIF(scheduled_at, ''), '9') , id LIMIT ?",
                                    (status, limit))
        return self.store._rows("SELECT * FROM content WHERE status NOT IN ('rejected', 'posted') "
                                "ORDER BY COALESCE(NULLIF(scheduled_at, ''), '9'), id LIMIT ?", (limit,))

    def revise(self, item_id: int, **fields) -> str:
        item = self.get(item_id)
        if item is None:
            return f"No draft #{item_id}."
        if item["status"] in ("posted", "rejected"):
            return f"#{item_id} is {item['status']}; make a new draft instead."
        clean = {k: (parse_when(v) if k == "scheduled_at" else str(v)) for k, v in fields.items() if k in EDITABLE and v is not None}
        if not clean:
            return "Nothing to change."
        only_timing = set(clean) <= {"scheduled_at", "media_path"}      # when and which file, not what it says
        status = item["status"] if only_timing else "draft"          # changing the content needs a fresh yes
        sets = ", ".join(f"{k} = ?" for k in clean)
        self.store._run(f"UPDATE content SET {sets}, status = ?, reminded = 0, updated = ? WHERE id = ?",
                        (*clean.values(), status, _now(), item_id))
        if status == "draft":
            self.store._run("UPDATE content SET approved_hash = '' WHERE id = ?", (item_id,))
        note = "" if status == item["status"] else " It is back to draft and needs your approval again."
        if status == "draft" and self.on_new and item["status"] != "draft":
            self.on_new(item_id)
        return f"Updated #{item_id}.{note}"

    def set_status(self, item_id: int, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError("Unknown status.")
        return self.store._run("UPDATE content SET status = ?, updated = ? WHERE id = ?", (status, _now(), item_id))[2] > 0

    def approve(self, item_id: int) -> str:
        item = self.get(item_id)
        if item is None:
            return f"No draft #{item_id}."
        if item["status"] != "draft":
            return f"#{item_id} is already {item['status']}."
        self.set_status(item_id, "approved")
        self.store._run("UPDATE content SET approved_hash = ? WHERE id = ?", (text_hash(item), item_id))
        when = f" It will be due on {item['scheduled_at']}." if item["scheduled_at"] else " It has no time yet."
        return f"Approved #{item_id}: {item['title']}.{when}"

    def reject(self, item_id: int) -> str:
        item = self.get(item_id)
        if item is None:
            return f"No draft #{item_id}."
        self.set_status(item_id, "rejected")
        return f"Rejected #{item_id}: {item['title']}."

    def due(self, now: datetime | None = None):
        stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        return self.store._rows("SELECT * FROM content WHERE status = 'approved' AND scheduled_at != '' AND scheduled_at <= ? "
                                "AND reminded = 0 ORDER BY scheduled_at", (stamp,))

    def attach_media(self, item_id: int, video: str, thumbnail: str = "") -> None:
        """A finished video belongs to the draft it was made from; the draft's approval is unchanged."""
        self.store._run("UPDATE content SET media_path = ?, thumb_path = ?, updated = ? WHERE id = ?",
                        (video, thumbnail, _now(), item_id))

    def set_posted(self, item_id: int, url: str) -> None:
        self.store._run("UPDATE content SET status = 'posted', posted_url = ?, updated = ? WHERE id = ?", (url, _now(), item_id))

    def mark_due(self, item_id: int) -> None:
        self.store._run("UPDATE content SET status = 'due', reminded = 1, updated = ? WHERE id = ?", (_now(), item_id))

    def command(self, text: str) -> str | None:
        """/queue, /approve N, /reject N, /posted N, /draft N. Only the user types these."""
        parts = text.strip().split()
        if not parts:
            return None
        cmd, arg = parts[0].lower().lstrip("/"), (parts[1].lstrip("#") if len(parts) > 1 else "")
        if cmd == "queue":
            rows = self.list()
            return "\n".join(summary(r) for r in rows) or "The content queue is empty."
        if cmd not in ("approve", "reject", "posted", "draft"):
            return None
        if not arg.isdigit():
            return f"Say which one, e.g. /{cmd} 3."
        item_id = int(arg)
        if cmd == "approve":
            return self.approve(item_id)
        if cmd == "reject":
            return self.reject(item_id)
        if cmd == "posted":
            return f"Marked #{item_id} as posted." if self.set_status(item_id, "posted") else f"No draft #{item_id}."
        item = self.get(item_id)
        return full(item) if item else f"No draft #{item_id}."


def summary(r) -> str:
    when = r["scheduled_at"] or "no time yet"
    return f"#{r['id']} [{r['status']}] {r['platform']} {r['kind']}: {r['title']} ({when})"


def full(r) -> str:
    parts = [summary(r)]
    for label, key in (("Hook", "hook"), ("Script", "script"), ("Visuals", "visual_notes"), ("Caption", "caption"),
                       ("Hashtags", "hashtags"), ("Description", "description"), ("Why now", "why_now"),
                       ("Fact check", "fact_notes"), ("Video file", "media_path"), ("Thumbnail", "thumb_path"), ("Posted at", "posted_url")):
        if r[key]:
            parts.append(f"{label}: {r[key]}")
    return "\n".join(parts)


def phone_card(r) -> str:
    """A draft as a phone message: short enough to read, with the parts you decide on."""
    lines = [f"Draft #{r['id']} · {r['platform']} {r['kind']}", r["title"]]
    if r["hook"]:
        lines.append(f"Hook: {r['hook']}")
    if r["caption"]:
        lines.append(f"Caption: {r['caption'][:400]}")
    if r["hashtags"]:
        lines.append(r["hashtags"])
    lines.append(f"Post: {r['scheduled_at'] or 'no time chosen'}" + (f" ({r['why_now']})" if r["why_now"] else ""))
    lines.append(f"Full text: /draft {r['id']}")
    return "\n".join(lines)


def due_message(r) -> str:
    lines = [f"Time to post: {r['platform']} {r['kind']} #{r['id']}", r["title"]]
    if r["caption"]:
        lines += ["", "Caption:", r["caption"]]
    if r["hashtags"]:
        lines += ["", r["hashtags"]]
    if r["description"]:
        lines += ["", "Description:", r["description"]]
    if r["media_path"]:
        lines += ["", f"Video file: {r['media_path']}"]
    if r["thumb_path"]:
        lines += [f"Thumbnail: {r['thumb_path']}"]
    lines += ["", f"When it's up, send /posted {r['id']}."]
    return "\n".join(lines)


# ------------------------------------------------------------------ routines
class Routines:
    def __init__(self, store: Store):
        self.store = store
        store._run(ROUTINE_SCHEMA)

    @staticmethod
    def parse_days(days: str) -> str:
        days = days.lower().replace(",", " ").split()
        if days in (["daily"], ["every day"], ["everyday"]) or not days:
            return ",".join(DAYS)
        if days == ["weekdays"]:
            return "mon,tue,wed,thu,fri"
        picked = [d[:3] for d in days]
        if any(d not in DAYS for d in picked):
            raise ValueError("days must be like 'mon thu', 'weekdays' or 'daily'.")
        return ",".join(d for d in DAYS if d in picked)

    def add(self, name: str, prompt: str, days: str, at: str) -> int:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("at must be a 24-hour time like 09:00.")
        if len(prompt.strip()) < 10:
            raise ValueError("Describe what to do.")
        return self.store._run("INSERT INTO routines (name, prompt, days, at, created) VALUES (?, ?, ?, ?, ?)",
                               (name.strip(), prompt.strip(), self.parse_days(days), at, _now()))[1]

    def all(self):
        return self.store._rows("SELECT * FROM routines ORDER BY id")

    def get(self, routine_id: int):
        rows = self.store._rows("SELECT * FROM routines WHERE id = ?", (routine_id,))
        return rows[0] if rows else None

    def remove(self, routine_id: int) -> bool:
        return self.store._run("DELETE FROM routines WHERE id = ?", (routine_id,))[2] > 0

    def due(self, now: datetime | None = None):
        now = now or datetime.now()
        today, hhmm, day = now.date().isoformat(), now.strftime("%H:%M"), DAYS[now.weekday()]
        return [r for r in self.all() if day in r["days"].split(",") and r["at"] <= hhmm
                and (r["last_run"] or "")[:10] != today]

    def mark_run(self, routine_id: int, now: datetime | None = None) -> None:
        self.store._run("UPDATE routines SET last_run = ? WHERE id = ?", ((now or datetime.now()).isoformat(timespec="minutes"), routine_id))


# ------------------------------------------------------------------ tools
def register(reg: Registry, queue: ContentQueue, routines: Routines) -> None:
    @reg.tool(
        "content_create",
        "Save a draft video or post in the content queue and send it to the user's phone for approval. Fill in "
        "everything you can: title, hook (the first line or seconds), script or outline, visual notes, caption, "
        "hashtags, description, when to post and why (from their analytics), and fact_notes with the sources "
        "behind any factual claim. You cannot approve drafts; only the user can.",
        {"platform": {"type": "string", "enum": list(PLATFORMS)},
         "kind": {"type": "string", "description": "One of: " + ", ".join(KINDS)},
         "title": {"type": "string"}, "hook": {"type": "string"}, "script": {"type": "string"},
         "visual_notes": {"type": "string"}, "caption": {"type": "string"}, "hashtags": {"type": "string"},
         "description": {"type": "string"}, "why_now": {"type": "string", "description": "Why this topic and this time."},
         "fact_notes": {"type": "string"},
         "scheduled_at": {"type": "string", "description": "Local time YYYY-MM-DD HH:MM."}},
        required=("platform", "kind", "title"),
    )
    def content_create(platform: str, kind: str, title: str, **fields) -> str:
        item_id = queue.add(platform, kind, title, **fields)
        return f"Saved draft #{item_id} and sent it to the phone for approval. It is not approved until the user says so."

    @reg.tool("content_list", "The content queue: drafts, approved and due items (or one status).",
              {"status": {"type": "string", "enum": list(STATUSES)}})
    def content_list(status: str = "") -> str:
        return "\n".join(summary(r) for r in queue.list(status or None)) or "The content queue is empty."

    @reg.tool("content_get", "The full text of one draft.", {"id": {"type": "integer"}}, required=("id",))
    def content_get(id: int) -> str:
        item = queue.get(id)
        return full(item) if item else f"No draft #{id}."

    @reg.tool(
        "content_revise",
        "Change a draft, or move its time. Changing the content of an approved draft sends it back for approval; "
        "changing only scheduled_at keeps the approval.",
        {"id": {"type": "integer"},
         **{k: {"type": "string"} for k in EDITABLE}},
        required=("id",),
    )
    def content_revise(id: int, **fields) -> str:
        return queue.revise(id, **fields)

    @reg.tool(
        "add_routine",
        "Set up a job you will do by yourself on a schedule, e.g. 'every Monday 09:00: analyse my YouTube and "
        "Instagram and draft this week's content'. The result goes to the user's phone, and anything that needs "
        "a yes is asked there. The user is asked to approve setting it up.",
        {"name": {"type": "string"},
         "prompt": {"type": "string", "description": "Exactly what to do each time, written as an instruction to yourself."},
         "days": {"type": "string", "description": "'mon thu', 'weekdays' or 'daily'."},
         "at": {"type": "string", "description": "24-hour local time, e.g. 09:00."}},
        required=("name", "prompt", "days", "at"),
        confirm=lambda a: f"Set up a routine '{a['name']}' to run {a['days']} at {a['at']}: {a['prompt'][:200]}",
    )
    def add_routine(name: str, prompt: str, days: str, at: str) -> str:
        routine_id = routines.add(name, prompt, days, at)
        return f"Routine #{routine_id} '{name}' will run {routines.get(routine_id)['days']} at {at}."

    @reg.tool("list_routines", "The routines you run by yourself.")
    def list_routines() -> str:
        return "\n".join(f"#{r['id']} {r['name']}: {r['days']} at {r['at']} (last run {r['last_run'] or 'never'})"
                         for r in routines.all()) or "No routines."

    @reg.tool("remove_routine", "Stop a routine, by its number.", {"id": {"type": "integer"}}, required=("id",))
    def remove_routine(id: int) -> str:
        return "Removed." if routines.remove(id) else f"No routine #{id}."
