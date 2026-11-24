"""YouTube channel analytics (read-only): what works on your channel, and what to make next.

Uses the official YouTube Data API and YouTube Analytics API with your own sign-in. It can only
read; it cannot upload, edit or delete anything (that needs a different permission, which Jay
does not ask for here).

One-time setup, the same Google Cloud project as Calendar/Gmail works:
  1. console.cloud.google.com -> APIs & Services -> Library: enable "YouTube Data API v3" and
     "YouTube Analytics API".
  2. OAuth consent screen: add yourself as a test user. Credentials -> Create OAuth client ->
     Desktop app -> download as data/google_credentials.json (skip if Calendar/Gmail already works).
  3. Ask Jay "analyse my YouTube channel". A browser opens once; sign in with the Google account
     that owns the channel and allow read-only access. The token is kept in data/youtube_token.json.

The numbers are worked out here, in code, not guessed by the model: Jay is handed tables (posting
day and hour, video length, retention, titles) with how many videos each figure rests on, and then
turns them into advice.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from .registry import Registry

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
LENGTH_BUCKETS = [(0, 61, "under 1 min (Shorts)"), (61, 300, "1-5 min"), (300, 600, "5-10 min"),
                  (600, 1200, "10-20 min"), (1200, 10**9, "over 20 min")]
DAYPARTS = [(5, 12, "morning (5-12)"), (12, 17, "afternoon (12-17)"), (17, 22, "evening (17-22)"),
            (22, 29, "night (22-5)")]
METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained"
MIN_AGE_DAYS = 3          # newer videos haven't had time to show how they perform


def iso_duration(text: str) -> int:
    """'PT1H2M30S' -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def rows_as_dicts(response: dict) -> list[dict]:
    names = [c["name"] for c in response.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in response.get("rows", [])]


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


# ------------------------------------------------------------------ the analysis (pure functions)
def analyse(videos: list[dict], today: date | None = None) -> dict:
    """videos: {title, published (aware datetime), duration, views, avg_pct, likes, comments, subs}.

    Compares by views per day since publishing, so an old video doesn't beat a new one just by age.
    """
    today = today or date.today()
    usable = []
    for v in videos:
        local = v["published"].astimezone()
        age = (today - local.date()).days
        if age < MIN_AGE_DAYS:
            continue
        usable.append({**v, "local": local, "age": age, "per_day": v["views"] / max(age, 1)})
    out: dict = {"considered": len(usable), "skipped_too_new": len(videos) - len(usable)}
    if not usable:
        return out
    median = statistics.median(v["per_day"] for v in usable)
    out["median_views_per_day"] = round(median, 1)

    def group(key, order):
        buckets: dict[str, list] = {}
        for v in usable:
            label = key(v)
            if label:
                buckets.setdefault(label, []).append(v)
        rows = []
        for label in order:
            items = buckets.get(label)
            if items:
                rows.append({"group": label, "videos": len(items),
                             "views_per_day": round(_mean(i["per_day"] for i in items), 1),
                             "avg_view_pct": round(_mean(i["avg_pct"] for i in items), 1),
                             "likes_per_1k_views": round(1000 * sum(i["likes"] for i in items) / max(sum(i["views"] for i in items), 1), 1),
                             "subs_per_1k_views": round(1000 * sum(i["subs"] for i in items) / max(sum(i["views"] for i in items), 1), 2)})
        return rows

    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    out["by_weekday"] = group(lambda v: weekdays[v["local"].weekday()], weekdays)
    out["by_daypart"] = group(
        lambda v: next((n for lo, hi, n in DAYPARTS if lo <= (v["local"].hour if v["local"].hour >= 5 else v["local"].hour + 24) < hi), None),
        [n for _, _, n in DAYPARTS])
    out["by_length"] = group(lambda v: next((n for lo, hi, n in LENGTH_BUCKETS if lo <= v["duration"] < hi), None),
                             [n for _, _, n in LENGTH_BUCKETS])
    out["by_title_style"] = group(
        lambda v: ("title has a number" if re.search(r"\d", v["title"]) else "no number") + " / " +
                  ("question" if "?" in v["title"] else "statement"),
        sorted({("title has a number" if re.search(r"\d", v["title"]) else "no number") + " / " +
                ("question" if "?" in v["title"] else "statement") for v in usable}))
    ranked = sorted(usable, key=lambda v: v["per_day"], reverse=True)

    def brief(v):
        return {"title": v["title"], "published": v["local"].strftime("%a %d %b %H:%M"),
                "length": f"{v['duration'] // 60}m{v['duration'] % 60:02d}s", "views": v["views"],
                "views_per_day": round(v["per_day"], 1), "times_the_median": round(v["per_day"] / median, 1) if median else None,
                "avg_view_pct": round(v["avg_pct"], 1)}

    out["best"] = [brief(v) for v in ranked[:3]]
    out["worst"] = [brief(v) for v in ranked[-3:]] if len(ranked) > 3 else []
    gaps = sorted((b["local"] - a["local"]).total_seconds() / 86400
                  for a, b in zip(sorted(usable, key=lambda v: v["local"]), sorted(usable, key=lambda v: v["local"])[1:]))
    out["typical_days_between_uploads"] = round(statistics.median(gaps), 1) if gaps else None
    return out


def format_analysis(result: dict) -> str:
    if not result.get("considered"):
        return ("Not enough published videos to analyse yet (videos need to be at least "
                f"{MIN_AGE_DAYS} days old).")
    lines = [f"Based on {result['considered']} videos (skipped {result['skipped_too_new']} too new). "
             f"Median {result['median_views_per_day']} views per day. Typical gap between uploads: "
             f"{result['typical_days_between_uploads']} days."]
    for title, key in (("POSTING DAY", "by_weekday"), ("TIME OF DAY (your local time)", "by_daypart"),
                       ("VIDEO LENGTH", "by_length"), ("TITLE STYLE", "by_title_style")):
        rows = result.get(key) or []
        if rows:
            lines.append(f"\n{title}  (group | videos | views/day | avg % watched | likes/1k views | subs/1k views)")
            for r in rows:
                flag = "  <- only 1-2 videos, weak evidence" if r["videos"] < 3 else ""
                lines.append(f"  {r['group']} | {r['videos']} | {r['views_per_day']} | {r['avg_view_pct']}% | "
                             f"{r['likes_per_1k_views']} | {r['subs_per_1k_views']}{flag}")
    for label, key in (("BEST", "best"), ("WORST", "worst")):
        if result.get(key):
            lines.append(f"\n{label} VIDEOS")
            for v in result[key]:
                lines.append(f"  {v['title']} ({v['published']}, {v['length']}): {v['views']} views, "
                             f"{v['views_per_day']}/day = {v['times_the_median']}x median, {v['avg_view_pct']}% watched")
    return "\n".join(lines)


def retention_summary(points: list[tuple[float, float]]) -> str:
    """(fraction of the video elapsed, share of viewers still watching) -> where people leave."""
    if not points:
        return "No retention data for this video yet."
    points = sorted(points)
    drops = sorted(((points[i][1] - points[i + 1][1], points[i][0], points[i + 1][0])
                    for i in range(len(points) - 1)), reverse=True)[:3]
    marks = ", ".join(f"{int(a * 100)}%-{int(b * 100)}% of the video (lost {drop * 100:.0f} points)" for drop, a, b in drops if drop > 0)
    at = lambda x: min(points, key=lambda p: abs(p[0] - x))[1]            # noqa: E731
    return (f"Still watching at 10%: {at(0.1) * 100:.0f}%, 25%: {at(0.25) * 100:.0f}%, 50%: {at(0.5) * 100:.0f}%, "
            f"75%: {at(0.75) * 100:.0f}%, near the end: {at(0.95) * 100:.0f}%. Biggest drops: {marks or 'none'}.")


# ------------------------------------------------------------------ talking to YouTube
class YouTube:
    def __init__(self, data_dir: Path, data_service=None, analytics_service=None):
        self.credentials_file = data_dir / "google_credentials.json"
        self.token_file = data_dir / "youtube_token.json"
        self._data, self._analytics, self._creds = data_service, analytics_service, None

    def _credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        if self._creds and self._creds.valid:
            return self._creds
        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        self._creds = creds
        return creds

    def data(self):
        if self._data is None:
            from googleapiclient.discovery import build

            self._data = build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)
        return self._data

    def analytics(self):
        if self._analytics is None:
            from googleapiclient.discovery import build

            self._analytics = build("youtubeAnalytics", "v2", credentials=self._credentials(), cache_discovery=False)
        return self._analytics

    def report(self, **kwargs) -> list[dict]:
        return rows_as_dicts(self.analytics().reports().query(ids="channel==MINE", **kwargs).execute())

    # -- data
    def channel(self) -> dict:
        items = self.data().channels().list(part="snippet,statistics,contentDetails", mine=True).execute().get("items", [])
        if not items:
            raise RuntimeError("This Google account has no YouTube channel.")
        return items[0]

    def videos(self, limit: int = 30) -> list[dict]:
        """The channel's most recent uploads with public stats and, when available, Analytics numbers."""
        uploads = self.channel()["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            page = self.data().playlistItems().list(part="contentDetails", playlistId=uploads,
                                                    maxResults=min(50, limit - len(ids)), pageToken=token).execute()
            ids += [i["contentDetails"]["videoId"] for i in page.get("items", [])]
            token = page.get("nextPageToken")
            if not token:
                break
        videos = []
        for start in range(0, len(ids), 50):
            for item in self.data().videos().list(part="snippet,statistics,contentDetails",
                                                  id=",".join(ids[start:start + 50])).execute().get("items", []):
                stats = item.get("statistics", {})
                videos.append({
                    "id": item["id"], "title": item["snippet"]["title"],
                    "published": datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00")),
                    "duration": iso_duration(item["contentDetails"].get("duration", "")),
                    "views": int(stats.get("viewCount", 0)), "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)), "avg_pct": 0.0, "subs": 0, "avg_seconds": 0})
        if videos:
            try:
                first = min(v["published"] for v in videos).date().isoformat()
                rows = self.report(startDate=first, endDate=date.today().isoformat(), dimensions="video", metrics=METRICS,
                                   filters="video==" + ",".join(v["id"] for v in videos), maxResults=200)
                by_id = {r["video"]: r for r in rows}
                for v in videos:
                    r = by_id.get(v["id"])
                    if r:
                        v.update(avg_pct=float(r.get("averageViewPercentage", 0)), subs=int(r.get("subscribersGained", 0)),
                                 avg_seconds=int(r.get("averageViewDuration", 0)), shares=int(r.get("shares", 0)))
            except Exception:
                pass                                   # public numbers still work without Analytics
        return videos

    def retention(self, video_id: str, published: date) -> list[tuple[float, float]]:
        rows = self.report(startDate=published.isoformat(), endDate=date.today().isoformat(),
                           dimensions="elapsedVideoTimeRatio", metrics="audienceWatchRatio", filters=f"video=={video_id}")
        return [(float(r["elapsedVideoTimeRatio"]), float(r["audienceWatchRatio"])) for r in rows]

    def overview(self, days: int = 28) -> str:
        ch = self.channel()
        stats = ch["statistics"]
        lines = [f"Channel: {ch['snippet']['title']} | {int(stats.get('subscriberCount', 0)):,} subscribers | "
                 f"{int(stats.get('viewCount', 0)):,} total views | {stats.get('videoCount', 0)} videos"]
        end = date.today()
        rows = self.report(startDate=(end - timedelta(days=days)).isoformat(), endDate=end.isoformat(),
                           metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost,likes,comments,shares")
        if rows:
            r = rows[0]
            lines.append(f"Last {days} days: {int(r.get('views', 0)):,} views, {int(r.get('estimatedMinutesWatched', 0)):,} minutes "
                         f"watched, average view {int(r.get('averageViewDuration', 0))}s, net subscribers "
                         f"{int(r.get('subscribersGained', 0)) - int(r.get('subscribersLost', 0)):+d}, {int(r.get('likes', 0))} likes, "
                         f"{int(r.get('comments', 0))} comments, {int(r.get('shares', 0))} shares")
        traffic = self.report(startDate=(end - timedelta(days=days)).isoformat(), endDate=end.isoformat(),
                              dimensions="insightTrafficSourceType", metrics="views", sort="-views", maxResults=6)
        if traffic:
            total = sum(int(t["views"]) for t in traffic) or 1
            lines.append("Where views come from: " + ", ".join(f"{t['insightTrafficSourceType'].replace('_', ' ').title()} "
                                                                 f"{100 * int(t['views']) / total:.0f}%" for t in traffic))
        return "\n".join(lines)


SETUP = (
    "YouTube isn't connected yet. Steps: 1) In console.cloud.google.com (the project you use for Google "
    "Calendar/Gmail, or a new one) enable 'YouTube Data API v3' and 'YouTube Analytics API'. 2) Create an OAuth "
    "client of type Desktop app, download it as data\\google_credentials.json, and add your Google account as a "
    "test user on the consent screen. 3) Ask me to analyse the channel: a browser opens once to sign in with the "
    "account that owns the channel, read-only.")


def is_configured(data_dir: Path) -> bool:
    return (data_dir / "google_credentials.json").exists()


def register(reg: Registry, data_dir: Path, client: YouTube | None = None) -> YouTube:
    yt = client or YouTube(data_dir)

    @reg.tool("youtube_setup", "Explain how to connect the user's YouTube channel (needed once).",
              enabled=lambda: client is None and not is_configured(data_dir))
    def youtube_setup() -> str:
        return SETUP

    tool = partial(reg.tool, enabled=lambda: client is not None or is_configured(data_dir))

    @tool("youtube_channel", "Overview of the user's YouTube channel: subscribers, views and watch time over the "
          "last weeks, and where views come from. Read-only.",
          {"days": {"type": "integer", "description": "Period to summarise (default 28)."}})
    def youtube_channel(days: int = 28) -> str:
        return yt.overview(max(1, min(int(days), 365)))

    @tool("youtube_videos", "List the user's latest YouTube videos with views, likes, average % watched and "
          "subscribers gained. Read-only.", {"limit": {"type": "integer", "description": "How many (default 15, max 50)."}})
    def youtube_videos(limit: int = 15) -> str:
        videos = yt.videos(max(1, min(int(limit), 50)))
        return "\n".join(f"{v['id']} | {v['published'].astimezone():%d %b %H:%M} | {v['duration'] // 60}m{v['duration'] % 60:02d}s | "
                         f"{v['title'][:70]} | {v['views']:,} views | {v['avg_pct']:.0f}% watched | {v['likes']} likes | "
                         f"+{v['subs']} subs" for v in videos) or "No videos found."

    @tool("youtube_insights", "Analyse what works on the user's YouTube channel: best posting day and time, best "
          "video length, title styles, best and worst videos, upload rhythm, each with how many videos it rests on. "
          "Use the result to recommend the next videos, a schedule and lengths, and say where the evidence is thin. "
          "Read-only.", {"limit": {"type": "integer", "description": "How many recent videos to study (default 30, max 50)."}})
    def youtube_insights(limit: int = 30) -> str:
        return format_analysis(analyse(yt.videos(max(5, min(int(limit), 50)))))

    @tool("youtube_video_report", "Where viewers drop off in one video (audience retention), by its video id. "
          "Use it to advise on hooks, pacing and length. Read-only.", {"video_id": {"type": "string"}}, required=("video_id",))
    def youtube_video_report(video_id: str) -> str:
        info = yt.data().videos().list(part="snippet", id=video_id).execute().get("items", [])
        if not info:
            return "No video with that id on this account."
        published = datetime.fromisoformat(info[0]["snippet"]["publishedAt"].replace("Z", "+00:00")).date()
        return f"{info[0]['snippet']['title']}\n" + retention_summary(yt.retention(video_id, published))

    return yt
