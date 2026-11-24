"""Google Calendar and Gmail tools.

Needs a one-time setup (see README): put an OAuth "Desktop app" client file at
data/google_credentials.json. The first call opens a browser to sign in; the
resulting token is kept in data/google_token.json.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import partial
from pathlib import Path

from .registry import Registry

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GoogleClient:
    def __init__(self, data_dir: Path):
        self.credentials_file = data_dir / "google_credentials.json"
        self.token_file = data_dir / "google_token.json"
        self._creds = None

    def service(self, api: str, version: str):
        from googleapiclient.discovery import build

        return build(api, version, credentials=self._credentials(), cache_discovery=False)

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
            if not self.credentials_file.exists():
                raise RuntimeError(
                    f"Google is not set up yet: {self.credentials_file} is missing. "
                    "Tell the user to follow the 'Google setup' section of the README.")
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        self._creds = creds
        return creds


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []):
        text = _body(part)
        if text:
            return text
    return ""


def is_configured(data_dir: Path) -> bool:
    return (data_dir / "google_credentials.json").exists()


def register(reg: Registry, data_dir: Path) -> None:
    google = GoogleClient(data_dir)
    # Not offered to the model until Google is set up, which keeps requests smaller.
    tool = partial(reg.tool, enabled=lambda: is_configured(data_dir))

    @tool(
        "calendar_list_events",
        "List upcoming events from the user's primary Google Calendar.",
        {"days": {"type": "integer", "description": "How many days ahead to look (default 7)."}},
    )
    def calendar_list_events(days: int = 7) -> str:
        now = datetime.now().astimezone()
        items = google.service("calendar", "v3").events().list(
            calendarId="primary", timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=50,
        ).execute().get("items", [])
        return "\n".join(
            f"{e['start'].get('dateTime', e['start'].get('date'))}  {e.get('summary', '(no title)')}"
            f"  [id {e['id']}]" for e in items) or f"No events in the next {days} days."

    @tool(
        "calendar_create_event",
        "Create an event on the user's primary Google Calendar. The user is asked to approve it.",
        {
            "summary": {"type": "string"},
            "start": {"type": "string", "description": "Local start time, ISO 8601, e.g. 2026-11-16T15:00."},
            "end": {"type": "string", "description": "Local end time, ISO 8601."},
            "description": {"type": "string"},
        },
        required=("summary", "start", "end"),
        confirm=lambda a: f"Create calendar event '{a['summary']}' from {a['start']} to {a['end']}",
    )
    def calendar_create_event(summary: str, start: str, end: str, description: str = "") -> str:
        def local(value: str) -> dict:
            return {"dateTime": datetime.fromisoformat(value).astimezone().isoformat()}

        event = google.service("calendar", "v3").events().insert(
            calendarId="primary",
            body={"summary": summary, "description": description, "start": local(start), "end": local(end)},
        ).execute()
        return f"Created: {event.get('htmlLink')}"

    @tool(
        "gmail_list",
        "List recent emails from the user's Gmail inbox.",
        {
            "query": {"type": "string", "description": "Gmail search, e.g. 'is:unread' or 'from:boss'. Default is:unread."},
            "max_results": {"type": "integer"},
        },
    )
    def gmail_list(query: str = "is:unread", max_results: int = 10) -> str:
        gmail = google.service("gmail", "v1").users().messages()
        refs = gmail.list(userId="me", q=query, maxResults=min(max_results, 25)).execute().get("messages", [])
        lines = []
        for ref in refs:
            m = gmail.get(userId="me", id=ref["id"], format="metadata",
                          metadataHeaders=["From", "Subject", "Date"]).execute()
            lines.append(f"[id {m['id']}] {_header(m, 'Date')} | {_header(m, 'From')} | "
                         f"{_header(m, 'Subject')} | {m.get('snippet', '')}")
        return "\n".join(lines) or "No matching emails."

    @tool(
        "gmail_read",
        "Read the full text of one email by id (ids come from gmail_list).",
        {"message_id": {"type": "string"}},
        required=("message_id",),
    )
    def gmail_read(message_id: str) -> str:
        m = google.service("gmail", "v1").users().messages().get(
            userId="me", id=message_id, format="full").execute()
        return (f"From: {_header(m, 'From')}\nTo: {_header(m, 'To')}\nDate: {_header(m, 'Date')}\n"
                f"Subject: {_header(m, 'Subject')}\n\n{_body(m['payload']) or m.get('snippet', '')}")

    @tool(
        "gmail_send",
        "Send an email from the user's Gmail account. The user is shown the full message and "
        "asked to approve it before it is sent.",
        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        required=("to", "subject", "body"),
        confirm=lambda a: f"Send email to {a['to']}\n  Subject: {a['subject']}\n  ---\n{a['body']}\n  ---",
    )
    def gmail_send(to: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = google.service("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Sent (id {sent['id']})."
