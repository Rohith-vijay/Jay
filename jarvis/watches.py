"""Things Jay keeps an eye on by itself, and tells you only when something is new.

A watch is a check that repeats every so often: news on a topic, job postings for a search,
a web page (college notices, a dashboard), or email matching a Gmail search. Each run is
compared with what was seen before; only new items are reported (a Windows notification,
a line in Jay's window, and a short spoken note if Jay is awake).
"""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from .store import Store
from .tools.registry import Registry

KINDS = {
    "news": "headlines about a topic (target = the topic)",
    "jobs": "job or internship postings (target = what to search, e.g. 'backend intern Java Hyderabad')",
    "site": "a web page that changes, like college notices (target = its address)",
    "email": "new email matching a Gmail search, e.g. 'from:kluniversity.in' (needs Google set up)",
}
SCHEMA = """CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
    every_minutes INTEGER NOT NULL, last_run TEXT, seen TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL)"""
SEEN_LIMIT = 300


class Watches:
    def __init__(self, store: Store, registry: Registry):
        self.store, self.registry = store, registry
        self.failing: set[int] = set()       # watches whose last check failed (warn once, not every time)
        store._run(SCHEMA)

    def add(self, name: str, kind: str, target: str, every_minutes: int) -> int:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
        if kind == "site" and not target.lower().startswith(("http://", "https://")):
            raise ValueError("A site watch needs a full http(s) address.")
        every = max(30, min(int(every_minutes), 7 * 24 * 60))
        browser = getattr(self.registry, "browser", None)
        if kind == "site" and browser is not None:
            browser.remember_host(target)             # the user approved watching this page
        return self.store._run(
            "INSERT INTO watches (name, kind, target, every_minutes, created) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), kind, target.strip(), every, datetime.now().isoformat(timespec="minutes")))[1]

    def all(self):
        return self.store._rows("SELECT * FROM watches ORDER BY id")

    def remove(self, watch_id: int) -> bool:
        return self.store._run("DELETE FROM watches WHERE id = ?", (watch_id,))[2] > 0

    def due(self):
        now = datetime.now()
        return [w for w in self.all()
                if not w["last_run"] or datetime.fromisoformat(w["last_run"]) + timedelta(minutes=w["every_minutes"]) <= now]

    def _items(self, watch) -> list[str]:
        """Run one check and return its items as short lines (whatever identifies each one)."""
        run = lambda name, args: self.registry.execute(name, args, lambda summary: False)  # noqa: E731
        kind, target = watch["kind"], watch["target"]
        if kind == "news":
            res = run("get_news", {"topic": target})
        elif kind == "jobs":
            res = run("web_search", {"query": f"{target} job opening apply"})
        elif kind == "email":
            res = run("gmail_list", {"query": f"({target}) newer_than:7d", "max_results": 15})
        else:
            res = run("check_site", {"name": f"watch {watch['id']}", "url": target})
            if res.is_error:
                raise RuntimeError(res.text)
            if res.text.startswith("No changes"):
                return []
            return [line for line in res.text.splitlines()[1:] if line.strip()][:20]
        if res.is_error:
            raise RuntimeError(res.text)
        if kind == "jobs":                     # results are blocks: title, link, snippet
            blocks = [b.strip().splitlines() for b in res.text.split("\n\n") if b.strip()]
            return [" | ".join(b[:2]) for b in blocks if len(b) >= 2]
        return [line.strip() for line in res.text.splitlines() if line.strip() and not line.startswith("No ")]

    def failed(self, watch) -> bool:
        """A check failed: try again in about half an hour, not every minute. True the first time only."""
        retry_in = min(watch["every_minutes"], 30)
        pretend = datetime.now() - timedelta(minutes=watch["every_minutes"] - retry_in)
        self.store._run("UPDATE watches SET last_run = ? WHERE id = ?",
                        (pretend.isoformat(timespec="minutes"), watch["id"]))
        first = watch["id"] not in self.failing
        self.failing.add(watch["id"])
        return first

    def run(self, watch) -> list[str]:
        """Check one watch now. Returns only the items not seen before (none on the very first run
        for news/jobs/email, which just records what's already there)."""
        items = self._items(watch)
        self.failing.discard(watch["id"])
        seen = json.loads(watch["seen"])
        first = watch["last_run"] is None and watch["kind"] != "site"
        new = [i for i in items if i not in seen]
        seen = (seen + new)[-SEEN_LIMIT:]
        self.store._run("UPDATE watches SET last_run = ?, seen = ? WHERE id = ?",
                        (datetime.now().isoformat(timespec="minutes"), json.dumps(seen), watch["id"]))
        return [] if first else new


def register(reg: Registry, watches: Watches) -> None:
    browser = getattr(reg, "browser", None)
    if browser is not None:                          # pages already being watched were the user's choice
        for row in watches.all():
            if row["kind"] == "site":
                browser.remember_host(row["target"])

    def watch_confirm(args: dict):
        if args.get("kind") != "site":
            return None
        browser = getattr(reg, "browser", None)
        if browser is not None and browser.site_confirm({"url": args.get("target")}) is None:
            return None                                  # an address the user already trusts
        return f"Watch the page {args.get('target')} on its own, from now on"

    @reg.tool(
        "add_watch",
        "Keep an eye on something by yourself and tell the user only when there's something new. "
        "Kinds: " + "; ".join(f"{k} = {v}" for k, v in KINDS.items()) + ". Use for 'tell me when…', "
        "'keep checking…', 'watch for…', and for job hunting.",
        {"name": {"type": "string", "description": "Short name, e.g. 'ERP notices'."},
         "kind": {"type": "string", "enum": list(KINDS)},
         "target": {"type": "string"},
         "every_minutes": {"type": "integer", "description": "How often to check; at least 30. Default 180."}},
        required=("name", "kind", "target"),
        # A page watch will visit that address by itself from now on, so the user says yes once.
        confirm=lambda a: watch_confirm(a),
    )
    def add_watch(name: str, kind: str, target: str, every_minutes: int = 180) -> str:
        watch_id = watches.add(name, kind, target, every_minutes)
        return f"Watching '{name}' (#{watch_id}); I'll check every {max(30, every_minutes)} minutes and tell you what's new."

    @reg.tool("list_watches", "What you're keeping an eye on for the user.")
    def list_watches() -> str:
        rows = watches.all()
        return "\n".join(f"#{w['id']} {w['name']} ({w['kind']}: {w['target']}) every {w['every_minutes']} min, "
                         f"last checked {w['last_run'] or 'not yet'}" for w in rows) or "Nothing is being watched."

    @reg.tool("remove_watch", "Stop watching something, by its number.",
              {"watch_id": {"type": "integer"}}, required=("watch_id",))
    def remove_watch(watch_id: int) -> str:
        return "Stopped watching." if watches.remove(watch_id) else f"No watch #{watch_id}."


# ---------------------------------------------------------------- backups
BACKUP_FILES = ("jarvis.db", "profile.json", "voiceprints.json")
KEEP_BACKUPS = 7


def backup(data_dir: Path) -> Path | None:
    """Once a day, copy Jay's memory, profile and voiceprints to data/backups/<date>; keep a week."""
    import sqlite3

    target = data_dir / "backups" / date.today().isoformat()
    if target.exists():
        return None
    target.mkdir(parents=True)
    for name in BACKUP_FILES:
        src = data_dir / name
        if not src.exists():
            continue
        if name.endswith(".db"):                      # a consistent copy even while Jay is using it
            live, copy = sqlite3.connect(src), sqlite3.connect(target / name)
            try:
                live.backup(copy)
            finally:
                copy.close()                          # (a `with` block would commit but leave the file open)
                live.close()
        else:
            shutil.copy2(src, target / name)
    days = sorted(p for p in (data_dir / "backups").iterdir() if p.is_dir())
    for old in days[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return target


# ---------------------------------------------------------------- coding help
def register_git(reg: Registry) -> None:
    import subprocess

    def git(path: str, *args: str) -> str:
        repo = Path(path).expanduser()
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30,
                              errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[0] if (proc.stderr or proc.stdout).strip()
                               else f"git failed in {repo}")
        return proc.stdout.strip() or "(nothing)"

    @reg.tool(
        "git_status",
        "For one of the user's code projects: current branch, changed files, and the last few commits. "
        "Read-only. Ask for the folder if you don't know it, and remember it.",
        {"path": {"type": "string", "description": "The project folder."},
         "commits": {"type": "integer", "description": "How many recent commits to show (default 5)."}},
        required=("path",),
    )
    def git_status(path: str, commits: int = 5) -> str:
        return (f"Branch and changes:\n{git(path, 'status', '--short', '--branch')}\n\n"
                f"Recent commits:\n{git(path, 'log', f'-{max(1, min(commits, 30))}', '--date=relative', '--pretty=format:%h %ad %s')}")

    @reg.tool(
        "git_diff",
        "Show the uncommitted changes in one of the user's code projects (read-only), to review them "
        "or write a commit message.",
        {"path": {"type": "string"}},
        required=("path",),
    )
    def git_diff(path: str) -> str:
        return git(path, "diff", "--stat") + "\n\n" + git(path, "diff")[:15_000]
