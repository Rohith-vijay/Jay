"""The running assistant, independent of how it is displayed.

One worker thread owns the agent (and its browser). Everything else talks to it
through `submit()` and reads what happens from a stream of events, which the
window (jarvis/ui.py) or the console (jarvis/__main__.py) turns into a display.

Voice works like "Hey Siri":
  standby  - waiting for "Hey Jay"; nothing else is acted on
  awake    - in conversation; follow-ups need no wake word for a short while
  "Stop Jay" - stops whatever is happening and returns to standby
"""
from __future__ import annotations

import os
import queue
import threading
import time
from . import dialogue
from datetime import date, datetime

from .agent import ROUTINES, Agent
from .config import Config
from .profile import Profile
from .speakers import Speakers
from .watches import Watches, backup, register_git
from .watches import register as register_watches
from .store import describe_task

APPROVAL_TIMEOUT = 300   # seconds; an unanswered approval counts as "no"
PHONE_APPROVAL_TIMEOUT = 900   # when the request came from the phone, there is more time to tap a button
MIRROR_AFTER = 20        # a card nobody answers on the laptop for this long is also sent to the phone
# Said the moment Jay starts a job that takes a few steps, so there is no silent wait.
ACKNOWLEDGEMENTS = ["On it.", "One moment.", "Right away.", "Leave it with me.", "Checking now."]
BUSY = {"starting", "thinking", "speaking", "waiting", "listening"}

HELP = """\
/brief       briefing: weather, schedule, calendar, email, news in your fields
/night       end-of-day summary and tomorrow's first commitments
/quiz [topic]    quiz me from my notes, one question at a time
/revise      revision plan for my upcoming exams
/plan        plan my day and apply it to the schedule
/news        what's new in the fields you follow
/messages    WhatsApp: unread chats and suggested replies (sends only with your approval)
/dashboards  check the pages you want watched and report what's new
/deadlines   find exam dates and deadlines in email (needs Google)
/erp         open the college ERP portal and summarise it
/study TOPIC teach a topic from your notes, with page citations
/exportcalendar  save your schedule as an .ics file for Google or Outlook
/ragcheck    test how well notes search finds the right page
/evening     send the evening check-in now
/weekly      this week's done/missed summary
/autoapprove  on/off: do everyday things without approval cards
/pushbrief   send today's plan to your phone now
/timetable   learn a timetable file, e.g. /timetable D:/timetable.pdf
/getknow     Jay interviews you to learn who you are
/jobs        find and shortlist openings in your domain
/youtube     analyse my YouTube channel and plan the next videos
/content     draft the next videos and posts for my channels (then /queue, /approve N)
/video N     make the video for draft N (voiceover, pictures, captions)
/publish N   post draft N now to YouTube / Instagram (it must be approved, with a video)
/guide       learn what Jay can do: /guide TOPIC, /commands, /tour, /setup, /autopilot
/connect youtube   one-time sign-in that lets Jay upload to your channel
/instagram   analyse my Instagram and plan the next posts
/patterns    what the numbers say about how you work
/reflect     review recent conversations now and save what was learned
/model NAME  switch brain ({brains}), or /model auto for the quickest
/memory      what I remember about you
/tasks       the schedule
/reset       forget this conversation (long-term memory is kept)"""


class Inbox(queue.Queue):
    """Work for the worker thread. Anything the user asks goes ahead of background jobs
    (schedule check-ins, watches, the night summary), so a slow background job never makes
    them wait behind the queue; within each kind, first come first served."""

    BACKGROUND = frozenset({"checkin", "watches", "night", "startup", "cookies", "briefpush", "weekly", "routine", "evening"})

    def _init(self, maxsize) -> None:
        self.queue, self._count, self.last_wait = [], 0, 0.0

    def _qsize(self) -> int:
        return len(self.queue)

    def _put(self, item) -> None:
        import heapq
        self._count += 1
        heapq.heappush(self.queue, (item[0] in self.BACKGROUND, self._count, time.monotonic(), item))

    def _get(self):
        import heapq
        *_, queued, item = heapq.heappop(self.queue)
        self.last_wait = time.monotonic() - queued
        return item


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.inbox = Inbox()
        self.events: list[dict] = []
        self._changed = threading.Condition()
        self._approvals: dict[int, list] = {}       # id -> [Event, answer]
        self.status = "starting"
        self.voice = None
        self.speak_replies = False
        self.handsfree_stop: threading.Event | None = None
        self.awake = False
        self.awake_until = 0.0
        self.on_wake = lambda: None                 # the window layer uses this to open the window
        self.on_mini = lambda on: False             # the window layer supplies the real resizing
        self.on_close_window = lambda: None         # ... and this to note the window is being put away
        self.stopped = threading.Event()
        self.quitting = threading.Event()
        self._background = False             # True during check-ins and the morning brief
        self._phone_turn = False             # True while answering a request that came from the phone
        self._last_step = ""                 # what Jay last did, for progress messages
        self._approval_tools: dict[int, str | None] = {}
        self.phone_approvals = os.getenv("JARVIS_PHONE_APPROVALS", "1") != "0"
        self._acknowledged = True            # one quick "On it." per request at most
        self._ack_turn = 0
        self._job: threading.Thread | None = None    # the request being worked on, if any
        self._job_text = ""                          # what that request says
        self._job_input_at = 0.0                     # when its latest words arrived
        self._job_tools0 = 0                         # tools run before it started (to tell if it has done anything)
        self._note_next: str | None = None           # context for the next request (an interrupted one)
        self._preempt = threading.Event()            # set when it should be abandoned
        self.agent = Agent(cfg, self.confirm, notice=self._step)
        self.speakers = Speakers(cfg.data_dir)
        self.watches = Watches(self.agent.store, self.agent.registry)
        register_watches(self.agent.registry, self.watches)
        register_git(self.agent.registry)
        from .guide import Guide, register as register_guide
        self.guide = Guide(self.agent.store, cfg.data_dir, self.agent.registry, telegram=lambda: self.telegram)
        register_guide(self.agent.registry, self.guide)
        self.enrolling: dict | None = None   # voice setup in progress
        self.pending_enrol: str | None = None
        self.last_speaker: str | None = None
        self._register_voice_tools()
        self.last_reply = ""
        self.telegram = None
        self.agent.registry.on_result = self._card
        self.agent.registry.content.on_new = self._send_draft

    # ---- voices: who is speaking --------------------------------------------
    def role_for(self, name: str | None) -> str:
        if not self.speakers.enrolled:
            return "owner"                   # nobody set up yet: everyone is treated as the owner
        if name is None:
            return "guest"
        return "owner" if name == self.speakers.owner else "member"

    def known_voice(self, audio) -> bool:
        """Should this utterance be listened to? Only voices Jay knows, once anyone is set up."""
        if not self.speakers.enrolled or self.cfg.wake_anyone:
            return True
        try:
            found = self.speakers.best(audio)
        except Exception:
            return True                      # never lock the user out because of a recognition fault
        from .speakers import GATE_THRESHOLD
        ok = found is not None and found[1] >= GATE_THRESHOLD
        self._voice_log({"check": "known", "best": found[0] if found else None,
                         "score": found[1] if found else None, "seconds": round(len(audio) / 16000, 1), "ok": ok})
        return ok

    def _voice_log(self, entry: dict) -> None:
        """A small record of recognition decisions (no audio, no words) for tuning thresholds."""
        import json
        try:
            path = self.cfg.data_dir / "voice_log.jsonl"
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
            lines = path.read_text(encoding="utf-8").splitlines()[-499:] if path.exists() else []
            path.write_text("\n".join([*lines, json.dumps(entry)]) + "\n", encoding="utf-8")
        except Exception:
            pass

    def identify(self, audio) -> str | None:
        if audio is None or not self.speakers.enrolled:
            return None
        try:
            match = self.speakers.identify(audio)
            self._voice_log({"check": "who", "best": match.name, "score": match.score,
                             "seconds": round(len(audio) / 16000, 1)})
            if match.name is None and self.awake and self.last_speaker not in (None, "Guest")                     and len(audio) < 16000 * 1.2:
                return self.last_speaker     # a short follow-up in the same conversation: same person
            return match.name
        except Exception as exc:
            self.emit("info", text=f"Voice recognition problem: {type(exc).__name__}: {exc}")
            return None

    CARD_TOOLS = {"get_weather": "WEATHER", "get_news": "HEADLINES", "convert_currency": "EXCHANGE",
                  "wikipedia": "BRIEFING", "set_timer": "TIMER", "list_tasks": "SCHEDULE", "add_watch": "WATCH"}

    def _card(self, name: str, args, result) -> None:
        """Show a visual card in the window for results worth seeing, not just hearing."""
        if name in self.CARD_TOOLS and not result.is_error and result.text:
            self.emit("card", title=self.CARD_TOOLS[name], tool=name, text=result.text[:1200],
                      subject=next((str(v) for v in (args or {}).values() if isinstance(v, str)), ""))

    def set_mini(self, on: bool) -> bool:
        """Small floating window in the corner (on) or the normal window (off)."""
        ok = self.on_mini(on)
        self.emit("mini", on=on)
        return ok

    def _register_voice_tools(self) -> None:
        reg = self.agent.registry

        @reg.tool("mini_mode",
                  "Shrink Jay's window to a small floating panel in the corner of the screen (on), or "
                  "bring back the full window (off). For 'mini mode', 'make yourself small', 'go full screen'.",
                  {"on": {"type": "boolean"}}, required=("on",))
        def mini_mode(on: bool) -> str:
            self.set_mini(on)
            return "Mini mode on." if on else "Back to the full window."

        @reg.tool("check_progress",
                  "Check whether today's LeetCode problem and coding blocks were really done, from the user's "
                  "public LeetCode and GitHub profiles, and mark them done if so. Use it when they ask "
                  "'did I do my LeetCode?' or 'what did I finish today?', before saying a block was missed.")
        def check_progress() -> str:
            from . import verify
            return verify.progress_report(self.agent.store, self.cfg.leetcode_user, self.cfg.github_user)

        @reg.tool("check_microphone",
                  "Check whether the microphone is working right now and why Jay might not be hearing the "
                  "user: whether Windows delivers any sound, which input is in use, and what to try. Use it "
                  "when they say you can't hear them or aren't listening, instead of promising to check.")
        def check_microphone() -> str:
            if not self.voice and not self.enable_voice():
                return "Voice isn't available on this laptop (the voice packages did not load)."
            v = self.voice
            import time as _t
            alive = _t.monotonic() - getattr(v, "heartbeat", 0.0) < 3
            silent = getattr(v, "silent_for", 0.0)
            try:
                import sounddevice as sd
                name = sd.query_devices(v.device if v.device is not None else sd.default.device[0])["name"]
            except Exception:
                name = "unknown"
            lines = [f"Input in use: {name}.",
                     f"Listener running: {'yes' if alive and self.handsfree_stop else 'NO, hands-free is off or stalled'}.",
                     f"Level right now: {v.mic_level:.2f} (0 = silence)."]
            if silent > 10:
                lines.append(f"Windows has delivered pure silence for {int(silent)} seconds, so the microphone is muted, "
                             "blocked by a driver or noise-suppression setting, or held by another app. Try the laptop's "
                             "mic key, Settings > Sound > Input, or scripts\fix_microphone.ps1 as administrator.")
            else:
                lines.append("Sound is arriving normally, so say 'Hey Jay' clearly, or type to me.")
            return " ".join(lines)

        @reg.tool("learn_voice",
                  "Learn a person's voice so you recognise them from now on. Right after your reply, "
                  "the app shows them four short lines to read aloud. Use it when someone asks you to "
                  "learn or remember their voice.",
                  {"name": {"type": "string", "description": "The person's first name."}}, required=("name",))
        def learn_voice(name: str) -> str:
            if not self.voice and not self.enable_voice():
                return "Voice isn't available on this laptop, so voices can't be learned."
            self.pending_enrol = name.strip().title()
            return (f"Voice setup for {self.pending_enrol} starts as soon as you finish speaking: tell them to "
                    "read the lines that appear on screen, one at a time, in their normal voice.")

        @reg.tool("list_voices", "Who you can recognise by voice, and who the owner is.")
        def list_voices() -> str:
            people = self.speakers.enrolled
            if not people:
                return "No voices are set up yet."
            return "Recognised voices: " + ", ".join(
                f"{p} (owner)" if p == self.speakers.owner else p for p in people)

        @reg.tool("forget_voice", "Stop recognising a person's voice and delete their voiceprint.",
                  {"name": {"type": "string"}}, required=("name",))
        def forget_voice(name: str) -> str:
            ok = self.speakers.remove(name)
            self.emit("refresh")
            return f"Forgot {name}'s voice." if ok else f"No voice saved for {name}."

        @reg.tool("set_owner_voice", "Make another recognised person the owner (the one with full access).",
                  {"name": {"type": "string"}}, required=("name",))
        def set_owner_voice(name: str) -> str:
            ok = self.speakers.set_owner(name)
            self.emit("refresh")
            return f"{name.title()} is now the owner." if ok else f"No voice saved for {name}."

    def start_enrol(self, name: str) -> None:
        from .speakers import ENROL_LINES

        if not self.enable_voice():
            return
        self.enrolling = {"name": name, "clips": [], "step": 0}
        self.set_status("idle")
        self.emit("enrol", name=name, step=1, total=len(ENROL_LINES), line=ENROL_LINES[0])
        self.on_wake()
        if not self.handsfree_stop:          # no background listening: record here instead
            threading.Thread(target=self._enrol_by_recording, daemon=True).start()

    def _enrol_by_recording(self) -> None:
        while self.enrolling:
            audio = self.voice.record(wait_seconds=12)
            if not self.enrolling:
                return
            if audio is None:
                self.emit("info", text="I didn't hear anything; voice setup cancelled.")
                self.enrolling = None
                self.emit("enrol_done", ok=False)
                return
            self._enrol_audio(audio)

    def _enrol_audio(self, audio) -> None:
        from .speakers import ENROL_LINES, MIN_SECONDS, SAMPLE_RATE

        job = self.enrolling
        if not job:
            return
        if len(audio) < SAMPLE_RATE * MIN_SECONDS:
            self.emit("enrol", name=job["name"], step=job["step"] + 1, total=len(ENROL_LINES),
                      line=ENROL_LINES[job["step"]], retry=True)
            return
        job["clips"].append(audio)
        job["step"] += 1
        if job["step"] < len(ENROL_LINES):
            self.emit("enrol", name=job["name"], step=job["step"] + 1, total=len(ENROL_LINES), line=ENROL_LINES[job["step"]])
            return
        self.enrolling = None
        try:
            was_first = not self.speakers.enrolled
            self.speakers.enrol(job["name"], job["clips"])
            owner = " You're the owner, with full access." if was_first else ""
            self.emit("enrol_done", ok=True, name=job["name"])
            self.inbox.put(("announce", f"Got it, {job['name']}. I'll recognise your voice from now on.{owner}"))
        except Exception as exc:
            self.emit("enrol_done", ok=False)
            self.emit("info", text=f"Voice setup didn't work: {exc}")
        self.emit("refresh")

    def cancel_enrol(self) -> None:
        if self.enrolling:
            self.enrolling = None
            self.emit("enrol_done", ok=False)

    def _step(self, text: str) -> None:
        self._last_step = text
        self.emit("step", text=text)
        if self._acknowledged or not (self.voice and self.speak_replies):
            return
        self._acknowledged = True
        phrase = ACKNOWLEDGEMENTS[self._ack_turn % len(ACKNOWLEDGEMENTS)]
        self._ack_turn += 1
        threading.Thread(target=self.voice.say_ready, args=(phrase,), daemon=True).start()

    # ---- events --------------------------------------------------------------
    def emit(self, kind: str, **data) -> None:
        with self._changed:
            self.events.append({"id": len(self.events) + 1, "kind": kind,
                                "time": datetime.now().strftime("%H:%M"), **data})
            self._changed.notify_all()

    def events_after(self, after: int, timeout: float = 20) -> list[dict]:
        with self._changed:
            if len(self.events) <= after:
                self._changed.wait(timeout)
            return self.events[after:]

    def set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self.emit("status", status=status)

    # ---- approvals -----------------------------------------------------------
    def confirm_on_phone(self, summary: str) -> bool:
        """Ask on the phone only, whatever else is going on (used for things Jay does by itself, like a scheduled post)."""
        return self.confirm(summary, phone_only=True)

    def confirm(self, summary: str, phone_only: bool = False) -> bool:
        phone_turn = self._phone_turn or phone_only
        if self._background and not phone_turn:
            # Work done while you're away never pops up a request, and never does anything risky on its own;
            # it says once, on the phone, what it held back and why.
            held = getattr(self, "_held", None)
            if held is None:
                held = self._held = set()
            if summary not in held and len(held) < 20:
                held.add(summary)
                self.agent.registry.phone.send_async("Jay · needs your OK", f"I held back, waiting for you: {summary[:300]}")
            return False
        approval_id = len(self._approvals) + 1
        waiter = [threading.Event(), False]
        self._approvals[approval_id] = waiter
        self._approval_tools[approval_id] = self.agent.registry.last_confirm[0]
        before = self.status
        mirror = None
        if phone_turn:                               # asked from the phone: the answer comes from the phone too
            self._ask_phone(approval_id, summary)
            timeout = PHONE_APPROVAL_TIMEOUT
        else:
            self.emit("approval", approval=approval_id, summary=summary)
            self.set_status("waiting")
            self.on_wake()                           # make sure the card can be seen
            mirror = threading.Timer(MIRROR_AFTER, self._ask_phone, (approval_id, summary))
            mirror.daemon = True
            mirror.start()                           # nobody at the laptop? the phone gets it too
            timeout = APPROVAL_TIMEOUT
        answered = waiter[0].wait(timeout)
        if mirror is not None:
            mirror.cancel()
        allowed = bool(answered and waiter[1])
        if not phone_turn:
            self.emit("approval_done", approval=approval_id, allowed=allowed)
            self.set_status(before)
        if self.telegram is not None:
            self.telegram.settle(approval_id, ("✔ Allowed." if allowed else "✖ Denied.") if answered else "⌛ No answer; I did not do it.")
        return allowed

    def _ask_phone(self, approval_id: int, summary: str) -> None:
        """Send this approval to the paired phone, with buttons, unless it was answered already."""
        waiter = self._approvals.get(approval_id)
        tg = self.telegram
        if not self.phone_approvals or tg is None or not tg.chat_id or waiter is None or waiter[0].is_set():
            return
        from .grants import DEFAULT_MINUTES, HIGH_RISK, category_of
        category = category_of(self._approval_tools.get(approval_id) or "")
        label = ""
        if category:
            minutes = DEFAULT_MINUTES[category in HIGH_RISK]
            label = f"Allow {category} for {minutes // 60}h"
        try:
            tg.ask(approval_id, summary, label)
        except Exception as exc:
            self.emit("info", text=f"Couldn't send the approval to your phone: {exc}")

    def on_phone_approval(self, approval_id: int, choice: str) -> str:
        """A button tapped on the phone (called on the Telegram thread)."""
        waiter = self._approvals.get(approval_id)
        if waiter is None or waiter[0].is_set():
            return "That was already answered."
        if choice == "g":
            from .grants import category_of
            category = category_of(self._approval_tools.get(approval_id) or "")
            if category:
                grant = self.agent.registry.grants.add(category)
                self.answer_approval(approval_id, True)
                return f"Allowed. {grant.describe(self.agent.registry.grants.clock())}"
            choice = "y"
        self.answer_approval(approval_id, choice == "y")
        return "Allowed." if choice == "y" else "Denied."

    def _send_draft(self, item_id: int) -> None:
        """A new draft goes to the phone with Approve / Reject buttons (in the background, never delaying Jay)."""
        tg = self.telegram
        item = self.agent.registry.content.get(item_id)
        if tg is None or not tg.chat_id or item is None:
            self.emit("info", text=f"Draft #{item_id} is waiting for your approval: /queue")
            return
        from .content import phone_card

        def send() -> None:
            try:
                tg.ask_content(item_id, phone_card(item))
            except Exception as exc:
                self.emit("info", text=f"Couldn't send draft #{item_id} to your phone: {exc}")

        threading.Thread(target=send, daemon=True).start()

    def on_phone_content(self, item_id: int, action: str) -> str:
        queue = self.agent.registry.content
        return queue.approve(item_id) if action == "a" else queue.reject(item_id)

    def _deliver_due_content(self) -> None:
        """Approved drafts whose time has come: everything ready to post goes to the phone and the desktop."""
        queue = self.agent.registry.content
        from .content import due_message

        publisher = self.agent.registry.publisher
        for item in queue.due():
            if publisher.autopublish_enabled() and publisher.ready_platforms(item):
                queue.mark_due(item["id"])
                threading.Thread(target=self._autopublish, args=(item["id"],), daemon=True).start()
                continue
            text = due_message(item)
            media = item["media_path"] if item["media_path"] and os.path.isfile(item["media_path"]) else ""
            phone = self.agent.registry.phone
            if phone.available():
                phone.send("Jay · time to post", text, media)
            try:
                from .tools.system import notify
                notify("Jay · time to post", item["title"][:120])
            except Exception:
                pass
            self.emit("info", text=text)
            queue.mark_due(item["id"])

    def _autopublish(self, item_id: int) -> None:
        """A scheduled post whose time has come: post it if you said yes (a standing permission, or a button on the
        phone). If not, or if posting fails, you get the ready-to-post package instead."""
        from .content import due_message

        registry = self.agent.registry
        result = registry.execute("publish_content", {"id": item_id}, self.confirm_on_phone)
        item = registry.content.get(item_id)
        posted = item is not None and item["status"] == "posted"
        phone = registry.phone
        if posted:
            message = result.text
        else:
            message = (f"I didn't post #{item_id} ({result.text.strip()[:200]}). Here it is, ready to post yourself:\n\n"
                       + due_message(item))
        if phone.available():
            phone.send("Jay · posted" if posted else "Jay · time to post", message)
        self.emit("info", text=message)
        try:
            from .tools.system import notify
            notify("Jay · posted" if posted else "Jay · time to post", (item["title"] if item else "")[:120])
        except Exception:
            pass

    def publish_command(self, text: str) -> str | None:
        """/publish N (post now, you asked for it) and /connect youtube (one-time sign-in for uploading)."""
        parts = text.strip().split()
        cmd = parts[0].lower().lstrip("/") if parts else ""
        arg = parts[1].lstrip("#") if len(parts) > 1 else ""
        if cmd == "connect":
            if arg.lower() != "youtube":
                return "Say /connect youtube (Instagram uses INSTAGRAM_PUBLISH_TOKEN in .env)."

            def connect() -> None:
                try:
                    self.emit("info", text=self.agent.registry.publisher.youtube.authorize())
                except Exception as exc:
                    self.emit("info", text=f"Couldn't connect YouTube: {exc}")

            threading.Thread(target=connect, daemon=True).start()
            return "A Google sign-in is opening on the laptop. Allow uploading, then you're set."
        if cmd != "publish":
            return None
        if not arg.isdigit():
            return "Say which one, e.g. /publish 3."

        def post() -> None:
            from .publish import PublishError

            try:
                message = self.agent.registry.publisher.publish(int(arg), ignore_schedule=True)
            except PublishError as exc:
                message = f"Not posted: {exc}"
            except Exception as exc:
                message = f"Posting failed: {type(exc).__name__}: {str(exc)[:200]}"
            self.emit("info", text=message)
            self.agent.registry.phone.send_async("Jay · posting", message)

        threading.Thread(target=post, daemon=True).start()
        return f"Posting #{arg} now. I'll message you the result."

    def _run_routine(self, routine_id: int) -> None:
        routine = self.agent.registry.routines.get(routine_id)
        if routine is None:
            return
        prompt = f"(Routine '{routine['name']}', running by itself on schedule.) {routine['prompt']}"
        tg = self.telegram
        if tg is not None and tg.chat_id:
            self._from_phone(prompt, tg.chat_id, quiet=True)
        else:
            self._background = True
            try:
                self.agent.registry.cancelled.clear()
                self.answer(prompt, shown=f"routine: {routine['name']}", source="routine")
            finally:
                self._background = False

    def phone_command(self, text: str) -> str | None:
        """Commands sent from the phone that work at once, even while Jay is busy."""
        cmd = text.strip().split(None, 1)[0].lower()
        if cmd == "/stop":
            self.stand_down(close_window=False, sound=False)
            for name in list(self.agent.registry.builds.jobs):
                self.agent.registry.builds.stop(name)
            return "Stopped. I dropped what I was doing and refused anything waiting."
        if cmd == "/status":
            waiting = sum(1 for w in self._approvals.values() if not w[0].is_set())
            lines = [f"Jay is {self.status}." + (f" Last step: {self._last_step}" if self._last_step else "")]
            if waiting:
                lines.append(f"{waiting} approval(s) waiting for you.")
            lines.append(self.agent.registry.builds.status())
            lines.append(self.agent.registry.grants.describe())
            return "\n".join(lines)
        return (self.guide.handle(text) or self.publish_command(text) or self.agent.registry.content.command(text)
                or self.agent.registry.grants.command(text))

    def answer_approval(self, approval_id: int, allow: bool) -> bool:
        waiter = self._approvals.get(approval_id)
        if waiter is None or waiter[0].is_set():
            return False
        waiter[1] = allow
        waiter[0].set()
        return True

    def pending_approval(self) -> int | None:
        return next((i for i, w in self._approvals.items() if not w[0].is_set()), None)

    # ---- wake / stop ---------------------------------------------------------
    def mode(self) -> str:
        """What the microphone loop should listen for right now."""
        if self.enrolling:
            return "enrol"
        if self.status == "thinking" and not self._background and self.awake:
            return "active"     # working on your request: a new command replaces it (Jay is silent now, so no echo)
        if self.status in BUSY and not (self._background and self.status == "thinking"):
            return "busy"       # (a background check-in thinking quietly must not deafen "Hey Jay")
        if self.awake and time.monotonic() < self.awake_until:
            return "active"
        if self.awake:                               # the conversation went quiet: back to standby
            self.awake = False
            self.emit("mode", awake=False, close=False)
        return "standby"

    def wake(self) -> None:
        self.awake = True
        self.awake_until = time.monotonic() + self.cfg.active_seconds
        self.emit("mode", awake=True)
        self.on_wake()

    def stand_down(self, close_window: bool = True, sound: bool = True) -> None:
        """'Stop Jay': drop what is happening and go back to waiting for the wake word."""
        self.cancel_enrol()
        self.agent.registry.cancelled.set()
        self.agent.preempt()                         # the request in progress is dropped and stays silent
        self._preempt.set()
        if self.voice:
            self.voice.stop_speaking()
        pending = self.pending_approval()
        while pending is not None:                   # anything waiting for a yes is refused
            self.answer_approval(pending, False)
            pending = self.pending_approval()
        kept = []
        while True:                                  # forget requests that were queued up
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if item[0] not in ("typed", "voice", "talk"):
                kept.append(item)
        for item in kept:
            self.inbox.put(item)
        self.awake = False
        if sound and self.voice:
            from .voice import chime
            chime("stop")
        if close_window:
            self.on_close_window()
        self.emit("mode", awake=False, close=close_window)

    def _heard(self, event: str, text: str, audio=None) -> None:
        """Called by the microphone loop."""
        if event == "stop":
            self.emit("heard", text=text, what="stop")
            self.stand_down()
            return
        if event == "wake":
            from .voice import chime
            chime("wake")
            self.wake()
        self.awake_until = time.monotonic() + self.cfg.active_seconds
        if text:
            self.interrupt_background()
            text = self.route_input(text, "voice")
            self.inbox.put(("voice", text, self.identify(audio)))

    # ---- things the display can ask for ---------------------------------------
    def _drop_current(self) -> None:
        """Abandon the request in progress: it stops at its next step and says nothing."""
        self.agent.preempt()
        self._preempt.set()
        pending = self.pending_approval()
        while pending is not None:                   # a question waiting for a yes is answered no
            self.answer_approval(pending, False)
            pending = self.pending_approval()
        if self.voice:
            self.voice.stop_speaking()

    def route_input(self, text: str, source: str) -> str:
        """Decide what a new input means while a request may still be in progress, and return the
        text to queue. Nothing the person says is thrown away:

        * an addition ("...and open the folder", or anything spoken a moment after the first part)
          is joined to the request in progress and the two are handled together, if that request
          has not yet done anything; if it already has, the addition simply waits its turn;
        * a correction ("no, I meant ...") replaces the request, which is kept as context;
        * something unrelated replaces a request that has not started doing anything, and otherwise
          waits its turn, so half-finished work is not abandoned by accident.
        """
        job = self._job
        if job is None or not job.is_alive():
            return text
        if self.status == "speaking":                # the work is done and being read out: cut the speech, queue this
            if self.voice:
                self.voice.stop_speaking()
            return text
        now = time.monotonic()
        gap = now - self._job_input_at if source == "voice" else None
        kind = dialogue.classify(text, gap, self.cfg.merge_seconds)
        ran = self.agent.registry.tools_run - self._job_tools0
        if kind == "continue" and ran == 0:
            merged = dialogue.merge(self._job_text, text)
            self.emit("info", text="Got it, adding that to your request.")
            self._drop_current()
            return merged
        if kind == "correct":
            self._note_next = dialogue.interrupted_note(self._job_text, ran)
            self.emit("info", text="Okay, switching to your correction.")
            self._drop_current()
            return text
        if ran == 0:                                 # unrelated, and the old request has done nothing yet
            self._note_next = dialogue.interrupted_note(self._job_text, 0)
            self.emit("info", text="Okay, taking your new request instead.")
            self._drop_current()
            return text
        self.emit("info", text="Got it, I'll do that right after this.")
        return text

    def submit(self, text: str, source: str = "typed") -> None:
        if source == "typed" and text.strip() and not text.startswith("/"):
            text = self.route_input(text, "typed")
        self.interrupt_background()
        if self.status == "speaking" and self.voice and text.strip() and not text.startswith("/"):
            self.voice.stop_speaking()      # typing over Jay's reply cuts it off instead of queueing behind it
        self.inbox.put((source, text))

    def talk_once(self) -> None:
        self.inbox.put(("talk", ""))

    def stop(self) -> None:
        self.quitting.set()
        self.agent.registry.cancelled.set()
        if self.voice:
            self.voice.stop_speaking()
        self.inbox.put(("quit", ""))

    def enable_voice(self) -> bool:
        if self.voice:
            return True
        from .voice import Voice
        try:
            self.voice = Voice(self.cfg)
            self.voice.on_problem = lambda text: self.emit("info", text=text)
            self.voice.prepare(ACKNOWLEDGEMENTS)
            self.voice.known_voice = self.known_voice
        except ImportError as exc:
            self.emit("info", text=f"Voice is unavailable ({exc}). Install it with: pip install -r requirements-voice.txt")
        except Exception as exc:
            self.emit("info", text=f"Voice could not start: {type(exc).__name__}: {exc}")
        return self.voice is not None

    def set_speaking(self, on: bool) -> None:
        self.speak_replies = bool(on and self.enable_voice())
        self.emit("voice", **self.voice_state())

    def set_handsfree(self, on: bool) -> None:
        if self.handsfree_stop:
            self.handsfree_stop.set()
            self.handsfree_stop = None
        if on and self.enable_voice():
            try:
                self.voice.warm_up()                 # loads the wake-word model (first run downloads it)
            except Exception as exc:
                self.emit("info", text=f"Wake word is unavailable: {type(exc).__name__}: {exc}")
                self.emit("voice", **self.voice_state())
                return
            if self.speakers.enrolled:                # load the voice-recognition model now, not at the first "Hey Jay"
                try:
                    self.speakers._model()
                except Exception:
                    pass
            self.speak_replies = True
            self.handsfree_stop = stop = threading.Event()
            threading.Thread(target=self.voice.run_handsfree, daemon=True,
                             args=(self._heard, self.mode, stop, self._enrol_audio)).start()
        self.emit("voice", **self.voice_state())

    fps = 0   # frames per second the window reports drawing at

    def levels(self) -> dict:
        """Live loudness for the display: Jay's voice and the microphone."""
        v = self.voice
        return {"voice": round(v.level, 3) if v else 0.0, "mic": round(v.mic_level, 3) if v else 0.0,
                "status": self.status, "awake": self.awake}

    def voice_state(self) -> dict:
        return {"speak": self.speak_replies, "handsfree": self.handsfree_stop is not None, "awake": self.awake}

    def state(self) -> dict:
        store = self.agent.store
        return {
            "name": self.cfg.name, "status": self.status, "brain": self.agent.provider_name,
            "brains": list(self.agent._factories), **self.voice_state(),
            "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"], "start": t["start"],
                       "minutes": t["minutes"], "priority": t["priority"]} for t in store.list_tasks()],
            "memories": [{"id": f["id"], "category": f["category"], "fact": f["fact"]} for f in store.all_facts()],
            "patterns": store.work_patterns(), "fps": self.fps,
            "profile": Profile(self.cfg.data_dir).form(),
            "voices": self.speakers.enrolled, "owner": self.speakers.owner, "speaker": self.last_speaker,
            "enrolling": bool(self.enrolling), "pinned": self.agent.pinned,
            "speeds": {n: round(p.latency, 1) for n, p in self.agent._providers.items()
                       if getattr(p, "latency", None)},
        }

    # ---- the worker ------------------------------------------------------------
    def say(self, text: str) -> None:
        self.emit("jay", text=text, brain=self.agent.provider_name)
        if self.voice and self.speak_replies:
            self.set_status("speaking")
            self.voice.speak(text)

    def answer(self, text: str, shown: str | None = None, source: str = "typed", speaker: str | None = None,
               note: str = "") -> str:
        cancelled = self.agent.registry.cancelled
        cancelled.clear()
        # Typed input comes from whoever is at the keyboard, which is treated as the owner.
        role = self.role_for(speaker) if source == "voice" else "owner"
        if source != "voice":
            speaker = self.speakers.owner
        self.last_speaker = speaker if role != "guest" else "Guest"
        self.emit("user", text=shown or text, source=source, speaker=self.last_speaker if self.speakers.enrolled else None,
                  role=role)
        self.set_status("thinking")
        self._acknowledged = False
        generation = self.agent.registry.generation
        replaced = lambda: generation != self.agent.registry.generation       # noqa: E731
        try:
            reply = self.agent.ask(text, speaker=speaker if self.speakers.enrolled else None, speaker_role=role,
                                   note=note)
            self._acknowledged = True
            if cancelled.is_set():
                self.emit("info", text="Stopped.")
            elif replaced() or not reply:
                return ""                            # a newer request took over: stay silent
            else:
                self.last_reply = reply
                self.say(reply)
        finally:
            if replaced() and not cancelled.is_set():
                return ""                            # the newer request now owns the status and the window
            self.agent.registry.speaker_is_owner = True
            self.emit("refresh")
            self.set_status("idle")
            if self.awake:                           # leave room for a follow-up without the wake word
                self.awake_until = time.monotonic() + self.cfg.active_seconds
            if self.pending_enrol:                   # "learn my voice" was asked for: start once the reply is done
                name, self.pending_enrol = self.pending_enrol, None
                self.start_enrol(name)

    def command(self, line: str) -> None:
        cmd, _, arg = line[1:].partition(" ")
        agent, info = self.agent, lambda text: self.emit("info", text=text)
        if cmd == "help":
            info(HELP.format(brains=", ".join(agent._factories)))
        elif cmd in ROUTINES:
            self.answer(ROUTINES[cmd] + (f" Extra instructions: {arg}" if arg.strip() else ""), shown=line)
        elif cmd == "exportcalendar":
            from . import ics_export
            info(f"Your schedule is saved as a calendar file: {ics_export.write(agent.store, self.cfg.data_dir)}. "
                 "Import it in Google Calendar (Settings, Import & export) or open it with Outlook or your phone.")
        elif cmd == "ragcheck":
            from . import rag_eval
            info(rag_eval.describe(rag_eval.evaluate(agent.store)))
        elif cmd == "evening":
            self.inbox.put(("evening", ""))
            info("Writing the evening check-in.")
        elif cmd == "weekly":
            self.inbox.put(("weekly", ""))
            info("Writing this week's summary.")
        elif cmd == "sweep":
            self._sweep_blocks()
            info("Checked your planned blocks.")
        elif cmd == "pushbrief":
            self.inbox.put(("briefpush", ""))
            info("Sending today's plan to your phone.")
        elif cmd == "model":
            try:
                agent.switch(arg.strip().lower())
                info("Choosing the quickest brain automatically." if agent.provider_name and not agent.pinned else f"Now using {agent.provider_name}.")
            except Exception as exc:
                info(f"Could not switch: {exc}")
        elif cmd == "memory":
            info("\n".join(f"#{f['id']} [{f['category']}] {f['fact']}" for f in agent.store.all_facts()) or "Nothing yet.")
        elif cmd == "tasks":
            info("\n".join(describe_task(t) for t in agent.store.list_tasks()) or "No tasks.")
        elif cmd == "patterns":
            info(agent.store.work_patterns())
        elif cmd == "reflect":
            self.set_status("thinking")
            info(agent.reflect(force=True) or "Nothing new to review.")
            self.set_status("idle")
        elif cmd == "reset":
            agent.reset()
            info("Conversation cleared.")
        elif cmd in ("guide", "commands", "tour", "setup", "autopilot"):
            info(self.guide.handle(line) or "Type /guide for the list of topics.")
        elif cmd in ("stop", "status"):
            info(self.phone_command(line) or "")
        elif cmd in ("publish", "connect"):
            info(self.publish_command(line) or "Say which one, e.g. /publish 3.")
        elif cmd in ("queue", "approve", "reject", "posted", "draft"):
            info(agent.registry.content.command(line) or "Say which one, e.g. /approve 3.")
        elif cmd in ("allow", "grants", "revoke", "autoapprove"):
            info(agent.registry.grants.command(line) or "Say what to allow, e.g. /allow mail for 8h.")
        else:
            info("Unknown command. Type /help.")
        self.emit("refresh")

    def _check_in(self) -> None:
        if not self.inbox.empty():
            return                      # the user is mid-conversation; don't interrupt
        self._background = True
        try:
            self.agent.registry.cancelled.clear()
            words = self.agent.check_in()
            if words:
                self.say(words)
                self.emit("refresh")
        finally:
            self._background = False
            self.set_status("idle")

    def _startup_jobs(self) -> None:
        """The daily review and the first-start-of-day briefing. They run as background work after
        the app is already answering, so "Hey Jay" never waits behind them."""
        try:
            self._background = True
            self.set_status("thinking")
            learned = self.agent.reflect()      # once a day: learn from recent conversations
            if learned:
                self.emit("info", text=f"Reviewed recent days: {learned}")
        except Exception as exc:
            self.emit("info", text=f"Daily review skipped ({type(exc).__name__}).")
        if self.cfg.morning_brief and not self.agent.registry.cancelled.is_set():
            try:
                greeting = self.agent.morning()     # first start of the day only
                if greeting:
                    self.say(greeting)
                    self.emit("refresh")
            except Exception as exc:
                self.emit("info", text=f"Morning brief skipped ({type(exc).__name__}).")
        self._background = False
        self.set_status("idle")

    def interrupt_background(self) -> None:
        """A person is asking for something: wind down any background job (it is retried later)."""
        if self._background:
            self.agent.registry.cancelled.set()

    def _run_job(self, source: str, text: str, speaker) -> None:
        """Work on a request in its own thread, so the worker stays free to take a newer request
        (or "stop") the moment it arrives instead of being stuck until this one finishes."""
        note, self._note_next = self._note_next or "", None

        def run() -> None:
            try:
                if source == "typed" and text.startswith("/"):
                    self.command(text)
                else:
                    self.answer(text, source=source, speaker=speaker, note=note)
            except Exception as exc:                # keep the assistant alive on unexpected errors
                self.emit("info", text=f"Something went wrong: {type(exc).__name__}: {exc}")
                self.set_status("idle")

        self._preempt.clear()
        self._job_text, self._job_input_at = text, time.monotonic()
        self._job_tools0 = self.agent.registry.tools_run
        job = self._job = threading.Thread(target=run, daemon=True)
        job.start()
        while job.is_alive():
            if self._preempt.wait(0.1):
                self._preempt.clear()
                return                               # abandoned: it winds down by itself, silently

    def _work(self, brief: bool) -> None:
        if not brief:
            self.inbox.put(("startup", ""))
        self.set_status("idle")
        if brief:
            self.answer(ROUTINES["brief"], shown="/brief")
        while True:
            item = self.inbox.get()
            if self.inbox.last_wait > 3 and item[0] not in Inbox.BACKGROUND:
                print(f"[slow turn] request waited {self.inbox.last_wait:.1f}s behind other work", flush=True)
            source, text, speaker = (item + (None,))[:3]
            text = text.strip()
            try:
                if source == "quit":
                    break
                if source == "telegram":
                    self._from_phone(text, speaker)
                    continue
                if source == "startup":
                    self._startup_jobs()
                elif source == "briefpush":
                    self._brief_push()
                elif source == "weekly":
                    self._weekly()
                elif source == "evening":
                    self._evening_digest()
                elif source == "cookies":
                    self.agent.registry.run_serial(self.agent.registry.browser.save_cookies, True)
                elif source == "routine":
                    self._run_routine(int(text))
                elif source == "checkin":
                    self._check_in()
                elif source == "watches":
                    self._run_watches()
                elif source == "night":
                    self._night()
                elif source == "announce":
                    self.say(text)
                    self.set_status("idle")
                elif source == "talk":
                    if not self.enable_voice():
                        continue
                    self.set_status("listening")
                    heard = self.voice.listen()
                    self.set_status("idle")
                    if heard:
                        self.answer(heard, source="voice", speaker=self.identify(self.voice.last_audio))
                    elif getattr(self.voice, "last_peak", 1.0) < 1e-4:
                        self.emit("info", text="I can't hear anything: the microphone is delivering silence "
                                               "(muted, or another app has it). Say 'check the microphone' or see the README.")
                    else:
                        self.emit("info", text="I didn't catch anything; it was too quiet or unclear. Try again closer to the mic.")
                elif not text:
                    continue
                else:
                    self._run_job(source, text, speaker)
            except Exception as exc:    # keep the assistant alive on unexpected errors
                self.emit("info", text=f"Something went wrong: {type(exc).__name__}: {exc}")
                self.set_status("idle")
        self.set_handsfree(False)
        try:
            self.agent.registry.run_serial(self.agent.registry.browser.close)
        except Exception:
            pass
        self.stopped.set()

    def _reminders(self) -> None:
        while not self.stopped.wait(5):
            try:
                due = self.agent.store.pop_due_reminders()
            except Exception:
                continue
            for task in due:
                if (self.cfg.reminder_mode == "digest" and task["start"] and task["minutes"]
                        and not task["title"].startswith("⏱")):
                    continue            # a planned block: it is in the morning and evening summaries, not a ping
                timer = task["title"].startswith("⏱")
                title = task["title"].lstrip("⏱ ").strip()
                words = f"Your {title.lower()} timer is done." if timer else f"Reminder: {title}"
                self.emit("jay", text=words, brain="reminder")
                self.agent.registry.phone.send_async("Jay · reminder", words)
                try:                                     # seen even when Jay's window is closed
                    from .tools.system import notify
                    notify("Jay", words)
                except Exception:
                    pass
                if self.voice and self.speak_replies:
                    self.voice.speak(words)

    def _from_phone(self, text: str, chat_id, quiet: bool = False) -> None:
        """A message from the paired phone: answer it there. Nothing that needs approval can run."""
        was_speaking, self.speak_replies = self.speak_replies, False
        self._background = True
        self._phone_turn = True                      # anything that needs a yes is asked on the phone
        self.agent.registry.remote = True
        self.last_reply = ""
        done = threading.Event()
        if not quiet:
            try:
                self.telegram.send(chat_id, "👍 On it.")
            except Exception:
                pass

        def progress() -> None:
            for _ in range(12):                      # a quiet phone is a worrying phone: say what's happening
                if done.wait(75):
                    return
                try:
                    self.telegram.send(chat_id, f"Still working. {self._last_step or ''}".strip())
                except Exception:
                    return

        if not quiet:
            threading.Thread(target=progress, daemon=True).start()
        try:
            cmd = text[1:].split(None, 1)[0].lower() if text.startswith("/") else ""
            request = ROUTINES[cmd] + (text[len(cmd) + 1:] and f" Extra instructions: {text[len(cmd) + 1:].strip()}") if cmd in ROUTINES else text
            self.answer(f"(Sent from my phone; I'm not at the laptop.) {request}", shown=text, source="phone")
        finally:
            done.set()
            self._phone_turn = False
            self.agent.registry.remote = False
            self._background = False
            self.speak_replies = was_speaking
        try:
            self.telegram.send(chat_id, self.last_reply or "Sorry, I couldn't answer that just now.")
        except Exception as exc:
            self.emit("info", text=f"Couldn't reply on Telegram: {exc}")

    def start_telegram(self) -> None:
        import os

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return
        from .telegram import TelegramLink
        self.telegram = TelegramLink(token, self.cfg.data_dir,
                                     on_message=lambda text, chat: self.inbox.put(("telegram", text, chat)),
                                     on_info=lambda text: self.emit("info", text=text),
                                     on_approval=self.on_phone_approval, on_command=self.phone_command,
                                     on_content=self.on_phone_content)
        self.agent.registry.phone.telegram = self.telegram
        threading.Thread(target=self.telegram.run, daemon=True).start()

    def _run_watches(self) -> None:
        """Check whatever is due and report only what's new. Never opens the window or asks anything."""
        self._background = True
        try:
            for watch in self.watches.due():
                try:
                    new = self.watches.run(watch)
                except Exception as exc:
                    if self.watches.failed(watch):          # say so once; keep trying quietly
                        self.emit("info", text=f"Couldn't check '{watch['name']}' just now ({str(exc)[:120]}). "
                                               "I'll keep trying.")
                    continue
                if not new:
                    continue
                listing = "\n".join(f"- {item[:160]}" for item in new[:6])
                more = f"\n(and {len(new) - 6} more)" if len(new) > 6 else ""
                self.emit("jay", text=f"New on {watch['name']}:\n{listing}{more}", brain="watch")
                self.agent.registry.phone.send_async(f"Jay · {watch['name']}", f"{listing}{more}")
                try:
                    from .tools.system import notify
                    notify(f"Jay · {watch['name']}", new[0][:200] + (f"  (+{len(new) - 1} more)" if len(new) > 1 else ""))
                except Exception:
                    pass
                if self.voice and self.speak_replies and self.awake:
                    self.voice.speak(f"Something new on {watch['name']}.")
        finally:
            self._background = False
            self.set_status("idle")

    def _night(self) -> None:
        from .agent import NIGHT_PROMPT

        self._background = True
        try:
            self.set_status("thinking")
            words = self.agent.ask(NIGHT_PROMPT, record=False)
            if words and not words.startswith("I can't reach a brain"):
                self.emit("jay", text=words, brain=self.agent.provider_name)
                self.agent.registry.phone.send_async("Jay · end of day", words)
                try:
                    from .tools.system import notify
                    notify("Jay · end of day", words[:220])
                except Exception:
                    pass
                if self.voice and self.speak_replies:
                    self.set_status("speaking")
                    self.voice.speak(words)
                self.emit("refresh")
        finally:
            self._background = False
            self.set_status("idle")

    def _brief_push(self) -> None:
        """v0: send today's plan (schedule + calendar) to the phone, once a day."""
        from . import brief

        self.agent.store.set_meta("last_brief_push", date.today().isoformat())
        data = brief.gather(self.agent.store)
        self._background = True
        try:
            words = self.agent.ask(brief.PUSH_PROMPT.format(today=date.today(), data=data), record=False)
            if not words or words.startswith("I can't reach a brain") or words.startswith("I was cut off")                     or self.agent.registry.cancelled.is_set():
                words = brief.plain_plan(data)
        except Exception:
            words = brief.plain_plan(data)
        finally:
            self._background = False
            self.agent._forget_app_turn()           # the instruction itself isn't part of the real conversation
        self.emit("jay", text=words, brain="morning plan")
        self._speak_digest(words)
        self.agent.store.set_meta("brief_pending", words)       # kept until a phone channel accepts it
        self._deliver_pending_plan(first_try=True)
        self.emit("refresh")

    def _check_rhythm(self) -> None:
        """Morning and evening summaries, sent when you are actually at the laptop (not on a fixed clock).
        With reminder_mode "each" the old fixed-time morning push is used instead."""
        from . import rhythm

        store, now = self.agent.store, datetime.now()
        today = date.today().isoformat()
        if self.cfg.reminder_mode != "digest":
            at = self.cfg.brief_time
            if at and len(at) == 5 and at <= now.strftime("%H:%M") < "14:00" and store.get_meta("last_brief_push") != today:
                store.set_meta("last_brief_push", today)
                self.inbox.put(("briefpush", ""))
            return
        lines = None
        try:
            from .tools.mailbox import feed_events, feed_urls
            if feed_urls():
                lines = rhythm.calendar_lines(date.today(), feed_events(2)) or None
        except Exception:
            pass
        kind, skip = rhythm.due(now, store.get_meta("last_brief_push") or "", store.get_meta("last_evening") or "",
                                rhythm.idle_seconds(), rhythm.college_day(date.today(), lines),
                                self.cfg.evening_after, self.cfg.evening_other)
        if skip:
            store.set_meta("last_brief_push", today)       # the morning window passed: no "morning" plan at lunchtime
        if kind == "morning":
            store.set_meta("last_brief_push", today)
            self.inbox.put(("briefpush", ""))
        elif kind == "evening":
            store.set_meta("last_evening", today)
            self.inbox.put(("evening", ""))

    def _speak_digest(self, words: str) -> None:
        """Read a morning or evening summary aloud, once, but only if you are at the laptop and Jay is not
        busy with you; otherwise it stays on the screen and the phone."""
        from . import rhythm

        if not (self.cfg.digest_speak and self.voice and self.speak_replies):
            return
        if self.status not in ("idle", "thinking") or rhythm.idle_seconds() > 180:
            return
        try:
            self.set_status("speaking")
            self.voice.speak(words)
        finally:
            self.set_status("idle")

    def _verify_progress(self) -> list[str]:
        """Check LeetCode and GitHub for proof that today's blocks were done; mark them done. Quiet."""
        from . import verify

        if not (self.cfg.leetcode_user or self.cfg.github_user):
            return []
        proofs = verify.verify_today(self.agent.store, self.cfg.leetcode_user, self.cfg.github_user)
        if proofs:
            store = self.agent.store
            store.set_meta("digest_proofs", ((store.get_meta("digest_proofs") or "") + "\n" + "\n".join(proofs)).strip())
            self.emit("refresh")
        return proofs

    def _evening_digest(self) -> None:
        """The one evening summary: what is done (with proof), what is open, what moved, what tomorrow starts with."""
        from . import brief

        store = self.agent.store
        try:
            self._verify_progress()
        except Exception:
            pass
        proofs = [p for p in (store.get_meta("digest_proofs") or "").split("\n") if p.strip()]
        notes = store.get_meta("digest_notes") or ""
        data = brief.gather_evening(store, proofs, notes)
        self._background = True
        try:
            words = self.agent.ask(brief.EVENING_PROMPT.format(today=date.today(), data=data), record=False)
            if not words or words.startswith("I can't reach a brain") or words.startswith("I was cut off") \
                    or self.agent.registry.cancelled.is_set():
                words = "Evening check-in\n" + data
        except Exception:
            words = "Evening check-in\n" + data
        finally:
            self._background = False
            self.agent._forget_app_turn()
        store.set_meta("digest_notes", "")
        store.set_meta("digest_proofs", "")
        self.emit("jay", text=words, brain="evening check-in")
        self._speak_digest(words)
        store.set_meta("evening_pending", words)
        self._deliver_pending_plan(first_try=True, key="evening_pending", title="Jay · evening check-in")
        self.emit("refresh")

    def _deliver_pending_plan(self, first_try: bool = False, key: str = "brief_pending",
                              title: str = "Jay · today's plan") -> None:
        """Send a waiting plan or summary to the phone; if the network won't allow it, try again
        every minute (a morning plan only until the afternoon) and meanwhile show a Windows notification."""
        words = self.agent.store.get_meta(key)
        if not words:
            return
        phone = self.agent.registry.phone
        sent = phone.send(title, words) if phone.available() else []
        if sent:
            self.agent.store.set_meta(key, "")
            self.emit("info", text=f"{title.split('· ')[-1].capitalize()} sent to your phone ({', '.join(sent)}).")
        elif first_try:
            try:
                from .tools.system import notify
                notify(title, words[:220])
            except Exception:
                pass
            why = "no phone is linked yet (say \"set up phone alerts\")" if not phone.available()                 else "the phone service can't be reached from this network right now; I'll keep retrying"
            self.emit("info", text=f"{title.split('· ')[-1].capitalize()} is shown here; {why}.")
        elif key == "brief_pending" and datetime.now().strftime("%H:%M") >= "15:00":
            self.agent.store.set_meta(key, "")      # too late to be a morning plan

    def _sweep_blocks(self) -> None:
        """v1: every minute, check planned blocks: ask about the ones just finished, mark the
        ones that slipped as missed and move them to the next free slot."""
        from . import planner

        result = planner.sweep(self.agent.store)
        if result["missed"]:
            # No message now: it goes into the next summary (that is how the all-day re-announcing is avoided).
            store = self.agent.store
            store.set_meta("digest_notes", ((store.get_meta("digest_notes") or "") + "\n"
                                            + "\n".join(f"- {n}" for n in result["missed"])).strip())
            self.emit("refresh")

    def _weekly(self, force: bool = False) -> None:
        """v1: the week's summary, from the done/missed record, to the phone; and what it taught."""
        from . import planner

        week = date.today().isocalendar()[:2]
        self.agent.store.set_meta("last_weekly", f"{week[0]}-{week[1]}")
        stats = planner.week_stats(self.agent.store)
        text = planner.describe_stats(stats)
        self._background = True
        try:
            words = self.agent.ask(planner.WEEKLY_PROMPT.format(stats=text), record=False)
            if not words or words.startswith("I can't reach a brain") or words.startswith("I was cut off")                     or self.agent.registry.cancelled.is_set():
                words = "Week in review\n" + text
        except Exception:
            words = "Week in review\n" + text
        finally:
            self._background = False
            self.agent._forget_app_turn()
        learned = planner.learn_from_week(self.agent.store, stats)
        self.agent.store.add_knowledge(f"note: weekly summary {date.today():%Y-%m-%d}", f"{words}\n\n{text}")
        if learned:
            self.agent.refresh_system()
        self.emit("jay", text=words, brain="weekly summary")
        self.agent.store.set_meta("weekly_pending", words)
        self._deliver_pending_plan(first_try=True, key="weekly_pending", title="Jay · your week")
        self.emit("refresh")

    def _housekeeping(self) -> None:
        """Every minute: due watches, the nightly summary, and the daily backup."""
        while not self.stopped.wait(60):
            try:
                self._revive_listener()
            except Exception:
                pass
            self._cookie_tick = getattr(self, "_cookie_tick", 0) + 1
            if self._cookie_tick % 3 == 0 and self.agent.registry.browser._context is not None:
                self.inbox.put(("cookies", ""))
            try:
                backup(self.cfg.data_dir)
            except Exception:
                pass
            try:
                if self.watches.due():
                    self.inbox.put(("watches", ""))
            except Exception:
                pass
            try:
                self._deliver_due_content()
                for routine in self.agent.registry.routines.due():
                    self.agent.registry.routines.mark_run(routine["id"])
                    self.inbox.put(("routine", str(routine["id"])))
            except Exception as exc:
                print(f"[content] {type(exc).__name__}: {exc}", flush=True)
            try:
                from . import ics_export
                ics_export.write(self.agent.store, self.cfg.data_dir)
            except Exception as exc:
                print(f"[ics] {type(exc).__name__}: {exc}", flush=True)
            try:
                self._sweep_blocks()
            except Exception as exc:
                print(f"[sweep] {type(exc).__name__}: {exc}", flush=True)
            self._verify_tick = getattr(self, "_verify_tick", 0) + 1
            if self._verify_tick % 10 == 0:                      # every ten minutes, quietly, off this thread
                threading.Thread(target=lambda: self._safely(self._verify_progress), daemon=True).start()
            for key, title in (("brief_pending", "Jay · today's plan"), ("weekly_pending", "Jay · your week"),
                               ("evening_pending", "Jay · evening check-in")):
                if self.agent.store.get_meta(key):
                    threading.Thread(target=self._deliver_pending_plan, kwargs={"key": key, "title": title},
                                     daemon=True).start()
            now = datetime.now()
            week = now.isocalendar()[:2]
            if now.weekday() == 6 and now.strftime("%H:%M") >= self.cfg.weekly_time                     and self.agent.store.get_meta("last_weekly") != f"{week[0]}-{week[1]}":
                self.agent.store.set_meta("last_weekly", f"{week[0]}-{week[1]}")
                self.inbox.put(("weekly", ""))
            try:
                self._check_rhythm()
            except Exception as exc:
                print(f"[rhythm] {type(exc).__name__}: {exc}", flush=True)
            night = self.cfg.night_time
            if night and len(night) == 5 and datetime.now().strftime("%H:%M") >= night \
                    and self.agent.store.get_meta("last_night") != date.today().isoformat() \
                    and self.status == "idle":
                self.agent.store.set_meta("last_night", date.today().isoformat())
                self.inbox.put(("night", ""))

    def _safely(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[background] {fn.__name__}: {type(exc).__name__}: {exc}", flush=True)

    def _warm_calendar(self) -> None:
        """Fetch the calendar feed once at start-up, in the background, so the first question about it is quick."""
        try:
            from .tools.mailbox import feed_events, feed_urls

            if feed_urls():
                feed_events(1)
        except Exception:
            pass

    def _revive_listener(self) -> None:
        """If the microphone loop has stopped reading (it died, or the device vanished), restart it."""
        voice = self.voice
        if not (voice and self.handsfree_stop and not self.handsfree_stop.is_set()) or self.enrolling:
            return
        if getattr(voice, "silent_for", 0.0) > 45 and time.monotonic() - getattr(self, "_mic_warned", -9999) > 1800:
            self._mic_warned = time.monotonic()
            note = ("I can't hear anything: the microphone is delivering pure silence. Check the laptop's "
                    "microphone mute key or privacy switch, Windows Sound settings (Input), and close any "
                    "app that may be holding the microphone (Zoom, Teams, a browser call).")
            self.emit("info", text=note)
            try:
                from .tools.system import notify
                notify("Jay can't hear you", note[:200])
            except Exception:
                pass
        beat = getattr(voice, "heartbeat", 0.0)
        if beat and time.monotonic() - beat > 20:
            self.emit("info", text="The microphone listener had stalled; restarting it.")
            self.set_handsfree(True)

    def _checkin_timer(self) -> None:
        while not self.stopped.is_set():
            time.sleep(self.cfg.checkin_minutes * 60)
            self.inbox.put(("checkin", ""))

    def start(self, brief: bool = False, handsfree: bool = False) -> None:
        threading.Thread(target=self._work, args=(brief,), daemon=True).start()
        threading.Thread(target=self._reminders, daemon=True).start()
        threading.Thread(target=self._warm_calendar, daemon=True).start()
        threading.Thread(target=self._housekeeping, daemon=True).start()
        self.start_telegram()
        if self.cfg.checkin_minutes:
            threading.Thread(target=self._checkin_timer, daemon=True).start()
        if handsfree:       # the microphone takes a moment to start; don't hold everything up for it
            threading.Thread(target=self.set_handsfree, args=(True,), daemon=True).start()

    def save_profile(self, values: dict) -> None:
        """Profile edited in the window: save it and make sure Jay uses it from the next request."""
        Profile(self.cfg.data_dir).update({k: v for k, v in values.items() if isinstance(v, str)})
        self.agent.refresh_system()
        self.emit("refresh")
