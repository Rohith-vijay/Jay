"""Email and calendar without a Google Cloud project.

Mail: any provider over IMAP/SMTP with an app password (Gmail, Outlook, a college account).
Calendar: any "secret iCal address" (Google Calendar, Outlook, a timetable site) read as a feed.
Both are configured in .env, see the README.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from functools import partial

import requests

from .registry import Registry


# --------------------------------------------------------------------------- mail
def mail_settings() -> dict | None:
    user, password = os.getenv("JARVIS_MAIL_USER", "").strip(), os.getenv("JARVIS_MAIL_PASSWORD", "").replace(" ", "")
    if not user or not password:
        return None
    domain = user.split("@")[-1].lower()
    outlook = domain in ("outlook.com", "hotmail.com", "live.com")
    return {
        "user": user, "password": password,
        "imap": os.getenv("JARVIS_MAIL_IMAP", "outlook.office365.com" if outlook else "imap.gmail.com").strip(),
        "smtp": os.getenv("JARVIS_MAIL_SMTP", "smtp.office365.com" if outlook else "smtp.gmail.com").strip(),
    }


def _decode(value) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return str(value or "")


def _text_of(msg: email.message.Message) -> str:
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            body = part.get_payload(decode=True)
            if body is None:
                continue
            text = body.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = text
    if plain:
        return plain
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", html))).strip()


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "").replace('"', "") + '"'


def _folder_name(cfg: dict, folder: str) -> str:
    """'inbox', 'sent' or 'drafts' by everyday name; anything else is used as given."""
    gmail = "gmail" in cfg.get("imap", "")
    names = {"inbox": "INBOX", "sent": "[Gmail]/Sent Mail" if gmail else "Sent",
             "drafts": "[Gmail]/Drafts" if gmail else "Drafts"}
    return names.get((folder or "inbox").strip().lower(), (folder or "INBOX").strip())


def _connect(cfg: dict, folder: str = "inbox") -> imaplib.IMAP4_SSL:
    box = imaplib.IMAP4_SSL(cfg["imap"], timeout=30)
    box.login(cfg["user"], cfg["password"])
    name = _folder_name(cfg, folder).replace('"', "")
    status, _ = box.select(f'"{name}"', readonly=True)         # read-only: looking never marks mail as read
    if status != "OK":
        raise ValueError(f"There is no mail folder called '{folder}'.")
    return box


def list_mail(cfg: dict, unread_only: bool = True, sender: str = "", subject: str = "",
              days: int = 0, max_results: int = 10, folder: str = "inbox") -> str:
    box = _connect(cfg, folder)
    try:
        crit = ["UNSEEN"] if unread_only else ["ALL"]
        if sender:
            crit += ["FROM", _imap_quote(sender)]
        if subject:
            crit += ["SUBJECT", _imap_quote(subject)]
        if days:
            crit += ["SINCE", (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")]
        status, data = box.uid("SEARCH", None, *crit)
        every = data[0].split() if status == "OK" and data and data[0] else []
        uids = every[-min(max_results, 25):][::-1]
        lines = []
        for uid in uids:
            status, parts = box.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            head = email.message_from_bytes(parts[0][1])
            lines.append(f"[id {uid.decode()}] {_decode(head['Date'])} | {_decode(head['From'])} | {_decode(head['Subject'])}")
        if not lines:
            return "No matching emails."
        return f"{len(every)} matching in total; newest {len(lines)}:\n" + "\n".join(lines)
    finally:
        try:
            box.logout()
        except Exception:
            pass


def read_mail(cfg: dict, uid: str, folder: str = "inbox") -> str:
    box = _connect(cfg, folder)
    try:
        status, parts = box.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            return f"No email with id {uid}."
        msg = email.message_from_bytes(parts[0][1])
        names = [_decode(p.get_filename()) for p in msg.walk() if p.get_filename()]
        attach = f"\nAttachments: {', '.join(names)}" if names else ""
        return (f"From: {_decode(msg['From'])}\nTo: {_decode(msg['To'])}\nDate: {_decode(msg['Date'])}\n"
                f"Subject: {_decode(msg['Subject'])}{attach}\n\n{_text_of(msg)[:12000]}")
    finally:
        try:
            box.logout()
        except Exception:
            pass


def send_mail(cfg: dict, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["user"], to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg["smtp"], 465, timeout=30) as smtp:
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)
    return f"Sent to {to}."


# --------------------------------------------------------------------------- calendar feed
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_when(prop: str, value: str):
    """(datetime or date) for a DTSTART/DTEND line; times in UTC become local, others stay as written."""
    value = value.strip()
    if "VALUE=DATE" in prop or (len(value) == 8 and value.isdigit()):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")


_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def parse_ics(text: str, start: datetime, end: datetime) -> list[tuple[datetime, str, str]]:
    """Events (with simple daily/weekly/monthly/yearly repeats) that begin between start and end."""
    events: list[tuple[datetime, str, str]] = []
    block: dict | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            block = {"EX": set()}
        elif line == "END:VEVENT" and block is not None:
            events.extend(_expand(block, start, end))
            block = None
        elif block is not None and ":" in line:
            prop, _, value = line.partition(":")
            name = prop.split(";")[0].upper()
            if name == "EXDATE":
                for v in value.split(","):
                    try:
                        w = _parse_when(prop, v)
                        block["EX"].add(w if isinstance(w, date) and not isinstance(w, datetime) else w.date())
                    except ValueError:
                        pass
            elif name in ("DTSTART", "SUMMARY", "LOCATION", "RRULE", "STATUS"):
                block[name] = (prop, value)
    events.sort(key=lambda e: e[0])
    return events


def _expand(block: dict, start: datetime, end: datetime):
    if "DTSTART" not in block or block.get("STATUS", ("", ""))[1].strip().upper() == "CANCELLED":
        return
    try:
        first = _parse_when(*block["DTSTART"])
    except ValueError:
        return
    all_day = not isinstance(first, datetime)
    first_dt = datetime.combine(first, datetime.min.time()) if all_day else first
    title = block.get("SUMMARY", ("", "(no title)"))[1].replace("\\,", ",").replace("\\n", " ").strip()
    where = block.get("LOCATION", ("", ""))[1].replace("\\,", ",").strip()
    label = title + (f" @ {where}" if where else "") + ("  (all day)" if all_day else "")

    def emit(moment: datetime):
        if start <= moment <= end and moment.date() not in block["EX"]:
            yield moment, label, "all-day" if all_day else moment.strftime("%H:%M")

    rule = block.get("RRULE")
    if not rule:
        yield from emit(first_dt)
        return
    parts = dict(p.split("=", 1) for p in rule[1].split(";") if "=" in p)
    freq, interval = parts.get("FREQ", ""), int(parts.get("INTERVAL", "1") or 1)
    until = None
    if "UNTIL" in parts:
        try:
            u = _parse_when("", parts["UNTIL"])
            until = datetime.combine(u, datetime.max.time()) if not isinstance(u, datetime) else u
        except ValueError:
            pass
    limit = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    by_day = [d[-2:] for d in parts.get("BYDAY", "").split(",") if d]
    produced, cursor = 0, first_dt
    for _ in range(4000):                                          # hard stop: never loop forever
        if until and cursor > until or cursor > end or (limit is not None and produced >= limit):
            break
        if freq == "WEEKLY" and by_day:
            monday = cursor - timedelta(days=cursor.weekday())
            for code in by_day:
                if code in _DAYS:
                    day = monday + timedelta(days=_DAYS.index(code))
                    if day >= first_dt and not (until and day > until) and not (limit is not None and produced >= limit):
                        produced += 1
                        yield from emit(day)
            cursor = monday + timedelta(weeks=interval)
            continue
        produced += 1
        yield from emit(cursor)
        if freq == "DAILY":
            cursor += timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cursor.month - 1 + interval
            try:
                cursor = cursor.replace(year=cursor.year + month // 12, month=month % 12 + 1)
            except ValueError:
                cursor += timedelta(days=30 * interval)
        elif freq == "YEARLY":
            try:
                cursor = cursor.replace(year=cursor.year + interval)
            except ValueError:
                cursor += timedelta(days=365 * interval)
        else:
            break


def feed_urls() -> list[str]:
    raw = os.getenv("JARVIS_ICS_URLS", "")
    return [u.strip().replace("webcal://", "https://", 1) for u in re.split(r"[,\s]+", raw) if u.strip()]


_feed_cache: dict[str, tuple[float, str]] = {}
_feed_failed: dict[str, float] = {}
FEED_TTL = 300          # seconds: a calendar is fetched at most every five minutes (some networks resolve its host slowly)


def _feed_text(url: str) -> str:
    import time as _time

    hit = _feed_cache.get(url)
    if hit and _time.time() - hit[0] < FEED_TTL:
        return hit[1]
    failed = _feed_failed.get(url, 0.0)
    if _time.time() - failed < 90:                  # it just failed: don't hold everything up retrying every minute
        if hit:
            return hit[1]
        raise OSError("the calendar feed could not be reached a moment ago")
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except Exception:
        _feed_failed[url] = _time.time()
        if hit:                                 # the network hiccuped: the last copy is better than nothing
            return hit[1]
        raise
    _feed_cache[url] = (_time.time(), resp.text)
    return resp.text


def feed_events(days: int, urls: list[str] | None = None) -> str:
    now = datetime.now()
    window_end = now + timedelta(days=days)
    start = datetime.combine(now.date(), datetime.min.time())
    found: list[tuple[datetime, str, str]] = []
    problems = []
    for url in urls or feed_urls():
        try:
            found.extend(parse_ics(_feed_text(url), start, window_end))
        except Exception as exc:
            problems.append(f"Couldn't read one calendar feed ({type(exc).__name__}).")
    found.sort(key=lambda e: e[0])
    lines = [f"{when:%a %d %b} {clock}  {label}" for when, label, clock in found[:60]]
    return "\n".join(lines + problems) or f"No events in the next {days} days."


# --------------------------------------------------------------------------- tools
def register(reg: Registry) -> None:
    mail = partial(reg.tool, enabled=lambda: mail_settings() is not None)

    @mail(
        "mail_list",
        "List emails from the user's inbox over IMAP (unread by default). Looking never marks mail as read.",
        {
            "unread_only": {"type": "boolean", "description": "Only unread mail (default true)."},
            "sender": {"type": "string", "description": "Only mail from this address or name."},
            "subject": {"type": "string", "description": "Only mail whose subject contains this."},
            "days": {"type": "integer", "description": "Only mail from the last N days."},
            "max_results": {"type": "integer"},
            "folder": {"type": "string", "description": "inbox (default), sent or drafts."},
        },
    )
    def mail_list(unread_only: bool = True, sender: str = "", subject: str = "", days: int = 0,
                  max_results: int = 10, folder: str = "inbox") -> str:
        return list_mail(mail_settings(), unread_only, sender, subject, days, max_results, folder)

    @mail("mail_read", "Read one email in full by its id (ids come from mail_list).",
          {"message_id": {"type": "string"},
           "folder": {"type": "string", "description": "Folder the id came from (default inbox)."}},
          required=("message_id",))
    def mail_read(message_id: str, folder: str = "inbox") -> str:
        return read_mail(mail_settings(), message_id, folder)

    @mail(
        "mail_send",
        "Send an email from the user's own address. The user is shown the whole message and must approve it.",
        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        required=("to", "subject", "body"),
        confirm=lambda a: f"Send email to {a['to']}\n  Subject: {a['subject']}\n  ---\n{a['body']}\n  ---",
    )
    def mail_send(to: str, subject: str, body: str) -> str:
        return send_mail(mail_settings(), to, subject, body)

    @reg.tool(
        "calendar_feed",
        "List upcoming events from the user's calendar feeds (Google Calendar, timetable or other iCal links).",
        {"days": {"type": "integer", "description": "How many days ahead (default 7)."}},
        enabled=lambda: bool(feed_urls()),
    )
    def calendar_feed(days: int = 7) -> str:
        return feed_events(max(1, min(int(days), 90)))
