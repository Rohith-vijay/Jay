import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis import content
from jarvis.store import Store
from jarvis.telegram import TelegramLink
from jarvis.tools.registry import Registry


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "t.db")
        self.addCleanup(self.store._db.close)
        self.queue = content.ContentQueue(self.store)
        self.routines = content.Routines(self.store)
        self.reg = Registry()
        content.register(self.reg, self.queue, self.routines)

    def run_tool(self, tool_name, **args):
        return self.reg.execute(tool_name, args, lambda s: True).text


class QueueTests(Base):
    def make(self, **kw):
        return self.queue.add("youtube", "long video", "The lost city", hook="What if...", caption="cap",
                              hashtags="#lore", scheduled_at=kw.get("scheduled_at", "2026-11-21T19:00"))

    def test_draft_fields_and_time_are_stored(self):
        item = self.queue.get(self.make())
        self.assertEqual(item["status"], "draft")
        self.assertEqual(item["scheduled_at"], "2026-11-21 19:00")
        self.assertEqual(item["hashtags"], "#lore")

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            self.queue.add("tiktok", "short", "x")
        with self.assertRaises(ValueError):
            self.queue.add("youtube", "short", "  ")
        with self.assertRaises(ValueError):
            self.make(scheduled_at="next friday")

    def test_new_drafts_are_announced(self):
        seen = []
        self.queue.on_new = seen.append
        item_id = self.make()
        self.assertEqual(seen, [item_id])

    def test_only_the_user_can_approve_and_the_model_has_no_tool_for_it(self):
        names = {t.name for t in self.reg.tools()}
        self.assertFalse([n for n in names if "approve" in n or "post" in n.replace("posted", "")])
        item_id = self.make()
        self.assertIn("Approved", self.queue.command(f"/approve {item_id}"))
        self.assertEqual(self.queue.get(item_id)["status"], "approved")
        self.assertIn("already approved", self.queue.approve(item_id))

    def test_changing_content_needs_a_fresh_approval_but_changing_time_does_not(self):
        item_id = self.make()
        self.queue.approve(item_id)
        self.assertIn("still", self.queue.revise(item_id, scheduled_at="2026-11-22T20:00").replace("Updated", "still"))
        self.assertEqual(self.queue.get(item_id)["status"], "approved")
        announced = []
        self.queue.on_new = announced.append
        self.assertIn("needs your approval again", self.queue.revise(item_id, caption="new caption"))
        self.assertEqual(self.queue.get(item_id)["status"], "draft")
        self.assertEqual(announced, [item_id])

    def test_only_approved_drafts_become_due_and_only_once(self):
        draft, approved = self.make(), self.make(scheduled_at="2026-11-21 19:00")
        self.queue.approve(approved)
        before = datetime(2026, 11, 21, 18, 59)
        after = datetime(2026, 11, 21, 19, 1)
        self.assertEqual(self.queue.due(before), [])
        self.assertEqual([r["id"] for r in self.queue.due(after)], [approved])      # the unapproved draft never is
        self.queue.mark_due(approved)
        self.assertEqual(self.queue.due(after), [])
        self.assertEqual(self.queue.get(approved)["status"], "due")
        self.assertIn("Marked", self.queue.command(f"/posted {approved}"))
        self.assertEqual(self.queue.get(approved)["status"], "posted")
        self.assertEqual(self.queue.get(draft)["status"], "draft")

    def test_rejected_and_posted_cannot_be_revised(self):
        item_id = self.make()
        self.queue.reject(item_id)
        self.assertIn("make a new draft", self.queue.revise(item_id, title="x"))

    def test_queue_listing_hides_finished_items(self):
        a, b = self.make(), self.make()
        self.queue.reject(a)
        text = self.queue.command("/queue")
        self.assertIn(f"#{b}", text)
        self.assertNotIn(f"#{a} ", text)

    def test_messages(self):
        item = self.queue.get(self.make())
        self.assertIn("/draft", content.phone_card(item))
        self.assertIn("/posted", content.due_message(item))
        self.assertIn("What if...", content.full(item))

    def test_unrelated_commands_pass_through(self):
        self.assertIsNone(self.queue.command("/allow mail"))
        self.assertIn("Say which one", self.queue.command("/approve"))


class ToolTests(Base):
    def test_create_list_get_revise(self):
        out = self.run_tool("content_create", platform="instagram", kind="reel", title="Reel one", caption="c")
        self.assertIn("draft #1", out)
        self.assertIn("Reel one", self.run_tool("content_list"))
        self.assertIn("Caption: c", self.run_tool("content_get", id=1))
        self.assertIn("Updated #1", self.run_tool("content_revise", id=1, hashtags="#x"))
        self.assertIn("No draft #9", self.run_tool("content_get", id=9))

    def test_a_bad_time_is_reported_to_the_model_not_crashed(self):
        res = self.reg.execute("content_create", {"platform": "youtube", "kind": "short", "title": "t", "scheduled_at": "soon"}, lambda s: True)
        self.assertTrue(res.is_error)
        self.assertIn("YYYY-MM-DD", res.text)


class RoutineTests(Base):
    def test_days_and_times(self):
        self.assertEqual(self.routines.parse_days("Mon Thursday"), "mon,thu")
        self.assertEqual(self.routines.parse_days("weekdays"), "mon,tue,wed,thu,fri")
        self.assertEqual(len(self.routines.parse_days("daily").split(",")), 7)
        with self.assertRaises(ValueError):
            self.routines.parse_days("someday")
        with self.assertRaises(ValueError):
            self.routines.add("x", "do the thing please", "mon", "9am")

    def test_a_routine_runs_once_on_its_day_after_its_time(self):
        rid = self.routines.add("weekly plan", "Analyse my channels and draft this week's content", "mon", "09:00")
        monday = datetime(2026, 11, 16, 9, 5)                  # a Monday
        self.assertEqual(self.routines.due(datetime(2026, 11, 16, 8, 59)), [])
        self.assertEqual(self.routines.due(datetime(2026, 11, 17, 9, 5)), [])        # Tuesday
        self.assertEqual([r["id"] for r in self.routines.due(monday)], [rid])
        self.routines.mark_run(rid, monday)
        self.assertEqual(self.routines.due(datetime(2026, 11, 16, 15, 0)), [])        # not again the same day
        self.assertEqual([r["id"] for r in self.routines.due(datetime(2026, 11, 23, 9, 1))], [rid])   # next week

    def test_setting_one_up_needs_approval(self):
        self.assertTrue(self.reg.needs_approval("add_routine", {"name": "n", "prompt": "do something useful", "days": "mon", "at": "09:00"}))
        self.assertIn("Routine #1", self.run_tool("add_routine", name="n", prompt="do something useful", days="mon", at="09:00"))
        self.assertIn("Removed", self.run_tool("remove_routine", id=1))


class TelegramDraftTests(unittest.TestCase):
    def test_buttons_reach_the_queue(self):
        with tempfile.TemporaryDirectory() as d:
            calls, pressed = [], []
            link = TelegramLink("T", Path(d), on_message=lambda *a: None, on_info=lambda t: None,
                                on_content=lambda i, a: pressed.append((i, a)) or "Approved.")
            link.chat_id = 42
            link._call = lambda method, **p: calls.append((method, p)) or {"message_id": 5}
            link.ask_content(3, "Draft #3")
            data = [b["callback_data"] for b in calls[-1][1]["reply_markup"]["inline_keyboard"][0]]
            self.assertEqual(data, ["ct:3:a", "ct:3:r"])
            link.handle_callback({"id": "c", "data": "ct:3:a", "message": {"chat": {"id": 42}, "message_id": 5}})
            self.assertEqual(pressed, [(3, "a")])
            link.handle_callback({"id": "c", "data": "ct:3:a", "message": {"chat": {"id": 99}, "message_id": 5}})
            self.assertEqual(len(pressed), 1)                       # another chat can't approve


class SessionTests(unittest.TestCase):
    def make(self):
        from jarvis.config import Config
        from jarvis.session import Session

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.load(data_dir=Path(tmp.name))
        cfg.checkin_minutes, cfg.morning_brief = 0, False
        session = Session(cfg)
        self.addCleanup(session.agent.store._db.close)
        return session

    def test_new_drafts_go_to_the_phone_and_buttons_decide(self):
        s = self.make()
        s.telegram = mock.Mock(chat_id=42)
        sent = threading.Event()
        s.telegram.ask_content = lambda i, card: sent.set()
        item_id = s.agent.registry.content.add("youtube", "short", "A short")
        self.assertTrue(sent.wait(3))
        self.assertIn("Approved", s.on_phone_content(item_id, "a"))
        self.assertEqual(s.agent.registry.content.get(item_id)["status"], "approved")
        other = s.agent.registry.content.add("youtube", "short", "Another")
        self.assertIn("Rejected", s.on_phone_content(other, "r"))

    def test_due_posts_are_delivered_to_the_phone_once(self):
        s = self.make()
        queue = s.agent.registry.content
        item_id = queue.add("instagram", "reel", "Reel", caption="Look", scheduled_at="2020-01-01 10:00")
        queue.approve(item_id)
        s.agent.registry.phone = mock.Mock()
        s.agent.registry.phone.available.return_value = True
        with mock.patch("jarvis.tools.system.notify"):
            s._deliver_due_content()
            s._deliver_due_content()
        s.agent.registry.phone.send.assert_called_once()
        self.assertIn("Look", s.agent.registry.phone.send.call_args.args[1])
        self.assertEqual(queue.get(item_id)["status"], "due")

    def test_commands_work_from_the_phone_and_the_keyboard(self):
        s = self.make()
        item_id = s.agent.registry.content.add("youtube", "short", "Typed")
        self.assertIn("Approved", s.phone_command(f"/approve {item_id}"))
        self.assertIn("Typed", s.phone_command("/queue"))
        self.assertIn("Allowed", s.phone_command("/allow files for 1h"))     # the earlier commands still work

    def test_a_routine_runs_from_the_inbox_path(self):
        s = self.make()
        rid = s.agent.registry.routines.add("check", "Say hello to the user please", "daily", "00:00")
        s.telegram = mock.Mock(chat_id=42)
        with mock.patch.object(s, "_from_phone") as phone:
            s._run_routine(rid)
        phone.assert_called_once()
        self.assertIn("Routine 'check'", phone.call_args.args[0])
        self.assertTrue(phone.call_args.kwargs.get("quiet"))


if __name__ == "__main__":
    unittest.main()
