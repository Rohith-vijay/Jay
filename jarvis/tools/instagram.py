"""Instagram account analytics (read-only): what your posts and reels do, and what to post next.

Uses Instagram's official API ("Instagram API with Instagram Login"). It only reads; it cannot
post, comment or message. Posting would need a different permission and is not part of this.

Setup, once (a Professional account is required, and a free Meta developer app):
  1. Instagram app: Settings -> Account type and tools -> switch to a Professional account
     (Creator or Business). Personal accounts have no insights.
  2. developers.facebook.com -> My Apps -> Create app (type: Business) -> add the product
     "Instagram" -> "API setup with Instagram login".
  3. In the app: App roles -> Roles -> add your Instagram account as an Instagram Tester, then accept
     the invite in Instagram (Settings -> Apps and websites -> Tester invites).
  4. In "API setup with Instagram login", add your account and press "Generate token". Allow
     instagram_business_basic and instagram_business_manage_insights. Put the token in .env as
     INSTAGRAM_ACCESS_TOKEN=... and restart Jay. Tokens last 60 days; Jay renews it by itself.
  5. Only for posting reels: generate a second token that also allows instagram_business_content_publish and
     put it in .env as INSTAGRAM_PUBLISH_TOKEN=... (the analytics token stays read-only).

As with YouTube, the numbers are worked out in code (posting day and hour, content type, caption
length, hashtag count, best and worst posts) and every figure says how many posts it rests on.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import date, datetime
from functools import partial
from pathlib import Path

import requests

from .registry import Registry

BASE = os.getenv("INSTAGRAM_GRAPH_URL", "https://graph.instagram.com")
CORE = ["reach", "likes", "comments", "shares", "saved", "total_interactions"]
MIN_AGE_DAYS = 3
DAYPARTS = [(5, 12, "morning (5-12)"), (12, 17, "afternoon (12-17)"), (17, 22, "evening (17-22)"),
            (22, 29, "night (22-5)")]
CAPTION_BUCKETS = [(0, 50, "caption under 50 characters"), (50, 150, "50-150"), (150, 400, "150-400"),
                   (400, 10**6, "over 400")]
TAG_BUCKETS = [(0, 1, "no hashtags"), (1, 6, "1-5 hashtags"), (6, 16, "6-15"), (16, 1000, "16 or more")]
REFRESH_AFTER_DAYS = 40


class InstagramError(RuntimeError):
    pass


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def hashtags(caption: str) -> list[str]:
    return [t.lower() for t in re.findall(r"#(\w+)", caption or "")]


# ------------------------------------------------------------------ analysis (pure)
def post_type(item: dict) -> str:
    if item.get("media_product_type") == "REELS":
        return "Reel"
    return {"CAROUSEL_ALBUM": "Carousel", "VIDEO": "Video"}.get(item.get("media_type"), "Photo")


def analyse(posts: list[dict], today: date | None = None) -> dict:
    """posts: {caption, type, published (aware datetime), reach, likes, comments, shares, saves,
    interactions, watch_s}. Posts without reach data are left out of the comparison."""
    today = today or date.today()
    usable = []
    for p in posts:
        local = p["published"].astimezone()
        if (today - local.date()).days < MIN_AGE_DAYS or not p.get("reach"):
            continue
        usable.append({**p, "local": local, "tags": hashtags(p.get("caption", "")),
                       "engagement": 100 * p.get("interactions", 0) / p["reach"]})
    out: dict = {"considered": len(usable), "skipped": len(posts) - len(usable)}
    if not usable:
        return out
    median = statistics.median(p["reach"] for p in usable)
    out["median_reach"] = round(median)

    def group(key, order):
        buckets: dict[str, list] = {}
        for p in usable:
            label = key(p)
            if label:
                buckets.setdefault(label, []).append(p)
        rows = []
        for label in order:
            items = buckets.get(label)
            if not items:
                continue
            reach = sum(i["reach"] for i in items)
            watch = [i["watch_s"] for i in items if i.get("watch_s")]
            rows.append({"group": label, "posts": len(items), "avg_reach": round(_mean(i["reach"] for i in items)),
                         "engagement_pct": round(_mean(i["engagement"] for i in items), 1),
                         "saves_per_1k_reach": round(1000 * sum(i.get("saves", 0) for i in items) / reach, 1),
                         "shares_per_1k_reach": round(1000 * sum(i.get("shares", 0) for i in items) / reach, 1),
                         "avg_watch_s": round(_mean(watch), 1) if watch else None})
        return rows

    def bucket(value, buckets):
        return next((name for lo, hi, name in buckets if lo <= value < hi), None)

    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    out["by_type"] = group(lambda p: p["type"], sorted({p["type"] for p in usable}))
    out["by_weekday"] = group(lambda p: weekdays[p["local"].weekday()], weekdays)
    out["by_daypart"] = group(lambda p: bucket(p["local"].hour if p["local"].hour >= 5 else p["local"].hour + 24, DAYPARTS),
                              [n for _, _, n in DAYPARTS])
    out["by_caption_length"] = group(lambda p: bucket(len(p.get("caption") or ""), CAPTION_BUCKETS),
                                     [n for _, _, n in CAPTION_BUCKETS])
    out["by_hashtag_count"] = group(lambda p: bucket(len(p["tags"]), TAG_BUCKETS), [n for _, _, n in TAG_BUCKETS])
    tag_posts: dict[str, list] = {}
    for p in usable:
        for tag in set(p["tags"]):
            tag_posts.setdefault(tag, []).append(p["reach"])
    overall = _mean(p["reach"] for p in usable)
    out["hashtags"] = sorted(
        ({"tag": "#" + t, "posts": len(v), "avg_reach": round(_mean(v)), "vs_average": round(_mean(v) / overall, 2)}
         for t, v in tag_posts.items() if len(v) >= 3), key=lambda r: r["avg_reach"], reverse=True)[:8]
    ranked = sorted(usable, key=lambda p: p["reach"], reverse=True)

    def brief(p):
        return {"caption": (p.get("caption") or "").strip().split("\n")[0][:70], "type": p["type"],
                "published": p["local"].strftime("%a %d %b %H:%M"), "reach": p["reach"],
                "times_the_median": round(p["reach"] / median, 1) if median else None,
                "engagement_pct": round(p["engagement"], 1), "saves": p.get("saves", 0), "shares": p.get("shares", 0)}

    out["best"] = [brief(p) for p in ranked[:3]]
    out["worst"] = [brief(p) for p in ranked[-3:]] if len(ranked) > 3 else []
    ordered = sorted(usable, key=lambda p: p["local"])
    gaps = [(b["local"] - a["local"]).total_seconds() / 86400 for a, b in zip(ordered, ordered[1:])]
    out["typical_days_between_posts"] = round(statistics.median(gaps), 1) if gaps else None
    return out


def format_analysis(result: dict) -> str:
    if not result.get("considered"):
        return ("Not enough posts with reach data to analyse yet (posts need to be at least "
                f"{MIN_AGE_DAYS} days old, and insights only exist for posts on a Professional account).")
    lines = [f"Based on {result['considered']} posts (left out {result['skipped']}: too new or no reach data). "
             f"Median reach {result['median_reach']:,}. Typical gap between posts: {result['typical_days_between_posts']} days."]
    sections = (("CONTENT TYPE", "by_type"), ("POSTING DAY", "by_weekday"), ("TIME OF DAY (your local time)", "by_daypart"),
                ("CAPTION LENGTH", "by_caption_length"), ("HASHTAG COUNT", "by_hashtag_count"))
    for title, key in sections:
        rows = result.get(key) or []
        if rows:
            lines.append(f"\n{title}  (group | posts | avg reach | engagement % | saves/1k reach | shares/1k reach | avg watch s)")
            for r in rows:
                flag = "  <- only 1-2 posts, weak evidence" if r["posts"] < 3 else ""
                watch = f"{r['avg_watch_s']}s" if r["avg_watch_s"] is not None else "-"
                lines.append(f"  {r['group']} | {r['posts']} | {r['avg_reach']:,} | {r['engagement_pct']}% | "
                             f"{r['saves_per_1k_reach']} | {r['shares_per_1k_reach']} | {watch}{flag}")
    if result.get("hashtags"):
        lines.append("\nHASHTAGS used on 3+ posts (tag | posts | avg reach | vs your average)")
        lines += [f"  {h['tag']} | {h['posts']} | {h['avg_reach']:,} | {h['vs_average']}x" for h in result["hashtags"]]
    for label, key in (("BEST", "best"), ("WORST", "worst")):
        if result.get(key):
            lines.append(f"\n{label} POSTS")
            lines += [f"  [{p['type']}] {p['caption']} ({p['published']}): reach {p['reach']:,} = {p['times_the_median']}x median, "
                      f"{p['engagement_pct']}% engaged, {p['saves']} saves, {p['shares']} shares" for p in result[key]]
    return "\n".join(lines)


def busiest_hours(values: dict) -> str:
    """Instagram's 'online_followers': {hour: followers online} -> the top hours."""
    hours = sorted(((int(v), int(h)) for h, v in values.items()), reverse=True)[:3]
    return ", ".join(f"{h:02d}:00 ({v:,} followers online)" for v, h in sorted(hours, key=lambda x: -x[0]))


# ------------------------------------------------------------------ talking to Instagram
class Instagram:
    def __init__(self, data_dir: Path, token: str | None = None, http=None):
        self.token_file = data_dir / "instagram_token.json"
        self._token = token
        self.http = http or requests
        self._user_id: str | None = None

    def token(self) -> str:
        if self._token:
            return self._token
        saved = {}
        if self.token_file.exists():
            try:
                saved = json.loads(self.token_file.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}
        env = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
        if saved.get("source") == env and saved.get("token"):          # a renewed copy of the .env token
            token = saved["token"]
            if time.time() - saved.get("renewed", 0) > REFRESH_AFTER_DAYS * 86400:
                token = self._renew(token, env)
        elif env:
            token = env
            self.token_file.write_text(json.dumps({"source": env, "token": env, "renewed": time.time()}), encoding="utf-8")
        else:
            raise InstagramError(SETUP)
        self._token = token
        return token

    def _renew(self, token: str, source: str) -> str:
        try:
            resp = self.http.get(f"{BASE}/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=20)
            new = resp.json().get("access_token")
        except Exception:
            return token
        if new:
            self.token_file.write_text(json.dumps({"source": source, "token": new, "renewed": time.time()}), encoding="utf-8")
            return new
        return token

    def get(self, path: str, **params) -> dict:
        resp = self.http.get(f"{BASE}/{path.lstrip('/')}", params={**params, "access_token": self.token()}, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            raise InstagramError(f"Instagram answered with something unreadable (HTTP {resp.status_code}).")
        if "error" in data:
            err = data["error"]
            if err.get("code") == 190:
                raise InstagramError("The Instagram token has expired or was revoked. Generate a new one (see instagram_setup) "
                                     "and put it in .env as INSTAGRAM_ACCESS_TOKEN.")
            raise InstagramError(f"Instagram said: {err.get('message', err)}")
        return data

    # -- data
    def profile(self) -> dict:
        data = self.get("me", fields="user_id,username,name,followers_count,follows_count,media_count,biography")
        self._user_id = data.get("user_id") or data.get("id")
        return data

    def insights(self, media_id: str, reel: bool) -> dict:
        wanted = [*CORE, "views"] + (["ig_reels_avg_watch_time"] if reel else [])
        for metrics in (wanted, CORE, ["reach"]):
            try:
                data = self.get(f"{media_id}/insights", metric=",".join(metrics))
            except InstagramError:
                continue
            return {m["name"]: (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value", 0))
                    for m in data.get("data", [])}
        return {}

    def posts(self, limit: int = 30) -> list[dict]:
        out: list[dict] = []
        data = self.get("me/media", fields="id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count",
                        limit=min(limit, 50))
        while True:
            for item in data.get("data", []):
                kind = post_type(item)
                stats = self.insights(item["id"], kind == "Reel")
                out.append({"id": item["id"], "caption": item.get("caption", ""), "type": kind, "permalink": item.get("permalink", ""),
                            "published": datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00").replace("+0000", "+00:00")),
                            "likes": item.get("like_count", 0), "comments": item.get("comments_count", 0),
                            "reach": int(stats.get("reach", 0) or 0), "saves": int(stats.get("saved", 0) or 0),
                            "shares": int(stats.get("shares", 0) or 0), "interactions": int(stats.get("total_interactions", 0) or 0),
                            "views": int(stats.get("views", 0) or 0),
                            "watch_s": round(float(stats["ig_reels_avg_watch_time"]) / 1000, 1) if stats.get("ig_reels_avg_watch_time") else 0})
                if len(out) >= limit:
                    return out
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                return out
            data = self.http.get(nxt, timeout=30).json()

    def online_hours(self) -> str:
        try:
            data = self.get("me/insights", metric="online_followers", period="lifetime")
            values = data["data"][0]["values"][0]["value"]
            return busiest_hours(values)
        except Exception:
            return ""


SETUP = (
    "Instagram isn't connected yet. 1) In Instagram switch to a Professional account (Settings, Account type and "
    "tools). 2) At developers.facebook.com create an app (Business), add the 'Instagram' product, 'API setup with "
    "Instagram login'. 3) Add your Instagram account under App roles as an Instagram Tester and accept the invite in "
    "Instagram (Settings, Apps and websites, Tester invites). 4) In that API setup page press 'Generate token', allow "
    "instagram_business_basic and instagram_business_manage_insights, and put the token in .env as "
    "INSTAGRAM_ACCESS_TOKEN=..., then restart Jay. Tokens last 60 days and Jay renews them.")


def is_configured() -> bool:
    return bool(os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip())


def register(reg: Registry, data_dir: Path, client: Instagram | None = None) -> Instagram:
    ig = client or Instagram(data_dir)

    @reg.tool("instagram_setup", "Explain how to connect the user's Instagram account (needed once).",
              enabled=lambda: client is None and not is_configured())
    def instagram_setup() -> str:
        return SETUP

    tool = partial(reg.tool, enabled=lambda: client is not None or is_configured())

    @tool("instagram_account", "Overview of the user's Instagram account: followers, post count, and when their "
          "followers are online. Read-only.")
    def instagram_account() -> str:
        p = ig.profile()
        lines = [f"@{p.get('username')} | {p.get('followers_count', 0):,} followers | follows {p.get('follows_count', 0):,} | "
                 f"{p.get('media_count', 0)} posts"]
        hours = ig.online_hours()
        if hours:
            lines.append("Followers are most online around (as Instagram reports it): " + hours)
        return "\n".join(lines)

    @tool("instagram_posts", "List the user's latest Instagram posts and reels with reach, likes, comments, saves "
          "and shares. Read-only.", {"limit": {"type": "integer", "description": "How many (default 12, max 50)."}})
    def instagram_posts(limit: int = 12) -> str:
        posts = ig.posts(max(1, min(int(limit), 50)))
        return "\n".join(f"{p['published'].astimezone():%d %b %H:%M} | {p['type']} | reach {p['reach']:,} | {p['likes']} likes | "
                         f"{p['comments']} comments | {p['saves']} saves | {p['shares']} shares | "
                         f"{(p['caption'] or '').strip().splitlines()[0][:60] if p['caption'] else '(no caption)'}"
                         for p in posts) or "No posts found."

    @tool("instagram_insights", "Analyse what works on the user's Instagram: best content type, posting day and "
          "time, caption length, hashtag count and individual hashtags, best and worst posts, each with how many "
          "posts it rests on. Use it to recommend what to post next, when, with what caption and hashtags, and say "
          "where the evidence is thin. Read-only.",
          {"limit": {"type": "integer", "description": "How many recent posts to study (default 30, max 50)."}})
    def instagram_insights(limit: int = 30) -> str:
        text = format_analysis(analyse(ig.posts(max(5, min(int(limit), 50)))))
        hours = ig.online_hours()
        return text + (f"\n\nFOLLOWERS ONLINE: {hours}" if hours else "")

    return ig
