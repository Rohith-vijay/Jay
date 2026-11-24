import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import grants, guide
from jarvis.agent import ROUTINES
from jarvis.store import Store
from jarvis.tools.registry import Registry

ROOT = Path(__file__).resolve().parent.parent


class AccuracyTests(unittest.TestCase):
    """The guide must not drift from what Jay really does."""

    def test_every_command_jay_understands_is_in_the_commands_list(self):
        source = (ROOT / "jarvis" / "session.py").read_text(encoding="utf-8")
        body = source[source.index("    def command(self, line: str)"):source.index("    def _check_in(self)")]
        found = set(re.findall(r'cmd == "(\w+)"', body))
        for group in re.findall(r"cmd in \(([^)]*)\)", body):
            found |= set(re.findall(r'"(\w+)"', group))
        found |= set(ROUTINES)                                        # /youtube, /content, /video ...
        found |= {"allow", "grants", "revoke", "queue", "approve", "reject", "posted", "draft", "publish", "connect",
                  "guide", "commands", "tour", "setup", "autopilot", "stop", "status"}
        missing = sorted(c for c in found if c not in guide.commands_in_text())
        self.assertEqual(missing, [], f"commands Jay understands but /commands doesn't list: {missing}")

    def test_every_command_in_the_list_really_exists(self):
        source = "".join((ROOT / "jarvis" / f).read_text(encoding="utf-8") for f in ("session.py", "grants.py", "content.py", "guide.py"))
        for name in guide.commands_in_text():
            self.assertTrue(f'"{name}"' in source or name in ROUTINES, f"/{name} is listed but nothing handles it")

    def test_topics_referred_to_exist(self):
        names = {t.name for t in guide.TOPICS}
        shown = " ".join([t.text for t in guide.TOPICS] + [ex for _, _, ex in guide.TOUR] + [guide.index_text(), guide.COMMANDS])
        mentioned = set(re.findall(r"/guide (\w+)", shown))
        self.assertEqual(sorted(m for m in mentioned if m not in names and m not in ("topic", "TOPIC", "followed")), [])

    def test_settings_named_in_the_guide_exist_in_the_code(self):
        sources = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "jarvis").rglob("*.py") if p.name != "guide.py")
        text = " ".join(t.text for t in guide.TOPICS) + guide.COMMANDS
        for setting in set(re.findall(r"\b(?:JARVIS|INSTAGRAM|PEXELS|TELEGRAM|GEMINI|GROQ|ANTHROPIC)_[A-Z_]+\b", text)):
            self.assertIn(setting, sources + (ROOT / ".env.example").read_text(encoding="utf-8"), f"{setting} is in the guide but not in the code")

    def test_permission_kinds_listed_match_the_real_ones(self):
        text = guide.topic_text("permissions")
        line = next(l for l in text.splitlines() if l.startswith("Kinds:"))
        self.assertEqual(set(re.findall(r"\w+", line.removeprefix("Kinds:"))), set(grants.CATEGORIES))

    def test_skills_named_exist(self):
        from jarvis.skills import SkillBook

        with tempfile.TemporaryDirectory() as d:
            real = set(SkillBook(Path(d)).skills)
        for name in ("meeting-notes", "topic-research", "web-summarize", "pdf-summarize", "email-draft", "calendar-prep",
                     "todo-from-notes", "code-lint", "security-scan"):
            self.assertIn(name, real)

    def test_every_topic_has_content_and_is_findable(self):
        for t in guide.TOPICS:
            self.assertTrue(t.text.strip() and t.title)
            self.assertIs(guide.find_topic(t.name), t)
        self.assertEqual(len({t.name for t in guide.TOPICS}), len(guide.TOPICS))


class FindingTests(unittest.TestCase):
    def test_plain_words_find_the_right_topic(self):
        self.assertEqual(guide.find_topic("make a video").name, "video")
        self.assertEqual(guide.find_topic("how do I build an app").name, "build")
        self.assertEqual(guide.find_topic("what is autopilot").name, "routines")
        self.assertEqual(guide.find_topic("stop asking me for permission").name, "permissions")
        self.assertIsNone(guide.find_topic("zzzz"))
        self.assertIsNone(guide.find_topic("  "))

    def test_unknown_topics_fall_back_to_the_index(self):
        text = guide.topic_text("zzzz")
        self.assertIn("don't have a topic", text)
        self.assertIn("/guide video", text)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = Path(self.dir.name)
        self.store = Store(self.data / "t.db")
        self.addCleanup(self.store._db.close)
        from jarvis import content

        self.reg = Registry()
        self.reg.grants = grants.Grants(self.data)
        self.reg.content = content.ContentQueue(self.store)
        self.reg.routines = content.Routines(self.store)
        self.guide = guide.Guide(self.store, self.data, self.reg)
        guide.register(self.reg, self.guide)


class CommandTests(Base):
    def test_guide_and_commands(self):
        self.assertIn("/guide video", self.guide.handle("/guide"))
        self.assertIn("Making videos", self.guide.handle("/guide video"))
        self.assertEqual(self.guide.handle("/commands"), guide.COMMANDS)
        self.assertIsNone(self.guide.handle("/allow mail"))
        self.assertIsNone(self.guide.handle("   "))

    def test_tour_walks_forward_back_and_stops(self):
        self.assertIn("step 1 of", self.guide.handle("/tour"))
        self.assertIn("step 2 of", self.guide.handle("/tour next"))
        self.assertIn("step 3 of", self.guide.handle("/tour next"))
        self.assertIn("step 2 of", self.guide.handle("/tour back"))
        self.assertIn("step 5 of", self.guide.handle("/tour 5"))
        for _ in range(20):
            last = self.guide.handle("/tour next")
        self.assertIn(f"step {len(guide.TOUR)} of", last)
        self.assertIn("that's the end", last)
        self.assertIn("Tour closed", self.guide.handle("/tour stop"))
        self.assertIn("step 1 of", self.guide.handle("/tour"))

    def test_autopilot_switches_routines_on_and_off(self):
        self.assertIn("✖ off", self.guide.handle("/autopilot"))
        self.assertIn("Added", self.guide.handle("/autopilot on"))
        names = {r["name"] for r in self.reg.routines.all()}
        self.assertEqual(names, {n for n, *_ in guide.AUTOPILOT})
        self.assertIn("already on", self.guide.handle("/autopilot on"))
        self.assertEqual(len(self.reg.routines.all()), 3)                      # no duplicates
        weekly = next(r for r in self.reg.routines.all() if "weekly" in r["name"])
        self.assertEqual((weekly["days"], weekly["at"]), ("mon", "09:00"))
        self.assertIn("Draft my next content", weekly["prompt"])
        self.reg.routines.add("my own", "something of my own choosing", "fri", "18:00")
        self.assertIn("3 routines removed", self.guide.handle("/autopilot off"))
        self.assertEqual([r["name"] for r in self.reg.routines.all()], ["my own"])      # only its own are touched
        self.assertIn("✔ on", self.guide.handle("/autopilot on").replace("Autopilot is on", "✔ on"))

    def test_setup_check_reports_connected_and_missing_with_next_steps(self):
        env = {"GEMINI_API_KEY": "k", "GROQ_API_KEY": "", "ANTHROPIC_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "JARVIS_MAIL_USER": "",
               "JARVIS_MAIL_PASSWORD": "", "INSTAGRAM_ACCESS_TOKEN": "", "INSTAGRAM_PUBLISH_TOKEN": "", "PEXELS_API_KEY": "x",
               "JARVIS_AUTOPUBLISH": "0"}
        (self.data / "google_credentials.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, env), mock.patch("jarvis.builder.docker_ready", return_value=False), \
                mock.patch("shutil.which", return_value=None):
            text = self.guide.handle("/setup")
        self.assertIn("✔ AI brains (Gemini)", text)
        self.assertIn("✔ Pexels", text)
        self.assertIn("✔ Google sign-in file", text)
        self.assertIn("✖ Telegram bot", text)
        self.assertIn("@BotFather", text)                                # says what to do next
        self.assertIn("✖ Docker", text)
        self.assertRegex(text, r"Setup check: \d+ of \d+ connected")

    def test_setup_notices_a_paired_phone(self):
        telegram = mock.Mock(chat_id=42)
        with mock.patch("jarvis.builder.docker_ready", return_value=True):
            text = guide.setup_text(self.data, self.reg, telegram)
        self.assertIn("✔ Phone paired", text)
        self.assertIn("✔ Docker", text)


class ToolTests(Base):
    def run_tool(self, name, **args):
        return self.reg.execute(name, args, lambda s: True).text

    def test_the_guide_tool_answers_from_the_guide(self):
        self.assertIn("Making videos", self.run_tool("guide", topic="video"))
        self.assertEqual(self.run_tool("guide", topic="commands"), guide.COMMANDS)
        self.assertIn("Jay's guide", self.run_tool("guide"))
        self.assertIn("don't have a topic", self.run_tool("guide", topic="zzzz"))

    def test_check_setup_tool(self):
        with mock.patch("jarvis.builder.docker_ready", return_value=False):
            self.assertIn("Setup check", self.run_tool("check_setup"))

    def test_check_setup_is_owner_only_and_guide_is_for_everyone(self):
        from jarvis.tools.registry import PRIVATE_TOOLS

        self.assertIn("check_setup", PRIVATE_TOOLS)
        self.assertNotIn("guide", PRIVATE_TOOLS)


class SessionTests(unittest.TestCase):
    def test_commands_work_from_the_phone_and_the_keyboard(self):
        import shutil

        from jarvis.config import Config
        from jarvis.session import Session

        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        cfg = Config.load(data_dir=Path(folder))
        cfg.checkin_minutes, cfg.morning_brief = 0, False
        s = Session(cfg)
        self.addCleanup(s.agent.store._db.close)                 # runs before the folder is removed
        self.assertIn("Making videos", s.phone_command("/guide video"))
        self.assertIn("step 1 of", s.phone_command("/tour"))
        self.assertIn("/youtube", s.phone_command("/commands"))
        s.command("/guide posting")
        self.assertTrue(any("Posting to YouTube" in e.get("text", "") for e in s.events))
        s.command("/autopilot")
        self.assertTrue(any("Autopilot" in e.get("text", "") for e in s.events))
        self.assertIn("guide", {t.name for t in s.agent.registry.tools()})


if __name__ == "__main__":
    unittest.main()
