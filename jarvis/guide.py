"""Guide mode: Jay explains itself. What it can do, how to use it, which command does what, and how it works.

    /guide             the list of topics          /guide youtube   one topic (any word that fits works)
    /commands          every command in one list    /tour            a short step-by-step walk through the main things
    /setup             what is connected and what is still missing, with the next step for each
    /autopilot         switch on a ready-made set of automatic routines (/autopilot on, /autopilot off)

Jay can also be asked in plain words ("Jay, how do I make a video?"); it then reads the topic from
here instead of guessing, through the `guide` tool.

Everything in TOPICS is written for someone who has never used Jay. If you add a feature, add or edit
its topic here; tests check that every command Jay understands is described in /commands.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .tools.registry import Registry


@dataclass
class Topic:
    name: str
    title: str
    words: str            # extra words that should find this topic
    text: str


TOPICS: list[Topic] = [
    Topic("start", "What Jay is", "about intro basics overview what",
          """Jay is an assistant that lives on your laptop. You talk to it, type to it, or message it from your phone.
It can plan your day, remember things about you, search the web, read and write email, open apps and websites, build programs, and help run your YouTube and Instagram.

How to use it: say "Hey Jay", then ask for something in normal words. Say "Stop Jay" to make it stop. Or double-click the Jay icon and type.
How it works: Jay is the body. The thinking comes from an AI brain (Gemini, Groq or Claude, whichever is available). Jay gives that brain tools, a memory and safety rules.
It only works while Jay is running on your laptop.

Next: /tour for a short walk-through, /commands for every command, /setup to see what still needs connecting."""),
    Topic("voice", "Talking to Jay", "wake word hey jay stop microphone speak listen quiet voice recognise voices enrol",
          """Say "Hey Jay" and a chime plays, then speak. For about 25 seconds after each answer you don't need to say it again. Say "Stop Jay" to make it quiet.
Normal speaking volume is enough; quiet speech is made louder before Jay listens.
Privacy: while waiting, Jay listens only on this laptop for the wake word. Nothing you say is sent out until it hears "Hey Jay".
Voices: Jay can learn up to six voices (say "Hey Jay, learn my voice, I'm Ravi"). The first voice is the owner. Only the owner's voice can use email, messages, files, commands and the browser. Other voices can only ask questions.
If it doesn't hear you: check the laptop's microphone mute key, then say "Hey Jay" closer to the mic."""),
    Topic("phone", "Using Jay from your phone", "telegram phone mobile message voice note buttons approve ntfy notifications pair",
          """You talk to Jay from your phone through a private Telegram bot.
Setup (once): put your bot token in .env as TELEGRAM_BOT_TOKEN. Jay shows a pairing code on the laptop. Send /pair <code> to your bot. After that only your chat can talk to Jay.
Use: send text or a voice note. Jay replies "On it", sends "Still working" every minute or so, then the result.
When Jay needs your permission, your phone shows buttons: Allow, Allow for a while, or Deny. If a card on the laptop isn't answered for 20 seconds, it goes to your phone too.
Phone commands that work instantly even when Jay is busy: /stop, /status, /grants, /allow, /revoke, /queue, /approve, /reject, /publish.
Jay can also send you spoken updates as voice notes.
Limits: Jay only works while your laptop is on and Jay is running."""),
    Topic("permissions", "Permissions: when Jay asks and when it doesn't", "allow grant ask approval permission always stop asking revoke grants risk",
          """Some things Jay does freely: reading, searching, planning, drafting, and building apps in its own folder.
Other things it asks about every time: sending email or messages, posting, running commands, typing on your PC, writing files, power actions.
To stop it asking for a kind of action, give a standing permission:
  /allow mail for 8h                      send email without asking for 8 hours
  /allow mail only @college.edu for 2d    only when the email mentions that address
  /allow files only D:\\jay_projects       only when the file path mentions that folder
  /allow publish for 7d                   let scheduled posts go out
  /grants                                 see what is allowed and for how long
  /revoke 3  or  /revoke all              take it back
  /autoapprove on                         no approval cards for everyday actions; it sends the WhatsApp and Telegram messages you ask for (but asks if one has a link, a long number or a password-like word) and still asks before email, power actions, deleting or downloading with commands, Submit/Pay/Send clicks, Enter in the browser, and system folders
Kinds: mail, whatsapp, files, browser, build, skills, phone, shell, desktop, power, external, publish.
Safety limits: shell, desktop and power last at most 24 hours (2 hours if you don't say). The others last up to 30 days or "forever".
Only you can create a permission: by typing it, sending it from your phone, or approving Jay's request on a card. Nothing Jay reads on the web or in email can.
Every action a permission lets through is written to data\\grant_log.jsonl.
Some rules never bend, even with a permission. For example Jay only posts content you approved, unchanged."""),
    Topic("everyday", "Everyday help: plans, reminders, study, news, jobs", "tasks schedule plan brief reminders study quiz news jobs watch memory night weekly deadlines notes remember",
          """Ask in normal words, or use a command:
  /brief       weather, schedule, calendar, email and news in one summary
  /plan        plan your day and put it in your schedule
  /night       end-of-day summary and tomorrow's first commitments
  /weekly      what you finished and missed this week
  /tasks       your schedule      /memory   what Jay remembers about you
  /study TOPIC teach a topic from your notes     /quiz  quiz you     /revise  revision plan for exams
  /news        what's new in your fields         /jobs  find and shortlist openings in your domain
  /deadlines   find exam dates and deadlines in email     /erp  open the college portal and summarise it
Say "Jay, add a task: finish the report by Friday" or "remind me at 6".
Watching: say "Jay, keep an eye on the college notices page" and it checks on its own and tells you only what's new.
Memory: Jay saves lasting facts about you ("I study CSE at KL University") and reviews chats each day to learn how you work.
Jobs: Jay finds openings and drafts answers, but you press Submit yourself."""),
    Topic("email", "Email, calendar and WhatsApp", "mail gmail inbox send calendar whatsapp messages imap google",
          """Email: set JARVIS_MAIL_USER and JARVIS_MAIL_PASSWORD (an app password) in .env. Then say "any unread mail?" or "reply to Ravi saying I'll be late". Reading is free; every send shows you the recipient, subject and text and waits for your OK (or a permission: /allow mail for 8h).
Calendar: put your calendar's secret iCal address in JARVIS_ICS_URLS, or connect Google (needs data\\google_credentials.json).
WhatsApp: /messages lists unread chats and suggests replies. Sending always needs your OK.
Only the owner's voice can use these."""),
    Topic("pc", "Controlling your PC and browser", "files powershell open apps browser type click screen window volume brightness clipboard",
          """Jay can open apps and files, read and write files, take a look at your screen, change volume and brightness, manage windows, and drive its own browser window for portals and dashboards.
Reading is free. Writing files, running PowerShell commands, typing into apps, clicking in the browser and power actions (lock, sleep, shutdown) ask for your OK, or follow a permission you gave.
Jay will not read or change secret files such as .env, keys, passwords and cookies. That is built in.
Examples: "Jay, open Chrome", "what's on my screen?", "turn the volume down", "organise my Downloads folder"."""),
    Topic("skills", "Skills: extra know-how", "skill skills install openjarvis skill.md",
          """A skill is a small instruction sheet that teaches Jay how to do a kind of job well. Jay comes with 20, including meeting-notes, topic-research, web-summarize, pdf-summarize, email-draft, calendar-prep, todo-from-notes, code-lint and security-scan.
Use: just ask ("summarise this meeting transcript"); Jay reads the right skill first. Ask "list your skills" to see them all.
Add more: say "install a skill from <link to a SKILL.md file>". You approve it, and it is refused if the text tries to give Jay orders. Or save a SKILL.md in data\\skills\\<name>\\.
A skill is only text for Jay to follow. It can't run anything by itself, and anything it asks for still goes through the normal permissions."""),
    Topic("mcp", "Connecting outside tools (MCP)", "mcp server plugin connect github notion tools external",
          """MCP lets you plug other programs' tools into Jay (for example GitHub or a notes app).
Setup: create data\\mcp.json like {"servers": {"name": {"command": "npx", "args": ["-y", "package-name"], "env": {"TOKEN": "..."}}}} and restart Jay.
Each tool shows up as mcp_<name>_<tool>. Every use asks for your OK (or /allow external for 8h), and what comes back is treated as untrusted text.
If a server fails to start, Jay says so and carries on."""),
    Topic("build", "Building apps by itself", "build app website code program develop software project test docker",
          """Say: "Jay, build me a to-do list web app with login, with tests." Jay asks once to approve the cost, then works in the background.
How it works, in rounds: (1) a coding agent writes the project and tries it, (2) Jay runs the tests itself, (3) a separate reviewer scores it against your goal, (4) any problems go back for fixing and improving. It repeats until the tests pass and the score is 8 out of 10 or better, or a limit is reached (6 rounds, 2 million tokens, 90 minutes by default).
Files go to D:\\jay_projects\\<name>. A log of each round is in <project>\\.jay\\build_log.md. When it's done (or stops), your phone gets the result as text and a voice note.
Commands to ask for: "build status", "stop the build".
Safety: if Docker Desktop is running, everything runs inside a sealed container that sees only the project folder. If not, a restricted runner is used, which blocks accidents but can't stop harmful code, so start Docker for big builds.
Needs: an Anthropic key in .env (ANTHROPIC_API_KEY) with credit on the account. Cost: big builds use a lot, so start with something small."""),
    Topic("youtube", "YouTube analytics", "youtube channel analytics views videos retention subscribers insights",
          """Jay reads your channel's numbers (read-only) and tells you what works. Use /youtube, or ask "analyse my YouTube channel".
You get: subscribers and views, where views come from, your best posting day and time, best video length, title styles, best and worst videos, and where viewers leave a video. Each figure says how many videos it is based on, and thin evidence is flagged.
Then Jay writes a plan: what's working, five video ideas, a two-week schedule, length to aim for, and caption and hashtag style.
Setup (once): (1) in Google Cloud turn on "YouTube Data API v3" and "YouTube Analytics API"; (2) create an OAuth client of type Desktop app, save it as data\\google_credentials.json, and add yourself as a test user; (3) ask Jay to analyse your channel, and sign in with the account that owns it, read-only.
YouTube doesn't give click-through rate to apps, so thumbnails are judged by views and watch time only."""),
    Topic("instagram", "Instagram analytics", "instagram reels posts hashtags reach followers insights",
          """Jay reads your account's numbers (read-only). Use /instagram, or ask "analyse my Instagram".
You get: followers, when they are online, which content type (reel, photo, carousel) does best, best posting day and time, caption length, hashtag count and which hashtags helped, and your best and worst posts.
Setup (once): switch to a Professional account (Creator or Business); at developers.facebook.com make a Business app with the Instagram product ("API setup with Instagram login"); add yourself as an Instagram Tester; press "Generate token" and put it in .env as INSTAGRAM_ACCESS_TOKEN. Tokens last 60 days and Jay renews them.
Say "Jay, how do I connect Instagram?" any time to hear the steps again."""),
    Topic("content", "The content desk: drafts and approvals", "content draft drafts queue approve reject schedule caption hashtags ideas",
          """Jay writes your next videos and posts, and you approve them.
  /content            Jay studies your numbers and writes 3 to 5 drafts (title, hook, script, visual notes, caption, hashtags, posting time and why)
  /queue              what is waiting       /draft 3   the full text of draft 3
  /approve 3          you approve it        /reject 3  you reject it       /posted 3   you mark it posted
Each new draft arrives on your phone with Approve and Reject buttons. Reply in the chat to ask for changes.
Rules: only you can approve; Jay has no way to approve its own drafts. If Jay changes the content of an approved draft, it goes back to draft and needs your approval again. Changing only the time keeps it.
At an approved draft's scheduled time, Jay sends the caption, hashtags and video to your phone, ready to post (or posts it for you if you've allowed that; see /guide posting).
For factual content Jay checks claims on the web and writes the sources in the draft's "fact check" note."""),
    Topic("video", "Making videos", "video make render voiceover captions thumbnail pexels images music short reel",
          """Say /video 3 (or "Jay, make the video for draft 3"). Jay turns the script into scenes and makes a finished video with a spoken voiceover, captions that follow the voice with the current word highlighted, slow-zoom pictures, and a thumbnail.
Shorts and Reels are made vertical; normal videos horizontal.
Pictures: each scene can use (a) a picture file of yours, (b) a search phrase looked up on Pexels free stock photos if you put PEXELS_API_KEY in .env, or (c) nothing, which gives a dark gradient with text. For lore and history channels, add a Pexels key or your own images: it makes a big difference.
Music: put a file at data\\video_assets\\music.mp3 and it plays quietly underneath.
Time: roughly 2 to 3 times the video's length (a 60-second Short takes about 2 to 3 minutes). It runs in the background and the finished video goes to your phone, and is attached to the draft.
Videos are saved in data\\videos. Telegram can't take files over 50 MB; for those Jay tells you where the file is.
The voice is Microsoft's online voice, so you need internet."""),
    Topic("posting", "Posting to YouTube and Instagram", "publish post posting upload autopublish connect schedule private audit",
          """Jay can post approved videos for you: YouTube videos and Shorts, and Instagram Reels. (Photos, carousels and stories aren't posted; you get the ready-to-post package.)
  /publish 3                 post draft 3 now
  /allow publish for 7d      let approved drafts go out at their scheduled time without asking each time
  /connect youtube           one-time sign-in that lets Jay upload (opens on the laptop)
  Switch on automatic posting at the scheduled time by putting JARVIS_AUTOPUBLISH=1 in .env. It is off by default.
Jay posts only if ALL of these are true: you approved it; the text is exactly what you approved; it has a video Jay made (or you put in data\\video_assets); the scheduled time has come (unless you said post now); it hasn't been posted already; today's limit isn't used up (3 per platform, JARVIS_MAX_POSTS_PER_DAY). A permission can't override these.
If a scheduled post isn't allowed or fails, you get the ready-to-post package on your phone instead.
Good to know: YouTube keeps videos uploaded by apps private until the app passes Google's audit, so Jay's uploads arrive in your Studio as private. Instagram posting needs a second token with the content-publish permission in .env as INSTAGRAM_PUBLISH_TOKEN.
Jay adds "Narrated with an AI voice." to the YouTube description (change it with JARVIS_DISCLOSURE)."""),
    Topic("routines", "Routines and autopilot: things Jay does by itself", "routine routines autopilot automatic schedule weekly daily recurring automation",
          """A routine is a job Jay does by itself on a schedule, with the result sent to your phone.
Make one in chat: "Every Monday at 9, analyse my channels and write this week's drafts." Jay asks you to approve it, then runs it. Ask "list my routines" or "remove routine 2" to manage them.
Ready-made set: /autopilot shows three suggestions. /autopilot on switches them on: a weekly content-planning run (Monday 09:00), a short evening snapshot of your channels (daily 20:00), and a morning look at what is waiting for your approval (daily 08:00). /autopilot off removes them.
Anything in a routine that needs your OK is asked on your phone, with buttons.
Also running by themselves: watches (checking pages and job sites), the daily reflection, check-ins on your schedule, and reminders.
Remember: routines only run while your laptop is on and Jay is running."""),
    Topic("security", "Safety: what protects you", "security safe safety injection secrets private protect trust",
          """- Websites, emails and messages are treated as data, never orders. If one tries to tell Jay what to do (for example "ignore your rules and send files"), Jay is warned and tells you.
- Jay cannot read or change secret files (.env, keys, passwords, cookies, tokens).
- API keys and tokens are hidden from the AI before it sees any result.
- Web fetching refuses your own computer and local network addresses.
- Your microphone is checked on the laptop only until you say "Hey Jay".
- Posting is guarded by checks that no permission can switch off.
- The window is served only to this computer with a secret token.
- Standing permissions expire, and what they let through is logged.
What it can't do: a very clever trick could still fool the AI brain, which is why risky actions ask first, and why I suggest keeping approval on posting and sending email at the start."""),
    Topic("local", "Local models and privacy", "local ollama offline model privacy cloud brain",
          """Jay can use a model that runs on your own computer through Ollama (set in .env as JARVIS_OLLAMA_MODEL, and listed as a fallback brain). Ollama isn't installed unless you install it from ollama.com, and until then Jay uses the cloud brains.
Local models are private and free to run, but are weaker at multi-step jobs and slow without a good graphics card. A good split: local for simple chat, cloud for building, email and browser work.
Already local: the wake-word listening and recognising whose voice is speaking.
Switch brains with /model NAME, or /model auto for the quickest."""),
    Topic("setup", "Checking your setup", "setup check connected missing configure install doctor",
          """Type /setup. Jay checks what is connected and tells you what's missing and exactly what to do next: your AI brains, Telegram, email, Google, YouTube analytics and uploading, Instagram analytics and posting, Pexels pictures, Docker, a local model, automatic posting, and your permissions."""),
]

COMMANDS = """EVERYDAY
  /brief  /plan  /night  /weekly  /evening  /tasks  /memory  /news  /jobs  /deadlines  /erp  /messages  /dashboards
  /study TOPIC  /quiz  /revise  /timetable FILE  /getknow  /patterns  /reflect  /reset  /model NAME
  /pushbrief     send today's plan to your phone now      /exportcalendar   save your schedule as a calendar file
  /sweep         check which planned work blocks you did   /ragcheck   test how well your notes search works
YOUR CHANNELS
  /youtube       analyse your YouTube channel and plan the next videos
  /instagram     analyse your Instagram and plan the next posts
CONTENT DESK
  /content       Jay writes 3 to 5 new drafts
  /queue         drafts waiting      /draft N   the full text of draft N
  /approve N     /reject N           /posted N   mark it posted
  /video N       make the video for draft N
  /publish N     post draft N now to YouTube / Instagram
  /connect youtube    one-time sign-in so Jay can upload
PERMISSIONS
  /allow KIND [only WORD] [for 8h|2d|forever]     stop asking for that kind of action
  /grants        what is allowed         /revoke N | KIND | all    take it back
  /autoapprove   on or off: carry out everyday actions (files, browser, apps, commands) without approval cards
CONTROL
  /stop          drop everything and refuse anything waiting
  /status        what Jay is doing, approvals waiting, builds, permissions
AUTOMATION
  /autopilot     see the ready-made routines;  /autopilot on  or  /autopilot off
LEARNING JAY
  /guide         list of topics;  /guide TOPIC   one topic (e.g. /guide video)
  /commands      this list       /tour   a step-by-step walk-through       /setup   what's connected and missing
  /help          the short list inside the window
IN PLAIN WORDS (no command needed)
  "Build me ..." (builds an app)   "Keep an eye on ..." (a watch)   "Every Monday at 9, ..." (a routine)
  "Install a skill from <link>"   "How do I ...?" (Jay explains)   "Learn my voice, I'm NAME"
All of these also work from Telegram."""

TOUR = [
    ("Talking to Jay", "Say \"Hey Jay\" and ask for something, or type in the window. Say \"Stop Jay\" to make it stop.", "Try: Hey Jay, what's on today?"),
    ("Your phone", "Message Jay on Telegram from anywhere. When it needs your OK, buttons appear on your phone.", "Try: send your bot \"what's on today?\". Not paired yet? /guide phone"),
    ("Who decides", "Jay does safe things by itself and asks before risky ones. You can pre-approve a kind of action for a while.", "Try: /allow files for 1h, then /grants, then /revoke all"),
    ("Daily help", "Plans, reminders, study help, news, job leads, and watching pages for changes.", "Try: /plan, or: Jay, keep an eye on my college notices page"),
    ("Building apps", "Jay writes, tests, reviews and improves a whole app by itself, then messages you.", "Try: Jay, build me a small notes web app with tests. (Start Docker first for safety.)"),
    ("Your channels", "Jay reads your YouTube and Instagram numbers and tells you what works and what to make next.", "Try: /youtube or /instagram (needs the one-time sign-in: /guide youtube)"),
    ("The content desk", "Jay writes drafts. You approve them on your phone. Only you can approve.", "Try: /content, then tap Approve on your phone, then /queue"),
    ("Making videos", "From an approved draft, Jay records a voiceover and makes a captioned video with a thumbnail.", "Try: /video 1  (add a free Pexels key for real pictures: /guide video)"),
    ("Posting", "Jay posts approved videos at the scheduled time, only if every safety check passes.", "Try: /publish 1 to post now, or /allow publish for 7d for scheduled posts"),
    ("Autopilot", "Routines let Jay work while you're away and report to your phone.", "Try: /autopilot to see the ready-made ones, then /autopilot on"),
    ("Check your setup", "See what's connected and exactly what's still missing.", "Try: /setup . That's the tour. /commands lists everything."),
]

STOPWORDS = frozenset("what whats is are the a an to of and for in on my me i you your it can do does how jay about with this that "
                      "please tell show explain use using want need get make".split())
AUTOPILOT = [
    ("autopilot: weekly content", "mon", "09:00", "content"),
    ("autopilot: evening snapshot", "daily", "20:00", None),
    ("autopilot: morning queue", "daily", "08:00", None),
]
SNAPSHOT_PROMPT = ("Give me a very short evening snapshot of my channels: if YouTube or Instagram is connected, call youtube_channel "
                   "and/or instagram_account and say in at most four sentences what changed, and how today's posts are doing. "
                   "If neither is connected, reply with one short sentence saying so. Do not post or change anything.")
QUEUE_PROMPT = ("Call content_list. In two or three sentences tell me what is waiting for my approval, what is approved and due "
                "today, and anything overdue. If the queue is empty, say so in one short line. Do not approve or post anything.")


def find_topic(query: str) -> Topic | None:
    query = re.sub(r"[^a-z0-9 ]", " ", query.lower()).strip()
    if not query:
        return None
    for t in TOPICS:
        if query == t.name:
            return t
    words = set(query.split()) - STOPWORDS
    if not words:
        words = set(query.split())
    best, score = None, 0
    for t in TOPICS:
        hay = set((t.name + " " + t.title + " " + t.words).lower().split())
        hits = len(words & hay)
        if hits > score:
            best, score = t, hits
    return best


def index_text() -> str:
    return ("Jay's guide. Type /guide followed by a topic, for example /guide video.\n\n"
            + "\n".join(f"  {t.name:<12} {t.title}" for t in TOPICS)
            + "\n\n/commands lists every command, /tour walks you through the main things, /setup shows what's still missing.")


def topic_text(query: str) -> str:
    topic = find_topic(query)
    if topic is None:
        return f"I don't have a topic for '{query}'.\n\n" + index_text()
    return f"{topic.title}\n{'-' * len(topic.title)}\n{topic.text}"


def commands_in_text() -> set[str]:
    """Every /command word mentioned in the commands list (used by tests to keep it complete)."""
    return set(re.findall(r"(?<![\w/])/([a-z]+)", COMMANDS))


# ------------------------------------------------------------------ what is connected
def checks(data_dir: Path, registry: Registry, telegram=None) -> list[tuple[bool, str, str]]:
    """(ok, what, what to do if it isn't) for every optional part of Jay."""
    from . import builder

    env = lambda name: bool(os.getenv(name, "").strip())          # noqa: E731
    brains = [n for n, key in (("Gemini", "GEMINI_API_KEY"), ("Groq", "GROQ_API_KEY"), ("Claude", "ANTHROPIC_API_KEY")) if env(key)]
    google = (data_dir / "google_credentials.json").exists()
    grants = registry.grants.items if registry.grants is not None else []
    return [
        (bool(brains), f"AI brains ({', '.join(brains) or 'none'})", "Put at least one key (GEMINI_API_KEY, GROQ_API_KEY or ANTHROPIC_API_KEY) in .env."),
        (env("ANTHROPIC_API_KEY"), "Claude key (needed to build apps)", "Put your Anthropic key in .env as ANTHROPIC_API_KEY, and some credit on that account. (/guide build)"),
        (env("TELEGRAM_BOT_TOKEN"), "Telegram bot", "Create a bot with @BotFather and put its token in .env as TELEGRAM_BOT_TOKEN. (/guide phone)"),
        (bool(telegram is not None and getattr(telegram, "chat_id", None)), "Phone paired", "Open your bot in Telegram and send /pair <code> (the code shows when Jay starts)."),
        (env("JARVIS_MAIL_USER") and env("JARVIS_MAIL_PASSWORD"), "Email (IMAP/SMTP)", "Add JARVIS_MAIL_USER and JARVIS_MAIL_PASSWORD (an app password) to .env. (/guide email)"),
        (google, "Google sign-in file", "Download an OAuth Desktop client as data\\google_credentials.json. (/guide youtube)"),
        ((data_dir / "youtube_token.json").exists(), "YouTube analytics signed in", "Ask Jay to analyse your YouTube channel; a browser opens once to sign in."),
        ((data_dir / "youtube_upload_token.json").exists(), "YouTube uploading signed in", "Send /connect youtube on the laptop. (/guide posting)"),
        (env("INSTAGRAM_ACCESS_TOKEN"), "Instagram analytics token", "Generate a token as in /guide instagram and put it in .env as INSTAGRAM_ACCESS_TOKEN."),
        (env("INSTAGRAM_PUBLISH_TOKEN"), "Instagram posting token", "Generate a token that also allows content publishing; put it in .env as INSTAGRAM_PUBLISH_TOKEN."),
        (env("PEXELS_API_KEY"), "Pexels pictures for videos", "Get a free key at pexels.com/api and put it in .env as PEXELS_API_KEY (or use your own images)."),
        (builder.docker_ready(), "Docker (safe app building)", "Start Docker Desktop before big builds."),
        (shutil.which("ollama") is not None, "Local model (Ollama)", "Optional: install Ollama from ollama.com to run a model on this PC. (/guide local)"),
        (os.getenv("JARVIS_AUTOPUBLISH", "0") == "1", "Automatic posting at scheduled time", "Optional and off by default: put JARVIS_AUTOPUBLISH=1 in .env. (/guide posting)"),
        (bool(registry.routines.all()), "Routines running by themselves", "Optional: /autopilot on. (/guide routines)"),
        (bool(grants), f"Standing permissions ({len(grants)} active)", "Optional: /allow mail for 8h and similar. (/guide permissions)"),
    ]


def setup_text(data_dir: Path, registry: Registry, telegram=None) -> str:
    rows = checks(data_dir, registry, telegram)
    done = sum(1 for ok, _, _ in rows if ok)
    lines = [f"Setup check: {done} of {len(rows)} connected.\n"]
    for ok, what, fix in rows:
        lines.append(f"  {'✔' if ok else '✖'} {what}" + ("" if ok else f"\n      → {fix}"))
    return "\n".join(lines)


# ------------------------------------------------------------------ the commands
class Guide:
    def __init__(self, store, data_dir: Path, registry: Registry, telegram=lambda: None):
        self.store, self.data_dir, self.registry, self.telegram = store, data_dir, registry, telegram

    # -- tour
    def _step(self, n: int) -> str:
        n = max(0, min(n, len(TOUR) - 1))
        self.store.set_meta("tour_step", str(n))
        title, text, example = TOUR[n]
        nav = "/tour next" if n < len(TOUR) - 1 else "that's the end: /commands lists everything"
        return f"Tour, step {n + 1} of {len(TOUR)}: {title}\n{text}\n{example}\n\n({nav} · /tour back · /tour stop)"

    def tour(self, arg: str) -> str:
        current = int(self.store.get_meta("tour_step") or 0)
        arg = arg.strip().lower()
        if arg in ("", "start", "begin"):
            return self._step(0)
        if arg == "next":
            return self._step(current + 1)
        if arg in ("back", "previous", "prev"):
            return self._step(current - 1)
        if arg.isdigit():
            return self._step(int(arg) - 1)
        if arg in ("stop", "end", "exit"):
            self.store.set_meta("tour_step", "0")
            return "Tour closed. /guide has everything in detail."
        return "Say /tour, /tour next, /tour back, or /tour 5."

    # -- autopilot
    def autopilot(self, arg: str) -> str:
        routines = self.registry.routines
        have = {r["name"]: r for r in routines.all()}
        arg = arg.strip().lower()
        if arg == "on":
            from .agent import ROUTINES

            made = []
            for name, days, at, key in AUTOPILOT:
                if name in have:
                    continue
                prompt = ROUTINES["content"] if key == "content" else (SNAPSHOT_PROMPT if "evening" in name else QUEUE_PROMPT)
                routines.add(name, prompt, days, at)
                made.append(f"{name} ({days} {at})")
            return ("Autopilot is on. " + ("Added: " + "; ".join(made) + "." if made else "They were already on.")
                    + " Results come to your phone, and anything needing your OK asks there. /autopilot off removes them.")
        if arg == "off":
            removed = [r["id"] for r in routines.all() if r["name"].startswith("autopilot:")]
            for rid in removed:
                routines.remove(rid)
            return f"Autopilot is off ({len(removed)} routine{'s' if len(removed) != 1 else ''} removed)."
        lines = ["Autopilot: ready-made routines Jay runs by itself while it's running on your laptop."]
        for name, days, at, key in AUTOPILOT:
            lines.append(f"  {'✔ on ' if name in have else '✖ off'}  {name}: {days} at {at}")
        lines.append("/autopilot on to switch them on, /autopilot off to remove them. Write your own in plain words, e.g. "
                     "\"every Friday at 6, summarise my week\".")
        return "\n".join(lines)

    # -- one entry point
    def handle(self, text: str) -> str | None:
        parts = text.strip().split(None, 1)
        if not parts:
            return None
        cmd, arg = parts[0].lower().lstrip("/"), (parts[1] if len(parts) > 1 else "")
        if cmd == "guide":
            return topic_text(arg) if arg.strip() else index_text()
        if cmd == "commands":
            return COMMANDS
        if cmd == "tour":
            return self.tour(arg)
        if cmd == "setup":
            return setup_text(self.data_dir, self.registry, self.telegram())
        if cmd == "autopilot":
            return self.autopilot(arg)
        return None


def register(reg: Registry, guide: Guide) -> None:
    @reg.tool(
        "guide",
        "Explain Jay itself to the user: what it can do, how to use a feature, which command does what, how something "
        "works, or how to set something up. Call it whenever the user asks 'what can you do', 'how do I ...', 'what "
        "does /something do', or seems lost; answer from what it returns instead of guessing. Give a topic word "
        "(start, voice, phone, permissions, everyday, email, pc, skills, mcp, build, youtube, instagram, content, "
        "video, posting, routines, security, local, setup), 'commands' for every command, or nothing for the list.",
        {"topic": {"type": "string"}},
    )
    def guide_tool(topic: str = "") -> str:
        topic = topic.strip().lower()
        if topic in ("commands", "command", "list"):
            return COMMANDS
        return topic_text(topic) if topic else index_text()

    @reg.tool("check_setup", "Check what is connected and what is still missing in Jay (brains, phone, email, Google, "
              "YouTube, Instagram, Pexels, Docker, local model) with the next step for each.")
    def check_setup() -> str:
        return setup_text(guide.data_dir, reg, guide.telegram())
