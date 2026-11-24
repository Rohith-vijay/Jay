import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jarvis.grants import Grants, category_of, parse_duration
from jarvis.telegram import TelegramLink
from jarvis.tools.registry import Registry


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.grants = Grants(Path(self.dir.name), clock=self.clock)

    def test_durations_parse(self):
        self.assertEqual(parse_duration("for 8h"), 480)
        self.assertEqual(parse_duration("2 days"), 2880)
        self.assertEqual(parse_duration("45 min"), 45)
        self.assertIsNone(parse_duration("mail please"))

    def test_a_grant_covers_its_tools_and_then_expires(self):
        self.assertIn("Allowed", self.grants.command("/allow mail for 2h"))
        self.assertIsNotNone(self.grants.allows("mail_send", "Send email to x@y.com"))
        self.assertIsNotNone(self.grants.allows("gmail_send", "Send email to x@y.com"))
        self.assertIsNone(self.grants.allows("write_file", "Create file a.txt"))
        self.clock.now += 3 * 3600
        self.assertIsNone(self.grants.allows("mail_send", "Send email to x@y.com"))

    def test_only_when_mentions_narrows_it(self):
        self.grants.command("/allow mail only @kluniversity.in for 1d")
        self.assertIsNotNone(self.grants.allows("mail_send", "Send email to prof@kluniversity.in\nSubject: hi"))
        self.assertIsNone(self.grants.allows("mail_send", "Send email to stranger@evil.example"))

    def test_risky_kinds_are_short_and_never_forever(self):
        reply = self.grants.command("/allow shell")
        self.assertIn("2h", reply)
        self.assertIn("can't be granted forever", self.grants.command("/allow shell forever"))
        self.grants.command("/allow power for 30d")
        self.assertLessEqual(self.grants.items[-1].expires - self.clock.now, 24 * 3600)

    def test_forever_works_for_ordinary_kinds(self):
        self.grants.command("/allow files forever")
        self.clock.now += 400 * 86400
        self.assertIsNotNone(self.grants.allows("write_file", "Create file a.txt"))

    def test_revoke(self):
        self.grants.command("/allow mail")
        self.grants.command("/allow files")
        self.assertIn("Removed 1", self.grants.command("/revoke mail"))
        self.assertIsNone(self.grants.allows("mail_send", "x"))
        self.assertIn("Removed 1", self.grants.command("/revoke all"))
        self.assertIn("No standing", self.grants.describe())

    def test_permissions_cannot_grant_themselves(self):
        self.assertIn("never be granted", self.grants.command("/allow grant_permission"))
        self.assertIsNone(self.grants.allows("grant_permission", "anything"))

    def test_grants_survive_a_restart_and_unknown_commands_pass_through(self):
        self.grants.command("/allow browser for 3h")
        again = Grants(Path(self.dir.name), clock=self.clock)
        self.assertIsNotNone(again.allows("browser_click", "Click Submit"))
        self.assertIsNone(again.command("/status"))

    def test_registry_skips_the_card_only_for_granted_actions(self):
        reg = Registry()
        reg.grants = self.grants
        ran = []
        reg.tool("mail_send", "x", {"to": {"type": "string"}}, required=("to",),
                 confirm=lambda a: f"Send email to {a['to']}")(lambda to: ran.append(to) or "sent")
        asked = []

        def deny(summary):
            asked.append(summary)
            return False

        self.assertIn("declined", reg.execute("mail_send", {"to": "a@b.com"}, deny).text)
        self.assertEqual(len(asked), 1)
        self.grants.command("/allow mail")
        self.assertEqual(reg.execute("mail_send", {"to": "a@b.com"}, deny).text, "sent")
        self.assertEqual(len(asked), 1)                            # not asked again
        self.assertTrue(self.grants.log_path.exists())             # but recorded
        self.assertEqual(reg.last_confirm[0], "mail_send")

    def test_categories(self):
        self.assertEqual(category_of("mail_send"), "mail")
        self.assertEqual(category_of("mcp_github_create_issue"), "external")
        self.assertIsNone(category_of("calculate"))


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.messages, self.commands, self.approvals = [], [], []
        self.link = TelegramLink("T", Path(self.dir.name), on_message=lambda t, c: self.messages.append(t),
                                 on_info=lambda t: None,
                                 on_approval=lambda i, c: self.approvals.append((i, c)) or "ok",
                                 on_command=lambda t: self.commands.append(t) or ("handled" if t.startswith("/allow") else None))
        self.link.chat_id = 42
        self.calls = []
        self.link._call = lambda method, **p: self.calls.append((method, p)) or {"message_id": 7}

    def test_commands_are_handled_at_once_and_other_text_is_queued(self):
        self.link.handle({"chat": {"id": 42}, "text": "/allow mail for 2h"})
        self.assertEqual(self.messages, [])
        self.assertEqual(self.calls[-1][1]["text"], "handled")
        self.link.handle({"chat": {"id": 42}, "text": "/unknown"})
        self.link.handle({"chat": {"id": 42}, "text": "build me an app"})
        self.assertEqual(self.messages, ["/unknown", "build me an app"])

    def test_other_chats_are_ignored(self):
        self.link.handle({"chat": {"id": 99}, "text": "/allow shell"})
        self.link.handle_callback({"id": "1", "data": "ap:1:y", "message": {"chat": {"id": 99}}})
        self.assertEqual(self.commands, [])
        self.assertEqual(self.approvals, [])

    def test_button_press_reaches_the_session(self):
        self.link.handle_callback({"id": "c1", "data": "ap:5:g", "message": {"chat": {"id": 42}}})
        self.assertEqual(self.approvals, [(5, "g")])
        self.assertEqual(self.calls[-1][0], "answerCallbackQuery")
        self.link.handle_callback({"id": "c2", "data": "garbage", "message": {"chat": {"id": 42}}})
        self.assertEqual(len(self.approvals), 1)

    def test_ask_sends_buttons_and_settle_removes_them(self):
        self.link.ask(3, "Send email to x", "Allow mail for 24h")
        method, params = self.calls[-1]
        buttons = params["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons], ["ap:3:y", "ap:3:g", "ap:3:n"])
        self.link.settle(3, "Allowed")
        self.assertEqual(self.calls[-2][0], "editMessageReplyMarkup")


class SessionApprovalTests(unittest.TestCase):
    """The approval flow itself, with the phone standing in for the user."""

    def make(self):
        from jarvis.config import Config
        from jarvis.session import Session

        cfg = Config.load(data_dir=Path(self.dir.name))
        cfg.checkin_minutes, cfg.morning_brief = 0, False
        session = Session(cfg)
        self.addCleanup(session.agent.store._db.close)
        sent = []
        session.telegram = mock.Mock(chat_id=42)
        session.telegram.ask = lambda i, s, label="": sent.append((i, s, label))
        session.telegram.settle = lambda i, text: sent.append(("settled", i, text))
        return session, sent

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_a_request_from_the_phone_is_approved_from_the_phone(self):
        session, sent = self.make()
        session._phone_turn = session._background = True
        session.agent.registry.last_confirm = ("mail_send", "Send email")
        result = []
        thread = threading.Thread(target=lambda: result.append(session.confirm("Send email to a@b.com")))
        thread.start()
        for _ in range(100):
            if sent:
                break
            threading.Event().wait(0.05)
        self.assertEqual(sent[0][1], "Send email to a@b.com")
        self.assertIn("mail", sent[0][2])
        session.on_phone_approval(sent[0][0], "y")
        thread.join(5)
        self.assertEqual(result, [True])

    def test_the_always_button_creates_a_grant_and_allows(self):
        session, sent = self.make()
        session._phone_turn = session._background = True
        session.agent.registry.last_confirm = ("mail_send", "Send email")
        result = []
        thread = threading.Thread(target=lambda: result.append(session.confirm("Send email to a@b.com")))
        thread.start()
        for _ in range(100):
            if sent:
                break
            threading.Event().wait(0.05)
        reply = session.on_phone_approval(sent[0][0], "g")
        thread.join(5)
        self.assertEqual(result, [True])
        self.assertIn("mail", reply)
        self.assertIsNotNone(session.agent.registry.grants.allows("mail_send", "Send email to anyone"))

    def test_deny_and_stop(self):
        session, sent = self.make()
        session._phone_turn = session._background = True
        result = []
        thread = threading.Thread(target=lambda: result.append(session.confirm("Delete everything")))
        thread.start()
        for _ in range(100):
            if sent:
                break
            threading.Event().wait(0.05)
        self.assertIn("Stopped", session.phone_command("/stop"))
        thread.join(5)
        self.assertEqual(result, [False])

    def test_status_and_grant_commands_from_the_phone(self):
        session, _ = self.make()
        self.assertIn("Allowed", session.phone_command("/allow files for 1h"))
        status = session.phone_command("/status")
        self.assertIn("Jay is", status)
        self.assertIn("files", status)

    def test_background_work_still_never_pops_up_or_acts_alone(self):
        session, sent = self.make()
        session._background = True                    # a watch or check-in, not a phone request
        self.assertFalse(session.confirm("Send email to a@b.com"))


if __name__ == "__main__":
    unittest.main()
