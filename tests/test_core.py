"""Offline tests: tools, memory, voice logic, and each brain's tool loop against scripted fakes."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np

from jarvis.agent import Agent, build_system_prompt
from jarvis.config import Config
from jarvis.providers import NO_VISION, ClaudeProvider, OpenAICompatProvider, ProviderUnavailable
from jarvis.tools import pc, web
from jarvis.tools.registry import ToolImage
from jarvis.voice import UtteranceDetector, extract_command, speakable

YES = lambda summary: True      # noqa: E731
QUIET = lambda name: None       # noqa: E731


def text(t):
    return NS(type="text", text=t)


def tool_use(name, args, id="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id)


class FakeClaude:
    """Stands in for anthropic.Anthropic(); replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class FakeCall:
    """An OpenAI-style tool call, with an extra provider field that must survive the round trip."""

    def __init__(self, name, args, id="call_1"):
        self.id = id
        self.function = NS(name=name, arguments=args if isinstance(args, str) else json.dumps(args))

    def model_dump(self, exclude_none=True):
        return {"id": self.id, "type": "function", "extra_content": {"signature": "abc"},
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class FakeOpenAI:
    def __init__(self, replies):
        """replies: list of (content, [FakeCall, ...]) or an Exception to raise."""
        self.replies = list(replies)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content, calls = reply
        return NS(choices=[NS(message=NS(content=content, tool_calls=calls or None))])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cfg = Config.load(data_dir=Path(self.tmp.name))
        self.approvals = []
        self.allow = True

        def confirm(summary):
            self.approvals.append(summary)
            return self.allow

        self.agent = Agent(self.cfg, confirm)
        self.reg = self.agent.registry
        self.run_tool = lambda name, args: self.reg.execute(name, args, confirm)

    def tearDown(self):
        self.agent.store._db.close()
        self.tmp.cleanup()


class ToolTests(Base):
    def test_memory_roundtrip_and_prompt(self):
        self.assertFalse(self.run_tool("remember", {"fact": "Prefers tea over coffee", "category": "preference"}).is_error)
        self.assertIn("tea", self.run_tool("recall", {"query": "coffee"}).text)
        self.assertIn("Prefers tea", build_system_prompt(self.cfg, self.agent.store))
        self.run_tool("forget", {"memory_id": 1})
        self.assertIn("Nothing saved", self.run_tool("recall", {"query": "coffee"}).text)

    def test_tasks_and_reminders(self):
        past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="minutes")
        self.run_tool("add_task", {"title": "Call mom", "due": past})
        self.run_tool("add_task", {"title": "Buy milk"})
        self.assertEqual([t["title"] for t in self.agent.store.pop_due_reminders()], ["Call mom"])
        self.assertEqual(self.agent.store.pop_due_reminders(), [])  # announced only once
        self.run_tool("complete_task", {"task_id": 2})
        self.assertNotIn("Buy milk", self.run_tool("list_tasks", {}).text)

    def test_invalid_input_and_unknown_tool(self):
        self.assertTrue(self.run_tool("remember", {}).is_error)
        self.assertTrue(self.run_tool("remember", {"fact": "x", "extra": 1}).is_error)
        self.assertTrue(self.run_tool("remember", None).is_error)
        self.assertTrue(self.run_tool("nope", {}).is_error)
        self.assertTrue(self.run_tool("add_task", {"title": "x", "due": "tomorrow-ish"}).is_error)

    def test_file_tools_and_confirmation(self):
        target = Path(self.tmp.name) / "note.txt"
        self.allow = False
        self.assertIn("declined", self.run_tool("write_file", {"path": str(target), "content": "hi"}).text)
        self.assertFalse(target.exists())
        self.allow = True
        self.run_tool("write_file", {"path": str(target), "content": "hi"})
        self.assertEqual(self.run_tool("read_file", {"path": str(target)}).text, "hi")
        self.assertIn("note.txt", self.run_tool("list_directory", {"path": self.tmp.name}).text)
        self.assertIn("note.txt", self.run_tool("search_files", {"root": self.tmp.name, "pattern": "*.txt"}).text)
        self.assertEqual(len(self.approvals), 2)

    def test_powershell_needs_approval_and_runs(self):
        res = self.run_tool("run_powershell", {"command": "Write-Output hello"})
        self.assertFalse(res.is_error)
        self.assertIn("hello", res.text)
        self.assertIn("Write-Output hello", self.approvals[0])

    def test_system_status(self):
        res = self.run_tool("system_status", {})
        self.assertFalse(res.is_error, res.text)
        self.assertIn("Memory", res.text)

    def test_media_and_volume_send_the_right_keys(self):
        with mock.patch.object(pc, "press_key") as press:
            self.run_tool("media_control", {"action": "next"})
            self.run_tool("set_volume", {"percent": 30})
            self.assertTrue(self.run_tool("set_volume", {"percent": 130}).is_error)
        self.assertEqual(press.call_args_list, [
            mock.call(0xB0), mock.call(pc.VK_VOLUME_DOWN, times=50), mock.call(pc.VK_VOLUME_UP, times=15)])

    def test_weather_report(self):
        geo = {"results": [{"name": "Hyderabad", "country": "India", "latitude": 17.4, "longitude": 78.5}]}
        forecast = {
            "current": {"temperature_2m": 27.1, "apparent_temperature": 29.0, "relative_humidity_2m": 60,
                        "weather_code": 2, "wind_speed_10m": 9.0},
            "daily": {"time": ["2026-11-15"], "temperature_2m_max": [30], "temperature_2m_min": [19],
                      "precipitation_probability_max": [10], "weather_code": [61]},
        }
        with mock.patch.object(web.requests, "get", side_effect=[NS(json=lambda: geo), NS(json=lambda: forecast)]):
            out = self.run_tool("get_weather", {"location": "Hyderabad"}).text
        self.assertIn("Hyderabad, India: 27.1°C", out)
        self.assertIn("partly cloudy", out)
        self.assertIn("light rain, rain chance 10%", out)
        with mock.patch.object(web.requests, "get", return_value=NS(json=lambda: {})):
            self.assertIn("Couldn't find", self.run_tool("get_weather", {"location": "Zzz"}).text)

    def test_google_hidden_until_configured(self):
        names = lambda: {t.name for t in self.reg.tools()}  # noqa: E731
        self.assertNotIn("gmail_send", names())
        self.assertIn("not connected", build_system_prompt(self.cfg, self.agent.store))
        res = self.run_tool("calendar_list_events", {})
        self.assertTrue(res.is_error)
        self.assertIn("not set up", res.text)
        (Path(self.tmp.name) / "google_credentials.json").write_text("{}")
        self.assertIn("gmail_send", names())


class ScheduleTests(Base):
    def iso(self, **delta):
        return (datetime.now() + timedelta(**delta)).isoformat(timespec="minutes")

    def test_reschedule_reprioritise_and_remind_at_start(self):
        self.run_tool("add_task", {"title": "DBMS assignment", "due": self.iso(hours=5), "minutes": 90,
                                   "priority": "high"})
        self.assertEqual(self.agent.store.pop_due_reminders(), [])        # deadline is hours away
        out = self.run_tool("update_task", {"task_id": 1, "start": self.iso(minutes=-1)}).text
        self.assertIn("planned", out)
        self.assertIn("high priority", out)
        self.assertEqual(len(self.agent.store.pop_due_reminders()), 1)    # reminded at planned start
        self.run_tool("update_task", {"task_id": 1, "start": self.iso(minutes=-2), "title": "DBMS lab"})
        again = self.agent.store.pop_due_reminders()
        self.assertEqual([t["title"] for t in again], ["DBMS lab"])       # moving it re-arms the reminder
        self.run_tool("update_task", {"task_id": 1, "start": ""})
        self.assertIsNone(self.agent.store.get_task(1)["start"])
        self.assertTrue(self.run_tool("update_task", {"task_id": 1, "priority": "urgent"}).is_error)
        self.assertIn("No task", self.run_tool("update_task", {"task_id": 99, "title": "x"}).text)

    def test_delete_task(self):
        self.run_tool("add_task", {"title": "Old idea"})
        self.assertEqual(self.run_tool("delete_task", {"task_id": 1}).text, "Deleted.")
        self.assertEqual(self.agent.store.list_tasks(), [])

    def test_work_patterns_reflect_history(self):
        store = self.agent.store
        self.assertIn("No completed tasks", store.work_patterns())
        store.add_task("On time", due=self.iso(hours=2))
        store.add_task("Late one", due=self.iso(hours=-3), start=self.iso(hours=-4), minutes=30)
        store.add_task("Still open", due=self.iso(hours=-1))
        store.complete_task(1)
        store.complete_task(2)
        store.update_task(3, start=self.iso(hours=1))        # first planned time: planning, not postponing
        store.update_task(3, due=self.iso(hours=4))
        store.update_task(3, due=self.iso(hours=8))
        store.log_message("user", "hello")
        out = self.run_tool("get_work_patterns", {}).text
        self.assertIn("2 tasks completed; 2 had deadlines, 1 of those finished late", out)
        self.assertIn("ran over by 180 minutes", out)
        self.assertIn("8.0x the estimated time", out)
        self.assertIn("Most often postponed: Still open x2", out)
        self.assertIn("usually active around", out)

    def test_old_database_is_upgraded(self):
        import sqlite3

        from jarvis.store import Store
        path = Path(self.tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due TEXT, "
                   "done INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)")
        db.execute("INSERT INTO tasks (title, created) VALUES ('legacy', '2026-11-01T10:00')")
        db.commit()
        db.close()
        store = Store(path)
        self.assertEqual(store.list_tasks()[0]["priority"], "normal")
        store.update_task(1, start="2026-11-20T09:00", minutes=20)
        self.assertEqual(store.get_task(1)["minutes"], 20)
        store._db.close()

    def test_calculate(self):
        calc = lambda e: self.run_tool("calculate", {"expression": e})  # noqa: E731
        self.assertEqual(calc("(1250 * 0.18) + 40").text, "265.0")
        self.assertEqual(calc("2^10").text, "1024")
        self.assertEqual(calc("sqrt(144) + max(3, 7)").text, "19.0")
        self.assertTrue(calc("__import__('os').system('dir')").is_error)
        self.assertTrue(calc("9**9**9").is_error)

    def test_key_combos(self):
        self.assertEqual(pc.parse_combo("Ctrl + Shift + S"), [0x11, 0x10, ord("S")])
        self.assertEqual(pc.parse_combo("f5"), [0x74])
        with self.assertRaises(ValueError):
            pc.parse_combo("ctrl+banana")
        with mock.patch.object(pc, "press_combo") as press, mock.patch.object(pc, "type_unicode") as typed:
            self.allow = False
            self.run_tool("press_keys", {"keys": "ctrl+s"})
            self.run_tool("type_text", {"text": "నమస్తే"})
            press.assert_not_called()                  # nothing is typed without approval
            typed.assert_not_called()
            self.allow = True
            self.run_tool("type_text", {"text": "నమస్తే"})
            typed.assert_called_once_with("నమస్తే")


class KnowledgeTests(Base):
    def test_learn_search_and_notes(self):
        doc = Path(self.tmp.name) / "dbms-notes.txt"
        doc.write_text(
            "Normalization reduces redundancy.\n\nThird normal form removes transitive dependencies; "
            "BCNF is stricter.\n\n" + "Indexing speeds up lookups using B+ trees. " * 60
            + "\n\nA transaction has ACID properties: atomicity, consistency, isolation, durability.",
            encoding="utf-8")
        out = self.run_tool("learn_document", {"path": str(doc)}).text
        self.assertIn("Learned 'dbms-notes.txt'", out)
        found = self.run_tool("search_knowledge", {"query": "what are ACID properties?"}).text
        self.assertIn("[dbms-notes.txt]", found)
        self.assertIn("atomicity", found)
        self.assertLess(len(found), 6500)                       # passages, not the whole document
        self.run_tool("save_note", {"title": "standup", "text": "Ravi will send the API spec on Friday."})
        self.assertIn("Ravi", self.run_tool("search_knowledge", {"query": "API spec"}).text)
        self.run_tool("save_note", {"title": "standup", "text": "Spec moved to Monday."})   # replaces
        self.assertNotIn("Friday", self.run_tool("search_knowledge", {"query": "spec"}).text)
        listing = self.run_tool("list_knowledge", {}).text
        self.assertIn("dbms-notes.txt", listing)
        self.assertIn("note: standup (1 passages)", listing)
        self.run_tool("learn_document", {"path": str(doc)})     # learning again must not duplicate
        self.assertEqual(listing, self.run_tool("list_knowledge", {}).text)
        self.assertIn("Nothing in the knowledge base", self.run_tool("search_knowledge", {"query": "zebra"}).text)
        self.assertIn("Nothing", self.run_tool("search_knowledge", {"query": "?? !!"}).text)
        self.assertEqual(self.run_tool("remove_knowledge", {"source": "note: standup"}).text, "Removed.")

    def test_telugu_notes_are_searchable(self):
        self.run_tool("save_note", {"title": "amma", "text": "అమ్మ పుట్టినరోజు డిసెంబర్ 12న ఉంది."})
        self.assertIn("డిసెంబర్", self.run_tool("search_knowledge", {"query": "పుట్టినరోజు"}).text)

    def test_passage_splitting(self):
        from jarvis.store import split_passages
        parts = split_passages("a" * 50 + "\n\n" + "word " * 700 + "\n\nend", size=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertGreaterEqual(len(parts), 4)
        self.assertTrue(parts[-1].endswith("end"))
        self.assertEqual(parts[0], "a" * 50)

    def test_site_change_detection(self):
        from jarvis.tools.browser import site_changes
        store = self.agent.store
        first = site_changes(store, "ERP notices", "Notices", "Exam on 20 Nov\nFee due 30 Nov")
        self.assertIn("First look", first)
        self.assertIn("No changes", site_changes(store, "erp notices", "Notices", "Exam on 20 Nov\n  Fee due 30 Nov "))
        new = site_changes(store, "ERP notices", "Notices", "Lab record submission 18 Nov\nExam on 20 Nov\nFee due 30 Nov")
        self.assertIn("Lab record submission 18 Nov", new)
        self.assertNotIn("Fee due", new)


class WhatsAppTests(Base):
    class FakePage:
        """Just enough of a browser page for the chat-matching logic."""
        url = "https://web.whatsapp.com/"

        def __init__(self, names):
            self.names, self.clicked, self.keys = names, [], []
            self.keyboard = NS(insert_text=self.keys.append, press=self.keys.append)

        def wait_for_selector(self, *a, **k): pass
        def wait_for_timeout(self, *a): pass
        def get_by_role(self, role): return NS(first=NS(click=lambda: None))
        def eval_on_selector_all(self, sel, js):
            return [{"name": n, "unread": 0, "preview": n} for n in self.names]

        def query_selector_all(self, sel):
            return [NS(query_selector=lambda s, n=n: NS(get_attribute=lambda a: n),
                       click=lambda n=n: self.clicked.append(n)) for n in self.names]

        def query_selector(self, sel):
            return NS(inner_text=lambda: self.clicked[-1] if self.clicked else "")

    def test_sending_needs_approval_and_is_never_automatic(self):
        self.allow = False
        res = self.run_tool("whatsapp_send", {"chat": "Amma", "message": "vasthunna"})
        self.assertIn("declined", res.text)                       # refused before the browser is even touched
        self.assertEqual(self.approvals, ["Send on WhatsApp to Amma:\nvasthunna"])
        self.assertTrue(self.run_tool("whatsapp_send", {"chat": "Amma"}).is_error)

    def test_chat_matching_is_strict(self):
        wa = self.reg.whatsapp
        page = self.FakePage(["Amma", "Ravi CSE", "Ravi Kumar", "CSE Batch 2027"])
        self.assertEqual(wa.open_chat(page, "amma"), "Amma")
        self.assertEqual(page.clicked, ["Amma"])
        self.assertEqual(wa.open_chat(page, "Ravi CSE"), "Ravi CSE")       # exact name beats partial matches
        with self.assertRaises(ValueError) as two:
            wa.open_chat(page, "ravi")                                     # ambiguous: must ask, not guess
        self.assertIn("Ravi CSE, Ravi Kumar", str(two.exception))
        with self.assertRaises(ValueError):
            wa.open_chat(page, "Suresh")
        self.assertEqual(page.clicked, ["Amma", "Ravi CSE"])               # nothing opened on the failures

    def test_multiline_message_does_not_send_early(self):
        wa = self.reg.whatsapp
        page = self.FakePage(["Amma"])
        page.query_selector = lambda sel: NS(click=lambda: None)
        wa.type_message(page, "line one\nline two")
        self.assertEqual(page.keys, ["line one", "Shift+Enter", "line two"])

    def test_lean_brain_can_switch_whatsapp_on(self):
        self.assertIn("whatsapp", self.reg.available_groups())
        self.assertNotIn("whatsapp_send", [t.name for t in self.reg.tools(groups=set())])
        self.assertIn("whatsapp_send", [t.name for t in self.reg.tools(groups={"whatsapp"})])


class ContinuityTests(Base):
    def brain_agent(self, *replies):
        log, queue_ = [], list(replies)

        class Brain:
            name = "gemini"
            def __init__(self): self.seeded = None
            def seed(self, transcript): self.seeded = list(transcript)
            def turn(self, text, confirm, notice):
                log.append(text)
                return queue_.pop(0)

        brains = []

        def factory(cfg, reg, system):
            brains.append(Brain())
            return brains[-1]

        self.cfg.provider, self.cfg.fallback_order = "gemini", ["gemini"]
        return Agent(self.cfg, YES, store=self.agent.store, providers={"gemini": factory}), log, brains

    def test_new_session_starts_from_the_last_conversation(self):
        first, _, _ = self.brain_agent("Your OS exam is Thursday.")
        first.ask("when is my OS exam?")
        second, _, brains = self.brain_agent("As I said, Thursday.")
        self.assertEqual(second.transcript[-2:], [("user", "when is my OS exam?"), ("assistant", "Your OS exam is Thursday.")])
        second.ask("remind me again")
        self.assertEqual(brains[0].seeded[0], ("user", "when is my OS exam?"))    # the brain was given the history

    def test_morning_brief_once_a_day(self):
        agent, log, _ = self.brain_agent("Good morning. Nothing urgent today.", "unused")
        self.assertEqual(agent.morning(), "Good morning. Nothing urgent today.")
        self.assertIsNone(agent.morning())                       # not again today
        self.assertEqual(len(log), 1)
        self.assertEqual(agent.transcript[-1], ("assistant", "Good morning. Nothing urgent today."))
        self.assertEqual(agent.store.unreviewed_log()[0], "")    # app-made turns aren't logged as the user's


class ProactiveTests(Base):
    def agent_saying(self, *replies):
        log, queue = [], list(replies)

        class Brain:
            name = "gemini"
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                log.append(text)
                return queue.pop(0)

        self.cfg.provider, self.cfg.fallback_order = "gemini", ["gemini"]
        agent = Agent(self.cfg, YES, store=self.agent.store, providers={"gemini": lambda c, r, s: Brain()})
        return agent, log

    def test_check_in_stays_quiet_unless_needed(self):
        agent, log = self.agent_saying("NOTHING.", "I moved the DBMS work to 6 pm.")
        self.assertIsNone(agent.check_in())            # no tasks: no request is even made
        self.assertEqual(log, [])
        agent.store.add_task("DBMS")
        self.assertIsNone(agent.check_in())
        self.assertEqual(agent.check_in(), "I moved the DBMS work to 6 pm.")
        self.assertEqual(agent.store.unreviewed_log()[0], "")   # check-ins are not logged as user talk

    def test_reflect_runs_once_a_day_on_new_conversation(self):
        agent, log = self.agent_saying("Hi!", "You study late at night.", "unused")
        self.assertIsNone(agent.reflect())             # nothing to review yet
        agent.ask("plan my evening")
        learned = agent.reflect()
        self.assertEqual(learned, "You study late at night.")
        self.assertIn("plan my evening", log[-1])      # the conversation was handed over for review
        self.assertIn("STATISTICS", log[-1])
        self.assertEqual(agent.transcript, [("user", "plan my evening"), ("assistant", "Hi!")])  # review turn dropped
        self.assertIsNone(agent.reflect())             # already done today
        self.assertEqual(agent.store.unreviewed_log()[0], "")


class ClaudeTests(Base):
    def test_tool_loop(self):
        client = FakeClaude([
            NS(stop_reason="tool_use", content=[text("One moment."), tool_use("add_task", {"title": "Ship it"})]),
            NS(stop_reason="end_turn", content=[text("Added.")]),
        ])
        provider = ClaudeProvider(self.cfg, self.reg, "sys", client=client)
        seen = []
        self.assertEqual(provider.turn("add a task", YES, seen.append), "Added.")
        self.assertEqual(seen, ["says: One moment.", 'add_task {"title": "Ship it"}'])  # visible trace
        self.assertEqual(self.agent.store.list_tasks()[0]["title"], "Ship it")
        result = client.calls[1]["messages"][-1]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result["is_error"]), ("tool_result", "tu_1", False))
        names = [t["name"] for t in client.calls[0]["tools"]]
        self.assertEqual(names.count("web_search"), 1)       # hosted tool replaces the local one
        self.assertNotIn("fetch_page", names)
        self.assertEqual(client.calls[0]["fallbacks"], "default")

    def test_small_model_gets_compatible_settings(self):
        self.cfg.claude_model = "claude-haiku-5-5"
        client = FakeClaude([NS(stop_reason="end_turn", content=[text("Hi.")])])
        provider = ClaudeProvider(self.cfg, self.reg, "sys", client=client)
        provider.turn("hello", YES, QUIET)
        call = client.calls[0]
        self.assertNotIn("fallbacks", call)                                  # Haiku has no refusal fallback
        hosted = [t["type"] for t in call["tools"] if t.get("name") in ("web_search", "web_fetch")]
        self.assertEqual(hosted, ["web_search_20250305", "web_fetch_20250910"])

    def test_refusal_and_cutoff_keep_history_valid(self):
        client = FakeClaude([
            NS(stop_reason="refusal", content=[]),
            NS(stop_reason="max_tokens", content=[text("Partial"), tool_use("add_task", {"title": "x"})]),
        ])
        provider = ClaudeProvider(self.cfg, self.reg, "sys", client=client)
        self.assertIn("can't help", provider.turn("bad", YES, QUIET))
        self.assertEqual(provider.messages, [])
        self.assertIn("cut off", provider.turn("long", YES, QUIET))
        self.assertEqual(self.agent.store.list_tasks(), [])   # the truncated tool call never ran
        self.assertEqual(provider.messages[-1], {"role": "assistant", "content": "Partial"})

    def test_image_is_sent_as_image_block(self):
        self.reg.tool("snap", "take a picture")(lambda: ToolImage("pic", b"JPEGDATA"))
        client = FakeClaude([
            NS(stop_reason="tool_use", content=[tool_use("snap", {})]),
            NS(stop_reason="end_turn", content=[text("I see it.")]),
        ])
        ClaudeProvider(self.cfg, self.reg, "sys", client=client).turn("look", YES, QUIET)
        content = client.calls[1]["messages"][-1]["content"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["text", "image"])
        self.assertEqual(content[1]["source"]["media_type"], "image/jpeg")


class OpenAICompatTests(Base):
    def provider(self, replies, preset="gemini"):
        client = FakeOpenAI(replies)
        return OpenAICompatProvider(self.cfg, self.reg, "sys", preset, client=client), client

    def test_tool_loop_and_roundtrip(self):
        provider, client = self.provider([
            ("", [FakeCall("add_task", {"title": "Ship it"})]),
            ("Added.", None),
        ])
        self.assertEqual(provider.turn("add a task", YES, QUIET), "Added.")
        self.assertEqual(self.agent.store.list_tasks()[0]["title"], "Ship it")
        sent = client.calls[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
        self.assertEqual(sent[2]["tool_calls"][0]["extra_content"], {"signature": "abc"})
        self.assertEqual(sent[3]["tool_call_id"], "call_1")
        self.assertIn("web_search", [t["function"]["name"] for t in client.calls[0]["tools"]])

    def test_a_replaced_request_runs_no_tools_and_leaves_no_history(self):
        from jarvis.tools.registry import Superseded
        provider, client = self.provider([("", [FakeCall("add_task", {"title": "Misheard task"})]), ("later", None)])
        original = client._create

        def replaced_while_waiting(**kwargs):        # the user speaks again while the model is still answering
            reply = original(**kwargs)
            self.reg.generation += 1
            provider.abandon()
            return reply

        client.chat.completions.create = replaced_while_waiting
        with self.assertRaises(Superseded):
            provider.turn("add a misheard task", YES, QUIET)
        self.assertEqual(self.agent.store.list_tasks(), [])            # the tool never ran
        self.assertEqual(provider.turns, [])                           # and the next request starts clean
        client.chat.completions.create = original
        self.assertEqual(provider.turn("hello", YES, QUIET), "later")

    def test_bad_json_arguments_are_reported_not_run(self):
        provider, client = self.provider([("", [FakeCall("add_task", "{not json")]), ("Sorry.", None)])
        provider.turn("add", YES, QUIET)
        self.assertIn("must be a JSON object", client.calls[1]["messages"][-1]["content"])
        self.assertEqual(self.agent.store.list_tasks(), [])

    def test_old_turns_are_dropped(self):
        self.cfg.history_turns = 2
        provider, client = self.provider([("a", None), ("b", None), ("c", None)])
        for q in ("one", "two", "three"):
            provider.turn(q, YES, QUIET)
        users = [m["content"] for m in client.calls[2]["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["two", "three"])

    def test_images_by_vision_support(self):
        self.reg.tool("snap", "take a picture")(lambda: ToolImage("pic", b"JPEGDATA"))
        script = lambda: [("", [FakeCall("snap", {})]), ("ok", None)]  # noqa: E731
        provider, client = self.provider(script(), "gemini")
        provider.turn("look", YES, QUIET)
        last = client.calls[1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        provider, client = self.provider(script(), "groq")
        provider.turn("look", YES, QUIET)
        self.assertTrue(client.calls[1]["messages"][-1]["content"].endswith(NO_VISION))

    def test_malformed_tool_call_is_retried_then_gives_up(self):
        import openai
        bad = lambda: openai.BadRequestError(  # noqa: E731
            "Tool call validation failed: expected string, but got null",
            response=mock.Mock(status_code=400, headers={}), body=None)
        provider, client = self.provider([bad(), ("fine", None)], "groq")
        self.assertEqual(provider.turn("hi", YES, QUIET), "fine")
        self.assertEqual(len(client.calls), 2)
        provider, _ = self.provider([bad(), bad(), bad()], "groq")
        with self.assertRaises(ProviderUnavailable):
            provider.turn("hi", YES, QUIET)

    def test_rate_limit_mid_task_is_waited_out(self):
        import openai
        limit = lambda: openai.RateLimitError(  # noqa: E731
            "slow down", response=mock.Mock(status_code=429, headers={}), body=None)
        # A brain with several models listed moves to the next model instead of waiting.
        provider, client = self.provider([limit(), ("From the second model.", None)], "gemini")
        self.assertEqual(provider.turn("hi", YES, QUIET), "From the second model.")
        self.assertEqual([c["model"] for c in client.calls], provider.models[:2])

        provider, client = self.provider(
            [("", [FakeCall("add_task", {"title": "Gym"})]), limit(), ("Done.", None)], "groq")
        provider.models = provider.models[:1]          # no other model left to move to: it must wait
        waits, seen = [], []
        provider._sleep = waits.append
        self.assertEqual(provider.turn("add gym", YES, seen.append), "Done.")
        self.assertEqual(waits, [20])
        self.assertTrue(any("waiting 20s" in s for s in seen))
        self.assertEqual(len(self.agent.store.list_tasks()), 1)       # the task was not added twice
        provider, _ = self.provider([("", [FakeCall("add_task", {"title": "Run"})]), limit(), limit(), limit()], "groq")
        provider.models = provider.models[:1]
        provider._sleep = waits.append
        with self.assertRaises(ProviderUnavailable) as caught:
            provider.turn("add run", YES, QUIET)
        self.assertTrue(caught.exception.partial)

    def test_stalled_model_is_skipped(self):
        import httpx
        import openai
        stall = openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
        provider, client = self.provider([stall, ("Quick answer.", None), ("Again.", None)], "gemini")
        self.assertEqual(provider.turn("hi", YES, QUIET), "Quick answer.")
        provider.turn("more", YES, QUIET)
        self.assertEqual([c["model"] for c in client.calls],
                         [provider.models[0], provider.models[1], provider.models[1]])   # stays on the working one

    def test_a_benched_model_comes_back_after_its_rest(self):
        import httpx
        import openai
        stall = openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
        provider, client = self.provider([stall, ("one", None), ("two", None)], "gemini")
        provider.turn("hi", YES, QUIET)
        provider._model_cool[provider.models[0]] = 0.0          # its rest is over
        provider.turn("again", YES, QUIET)
        self.assertEqual([c["model"] for c in client.calls][-1], provider.models[0])

    def test_lean_brain_gets_everyday_tools_and_can_ask_for_more(self):
        names = lambda call: [t["function"]["name"] for t in call["tools"]]  # noqa: E731
        provider, client = self.provider([
            ("", [FakeCall("enable_tools", {"group": "files"})]),
            ("", [FakeCall("list_directory", {"path": self.tmp.name})]),
            ("Here are your files.", None),
        ], "groq")
        self.assertEqual(provider.turn("what files are here", YES, QUIET), "Here are your files.")
        first, second = names(client.calls[0]), names(client.calls[1])
        self.assertIn("add_task", first)
        self.assertIn("enable_tools", first)
        for hidden in ("list_directory", "browser_open", "run_powershell"):
            self.assertNotIn(hidden, first)
        self.assertIn("list_directory", second)                  # switched on for the rest of the session
        self.assertNotIn("browser_open", second)
        self.assertLess(len(first), len(self.reg.tools()) * 0.6)     # well under the full set
        full, _ = self.provider([("x", None)], "gemini")
        full.groups = None                                       # a brain that isn't lean gets everything
        self.assertIn("browser_open", [t["function"]["name"] for t in full.tools])
        self.assertNotIn("enable_tools", [t["function"]["name"] for t in full.tools])

    def test_null_optional_arguments_are_ignored(self):
        provider, client = self.provider([
            ("", [FakeCall("add_task", {"title": "Gym", "due": None})]), ("ok", None)])
        provider.turn("add", YES, QUIET)
        self.assertEqual(self.agent.store.list_tasks()[0]["title"], "Gym")

    def test_missing_key_is_unavailable(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            with self.assertRaises(ProviderUnavailable):
                OpenAICompatProvider(self.cfg, self.reg, "sys", "gemini")


class FakeBrain:
    def __init__(self, name, behaviour, log):
        self.name, self.behaviour, self.log = name, behaviour, log

    def seed(self, transcript):
        self.log.append((self.name, "seed", len(transcript)))

    def turn(self, text, confirm, notice):
        self.log.append((self.name, "turn"))
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


class FallbackTests(Base):
    def agent_with(self, **behaviours):
        log = []
        factories = {n: (lambda cfg, reg, system, n=n, b=b: FakeBrain(n, b, log)) for n, b in behaviours.items()}
        self.cfg.provider, self.cfg.fallback_order = next(iter(behaviours)), list(behaviours)
        return Agent(self.cfg, YES, store=self.agent.store, providers=factories), log

    def test_falls_through_the_chain_and_stays_switched(self):
        agent, log = self.agent_with(gemini=ProviderUnavailable("rate limit."), groq="hi from groq", claude="unused")
        reply = agent.ask("hello")
        self.assertIn("hi from groq", reply)
        self.assertNotIn("Switched", reply)      # routine switches are not read aloud
        self.assertEqual(agent.provider_name, "groq")
        agent.ask("again")
        self.assertEqual([e for e in log if e[1] == "turn"], [("gemini", "turn"), ("groq", "turn"), ("groq", "turn")])

    def test_all_unavailable_lists_every_reason(self):
        agent, _ = self.agent_with(gemini=ProviderUnavailable("no API key."), ollama=ProviderUnavailable("not running."))
        reply = agent.ask("hello")
        self.assertIn("gemini: no API key.", reply)
        self.assertIn("ollama: not running.", reply)
        self.assertEqual(agent.transcript, [])

    def test_partial_turn_is_not_replayed_elsewhere(self):
        problem = ProviderUnavailable("rate limit.")
        problem.partial = True
        agent, log = self.agent_with(gemini=problem, groq="should not run")
        self.assertIn("cut off partway", agent.ask("do things"))
        self.assertNotIn(("groq", "turn"), log)

    def test_real_openai_provider_pops_failed_first_request(self):
        import openai
        error = openai.RateLimitError("slow down", response=mock.Mock(status_code=429, headers={}), body=None)
        provider = OpenAICompatProvider(self.cfg, self.reg, "sys", "groq", client=FakeOpenAI([error]))
        provider.models = provider.models[:1]
        with self.assertRaises(ProviderUnavailable) as caught:
            provider.turn("hi", YES, QUIET)
        self.assertFalse(caught.exception.partial)
        self.assertEqual(provider.turns, [])


class PromptTests(unittest.TestCase):
    def test_time_stamp_lists_the_coming_week(self):
        from jarvis.agent import ROUTINES, time_stamp
        stamp = time_stamp(datetime(2026, 11, 15, 22, 10))
        self.assertTrue(stamp.startswith("Now: Sunday 2026-11-15 22:10."))
        self.assertIn("Thu 2026-11-19", stamp)
        self.assertIn("Sun 2026-11-22", stamp)
        self.assertEqual(set(ROUTINES), {"brief", "plan", "deadlines", "erp", "jobs", "news", "messages", "dashboards", "night", "quiz", "revise", "timetable", "getknow", "study", "youtube", "instagram", "content", "video"})


class SessionTests(Base):
    """The app core and its window server, driven with a fake brain."""

    def session(self, *replies):
        from jarvis.session import Session
        queue_ = list(replies)

        class Brain:
            name = "gemini"
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                notice("list_tasks")
                reply = queue_.pop(0)
                return reply(confirm) if callable(reply) else reply

        self.cfg.provider, self.cfg.fallback_order, self.cfg.checkin_minutes = "gemini", ["gemini"], 0
        self.cfg.morning_brief = False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent._factories = {"gemini": lambda c, r, sys_: Brain()}
        s.start()
        return s

    def wait_for(self, s, kind, timeout=5):
        import time
        end = time.time() + timeout
        while time.time() < end:
            for e in list(s.events):
                if e["kind"] == kind:
                    return e
            time.sleep(0.02)
        self.fail(f"no '{kind}' event; got {[e['kind'] for e in s.events]}")

    def test_message_flows_to_events(self):
        s = self.session("Hello there.")
        s.submit("hi")
        reply = self.wait_for(s, "jay")
        self.assertEqual(reply["text"], "Hello there.")
        kinds = [e["kind"] for e in s.events]
        self.assertLess(kinds.index("user"), kinds.index("step"))
        self.assertLess(kinds.index("step"), kinds.index("jay"))
        self.assertEqual(s.state()["brain"], "gemini")
        s.stop()
        self.assertTrue(s.stopped.wait(5))

    def test_approval_waits_for_the_button(self):
        s = self.session(lambda confirm: "ran it" if confirm("Run PowerShell: dir") else "skipped")
        s.submit("list files")
        card = self.wait_for(s, "approval")
        self.assertEqual(card["summary"], "Run PowerShell: dir")
        self.assertEqual(s.pending_approval(), card["approval"])
        self.assertFalse([e for e in s.events if e["kind"] == "jay"])    # blocked until answered
        self.assertTrue(s.answer_approval(card["approval"], True))
        self.assertEqual(self.wait_for(s, "jay")["text"], "ran it")
        self.assertFalse(s.answer_approval(card["approval"], False))     # can't be answered twice
        s.stop()

    def test_closing_the_window_stops_jay(self):
        import threading
        import time

        from jarvis.ui import WindowWatch, watch_window
        s = self.session()
        window = WindowWatch()
        window.is_open = lambda: window.waiting > 0      # no between-polls allowance in this fast test
        threading.Thread(target=watch_window, args=(s, window, s.stop, 0.3, 0.05), daemon=True).start()
        time.sleep(0.6)
        self.assertFalse(s.quitting.is_set())      # no window has opened yet: keep waiting for one
        window.enter()                             # a window is open (its events request is waiting)
        time.sleep(0.6)
        self.assertFalse(s.quitting.is_set())
        window.leave()
        window.enter()                             # the page polls again straight away
        time.sleep(0.2)
        window.leave()                             # ... and then the window is closed
        self.assertTrue(s.stopped.wait(5))         # Jay shuts down by itself

    def test_window_told_to_close_counts_as_closed_at_once(self):
        from jarvis.ui import WindowWatch
        window = WindowWatch()
        self.assertFalse(window.is_open())
        hanging = window.enter()                    # the page's request that is waiting for events
        self.assertTrue(window.is_open())
        window.order_closed()                       # "Stop Jay": the page closes, its request is left hanging
        self.assertFalse(window.is_open())          # so "Hey Jay" right afterwards can reopen the window
        window.leave(hanging)
        self.assertFalse(window.is_open())
        fresh = window.enter()                      # a new window polls
        self.assertTrue(window.is_open())
        window.leave(fresh)

    def test_only_one_copy_can_run(self):
        from jarvis.ui import QuietServer, make_handler
        s = self.session()
        first = QuietServer(("127.0.0.1", 0), make_handler(s, "t"))
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            QuietServer(("127.0.0.1", first.server_port), make_handler(s, "t"))

    def test_commands_and_window_server_security(self):
        import json
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from jarvis.ui import make_handler
        s = self.session("Hi from the window.")
        s.agent.store.add_task("Visible task", priority="high")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, "secret"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def call(path, body=None, token="secret", host=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode())
            if token:
                req.add_header("X-Jay-Token", token)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, ""

        self.assertEqual(call("/api/state", token=None)[0], 403)              # no token
        self.assertEqual(call("/api/send", {"text": "x"}, token="wrong")[0], 403)
        self.assertEqual(call("/api/state", host="evil.example")[0], 403)     # another site's name
        self.assertEqual(call("/?t=nope", token=None)[0], 403)
        status, page = call("/?t=secret", token=None)
        self.assertEqual(status, 200)
        self.assertIn('const TOKEN = "secret"', page)
        state = json.loads(call("/api/state")[1])
        self.assertEqual(state["tasks"][0]["title"], "Visible task")
        self.assertEqual(call("/api/send", {"text": "hello"})[0], 200)
        self.assertEqual(self.wait_for(s, "jay")["text"], "Hi from the window.")
        events = json.loads(call("/api/events?after=0")[1])
        self.assertIn("jay", [e["kind"] for e in events])
        call("/api/task", {"id": 1, "action": "done"})
        self.assertEqual(json.loads(call("/api/state")[1])["tasks"], [])
        s.submit("/tasks")
        s.stop()


class WakeStopTests(Base):
    """'Hey Jay' / 'Stop Jay' behaviour."""
    NAMES = ["jay", "jai", "j"]

    def route(self, mode, local, accurate="(unused)"):
        from jarvis.voice import route_utterance
        used = []
        result = route_utterance(mode, lambda: used.append("local") or local,
                                 lambda: used.append("cloud") or accurate, self.NAMES)
        return result, used

    def test_standby_only_reacts_to_the_wake_word_and_stays_local(self):
        self.assertEqual(self.route("standby", "pass me the salt please"), (None, ["local"]))      # never leaves the laptop
        self.assertEqual(self.route("standby", "Hey Jay."), (("wake", ""), ["local"]))
        result, used = self.route("standby", "Hey J what's the whether", "Hey Jay, what's the weather?")
        self.assertEqual(result, ("wake", "what's the weather?"))        # the accurate hearing is used for the request
        self.assertEqual(used, ["local", "cloud"])
        result, _ = self.route("standby", "hey jai open chrome", "open Chrome")   # accurate one lost the name: keep local
        self.assertEqual(result, ("wake", "open chrome"))

    def test_active_conversation_needs_no_wake_word(self):
        self.assertEqual(self.route("active", "x", "move gym to six")[0], ("command", "move gym to six"))
        self.assertEqual(self.route("active", "x", "Hey Jay, and tomorrow?")[0], ("command", "and tomorrow?"))
        self.assertIsNone(self.route("active", "x", "")[0])
        self.assertEqual(self.route("active", "x", "Stop, Jay.")[0][0], "stop")

    def test_while_busy_only_stop_counts(self):
        self.assertEqual(self.route("busy", "Stop Jay"), (("stop", "Stop Jay"), ["local"]))
        self.assertEqual(self.route("busy", "what's the weather"), (None, ["local"]))
        self.assertEqual(self.route("busy", "Hey Jay do this"), (None, ["local"]))

    def test_stop_phrases(self):
        from jarvis.voice import is_stop
        for yes in ("Stop Jay", "Jay, stop.", "stop", "Stop!", "okay Jay stop talking", "cancel"):
            self.assertTrue(is_stop(yes, self.NAMES), yes)
        for no in ("the bus stop is near the college", "don't stop the music now please", "hello", ""):
            self.assertFalse(is_stop(no, self.NAMES), no)

    def session(self, *replies):
        from jarvis.session import Session
        queue_ = list(replies)

        class Brain:
            name = "gemini"
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                reply = queue_.pop(0)
                return reply(confirm) if callable(reply) else reply

        self.cfg.provider, self.cfg.fallback_order, self.cfg.checkin_minutes = "gemini", ["gemini"], 0
        self.cfg.morning_brief, self.cfg.active_seconds = False, 30
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent._factories = {"gemini": lambda c, r, sys_: Brain()}
        return s

    def wait_for(self, s, test, timeout=5):
        import time
        end = time.time() + timeout
        while time.time() < end:
            hit = [e for e in list(s.events) if test(e)]
            if hit:
                return hit[0]
            time.sleep(0.02)
        self.fail(f"event not seen; got {[e['kind'] for e in s.events]}")

    def test_wake_then_stop(self):
        s = self.session("It's sunny.")
        opened = []
        s.on_wake = lambda: opened.append(1)
        s.start()
        self.wait_for(s, lambda e: e["kind"] == "status" and e["status"] == "idle")
        self.assertEqual(s.mode(), "standby")
        with mock.patch("jarvis.voice.chime"):
            s._heard("wake", "what's the weather")
            self.assertEqual(opened, [1])                              # the window is asked to open
            self.assertEqual(self.wait_for(s, lambda e: e["kind"] == "jay")["text"], "It's sunny.")
            self.wait_for(s, lambda e: e["kind"] == "status" and e["status"] == "idle" and e["id"] > 4)
            self.assertEqual(s.mode(), "active")                       # follow-ups need no wake word
            s._heard("stop", "Stop Jay")
        self.assertEqual(s.mode(), "standby")
        closing = self.wait_for(s, lambda e: e["kind"] == "mode" and not e["awake"])
        self.assertTrue(closing["close"])                              # the window is told to go away
        self.assertTrue(s.agent.registry.cancelled.is_set())
        self.assertIn("said stop", self.run_cancelled(s))
        s.stop()

    def run_cancelled(self, s):
        return s.agent.registry.execute("add_task", {"title": "must not be added"}, YES).text

    def test_stop_refuses_pending_approval_and_drops_queued_requests(self):
        s = self.session(lambda confirm: "ran" if confirm("Run PowerShell: dir") else "refused", "second")
        s.start()
        s.submit("do it")
        self.wait_for(s, lambda e: e["kind"] == "approval")
        s.submit("and another thing")                                 # queued behind the first
        s.stand_down(sound=False)
        self.wait_for(s, lambda e: e["kind"] == "approval_done" and not e["allowed"])
        self.wait_for(s, lambda e: e["kind"] == "info" and e["text"] == "Stopped.")
        import time
        time.sleep(0.3)
        self.assertEqual([e["text"] for e in s.events if e["kind"] == "user"], ["do it"])   # the second never ran
        self.assertFalse([e for e in s.events if e["kind"] == "jay"])                       # and nothing was said
        s.stop()

    def test_a_new_request_replaces_one_still_being_processed(self):
        import threading
        import time
        release = threading.Event()
        s = self.session(lambda confirm: (release.wait(5), "answer to the misheard request")[1], "answer to what I meant")
        s.start()
        self.wait_for(s, lambda e: e["kind"] == "status" and e["status"] == "idle")
        s.submit("something misheard")
        self.wait_for(s, lambda e: e["kind"] == "user" and e["text"] == "something misheard")
        started = time.time()
        s.submit("what I actually meant")                    # typed over the one in progress
        said = self.wait_for(s, lambda e: e["kind"] == "jay")
        self.assertEqual(said["text"], "answer to what I meant")
        self.assertLess(time.time() - started, 2)            # not held up until the first one finished
        release.set()
        time.sleep(0.4)
        self.assertEqual([e["text"] for e in s.events if e["kind"] == "jay"], ["answer to what I meant"])
        self.assertEqual(s.status, "idle")
        s.stop()

    def test_spoken_commands_are_heard_while_working_but_not_during_background_work(self):
        s = self.session("x")
        s.status, s.awake, s.awake_until = "thinking", True, __import__("time").monotonic() + 30
        self.assertEqual(s.mode(), "active")
        s._background = True
        self.assertEqual(s.mode(), "active")           # a background job doesn't change an awake conversation
        s.awake = False
        self.assertEqual(s.mode(), "standby")
        s.status, s._background = "speaking", False
        self.assertEqual(s.mode(), "busy")             # while Jay talks, only "stop" is listened for

    def test_conversation_times_out_to_standby(self):
        s = self.session()
        s.status = "idle"
        s.wake()
        self.assertEqual(s.mode(), "active")
        s.awake_until = 0                                              # the quiet period has passed
        self.assertEqual(s.mode(), "standby")
        last = [e for e in s.events if e["kind"] == "mode"][-1]
        self.assertEqual((last["awake"], last["close"]), (False, False))   # goes quiet but keeps the window

    def test_english_only_by_default(self):
        self.assertEqual(self.cfg.languages, ["en"])
        self.assertIn("Always reply in English", build_system_prompt(self.cfg, self.agent.store))
        self.cfg.languages = ["en", "te"]
        self.assertIn("Telugu script", build_system_prompt(self.cfg, self.agent.store))


class WatchTests(Base):
    def make(self):
        from jarvis.watches import Watches
        return Watches(self.agent.store, self.reg)

    def test_reports_only_what_is_new(self):
        w = self.make()
        feed = ["- ISRO launches satellite (16 Nov 2026)", "- India wins toss (16 Nov 2026)"]
        w._items = lambda watch: list(feed)                           # stand-in for the news fetch
        wid = w.add("Space news", "news", "ISRO", 60)
        self.assertEqual(w.run(w.all()[0]), [])                      # the first run only records what's there
        self.assertEqual(w.run(w.all()[0]), [])                      # nothing changed
        feed.append("- Chandrayaan update (17 Nov 2026)")
        self.assertEqual(w.run(w.all()[0]), ["- Chandrayaan update (17 Nov 2026)"])
        self.assertEqual(w.run(w.all()[0]), [])                      # and it isn't reported twice
        self.assertEqual(wid, 1)

    def test_due_logic_and_limits(self):
        from datetime import timedelta
        w = self.make()
        w.add("Jobs", "jobs", "backend intern Java", 5)               # too frequent: raised to 30 minutes
        self.assertEqual(w.all()[0]["every_minutes"], 30)
        self.assertEqual(len(w.due()), 1)                            # never run: due now
        w._items = lambda watch: []
        w.run(w.all()[0])
        self.assertEqual(w.due(), [])
        old = (datetime.now() - timedelta(minutes=31)).isoformat(timespec="minutes")
        self.agent.store._run("UPDATE watches SET last_run = ?", (old,))
        self.assertEqual(len(w.due()), 1)
        with self.assertRaises(ValueError):
            w.add("x", "carrier-pigeon", "y", 60)
        with self.assertRaises(ValueError):
            w.add("x", "site", "not-a-web-address", 60)

    def test_site_watch_reports_changed_lines(self):
        w = self.make()
        w.add("Notices", "site", "https://example.edu/notices", 60)
        pages = iter(["First look at 'watch 1' (Notices):\nExam on 20 Nov\nFee due 30 Nov",
                      "No changes on 'watch 1' since the last check.",
                      "New on 'watch 1' since the last check:\nLab record due 18 Nov"])
        self.reg._tools["check_site"].func = lambda name, url: next(pages)
        self.assertEqual(w.run(w.all()[0]), ["Exam on 20 Nov", "Fee due 30 Nov"])   # sites report from the first look
        self.assertEqual(w.run(w.all()[0]), [])
        self.assertEqual(w.run(w.all()[0]), ["Lab record due 18 Nov"])

    def test_tools_and_owner_only(self):
        from jarvis.watches import register
        register(self.reg, self.make())
        call = lambda name, args: self.run_tool(name, args).text  # noqa: E731
        self.assertIn("Watching 'Jobs'", call("add_watch", {"name": "Jobs", "kind": "jobs", "target": "java intern"}))
        self.assertIn("Jobs (jobs: java intern)", call("list_watches", {}))
        self.reg.speaker_is_owner = False
        self.assertIn("only the owner", call("add_watch", {"name": "x", "kind": "news", "target": "y"}))
        self.reg.speaker_is_owner = True
        self.assertEqual(call("remove_watch", {"watch_id": 1}), "Stopped watching.")

    def test_backup_keeps_a_week_and_runs_once_a_day(self):
        import shutil
        import sqlite3
        from jarvis.watches import backup
        data = Path(self.tmp.name)
        (data / "profile.json").write_text("{}", encoding="utf-8")
        self.agent.store.add_fact("backed up fact")
        made = backup(data)
        self.assertTrue((made / "jarvis.db").exists() and (made / "profile.json").exists())
        db = sqlite3.connect(made / "jarvis.db")
        try:
            self.assertEqual(db.execute("SELECT fact FROM facts").fetchone()[0], "backed up fact")
        finally:
            db.close()
        self.assertIsNone(backup(data))                              # already done today
        for day in range(1, 10):
            (data / "backups" / f"2026-01-0{day}").mkdir()
        shutil.rmtree(made)                                          # a new day: the oldest backups are pruned
        backup(data)
        self.assertLessEqual(len(list((data / "backups").iterdir())), 7)

    def test_git_tools(self):
        import subprocess
        repo = Path(self.tmp.name) / "proj"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)  # noqa: E731
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (repo / "a.txt").write_text("one\n")
        run("add", ".")
        run("commit", "-qm", "first commit")
        (repo / "a.txt").write_text("one\ntwo\n")
        from jarvis.watches import register_git
        register_git(self.reg)
        out = self.run_tool("git_status", {"path": str(repo)}).text
        self.assertIn("first commit", out)
        self.assertIn("a.txt", out)
        self.assertIn("+two", self.run_tool("git_diff", {"path": str(repo)}).text)
        self.assertTrue(self.run_tool("git_status", {"path": self.tmp.name}).is_error)    # not a repository


class ResilienceTests(Base):
    def test_failed_watch_backs_off_and_warns_once(self):
        from datetime import timedelta
        from jarvis.watches import Watches
        w = Watches(self.agent.store, self.reg)
        w.add("Jobs", "jobs", "java intern", 1440)
        watch = w.all()[0]
        self.assertTrue(w.failed(watch))                           # first failure: tell the user
        self.assertFalse(w.failed(w.all()[0]))                     # after that, stay quiet
        self.assertEqual(w.due(), [])                              # not retried this minute
        later = datetime.fromisoformat(w.all()[0]["last_run"]) - timedelta(minutes=1)
        retry_at = later + timedelta(minutes=1440)                 # ... but within about half an hour, not a day
        self.assertLess(retry_at - datetime.now(), timedelta(minutes=32))
        w._items = lambda watch: ["a job"]
        w.run(w.all()[0])
        self.assertTrue(w.failed(w.all()[0]))                      # it worked once, so a new failure is worth saying

    def test_search_tries_other_backends(self):
        from jarvis.tools import web
        tried = []

        class FakeDDGS:
            def text(self, query, max_results, backend):
                tried.append(backend)
                if backend in ("auto", "google"):
                    raise RuntimeError("No results found.")
                return [{"title": "A job", "href": "http://x", "body": "details"}]

        import types
        fake = types.ModuleType("ddgs")
        fake.DDGS = FakeDDGS
        with mock.patch.dict("sys.modules", {"ddgs": fake}):
            self.assertEqual(web.search_web("java intern")[0]["title"], "A job")
            self.assertEqual(tried, ["auto", "google", "yahoo"])
            FakeDDGS.text = lambda self, query, max_results, backend: (_ for _ in ()).throw(RuntimeError("none"))
            with self.assertRaises(RuntimeError):
                web.search_web("nothing")

    def test_silent_model_reports_what_the_tool_said(self):
        provider, _ = None, None
        client = FakeOpenAI([("", [FakeCall("add_task", {"title": "Gym"})]), ("", None)])   # acts, then says nothing
        provider = OpenAICompatProvider(self.cfg, self.reg, "sys", "gemini", client=client)
        reply = provider.turn("add gym", YES, QUIET)
        self.assertIn("Added", reply)                                # not an empty chat bubble

    def test_access_token_is_kept_between_runs(self):
        import secrets
        from jarvis import ui
        data = Path(self.tmp.name)
        (data / "ui_token").write_text("abcdefghijklmnopqrstuvwx_-0123", encoding="utf-8")
        source = open(ui.__file__, encoding="utf-8").read()
        self.assertIn("token_file.read_text().strip() if token_file.exists()", source)
        self.assertNotIn("token_file.unlink()", source)
        self.assertGreaterEqual(len(secrets.token_urlsafe(24)), 24)


class PhoneAndCardTests(Base):
    def test_telegram_pairing_and_privacy(self):
        from jarvis.telegram import TelegramLink
        got, info, sent = [], [], []
        link = TelegramLink("T", Path(self.tmp.name), lambda text, chat: got.append((text, chat)), info.append)
        link.send = lambda chat, text: sent.append((chat, text))
        msg = lambda chat, text: {"chat": {"id": chat}, "text": text}        # noqa: E731
        link.handle(msg(7, "hello"))                                         # not paired: nothing reaches Jay
        link.handle(msg(7, "/pair 000000"))                                  # wrong code
        self.assertEqual((got, link.chat_id), ([], None))
        link.handle(msg(7, f"/pair {link.code}"))
        self.assertEqual(link.chat_id, 7)
        link.handle(msg(7, "what's on today?"))
        link.handle(msg(99, "let me in"))                                    # a stranger who found the bot
        self.assertEqual(got, [("what's on today?", 7)])
        again = TelegramLink("T", Path(self.tmp.name), lambda *a: None, info.append)
        self.assertEqual(again.chat_id, 7)                                   # pairing survives a restart

    def test_phone_requests_are_approved_on_the_phone_not_the_laptop(self):
        import threading

        from jarvis.session import Session

        class Brain:
            name = "gemini"
            def __init__(self, reg): self.reg = reg
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                res = self.reg.execute("run_powershell", {"command": "dir"}, confirm)
                return "blocked" if "declined" in res.text else "ran it"

        self.cfg.provider, self.cfg.fallback_order = "gemini", ["gemini"]
        self.cfg.checkin_minutes, self.cfg.morning_brief = 0, False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent._factories = {"gemini": lambda c, r, sys_: Brain(r)}
        s.telegram = mock.Mock(chat_id=7)
        asked = []
        s.telegram.ask = lambda i, summary, label="": asked.append(i)
        worker = threading.Thread(target=s._from_phone, args=("run dir", 7))
        worker.start()
        for _ in range(100):
            if asked:
                break
            threading.Event().wait(0.05)
        self.assertTrue(asked)                                               # the question went to the phone
        s.on_phone_approval(asked[0], "n")                                   # ... and a tap there answers it
        worker.join(10)
        self.assertEqual(s.telegram.send.call_args_list[-1].args, (7, "blocked"))
        self.assertFalse([e for e in s.events if e["kind"] == "approval"])   # no card was put up on the laptop

    def test_visual_cards_for_results_worth_seeing(self):
        from jarvis.session import Session
        self.cfg.checkin_minutes, self.cfg.morning_brief = 0, False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent.registry.execute("add_task", {"title": "x"}, YES)           # not a card-worthy result
        with mock.patch("jarvis.tools.web.news_headlines", return_value="- A headline (today)"):
            s.agent.registry.execute("get_news", {"topic": "ai"}, YES)
        cards = [e for e in s.events if e["kind"] == "card"]
        self.assertEqual([c["title"] for c in cards], ["HEADLINES"])
        self.assertIn("A headline", cards[0]["text"])

    def test_hotkey_default_and_crash_recovery(self):
        self.assertEqual(Config.load(data_dir=Path(self.tmp.name)).hotkey, "ctrl+alt+j")
        import jarvis.__main__ as launcher
        calls = []
        codes = iter([1, 0])
        with mock.patch("subprocess.call", side_effect=lambda cmd: calls.append(cmd) or next(codes)), \
                mock.patch("time.sleep"), mock.patch.object(launcher.sys, "argv", ["jarvis", "--brief"]):
            launcher.supervise()
        self.assertEqual(len(calls), 2)                                     # crashed once, restarted once, then quit
        self.assertIn("--child", calls[0])
        self.assertNotIn("--brief", calls[1])
        self.assertIn("--standby", calls[1])                                # comes back quietly, no repeated briefing


class FalseWakeTests(Base):
    NAMES = ["jay", "jai", "jey"]

    def test_name_must_open_the_sentence(self):
        from jarvis.voice import extract_command
        wake = lambda t: extract_command(t, self.NAMES, strict=True)  # noqa: E731
        self.assertEqual(wake("Hey Jay, what's the weather?"), "what's the weather?")
        self.assertEqual(wake("Jay, open Chrome."), "open Chrome.")
        self.assertEqual(wake("Okay Jai."), "")
        for background in ("I was listening to the Jay Shetty podcast", "We went to see Jay yesterday",
                           "said hey Jay is late again", "the bus stop near J P Nagar"):
            self.assertIsNone(wake(background), background)

    def test_unknown_voices_cannot_wake_or_continue(self):
        from jarvis.voice import route_utterance
        stranger = lambda: False  # noqa: E731
        self.assertIsNone(route_utterance("standby", lambda: "Hey Jay, play music", lambda: "x", self.NAMES, stranger))
        self.assertIsNone(route_utterance("active", lambda: "x", lambda: "and then delete it", self.NAMES, stranger))
        self.assertEqual(route_utterance("busy", lambda: "Stop Jay", lambda: "x", self.NAMES, stranger)[0], "stop")
        self.assertEqual(route_utterance("active", lambda: "x", lambda: "stop", self.NAMES, stranger)[0], "stop")
        known = lambda: True  # noqa: E731
        self.assertEqual(route_utterance("standby", lambda: "Hey Jay", lambda: "x", self.NAMES, known), ("wake", ""))

    def test_background_jobs_never_ask_or_open_the_window(self):
        from jarvis.session import Session
        self.cfg.checkin_minutes, self.cfg.morning_brief = 0, False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        opened = []
        s.on_wake = lambda: opened.append(1)
        s._background = True
        self.assertFalse(s.confirm("Run PowerShell: dir"))
        self.assertEqual(opened, [])
        self.assertFalse([e for e in s.events if e["kind"] == "approval"])

    def test_known_voice_gate_only_applies_once_voices_exist(self):
        from jarvis.session import Session
        self.cfg.checkin_minutes, self.cfg.morning_brief = 0, False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        audio = np.zeros(16000, dtype="float32")
        self.assertTrue(s.known_voice(audio))                 # nobody set up: anyone may talk
        s.speakers.prints = {"Rohith": [1.0, 0.0]}
        s.speakers.best = lambda a, min_seconds=0.5: ("Rohith", 0.2, None)
        self.assertFalse(s.known_voice(audio))                # a stranger or a video
        s.speakers.best = lambda a, min_seconds=0.5: ("Rohith", 0.55, None)
        self.assertTrue(s.known_voice(audio))
        self.cfg.wake_anyone = True
        s.speakers.best = lambda a, min_seconds=0.5: ("Rohith", 0.1, None)
        self.assertTrue(s.known_voice(audio))                 # the setting lets anyone in


class ProfileTests(Base):
    def test_profile_is_always_in_the_instructions(self):
        from jarvis.profile import Profile
        self.assertIn("not filled in yet", build_system_prompt(self.cfg, self.agent.store))
        out = self.run_tool("update_profile", {"field": "education", "value": "B.Tech CSE, 3rd year, graduating 2028"})
        self.assertIn("Studies", out.text)
        Profile(Path(self.tmp.name)).update({"call_me": "boss", "name": "Rohith"})
        prompt = build_system_prompt(self.cfg, self.agent.store)
        self.assertIn("- Studies: B.Tech CSE, 3rd year, graduating 2028", prompt)
        self.assertIn('Address the owner as "boss"', prompt)
        self.run_tool("update_profile", {"field": "education", "value": ""})
        self.assertNotIn("Studies", build_system_prompt(self.cfg, self.agent.store))
        self.assertTrue(self.run_tool("update_profile", {"field": "salary", "value": "x"}).is_error)
        self.reg.speaker_is_owner = False
        self.assertIn("only the owner", self.run_tool("update_profile", {"field": "name", "value": "Hacker"}).text)

    def test_saving_from_the_window_takes_effect_at_once(self):
        from jarvis.session import Session
        self.cfg.checkin_minutes, self.cfg.morning_brief = 0, False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.save_profile({"location": "Hyderabad", "goals": "Backend internship by summer"})
        self.assertIn("Location: Hyderabad", s.agent.system)
        form = {f["key"]: f["value"] for f in s.state()["profile"]}
        self.assertEqual(form["goals"], "Backend internship by summer")


class PowerTests(Base):
    """The added abilities: routines, timers, history, app matching, parallel tools."""

    def test_next_occurrence(self):
        from jarvis.store import next_occurrence
        fri = datetime(2026, 11, 20, 7, 0)                         # a Friday
        self.assertEqual(next_occurrence("2026-11-20T07:00", "daily", after=fri), "2026-11-21T07:00")
        self.assertEqual(next_occurrence("2026-11-20T07:00", "weekdays", after=fri), "2026-11-23T07:00")   # skips the weekend
        self.assertEqual(next_occurrence("2026-11-20T07:00", "weekly", after=fri), "2026-11-27T07:00")
        self.assertEqual(next_occurrence("2026-01-31T09:00", "monthly", after=datetime(2026, 1, 31, 9)), "2026-02-28T09:00")
        self.assertEqual(next_occurrence("2026-11-01T07:00", "daily", after=fri), "2026-11-21T07:00")      # catches up

    def test_repeating_reminder_rolls_forward(self):
        store = self.agent.store
        past = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="minutes")
        self.run_tool("add_task", {"title": "Gym", "start": past, "repeat": "daily"})
        self.assertEqual([t["title"] for t in store.pop_due_reminders()], ["Gym"])
        task = store.get_task(1)
        self.assertEqual(task["done"], 0)
        self.assertGreater(task["start"], datetime.now().isoformat(timespec="minutes"))   # moved to tomorrow
        self.assertEqual(store.pop_due_reminders(), [])
        before = task["start"]
        self.run_tool("complete_task", {"task_id": 1})                 # done for today: still a routine
        self.assertEqual(store.get_task(1)["done"], 0)
        self.assertIn("repeats daily", self.run_tool("list_tasks", {}).text)
        self.assertTrue(self.run_tool("add_task", {"title": "x", "repeat": "daily"}).is_error)   # needs a time
        self.assertTrue(before)

    def test_timer_fires_to_the_second(self):
        out = self.run_tool("set_timer", {"minutes": 0.01, "label": "Tea"}).text
        self.assertIn("Timer set: Tea", out)
        import time as t
        t.sleep(1.2)
        due = self.agent.store.pop_due_reminders()
        self.assertEqual([d["title"] for d in due], ["⏱ Tea"])
        self.assertTrue(self.run_tool("set_timer", {"minutes": 0}).is_error)

    def test_search_history(self):
        store = self.agent.store
        store.log_message("user", "My project review with Prof. Rao is on Friday")
        store.log_message("assistant", "Noted.")
        store.log_message("user", "unrelated chat")
        out = self.run_tool("search_history", {"query": "project review"}).text
        self.assertIn("Prof. Rao", out)
        self.assertNotIn("unrelated", out)
        self.assertIn("Nothing", self.run_tool("search_history", {"query": "zebra"}).text)

    def test_app_matching(self):
        from jarvis.tools.system import best_app
        apps = [{"Name": n, "AppID": n} for n in ("Google Chrome", "Visual Studio Code", "Spotify", "Calculator",
                                                  "Chrome Remote Desktop", "WhatsApp", "Settings")]
        pick = lambda q: (best_app(q, apps) or {}).get("Name")  # noqa: E731
        self.assertEqual(pick("chrome"), "Google Chrome")
        self.assertEqual(pick("spotify"), "Spotify")
        self.assertEqual(pick("visual studio"), "Visual Studio Code")
        self.assertEqual(pick("calculater"), "Calculator")                # a typo still matches
        self.assertIsNone(pick("photoshop"))

    def test_harmless_confirm_skips_the_card(self):
        with mock.patch("jarvis.tools.system.POWER", {"lock": ("Lock the screen", lambda: None),
                                                      "sleep": ("Sleep", lambda: None)}):
            self.run_tool("power_action", {"action": "lock"})
            self.assertEqual(self.approvals, [])                          # locking needs no approval
            self.allow = False
            self.assertIn("declined", self.run_tool("power_action", {"action": "sleep"}).text)
            self.assertEqual(self.approvals, ["Sleep"])

    def test_new_private_tools_are_owner_only(self):
        self.reg.speaker_is_owner = False
        for name in ("read_clipboard", "list_windows", "search_history", "power_action"):
            self.assertIn("only the owner", self.run_tool(name, {"query": "x"} if name == "search_history"
                                                         else {"action": "lock"} if name == "power_action" else {}).text)

    def test_independent_tools_run_together(self):
        import threading
        import time as t
        running, peak = [0], [0]
        lock = threading.Lock()

        def slow(**_):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            t.sleep(0.3)
            with lock:
                running[0] -= 1
            return "done"

        for n in ("slow_a", "slow_b", "slow_c"):
            self.reg.tool(n, "slow")(slow)
        provider = OpenAICompatProvider(self.cfg, self.reg, "sys", "gemini",
                                        client=FakeOpenAI([("", [FakeCall("slow_a", {}, "1"), FakeCall("slow_b", {}, "2"),
                                                                 FakeCall("slow_c", {}, "3")]), ("all done", None)]))
        start = t.time()
        self.assertEqual(provider.turn("go", YES, QUIET), "all done")
        self.assertLess(t.time() - start, 0.8)                            # not 0.9s one after another
        self.assertEqual(peak[0], 3)
        ids = [m["tool_call_id"] for m in provider.turns[-1] if isinstance(m, dict) and m.get("role") == "tool"]
        self.assertEqual(ids, ["1", "2", "3"])                            # results stay in order


class SpeakerTests(Base):
    """Recognising several people by voice, with fake embeddings (no model needed)."""

    def speakers(self):
        from jarvis.speakers import Speakers
        s = Speakers(Path(self.tmp.name))
        # Each "clip" is labelled by its first sample; the fake embedding is that person's direction.
        directions = {1.0: [1, 0, 0], 2.0: [0, 1, 0], 3.0: [0, 0, 1], 4.0: [.6, .8, 0]}
        s.embed = lambda audio: np.array(directions[float(audio[0])], dtype="float32")
        return s

    def clip(self, who, seconds=2):
        a = np.zeros(int(16000 * seconds), dtype="float32")
        a[0] = who
        return a

    def test_enrol_identify_and_owner(self):
        s = self.speakers()
        s.enrol("rohit", [self.clip(1), self.clip(1)])
        s.enrol("Ravi", [self.clip(2), self.clip(2), self.clip(2)])
        self.assertEqual((s.enrolled, s.owner), (["Rohit", "Ravi"], "Rohit"))     # first one is the owner
        self.assertEqual(s.identify(self.clip(1)).name, "Rohit")
        self.assertEqual(s.identify(self.clip(2)).name, "Ravi")
        self.assertIsNone(s.identify(self.clip(3)).name)                            # a stranger
        self.assertIsNone(s.identify(self.clip(1, seconds=0.5)).name)               # too short to tell
        again = type(s)(Path(self.tmp.name))                                        # saved across restarts
        self.assertEqual((again.enrolled, again.owner), (["Rohit", "Ravi"], "Rohit"))
        self.assertTrue(s.set_owner("ravi"))
        self.assertEqual(s.owner, "Ravi")
        self.assertTrue(s.remove("Ravi"))
        self.assertEqual(s.owner, "Rohit")                                          # ownership passes on
        with self.assertRaises(ValueError):
            s.enrol("Sam", [self.clip(3, seconds=0.4)])                             # not enough speech

    def test_private_actions_are_owner_only(self):
        reg = self.reg
        reg.speaker_is_owner = False
        for name, args in (("whatsapp_send", {"chat": "Amma", "message": "hi"}), ("run_powershell", {"command": "dir"}),
                           ("read_file", {"path": "C:/x.txt"}), ("delete_task", {"task_id": 1})):
            res = self.run_tool(name, args)
            self.assertTrue(res.is_error, name)
            self.assertIn("only the owner", res.text)
        self.assertEqual(self.approvals, [])                                       # refused before asking anyone
        self.assertFalse(self.run_tool("add_task", {"title": "Ravi: buy milk"}).is_error)   # everyone can add a task
        self.assertFalse(self.run_tool("calculate", {"expression": "2+2"}).is_error)
        reg.speaker_is_owner = True
        self.assertFalse(self.run_tool("delete_task", {"task_id": 1}).is_error)

    def test_session_roles_and_enrolment_flow(self):
        from jarvis.session import Session
        from jarvis.speakers import ENROL_LINES
        seen = []

        class Brain:
            name = "gemini"
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                seen.append(text)
                return "ok"

        self.cfg.provider, self.cfg.fallback_order, self.cfg.checkin_minutes = "gemini", ["gemini"], 0
        self.cfg.morning_brief = False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent._factories = {"gemini": lambda c, r, sys_: Brain()}
        s.speakers = self.speakers()
        self.assertEqual(s.role_for(None), "owner")                     # nobody enrolled: everyone is the owner
        s.voice = mock.Mock()
        s.handsfree_stop = object()                                     # pretend the mic loop is running
        s.start_enrol("Rohit")
        self.assertEqual(s.mode(), "enrol")
        s._enrol_audio(self.clip(1, seconds=0.3))                       # too short: asked to repeat the same line
        self.assertTrue([e for e in s.events if e["kind"] == "enrol" and e.get("retry")])
        for _ in ENROL_LINES:
            s._enrol_audio(self.clip(1))
        self.assertIsNone(s.enrolling)
        self.assertEqual(s.speakers.owner, "Rohit")
        self.assertTrue([e for e in s.events if e["kind"] == "enrol_done" and e["ok"]])
        self.assertEqual((s.role_for("Rohit"), s.role_for("Ravi"), s.role_for(None)), ("owner", "member", "guest"))
        s.answer("what's on my plate", source="voice", speaker=None)    # a stranger's voice
        self.assertIn("unrecognised voice, NOT the owner", seen[-1])
        self.assertTrue(s.agent.registry.speaker_is_owner)              # restored after the reply
        s.answer("hello", source="typed")                                # typed: treated as the owner
        self.assertIn("Rohit, the owner", seen[-1])

    def test_learn_voice_tool_starts_setup_after_the_reply(self):
        from jarvis.session import Session

        class Brain:
            name = "gemini"
            def __init__(self, reg): self.reg = reg
            def seed(self, transcript): pass
            def turn(self, text, confirm, notice):
                self.reg.execute("learn_voice", {"name": "ravi"}, confirm)
                return "Sure, read the lines on screen."

        self.cfg.provider, self.cfg.fallback_order, self.cfg.checkin_minutes = "gemini", ["gemini"], 0
        self.cfg.morning_brief = False
        s = Session(self.cfg)
        self.addCleanup(s.agent.store._db.close)
        s.agent._factories = {"gemini": lambda c, r, sys_: Brain(r)}
        s.voice, s.handsfree_stop = mock.Mock(), object()
        s.answer("learn my voice, I'm Ravi")
        self.assertEqual(s.enrolling["name"], "Ravi")


class VoiceLogicTests(unittest.TestCase):
    def test_language_pick(self):
        from jarvis.voice import Heard, pick_transcript
        en, te = Heard("Hey Jay, create a task", -0.175, 0.14), Heard("హే జే క్రీట్", -0.190, 0.14)
        self.assertEqual(pick_transcript(en, te), "Hey Jay, create a task")        # near tie -> English
        en, te = Heard("J. What is my schedule", -0.553, 0.0), Heard("జే రేపు నా షెడ్యూల్", -0.141, 0.0)
        self.assertEqual(pick_transcript(en, te), "జే రేపు నా షెడ్యూల్")
        self.assertEqual(pick_transcript(Heard("Thank you.", -0.4, 0.9), Heard("ధన్యవాదాలు", -0.5, 0.8)), "")

    def test_speakable(self):
        self.assertEqual(speakable("**Done** - see [docs](https://x.y) or https://a.b"), "Done - see docs or a link")

    def test_extract_command(self):
        names = ["jarvis", "jervis"]
        self.assertEqual(extract_command("Hey Jarvis, what's the weather?", names), "what's the weather?")
        self.assertEqual(extract_command("Jervis open Chrome", names), "open Chrome")
        self.assertEqual(extract_command("Jarvis.", names), "")
        self.assertIsNone(extract_command("pass the salt", names))

    def test_wake_word_in_english_and_telugu(self):
        from jarvis.voice import is_telugu
        names = ["jay", "jai", "జే", "జై"]
        self.assertEqual(extract_command("Jay, repu exam undi", names), "repu exam undi")
        self.assertEqual(extract_command("జే, రేపు నా షెడ్యూల్ ఏమిటి?", names), "రేపు నా షెడ్యూల్ ఏమిటి?")
        self.assertEqual(extract_command("జై ఈ రోజు వాతావరణం", names), "ఈ రోజు వాతావరణం")
        self.assertIsNone(extract_command("jaywalking is illegal", names))   # part of a longer word
        self.assertIsNone(extract_command("జేబులో డబ్బు ఉంది", names))
        self.assertTrue(is_telugu("రేపు exam ఉంది"))
        self.assertFalse(is_telugu("exam tomorrow"))

    def test_wav_encoding(self):
        import io
        import wave

        from jarvis.voice import to_wav
        data = to_wav(np.array([0.0, 0.5, -1.0, 2.0], dtype="float32"))
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 16000, 4))
            samples = np.frombuffer(w.readframes(4), dtype="<i2")
        self.assertEqual(list(samples), [0, 16383, -32767, 32767])

    def test_utterance_detector(self):
        quiet = np.zeros(1600, dtype="float32")
        loud = np.full(1600, 0.2, dtype="float32")
        det = UtteranceDetector(threshold=0.05, start_blocks=2, end_blocks=3, pre_roll=3)
        stream = [quiet] * 5 + [loud] + [quiet] * 2 + [loud] * 4 + [quiet] * 3 + [quiet] * 4
        out = [a for a in (det.feed(b) for b in stream) if a is not None]
        self.assertEqual(len(out), 1)                      # the single loud blip did not trigger
        self.assertEqual(len(out[0]) // 1600, 1 + 4 + 3)   # one quiet lead-in block + speech + trailing quiet
        clipped = UtteranceDetector(threshold=0.05, start_blocks=2, end_blocks=3, pre_roll=1)
        out = [a for a in (clipped.feed(b) for b in [loud] * 4 + [quiet] * 3) if a is not None]
        self.assertEqual(len(out[0]) // 1600, 4 + 3)       # short pre-roll must not clip the first word
        self.assertFalse(det.active)

    def test_utterance_detector_caps_length(self):
        loud = np.full(1600, 0.2, dtype="float32")
        det = UtteranceDetector(threshold=0.05, max_blocks=10)
        out = [a for a in (det.feed(loud) for _ in range(12)) if a is not None]
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()


class RoutingTests(Base):
    def make(self):
        log = []
        names = ("gemini", "groq")
        factories = {n: (lambda cfg, reg, system, n=n: FakeBrain(n, f"from {n}", log)) for n in names}
        self.cfg.provider, self.cfg.fallback_order = "gemini", list(names)
        agent = Agent(self.cfg, YES, store=self.agent.store, providers=factories)
        for n in names:
            agent._provider(n)
        return agent

    def test_prefers_a_clearly_faster_brain_when_the_current_one_is_slow(self):
        agent = self.make()
        agent._providers["gemini"].latency, agent._providers["groq"].latency = 8.0, 1.0
        self.assertEqual(agent._route()[0], "groq")

    def test_a_hand_picked_brain_is_not_overridden(self):
        agent = self.make()
        agent._providers["gemini"].latency, agent._providers["groq"].latency = 8.0, 1.0
        agent.switch("gemini")
        self.assertEqual(agent._route()[0], "gemini")

    def test_a_brain_that_is_cooling_off_goes_last(self):
        import time as _t
        agent = self.make()
        agent._providers["gemini"].cool_until = _t.monotonic() + 30
        self.assertEqual(agent._route(), ["groq", "gemini"])

    def test_fast_enough_brain_keeps_its_place(self):
        agent = self.make()
        agent._providers["gemini"].latency, agent._providers["groq"].latency = 2.0, 0.3
        self.assertEqual(agent._route()[0], "gemini")


class SerialToolTests(Base):
    def test_browser_and_whatsapp_tools_never_run_side_by_side(self):
        for name in ("browser_open", "open_portals", "browser_switch", "whatsapp_read", "check_site"):
            self.assertTrue(self.reg.is_serial(name), name)
        for name in ("get_weather", "list_tasks", "calculate"):
            self.assertFalse(self.reg.is_serial(name), name)

    def test_portal_tools_are_registered_and_owner_only(self):
        from jarvis.tools.registry import PRIVATE_TOOLS
        for name in ("open_portals", "browser_tabs", "browser_switch"):
            self.assertIn(name, [t.name for t in self.reg.tools()])
            self.assertIn(name, PRIVATE_TOOLS)


class InboxTests(unittest.TestCase):
    def test_user_requests_jump_ahead_of_background_jobs_but_keep_their_order(self):
        from jarvis.session import Inbox
        box = Inbox()
        for item in [("watches", ""), ("checkin", ""), ("typed", "first"), ("voice", "second", "Ravi"), ("night", "")]:
            box.put(item)
        order = [box.get() for _ in range(5)]
        self.assertEqual([i[0] for i in order], ["typed", "voice", "watches", "checkin", "night"])
        self.assertTrue(box.empty())


class ListenerTests(unittest.TestCase):
    def test_detector_hears_a_quiet_voice_once_it_has_learned_the_room(self):
        quiet = np.full(1600, 0.002, dtype="float32")
        soft_voice = np.full(1600, 0.012, dtype="float32")
        det = UtteranceDetector(threshold=0.05, adaptive_floor=0.005, end_blocks=2)
        for _ in range(60):
            det.feed(quiet)
        self.assertLess(det.threshold, 0.012)
        out = None
        for block in [soft_voice] * 5 + [quiet] * 3:
            out = det.feed(block) if out is None else out
        self.assertIsNotNone(out)

    def test_detector_threshold_rises_with_a_noisy_room(self):
        noisy = np.full(1600, 0.02, dtype="float32")
        det = UtteranceDetector(threshold=0.012, adaptive_floor=0.005, max_blocks=50)
        for _ in range(200):
            det.feed(noisy)
        self.assertGreater(det.threshold, 0.04)

    def test_stop_is_heard_in_pieces_while_jay_talks(self):
        speech = np.full(1600, 0.1, dtype="float32")
        det = UtteranceDetector(threshold=0.05, end_blocks=3, max_blocks=18)
        pieces = [a for a in (det.feed(speech) for _ in range(60)) if a is not None]
        self.assertGreaterEqual(len(pieces), 3)          # not one 6-second lump


class BackgroundWorkTests(Base):
    def make_session(self):
        from jarvis.session import Session
        return Session(self.cfg)

    def test_wake_word_still_works_while_a_background_check_is_thinking(self):
        s = self.make_session()
        s.status, s._background = "thinking", True
        self.assertEqual(s.mode(), "standby")
        s._background = False
        self.assertEqual(s.mode(), "busy")

    def test_a_request_winds_down_background_work(self):
        s = self.make_session()
        s._background = True
        s.submit("what time is it")
        self.assertTrue(s.agent.registry.cancelled.is_set())

    def test_startup_jobs_do_not_block_the_first_request(self):
        s = self.make_session()
        s.inbox.put(("startup", ""))
        s.inbox.put(("typed", "hello"))
        self.assertEqual(s.inbox.get()[0], "typed")


class StopHearingTests(unittest.TestCase):
    NAMES = ["jay", "jai"]

    def test_mishearings_of_stop_still_stop(self):
        from jarvis.voice import is_stop
        for heard in ("Stob Jay", "Stopp", "stop jay", "Jay, stop!", "cancel"):
            self.assertTrue(is_stop(heard, self.NAMES), heard)

    def test_ordinary_sentences_do_not_stop_it(self):
        from jarvis.voice import is_stop
        for heard in ("what is the bus stop timing today", "play some music", "stuck on a problem today", "hello there"):
            self.assertFalse(is_stop(heard, self.NAMES), heard)


class MorningPlanTests(Base):
    def test_gathers_todays_items_overdue_and_calendar_without_a_brain(self):
        from jarvis import brief
        today = datetime.now().date()
        store = self.agent.store
        store.add_task("Lab record", start=f"{today}T18:00", priority="high")
        store.add_task("Gym", start=f"{today}T07:30")
        store.add_task("Old form", due=f"{today - timedelta(days=2)}T10:00")
        store.add_task("Next week thing", start=f"{today + timedelta(days=7)}T10:00")
        text = brief.gather(store, calendar=["Wed 18 Nov 09:00  Capstone review"])
        self.assertIn("Capstone review", text)
        self.assertLess(text.index("Gym"), text.index("Lab record"))     # time order
        self.assertIn("Old form", text.split("Overdue:")[1])
        self.assertNotIn("Next week thing", text)
        self.assertTrue(brief.plain_plan(text).startswith("Today's plan"))

    def test_empty_day_is_stated_plainly(self):
        from jarvis import brief
        self.assertEqual(brief.gather(self.agent.store, calendar=[]), "(nothing scheduled)")


class PlanDeliveryTests(Base):
    def test_plan_is_kept_and_retried_until_the_phone_accepts_it(self):
        from jarvis.session import Session
        s = Session(self.cfg)
        phone = s.agent.registry.phone
        s.agent.store.set_meta("brief_pending", "Today's plan: gym at 7")
        sends = []
        phone.available = lambda: True
        phone.send = lambda title, text, file_path="": sends.append(text) or ([] if len(sends) < 2 else ["ntfy"])
        s._deliver_pending_plan(first_try=True)          # network down
        self.assertEqual(s.agent.store.get_meta("brief_pending"), "Today's plan: gym at 7")
        s._deliver_pending_plan()                        # network back
        self.assertEqual(s.agent.store.get_meta("brief_pending"), "")


class PlannerTests(Base):
    def setUp(self):
        super().setUp()
        from jarvis import planner
        self.planner = planner
        self.store = self.agent.store
        self.today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)

    def fire(self, task_id):
        """Pretend a task's start has arrived (as the reminder loop would)."""
        self.store.open_block(self.store.get_task(task_id))

    def test_free_slot_skips_what_is_planned_and_the_night(self):
        a = self.store.add_task("A", start=(self.today.replace(hour=10)).isoformat(), minutes=60)
        slot = self.planner.next_free_slot(self.store, 30, self.today.replace(hour=10, minute=0))
        self.assertGreaterEqual(slot, self.today.replace(hour=11, minute=5))
        late = self.today.replace(hour=22, minute=15)
        slot = self.planner.next_free_slot(self.store, 60, late)
        self.assertEqual((slot.date(), slot.hour), ((late + timedelta(days=1)).date(), 8))

    def test_missed_one_off_block_is_carried_once_to_tomorrow_evening_and_quietly(self):
        tid = self.store.add_task("Lab record", start=self.today.isoformat(), minutes=30)
        self.fire(tid)
        out = self.planner.sweep(self.store, self.today + timedelta(minutes=30 + 20))
        self.assertEqual((out["nudge"], out["missed"]), ([], []))              # nothing is announced early
        out = self.planner.sweep(self.store, self.today + timedelta(minutes=30 + 50))
        self.assertIn("carried to tomorrow at 7:30 PM", out["missed"][0])
        moved = self.store.get_task(tid)
        self.assertEqual(moved["start"][11:16], "19:30")
        self.assertEqual(moved["start"][:10], (self.today + timedelta(days=1)).date().isoformat())
        self.assertEqual(self.store.open_blocks(), [])
        self.assertEqual(self.planner.week_stats(self.store)["missed"], 1)

    def test_a_block_missed_again_is_let_go_not_moved_again(self):
        tid = self.store.add_task("Lab record", start=self.today.isoformat(), minutes=30)
        self.fire(tid)
        self.planner.sweep(self.store, self.today + timedelta(hours=2))                # first miss: carried
        self.fire(tid)
        later = datetime.fromisoformat(self.store.get_task(tid)["start"]) + timedelta(hours=3)
        out = self.planner.sweep(self.store, later)
        self.assertIn("missed twice", out["missed"][0])
        self.assertIsNone(self.store.get_task(tid))

    def test_a_missed_item_is_merged_when_the_same_thing_is_planned_tomorrow(self):
        a = self.store.add_task("LeetCode: one easy problem", start=self.today.isoformat(), minutes=30)
        self.store.add_task("LeetCode: one easy problem", start=(self.today + timedelta(days=1)).replace(hour=20).isoformat(), minutes=30)
        self.fire(a)
        out = self.planner.sweep(self.store, self.today + timedelta(hours=2))
        self.assertIn("merged into the one", out["missed"][0])
        self.assertEqual(len(self.store.list_tasks()), 1)                              # no pile-up of copies

    def test_done_block_is_counted_and_never_missed(self):
        tid = self.store.add_task("Revise", start=self.today.isoformat(), minutes=30)
        self.fire(tid)
        self.store.complete_task(tid)
        out = self.planner.sweep(self.store, self.today + timedelta(hours=3))
        self.assertEqual(out, {"nudge": [], "missed": []})
        st = self.planner.week_stats(self.store)
        self.assertEqual((st["done"], st["missed"], st["adherence"]), (1, 0, 100))

    def test_finishing_a_repeating_block_after_its_reminder_does_not_skip_the_next_one(self):
        tid = self.store.add_task("LeetCode", start=self.today.replace(day=self.today.day).isoformat(),
                                  minutes=45, repeat="daily")
        self.fire(tid)
        before = self.store.get_task(tid)["start"]
        self.store.complete_task(tid)
        self.assertEqual(self.store.get_task(tid)["start"], before)      # not rolled a second time

    def test_missed_daily_routine_is_logged_but_not_made_up(self):
        tid = self.store.add_task("LeetCode", start=self.today.isoformat(), minutes=45, repeat="daily")
        self.fire(tid)
        out = self.planner.sweep(self.store, self.today + timedelta(hours=3))
        self.assertIn("comes round again", out["missed"][0])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_missed_weekly_routine_gets_a_makeup(self):
        tid = self.store.add_task("80G filing", start=self.today.isoformat(), minutes=60, repeat="weekly")
        self.fire(tid)
        out = self.planner.sweep(self.store, self.today + timedelta(hours=3))
        self.assertIn("make-up", out["missed"][0])
        self.assertIn("Make-up: 80G filing", [t["title"] for t in self.store.list_tasks()])

    def test_deadlines_without_a_start_are_not_blocks(self):
        tid = self.store.add_task("Exam day", due=self.today.isoformat())
        self.fire(tid)
        self.assertEqual(self.store.open_blocks(), [])

    def test_replanning_a_block_does_not_count_as_missing_it(self):
        tid = self.store.add_task("Gym", start=self.today.isoformat(), minutes=30)
        self.fire(tid)
        self.store.update_task(tid, start=(self.today + timedelta(days=1)).isoformat())
        self.assertEqual(self.planner.week_stats(self.store)["missed"], 0)

    def test_week_learning_saves_a_missed_slot_insight_once(self):
        stats = {"missed_slots": [("Friday evening", 3)], "adherence": 40, "done": 2}
        self.assertEqual(len(self.planner.learn_from_week(self.store, stats)), 1)
        self.assertEqual(self.planner.learn_from_week(self.store, stats), [])


class StudyRagTests(Base):
    def make_pptx(self, path, slides):
        import zipfile
        with zipfile.ZipFile(path, "w") as z:
            for i, text in enumerate(slides, 1):
                z.writestr(f"ppt/slides/slide{i}.xml",
                           "<p:sld><a:p><a:r><a:t>" + text + "</a:t></a:r></a:p></p:sld>")

    def test_pages_are_cited_and_word_forms_match(self):
        self.agent.store.add_knowledge("OS notes.pdf", pages=[
            ("page 1", "Processes and threads share the CPU through scheduling."),
            ("page 7", "A deadlock needs four conditions: mutual exclusion, hold and wait, no preemption, circular wait."),
        ])
        found = self.run_tool("search_knowledge", {"query": "what causes deadlocks?"}).text
        self.assertIn("[OS notes.pdf, page 7]", found)
        self.assertNotIn("page 1", found.split("---")[0])
        self.assertIn("OS notes", self.run_tool("search_knowledge", {"query": "deadlock", "source": "os notes"}).text)
        self.assertIn("Nothing", self.run_tool("search_knowledge", {"query": "deadlock", "source": "dbms"}).text)

    def test_folders_of_slides_and_text_are_learned_with_slide_numbers(self):
        folder = Path(self.tmp.name) / "DBMS"
        folder.mkdir()
        self.make_pptx(folder / "unit2.pptx", ["Normalisation removes redundancy", "Third normal form removes transitive dependency"])
        (folder / "notes.txt").write_text("ACID means atomicity, consistency, isolation and durability.")
        (folder / "photo.jpg").write_bytes(b"x")
        said = self.run_tool("learn_folder", {"path": str(folder)}).text
        self.assertIn("Learned 2 files", said)
        hit = self.run_tool("search_knowledge", {"query": "transitive dependency"}).text
        self.assertIn("DBMS/unit2.pptx, slide 2", hit)

    def test_an_old_database_is_upgraded_without_losing_notes(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE VIRTUAL TABLE knowledge USING fts5(source, passage)")
        db.execute("INSERT INTO knowledge VALUES ('old.pdf', 'Semaphores coordinate processes')")
        db.commit()
        db.close()
        from jarvis.store import Store
        upgraded = Store(path)
        self.assertEqual(upgraded.search_knowledge("semaphore")[0]["source"], "old.pdf")
        upgraded.add_knowledge("new.pdf", pages=[("page 2", "Paging avoids external fragmentation")])
        self.assertEqual(upgraded.search_knowledge("fragment")[0]["page"], "page 2")

    def test_search_check_reports_how_often_the_right_page_comes_back(self):
        from jarvis import rag_eval
        topics = ["scheduling quantum round robin preemption dispatcher latency",
                  "normalisation functional dependency decomposition boyce codd",
                  "subnetting netmask gateway broadcast routing tables",
                  "paging segmentation fragmentation frames translation lookaside"]
        pages = [(f"page {i}", (t + " ") * 12) for i, t in enumerate(topics, 1)]
        self.agent.store.add_knowledge("mixed.pdf", pages=pages)
        result = rag_eval.evaluate(self.agent.store, samples=4)
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["top5"], 100)
        self.assertIn("Search check", rag_eval.describe(result))


class CalendarExportTests(Base):
    def test_schedule_becomes_a_valid_calendar_file(self):
        from jarvis import ics_export
        self.agent.store.add_task("LeetCode, daily; hard", start="2026-12-01T20:15", minutes=45, repeat="weekly")
        self.agent.store.add_task("Exam day", due="2026-11-23T09:00", priority="high")
        text = ics_export.build(self.agent.store)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR"))
        self.assertIn("DTSTART:20261201T201500", text)
        self.assertIn("DTEND:20261201T210000", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)
        self.assertIn("SUMMARY:LeetCode\, daily\; hard", text)
        self.assertIn("PRIORITY:1", text)
        path = ics_export.write(self.agent.store, Path(self.tmp.name))
        stamp = path.stat().st_mtime_ns
        ics_export.write(self.agent.store, Path(self.tmp.name))
        self.assertEqual(path.stat().st_mtime_ns, stamp)          # unchanged schedule: file not rewritten


class DialogueTests(unittest.TestCase):
    def test_classification_by_wording_and_timing(self):
        from jarvis.dialogue import classify
        self.assertEqual(classify("And open the Anti-Gravity folder", 9, 12), "continue")
        self.assertEqual(classify("also add a task", None, 12), "continue")
        self.assertEqual(classify("No, I meant the Documents folder", 2, 12), "correct")
        self.assertEqual(classify("Sorry, wait", 1, 12), "correct")
        self.assertEqual(classify("open the anti gravity folder", 4, 12), "continue")     # a quick follow-up joins
        self.assertEqual(classify("what time is it", 30, 12), "new")
        self.assertEqual(classify("what time is it", None, 12), "new")                    # typed: wording only

    def test_merge_keeps_both_parts(self):
        from jarvis.dialogue import interrupted_note, merge
        self.assertEqual(merge("Create a folder called AI on the desktop", "And open Antigravity"),
                         "Create a folder called AI on the desktop. And open Antigravity")
        self.assertIn("1 step", interrupted_note("Create a folder", 1))


class MidSpeechTests(WakeStopTests):
    """What happens to a second thing said while the first is still being processed."""

    def working_session(self, *replies):
        import threading
        self.release = threading.Event()
        gate = lambda confirm: (self.release.wait(5), "done")[1]       # noqa: E731
        s = self.session(gate, *replies)
        s.start()
        self.wait_for(s, lambda e: e["kind"] == "status" and e["status"] == "idle")
        s.status = "idle"
        return s

    def test_a_pause_then_more_speech_joins_the_request_instead_of_replacing_it(self):
        s = self.working_session("both parts handled")
        s.cfg.merge_seconds = 12
        s.submit("In a desktop, create a folder called AI.", source="voice")      # as the mic would
        self.wait_for(s, lambda e: e["kind"] == "user")
        s._heard("command", "And open the anti-gravity folder")
        said = self.wait_for(s, lambda e: e["kind"] == "jay")
        self.assertEqual(said["text"], "both parts handled")
        users = [e["text"] for e in s.events if e["kind"] == "user"]
        self.assertEqual(users[-1], "In a desktop, create a folder called AI. And open the anti-gravity folder")
        self.release.set()
        __import__('time').sleep(0.3)                 # let the dropped request finish winding down
        s.stop()

    def test_a_correction_replaces_and_carries_the_old_request_as_context(self):
        s = self.working_session("fixed")
        s.submit("Create a folder called AA", source="voice")
        self.wait_for(s, lambda e: e["kind"] == "user")
        s._heard("command", "No, I meant a folder called AI")
        self.wait_for(s, lambda e: e["kind"] == "jay")
        self.assertIn("Okay, switching to your correction.", [e.get("text") for e in s.events if e["kind"] == "info"])
        self.release.set()
        __import__('time').sleep(0.3)                 # let the dropped request finish winding down
        s.stop()

    def test_extra_work_waits_its_turn_when_the_first_part_has_already_acted(self):
        s = self.working_session("second part done")
        s.submit("Create a folder", source="voice")
        self.wait_for(s, lambda e: e["kind"] == "user")
        s.agent.registry.tools_run += 1                      # the first request has already done something
        s._heard("command", "and then open it")
        self.release.set()
        self.wait_for(s, lambda e: e["kind"] == "jay")
        self.assertIn("Got it, I'll do that right after this.", [e.get("text") for e in s.events if e["kind"] == "info"])
        s.stop()


class StallGuardTests(Base):
    def test_a_promise_without_action_is_pushed_to_act(self):
        from jarvis.providers import OpenAICompatProvider, STALLING
        self.assertTrue(STALLING.search("Let me check the volume settings, boss, one moment."))
        self.assertFalse(STALLING.search("Let me know if you need anything else."))
        client = FakeOpenAI([("Let me check the volume settings, boss.", None),
                             ("", [FakeCall("calculate", {"expression": "2+2"})]), ("It is four.", None)])
        provider = OpenAICompatProvider(self.cfg, self.reg, "sys", "gemini", client=client)
        self.assertEqual(provider.turn("hi", YES, QUIET), "It is four.")
        self.assertIn("called no tool", client.calls[1]["messages"][-1]["content"])


class AutopilotTests(Base):
    def setUp(self):
        super().setUp()
        self.reg.grants.set_autopilot(True)
        self.asked = []

    def ask_cb(self, summary):
        self.asked.append(summary)
        return False

    def test_everyday_actions_run_without_a_card(self):
        out = self.reg.execute("run_powershell", {"command": r"New-Item -ItemType Directory -Path $env:TEMP\jay_autopilot_test -Force"},
                               self.ask_cb)
        self.assertEqual(self.asked, [])
        self.assertFalse(out.is_error, out.text)
        import shutil, os
        shutil.rmtree(os.path.expandvars(r"%TEMP%\jay_autopilot_test"), ignore_errors=True)

    def test_hard_to_undo_things_still_ask(self):
        for tool, args in (("run_powershell", {"command": r"Remove-Item C:\Users\x\Documents -Recurse"}),
                           ("run_powershell", {"command": "iwr http://example.com/a.exe -OutFile a.exe"}),
                           ("browser_click", {"text": "Pay now"}),
                           ("browser_press", {"key": "Enter"}),
                           ("press_keys", {"keys": "alt+f4"}),
                           ("write_file", {"path": r"C:\Windows\System32\drivers\etc\hosts", "content": "x"})):
            self.asked.clear()
            self.reg.execute(tool, args, self.ask_cb)
            self.assertEqual(len(self.asked), 1, tool + str(args))

    def test_messages_and_power_never_ride_on_autopilot(self):
        for tool, args in (("whatsapp_send", {"chat": "Amma", "message": "see https://x.example/pay and the OTP 123456"}),
                           ("mail_send", {"to": "a@b.com", "subject": "s", "body": "b"}),
                           ("power_action", {"action": "shutdown"}),
                           ("install_skill", {"url": "https://example.com/SKILL.md"})):
            self.asked.clear()
            self.reg.execute(tool, args, self.ask_cb)
            self.assertEqual(len(self.asked), 1, tool)

    def test_switching_it_off_brings_the_cards_back(self):
        self.reg.grants.set_autopilot(False)
        self.reg.execute("run_powershell", {"command": "Get-Date"}, self.ask_cb)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("is OFF", self.reg.grants.command("/autoapprove"))
        self.assertIn("ON", self.reg.grants.command("/autoapprove on"))

    def test_every_autopilot_action_is_logged(self):
        self.reg.execute("run_powershell", {"command": "Get-Date"}, self.ask_cb)
        log = (self.reg.grants.log_path).read_text(encoding="utf-8")
        self.assertIn("Get-Date", log)


class AutoApproveWordingTests(unittest.TestCase):
    def test_harmless_commands_with_format_or_get_in_them_are_not_flagged(self):
        from jarvis.grants import needs_care
        for ok in ('Run PowerShell: Get-Date -Format "yyyy-MM-dd"', "Run PowerShell: Get-ChildItem D:/jay | Format-Table",
                   "Run PowerShell: New-Item -ItemType Directory -Path D:/x", "Run PowerShell: Get-Process | Sort CPU"):
            self.assertFalse(needs_care("run_powershell", ok), ok)
        for bad in ("Run PowerShell: format D: /q", "Run PowerShell: Format-Volume -DriveLetter D", "Run PowerShell: rm -r x",
                    "Run PowerShell: Remove-Item x"):
            self.assertTrue(needs_care("run_powershell", bad), bad)


class WhatsAppAutoApproveTests(Base):
    def test_plain_messages_go_out_but_risky_ones_ask(self):
        from jarvis.grants import needs_care
        nl = chr(10)
        head = "Send on WhatsApp to Ravi:" + nl
        self.assertFalse(needs_care("whatsapp_send", "Send on WhatsApp to Amma:" + nl + "I will be late, start dinner"))
        for risky in ("here is the link https://x.example", "my OTP is 482913", "the password is blue", "word " * 100):
            self.assertTrue(needs_care("whatsapp_send", head + risky), risky[:40])


class QuietRhythmTests(Base):
    def test_summaries_come_when_you_sit_down_not_on_the_clock(self):
        from jarvis import rhythm
        mon = datetime(2026, 11, 23)
        at = lambda h, m: mon.replace(hour=h, minute=m)                                  # noqa: E731
        self.assertEqual(rhythm.due(at(7, 10), "", "", 10, True)[0], "morning")           # first time at the laptop
        self.assertIsNone(rhythm.due(at(7, 10), "", "", 900, True)[0])                    # laptop idle: wait
        self.assertIsNone(rhythm.due(at(7, 10), "2026-11-23", "", 10, True)[0])           # already sent today
        self.assertIsNone(rhythm.due(at(18, 0), "2026-11-23", "", 10, True)[0])           # college day: not before 7:30
        self.assertEqual(rhythm.due(at(19, 35), "2026-11-23", "", 10, True)[0], "evening")
        self.assertEqual(rhythm.due(at(17, 40), "2026-11-23", "", 10, False)[0], "evening")   # a day off: earlier
        kind, skip = rhythm.due(at(13, 0), "", "", 10, True)
        self.assertEqual((kind, skip), (None, True))                                      # no "morning" plan at lunchtime

    def test_college_day_comes_from_the_calendar(self):
        from jarvis import rhythm
        day = datetime(2026, 11, 23).date()
        text = "Mon 23 Nov 09:00  CRT (9-5:30)\nTue 24 Nov 10:00  Dentist"
        self.assertTrue(rhythm.college_day(day, rhythm.calendar_lines(day, text)))
        tue = datetime(2026, 11, 24).date()
        self.assertFalse(rhythm.college_day(tue, rhythm.calendar_lines(tue, text)))
        self.assertTrue(rhythm.college_day(day, None))                                   # no calendar: a weekday counts
        self.assertFalse(rhythm.college_day(datetime(2026, 11, 22).date(), None))

    def test_planned_blocks_do_not_ring_but_plain_reminders_still_do(self):
        from jarvis.session import Session
        s = Session(self.cfg)
        s.cfg.reminder_mode = "digest"
        now = datetime.now()
        s.agent.store.add_task("Study block", start=(now - timedelta(minutes=1)).isoformat(), minutes=45)
        s.agent.store.add_task("Call Amma", start=(now - timedelta(minutes=1)).isoformat())
        said = []
        s.emit = lambda kind, **d: said.append((kind, d.get("text")))
        s.agent.registry.phone.send_async = lambda *a, **k: None
        calls = [0]

        def one_pass(timeout=None):
            calls[0] += 1
            return calls[0] > 1

        s.stopped.wait = one_pass
        s._reminders()
        texts = [t for k, t in said if k == "jay"]
        self.assertIn("Reminder: Call Amma", texts)
        self.assertNotIn("Reminder: Study block", texts)
        self.assertEqual(len(s.agent.store.open_blocks()), 1)                            # still tracked for the summary


class EvidenceTests(Base):
    def planner_stats(self):
        from jarvis import planner
        return planner.week_stats(self.agent.store)

    def test_a_block_with_a_real_solve_is_marked_done_with_the_reason(self):
        from jarvis import verify
        today = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
        tid = self.agent.store.add_task("LeetCode: one easy problem", start=today.isoformat(), minutes=30)
        with mock.patch.object(verify, "leetcode_solves", return_value=[("Two Sum", datetime.now())]):
            lines = verify.verify_today(self.agent.store, "someone", "")
        self.assertEqual(len(lines), 1)
        self.assertIn("solved Two Sum on LeetCode", lines[0])
        self.assertTrue(self.agent.store.get_task(tid)["done"])
        self.assertIn("verified", [e["kind"] for e in self.agent.store._rows("SELECT kind FROM events")])

    def test_no_solve_means_no_claim(self):
        from jarvis import verify
        self.agent.store.add_task("LeetCode: one easy problem", start=datetime.now().isoformat(), minutes=30)
        with mock.patch.object(verify, "leetcode_solves", return_value=[("Old problem", datetime.now() - timedelta(days=3))]):
            self.assertEqual(verify.verify_today(self.agent.store, "someone", ""), [])
        self.assertEqual(len(self.agent.store.list_tasks()), 1)

    def test_network_trouble_changes_nothing(self):
        from jarvis import verify
        self.agent.store.add_task("LeetCode: one easy problem", start=datetime.now().isoformat(), minutes=30)
        with mock.patch.object(verify, "leetcode_solves", side_effect=OSError("offline")):
            self.assertEqual(verify.verify_today(self.agent.store, "someone", ""), [])
        self.assertFalse(self.agent.store.list_tasks()[0]["done"])

    def test_a_block_marked_missed_is_corrected_when_proof_turns_up(self):
        from jarvis import planner, verify
        start = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        tid = self.agent.store.add_task("LeetCode: one easy problem", start=start.isoformat(), minutes=30)
        self.agent.store.open_block(self.agent.store.get_task(tid))
        planner.sweep(self.agent.store, start + timedelta(hours=3))
        self.assertEqual(self.planner_stats()["missed"], 1)
        # the carry-over moved it to tomorrow evening; proof arrives for the original day
        self.agent.store._run("UPDATE tasks SET start = ? WHERE id = ?", (start.isoformat(timespec="minutes"), tid))
        with mock.patch.object(verify, "leetcode_solves", return_value=[("Two Sum", start + timedelta(hours=4))]):
            verify.verify_today(self.agent.store, "someone", "", today=start.date())
        self.assertEqual(self.planner_stats()["missed"], 0)
        self.assertEqual(self.planner_stats()["done"], 1)


class SpokenSummaryTests(Base):
    def make(self):
        from jarvis.session import Session
        s = Session(self.cfg)
        said = []
        s.voice = mock.Mock(speak=lambda text: said.append(text))
        s.speak_replies = True
        s.status = "idle"
        return s, said

    def test_a_summary_is_read_aloud_when_you_are_at_the_laptop(self):
        s, said = self.make()
        s.cfg.digest_speak = True
        with mock.patch("jarvis.rhythm.idle_seconds", return_value=5):
            s._speak_digest("Evening check-in, boss.")
        self.assertEqual(said, ["Evening check-in, boss."])
        self.assertEqual(s.status, "idle")

    def test_it_stays_quiet_when_you_are_away_busy_or_have_turned_it_off(self):
        s, said = self.make()
        s.cfg.digest_speak = True
        with mock.patch("jarvis.rhythm.idle_seconds", return_value=900):
            s._speak_digest("away")
        with mock.patch("jarvis.rhythm.idle_seconds", return_value=5):
            s.status = "speaking"
            s._speak_digest("busy")
            s.status = "idle"
            s.cfg.digest_speak = False
            s._speak_digest("switched off")
            s.cfg.digest_speak, s.speak_replies = True, False
            s._speak_digest("voice off")
        self.assertEqual(said, [])

class RemoteSafetyTests(Base):
    def test_from_the_phone_commands_and_typing_still_ask_even_on_auto_approve(self):
        self.reg.grants.set_autopilot(True)
        asked = []
        decline = lambda summary: asked.append(summary) or False       # noqa: E731
        self.reg.remote = True
        self.reg.execute("run_powershell", {"command": "Get-Date"}, decline)
        self.assertEqual(len(asked), 1)                                   # one tap on the phone
        asked.clear()
        self.reg.execute("browser_open", {"url": "https://example.com"}, decline)
        self.assertEqual(asked, [])                                       # harmless things still just run
        self.reg.remote = False
        self.reg.execute("run_powershell", {"command": "Get-Date"}, decline)
        self.assertEqual(asked, [])                                       # at the laptop: no card

    def test_an_explicit_permission_still_applies_from_the_phone(self):
        self.reg.grants.add("shell", 60)
        self.reg.remote = True
        asked = []
        self.reg.execute("run_powershell", {"command": "Get-Date"}, lambda s: asked.append(s) or False)
        self.assertEqual(asked, [])
