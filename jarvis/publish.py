"""Posting approved content to YouTube and Instagram.

Posting is the one step in Jay's content desk that other people see, so it is guarded in code,
whatever a permission or the model says. A draft is posted only if ALL of these hold:
  * you approved it (status approved or due), and its text is exactly what you approved (a fingerprint
    taken at approval is checked again, so nothing edited afterwards goes out);
  * it has a video that Jay made or you supplied (inside data/videos or data/video_assets);
  * its scheduled time has arrived (unless you ask for it to go now, which still needs your yes);
  * it has not been posted already, and today's limit per platform is not used up (3 by default);
  * you said yes to this post: a button on your phone, `/publish N`, or a standing permission you gave
    (`/allow publish for 7d`). Automatic posting at the scheduled time is off unless you set
    JARVIS_AUTOPUBLISH=1 in .env.

What it posts: YouTube videos (long videos and Shorts) through the YouTube Data API, and Instagram
Reels through the Instagram API. Photos, carousels and stories are not posted (those need other
formats); for those you still get the ready-to-post package on your phone.

Things to know before relying on it:
  * YouTube locks videos uploaded through the API to PRIVATE until the Google Cloud project passes
    Google's API audit (a form on YouTube's developer site). Until then Jay's uploads arrive in your
    Studio as private videos, which you publish by hand there. That is Google's rule, not Jay's.
  * Instagram needs a token that includes the content-publish permission (see INSTAGRAM_PUBLISH_TOKEN).
  * Both platforms ask for AI-generated content to be disclosed; Jay adds "Narrated with an AI voice."
    to the YouTube description and sets YouTube's synthetic-media flag. Change it with JARVIS_DISCLOSURE.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from .content import HASH_FIELDS, text_hash          # noqa: F401
from .tools.registry import Registry

YT_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
INSTAGRAM_BASE = os.getenv("INSTAGRAM_GRAPH_URL", "https://graph.instagram.com")
HASH_FIELDS = ("platform", "kind", "title", "hook", "script", "caption", "hashtags", "description")
YOUTUBE_KINDS = ("long video", "short")
INSTAGRAM_KINDS = ("reel",)
DEFAULT_DISCLOSURE = "Narrated with an AI voice."
LOG_SCHEMA = """CREATE TABLE IF NOT EXISTS publish_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content_id INTEGER NOT NULL, platform TEXT NOT NULL,
    remote_id TEXT, url TEXT, ts TEXT NOT NULL)"""


class PublishError(Exception):
    pass


@dataclass
class Posted:
    platform: str
    remote_id: str
    url: str


def platforms_for(item) -> list[str]:
    wanted = ["youtube", "instagram"] if item["platform"] == "both" else [item["platform"]]
    return [p for p in wanted if (p == "youtube" and item["kind"] in YOUTUBE_KINDS) or (p == "instagram" and item["kind"] in INSTAGRAM_KINDS)]


def hashtag_list(text: str) -> list[str]:
    return re.findall(r"#\w+", text or "")


# ------------------------------------------------------------------ YouTube
class YouTubeUploader:
    def __init__(self, data_dir: Path, service=None):
        self.credentials_file = data_dir / "google_credentials.json"
        self.token_file = data_dir / "youtube_upload_token.json"
        self._service = service

    def configured(self) -> bool:
        return self._service is not None or self.token_file.exists()

    def authorize(self) -> str:
        """One-time sign-in (opens a browser on this PC) that allows uploading and nothing else."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.credentials_file.exists():
            raise PublishError("data\\google_credentials.json is missing; see the YouTube setup steps.")
        creds = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), YT_UPLOAD_SCOPE).run_local_server(port=0)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        return "YouTube uploading is connected."

    def service(self):
        if self._service is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            if not self.token_file.exists():
                raise PublishError("YouTube uploading isn't connected. Send /connect youtube once, on the laptop.")
            creds = Credentials.from_authorized_user_file(str(self.token_file), YT_UPLOAD_SCOPE)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
            self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def body(self, item) -> dict:
        disclosure = os.getenv("JARVIS_DISCLOSURE", DEFAULT_DISCLOSURE).strip()
        tags = [t.lstrip("#") for t in hashtag_list(item["hashtags"])][:15]
        description = "\n\n".join(p for p in (item["description"] or item["caption"], " ".join(hashtag_list(item["hashtags"])[:15]), disclosure) if p)
        status = {"privacyStatus": os.getenv("JARVIS_YT_PRIVACY", "public"), "selfDeclaredMadeForKids": False}
        if disclosure:
            status["containsSyntheticMedia"] = True
        return {"snippet": {"title": item["title"][:100], "description": description[:5000], "tags": tags,
                            "categoryId": os.getenv("JARVIS_YT_CATEGORY", "24")}, "status": status}

    def upload(self, item) -> Posted:
        from googleapiclient.http import MediaFileUpload

        service = self.service()
        body = self.body(item)
        media = MediaFileUpload(item["media_path"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        for attempt in (1, 2):
            try:
                request = service.videos().insert(part="snippet,status", body=body, media_body=media)
                response = None
                while response is None:
                    _, response = request.next_chunk()
                break
            except Exception as exc:
                if attempt == 1 and "containsSyntheticMedia" in str(exc) and body["status"].pop("containsSyntheticMedia", None):
                    continue                                   # an older API surface that doesn't know the flag
                raise PublishError(f"YouTube refused the upload: {str(exc)[:300]}") from exc
        video_id = response["id"]
        notes = ""
        thumb = item["thumb_path"] if "thumb_path" in item.keys() else ""
        if thumb and os.path.isfile(thumb):
            try:
                service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
            except Exception:
                notes = " (the thumbnail was not accepted; set it in Studio)"
        privacy = body["status"]["privacyStatus"]
        return Posted("youtube", video_id, f"https://youtu.be/{video_id}" + notes + (
            "" if privacy == "public" else f" [{privacy}]"))


# ------------------------------------------------------------------ Instagram
class InstagramPublisher:
    def __init__(self, http=None, sleep=time.sleep):
        self.http, self.sleep = http or requests, sleep

    @staticmethod
    def token() -> str:
        return (os.getenv("INSTAGRAM_PUBLISH_TOKEN") or os.getenv("INSTAGRAM_ACCESS_TOKEN") or "").strip()

    def configured(self) -> bool:
        return bool(self.token())

    def _ok(self, response, what: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            raise PublishError(f"Instagram gave an unreadable answer while {what}.") from None
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            hint = " The token needs the instagram_business_content_publish permission." if err.get("code") in (10, 200, 190) else ""
            raise PublishError(f"Instagram refused while {what}: {err.get('message', err)}.{hint}")
        return data

    def caption(self, item) -> str:
        text = "\n\n".join(p for p in (item["caption"], item["hashtags"]) if p).strip()
        if len(text) > 2200:
            raise PublishError(f"The caption and hashtags are {len(text)} characters; Instagram allows 2,200.")
        if len(hashtag_list(text)) > 30:
            raise PublishError("Instagram allows at most 30 hashtags.")
        return text

    def publish(self, item) -> Posted:
        token = self.token()
        if not token:
            raise PublishError("Instagram isn't connected (INSTAGRAM_ACCESS_TOKEN in .env).")
        caption = self.caption(item)
        path = item["media_path"]
        size = os.path.getsize(path)
        me = self._ok(self.http.get(f"{INSTAGRAM_BASE}/me", params={"fields": "user_id", "access_token": token}, timeout=30), "looking up the account")
        user_id = me.get("user_id") or me.get("id")
        made = self._ok(self.http.post(f"{INSTAGRAM_BASE}/{user_id}/media", timeout=60, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true", "access_token": token}),
            "creating the reel")
        container, uri = made["id"], made.get("uri")
        if not uri:
            raise PublishError("Instagram did not offer an upload address for the video.")
        with open(path, "rb") as handle:
            sent = self._ok(self.http.post(uri, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                                           data=handle, timeout=900), "uploading the video")
        if sent.get("success") is False:
            raise PublishError("Instagram did not accept the video upload.")
        for _ in range(90):                                    # Instagram processes the video before it can be published
            state = self._ok(self.http.get(f"{INSTAGRAM_BASE}/{container}", params={"fields": "status_code,status", "access_token": token},
                                           timeout=30), "checking the upload")
            code = state.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the video ({state.get('status', code)}).")
            self.sleep(5)
        else:
            raise PublishError("Instagram is still processing the video; try again in a few minutes.")
        published = self._ok(self.http.post(f"{INSTAGRAM_BASE}/{user_id}/media_publish", timeout=60,
                                            data={"creation_id": container, "access_token": token}), "publishing")
        media_id = published["id"]
        try:
            link = self.http.get(f"{INSTAGRAM_BASE}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=30).json().get("permalink", "")
        except Exception:
            link = ""
        return Posted("instagram", media_id, link or f"instagram media {media_id}")


# ------------------------------------------------------------------ the gate
class Publisher:
    def __init__(self, queue, data_dir: Path, youtube: YouTubeUploader | None = None, instagram: InstagramPublisher | None = None):
        self.queue, self.data_dir = queue, data_dir
        self.youtube = youtube or YouTubeUploader(data_dir)
        self.instagram = instagram or InstagramPublisher()
        queue.store._run(LOG_SCHEMA)
        self._lock = threading.Lock()
        self.daily_limit = int(os.getenv("JARVIS_MAX_POSTS_PER_DAY", "3"))

    def autopublish_enabled(self) -> bool:
        return os.getenv("JARVIS_AUTOPUBLISH", "0") == "1"

    def ready_platforms(self, item) -> list[str]:
        out = []
        for p in platforms_for(item):
            if (p == "youtube" and self.youtube.configured()) or (p == "instagram" and self.instagram.configured()):
                out.append(p)
        return out

    def posted_today(self, platform: str) -> int:
        stamp = datetime.now().strftime("%Y-%m-%d")
        return len(self.queue.store._rows("SELECT 1 FROM publish_log WHERE platform = ? AND ts LIKE ?", (platform, stamp + "%")))

    def check(self, item, ignore_schedule: bool = False, now: datetime | None = None) -> list[str]:
        """Raise PublishError if this must not go out; otherwise the platforms it will go to."""
        if item is None:
            raise PublishError("No such draft.")
        if item["status"] == "posted":
            raise PublishError("That one has already been posted.")
        if item["status"] not in ("approved", "due"):
            raise PublishError(f"It is {item['status']}, not approved. Only approved drafts are posted.")
        if not item["approved_hash"] or item["approved_hash"] != text_hash(item):
            raise PublishError("The text changed after you approved it, so it won't be posted. Approve it again.")
        media = item["media_path"]
        if not media or not os.path.isfile(media):
            raise PublishError("There's no video file for it yet (make one with /video N).")
        resolved = Path(media).resolve()
        if not any(base.resolve() in resolved.parents for base in (self.data_dir / "videos", self.data_dir / "video_assets")):
            raise PublishError("Only videos made by Jay or kept in data\\video_assets are posted.")
        if not ignore_schedule:
            if not item["scheduled_at"]:
                raise PublishError("It has no scheduled time. Give it one, or ask to post it now.")
            if item["scheduled_at"] > (now or datetime.now()).strftime("%Y-%m-%d %H:%M"):
                raise PublishError(f"It isn't due until {item['scheduled_at']}.")
        platforms = platforms_for(item)
        if not platforms:
            raise PublishError(f"I can't post a {item['kind']} to {item['platform']} (only YouTube videos and Instagram reels). "
                               "You'll get the ready-to-post package instead.")
        missing = [p for p in platforms if p not in self.ready_platforms(item)]
        if missing:
            raise PublishError(f"{', '.join(missing)} isn't connected for posting yet.")
        for p in platforms:
            if self.posted_today(p) >= self.daily_limit:
                raise PublishError(f"Today's limit of {self.daily_limit} {p} posts has been reached.")
        return platforms

    def publish(self, item_id: int, ignore_schedule: bool = False) -> str:
        """Post it. Returns a plain-language result. Raises PublishError if the gate says no."""
        with self._lock:                                       # never two posts of the same thing at once
            item = self.queue.get(item_id)
            platforms = self.check(item, ignore_schedule)
            done, problems = [], []
            for platform in platforms:
                if platform in {r["platform"] for r in self.queue.store._rows("SELECT platform FROM publish_log WHERE content_id = ?", (item_id,))}:
                    continue                                   # this platform already has it
                try:
                    posted = self.youtube.upload(item) if platform == "youtube" else self.instagram.publish(item)
                except PublishError as exc:
                    problems.append(f"{platform}: {exc}")
                    continue
                self.queue.store._run("INSERT INTO publish_log (content_id, platform, remote_id, url, ts) VALUES (?, ?, ?, ?, ?)",
                                      (item_id, platform, posted.remote_id, posted.url, datetime.now().isoformat(timespec="seconds")))
                done.append(posted)
            if done and not problems:
                self.queue.set_posted(item_id, "; ".join(p.url for p in done))
            lines = [f"Posted #{item['id']} to {p.platform}: {p.url}" for p in done] + [f"Couldn't post: {m}" for m in problems]
            if problems and not done:
                raise PublishError("; ".join(problems))
            return "\n".join(lines) or "Already posted everywhere it can go."


# ------------------------------------------------------------------ tool
def register(reg: Registry, publisher: Publisher) -> None:
    @reg.tool(
        "publish_content",
        "Post an approved draft that has a video to YouTube and/or Instagram. It only works for drafts the user "
        "approved, unchanged since, whose scheduled time has come (set ignore_schedule only if the user asked you to "
        "post it now). The user is asked to confirm each post unless they gave a standing permission.",
        {"id": {"type": "integer"}, "ignore_schedule": {"type": "boolean", "description": "Post now, before the scheduled time (the user asked)."}},
        required=("id",),
        confirm=lambda a: _confirm_text(publisher, a),
    )
    def publish_content(id: int, ignore_schedule: bool = False) -> str:
        try:
            return publisher.publish(id, ignore_schedule)
        except PublishError as exc:
            return f"Not posted: {exc}"


def _confirm_text(publisher: Publisher, args: dict) -> str | None:
    item = publisher.queue.get(args["id"])
    try:
        publisher.check(item, bool(args.get("ignore_schedule")))
    except PublishError:
        return None                       # it would be refused anyway: nothing to approve, the tool explains why
    early = " NOW, before its scheduled time" if args.get("ignore_schedule") else ""
    return f"Post #{item['id']} to {item['platform']}{early}: {item['title']}\nCaption: {item['caption'][:200]}\n{item['hashtags']}"
