"""Did the work really happen? Evidence from the places the work leaves a trace.

Jay used to ask "did you finish?" and trust the answer, or assume a block was missed if nobody ticked it.
For the blocks where there is public evidence it now checks:

  * LeetCode: your public profile's recent accepted submissions (no password, nothing private).
  * GitHub: your public push events, for coding blocks (Jay project, capstone, TrustCore ...).

A block with evidence is marked done (and says why). Blocks with no way to check (class, Trust work, ...)
are simply asked about once, in the evening summary, instead of all day.
"""
from __future__ import annotations

import time
from datetime import date, datetime

import requests

from .store import Store

LEETCODE_WORDS = ("leetcode", "neetcode", "dsa problem", "easy problem")
GITHUB_WORDS = ("jay project", "ai project", "capstone", "trustcore", "bug fix", "github", "commit", "coding")
_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 240


def _cached(key: str, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fetch()
    _cache[key] = (time.time(), value)
    return value


def leetcode_solves(user: str) -> list[tuple[str, datetime]]:
    """Recent accepted submissions (title, local time), newest first. Raises on network trouble."""
    query = {"query": "query($u:String!){recentAcSubmissionList(username:$u,limit:20){title timestamp}}",
             "variables": {"u": user}}

    def fetch():
        resp = requests.post("https://leetcode.com/graphql", json=query, timeout=20,
                             headers={"Content-Type": "application/json", "Referer": "https://leetcode.com",
                                      "User-Agent": "Mozilla/5.0 (personal assistant)"})
        resp.raise_for_status()
        items = (resp.json().get("data") or {}).get("recentAcSubmissionList") or []
        return [(i["title"], datetime.fromtimestamp(int(i["timestamp"]))) for i in items]

    return _cached("lc:" + user, fetch)


def github_pushes(user: str) -> list[tuple[str, datetime, int]]:
    """Recent public pushes (repo, local time, commits), newest first."""
    def fetch():
        resp = requests.get(f"https://api.github.com/users/{user}/events/public", timeout=20,
                            headers={"User-Agent": "jay-personal-assistant"})
        resp.raise_for_status()
        out = []
        for event in resp.json():
            if event.get("type") != "PushEvent":
                continue
            when = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            payload = event.get("payload") or {}
            out.append((event["repo"]["name"], when, int(payload.get("distinct_size") or payload.get("size") or 1)))
        return out

    return _cached("gh:" + user, fetch)


def kind_of(title: str) -> str | None:
    low = title.lower()
    if any(w in low for w in LEETCODE_WORDS):
        return "leetcode"
    if any(w in low for w in GITHUB_WORDS):
        return "github"
    return None


def evidence(title: str, since: datetime, leetcode_user: str = "", github_user: str = "") -> str | None:
    """A sentence of proof that this block's work happened since `since`, or None."""
    kind = kind_of(title)
    if kind == "leetcode" and leetcode_user:
        done = [(t, when) for t, when in leetcode_solves(leetcode_user) if when >= since]
        if done:
            names = ", ".join(t for t, _ in done[:3])
            return f"solved {names} on LeetCode at {done[0][1]:%I:%M %p}".replace(" 0", " ")
    if kind == "github" and github_user:
        pushes = [(repo, when, n) for repo, when, n in github_pushes(github_user) if when >= since]
        if pushes:
            repo = pushes[0][0].split("/")[-1]
            return f"pushed {sum(n for _, _, n in pushes)} commit(s) to {repo} at {pushes[0][1]:%I:%M %p}".replace(" 0", " ")
    return None


def verify_today(store: Store, leetcode_user: str = "", github_user: str = "", today: date | None = None) -> list[str]:
    """Mark today's blocks done where there is proof. Returns a line for each one, e.g.
    "LeetCode: one easy problem: solved Two Sum on LeetCode at 8:41 PM"."""
    if not (leetcode_user or github_user):
        return []
    today = today or date.today()
    start_of_day = datetime.combine(today, datetime.min.time())
    lines = []
    for task in store.list_tasks():
        if not task["start"] or task["done"]:
            continue
        begin = datetime.fromisoformat(task["start"])
        if begin.date() > today or begin.date() < today:
            continue
        try:
            proof = evidence(task["title"], start_of_day, leetcode_user, github_user)
        except Exception:
            return lines                      # no network right now: try again at the next check
        if proof:
            store.mark_verified(task["id"], proof)
            lines.append(f"{task['title']}: {proof}")
    return lines


def progress_report(store: Store, leetcode_user: str = "", github_user: str = "") -> str:
    """For the 'did I do my LeetCode today?' question: checks now and says what it found."""
    found = verify_today(store, leetcode_user, github_user)
    if found:
        return "Verified and marked done: " + "; ".join(found)
    today = date.today().isoformat()
    open_checkable = [t["title"] for t in store.list_tasks()
                      if t["start"] and t["start"].startswith(today) and kind_of(t["title"])]
    if not (leetcode_user or github_user):
        return "No LeetCode or GitHub username is set (JARVIS_LEETCODE_USER, JARVIS_GITHUB_USER in .env)."
    if open_checkable:
        return "No evidence yet today for: " + ", ".join(open_checkable) + " (nothing new on your public LeetCode or GitHub)."
    return "Nothing checkable is planned for today."
