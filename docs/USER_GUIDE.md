# Jay

A voice-first personal assistant for your Windows laptop. Say "Hey Jay" and talk; say
"Stop Jay" and it goes quiet. Jay runs your schedule and re-plans it as things change,
learns how you actually work, keeps up with the internet for you, controls the laptop,
and remembers you between sessions. (The name is set by `JARVIS_NAME` in `.env`; the
project folder and commands still say "jarvis".)

## Using it

Jay starts with Windows and waits in the background. It works like "Hey Siri":

| You say or do | What happens |
|---|---|
| **"Hey Jay"** | A chime, and Jay's window opens with the 3D sphere. Then just talk. |
| **"Hey Jay, what's on today?"** | Same, and it answers straight away. |
| Keep talking | For about 25 seconds after each reply you don't need "Hey Jay" again. |
| **"Stop Jay"** | It stops talking or working at once, the window closes, and it waits for "Hey Jay". |
| Double-click the **Jay** desktop icon | Opens the window and wakes it, without using your voice. |
| Close the window | Same as "Stop Jay": it goes back to waiting. |
| Start typing in the window | The text panel opens; type a message or a `/command`. |

The sphere shows what Jay is doing: dim grey-blue in standby, cyan when listening for you,
white when recording from the microphone button, violet and fast while working, rippling
while speaking, amber when it needs your approval.

- **Approval cards** appear over the sphere when Jay wants to run a command, type into an
  app, click in the browser or send something: press Allow or Don't. "Stop Jay" refuses them.
- **The ☰ button** opens details: voice switches, your schedule (tick a task to mark it
  done), which brain is answering, how you work, what Jay remembers, and **Shut Jay down
  completely**.
- **The keyboard button** (or just typing) opens the conversation panel with the full
  history and each reply's "What I did" steps.

**Privacy of the microphone.** While waiting, Jay checks what it hears with a small speech
model that runs on this laptop (`base.en`, about 140 MB). Nothing you say leaves the laptop
until it hears "Hey Jay". After that, your requests go to Groq for accurate transcription.

**Speech is English only** (`JARVIS_LANGUAGES=en`). Jay always answers in English, even if
you type Telugu words in English letters. Set `JARVIS_LANGUAGES=en,te` to bring Telugu
back; it then hears every clip both ways and picks the more confident one, which is slower
and sometimes wrong on short English sentences.

**The display.** A HUD around a voice-reactive reactor core: the bars around the sphere
follow Jay's voice while it speaks and your microphone while it listens. Left: live CPU,
memory, storage, battery, processes and network traffic, plus an activity log of every
step. Right: your schedule and the voice link. Top: brain in use, clock and date. Colour
shows the state: steel blue standby, cyan listening, violet working, amber approval.

**Voice.** A British English neural voice (`en-GB-RyanNeural`, slightly faster and deeper
than default). Speech streams, so it starts within about a second, and Jay says a short
"On it." the moment it starts a job that needs tools. Samples of other voices are in
`data\voice_samples`; switch with `JARVIS_VOICE_EN`, and set `JARVIS_ADDRESS` (e.g. `sir`)
if you'd like Jay to address you.

**Voices (who is speaking).** Jay can recognise up to six people by voice and personalise
for each: it greets them by name, remembers facts under their name, and uses what it knows
about them.

- *Add a person:* ☰ → **Voices I recognise** → type a name → **Add voice**, or just say
  "Hey Jay, learn my voice, I'm Ravi". A card shows four short lines to read aloud; that's it.
- *The owner:* the first voice added is the owner. Only the owner's voice can use messages,
  email, calendar, files, commands, the browser, or change or delete schedule items.
  Everyone else (and any voice Jay doesn't know) can ask questions, search, get the weather,
  calculate and add reminders. Typing at the keyboard counts as the owner.
- *Privacy:* recognition runs on this laptop (NVIDIA's free TitaNet-small model, 40 MB, in
  `data\models`); voiceprints are stored in `data\voiceprints.json`. Only add people who
  agree to it, and remove anyone from the same panel.
- If Jay mixes up or fails to recognise real voices, add the person again in a quieter
  room; the match threshold is in `jarvis\speakers.py` (0.6).

**Shortcuts.** `scripts\install_startup.ps1` created the desktop icon, a Start menu entry
and the start-up entry. To put Jay on the taskbar: search "Jay" in the Start menu,
right-click it, and choose **Pin to taskbar** (Windows doesn't let programs pin themselves).
The icon is drawn by `scripts\make_icon.py`. To remove both: `powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove`.
To keep only the desktop icon, run it again with `-NoStartup` after removing.

Launch options (`jarvis.bat ...`): `--standby` (start hidden, as at Windows start-up),
`--no-voice` (typing only), `--quit-on-close` (closing the window shuts Jay down instead
of sending it to standby), `--brief`, `--local`, `--console` (terminal instead of window).

Only one copy of Jay can run. If it ever seems stuck, this stops it:

```
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object CommandLine -match '-m jarvis' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
```

**Learning Jay from inside Jay.** Type `/guide` for a list of topics (`/guide video`, `/guide posting`, ...),
`/commands` for every command, `/tour` for a short step-by-step walk-through, `/setup` to see what is connected
and what is still missing (with the next step for each), and `/autopilot` for ready-made automatic routines.
You can also just ask, "Jay, how do I make a video?". All of these work from Telegram too.

Commands you can type in the chat: `/help`, `/brief`, `/plan`, `/news`, `/messages`,
`/dashboards`, `/deadlines`, `/erp`, `/jobs`, `/patterns`, `/reflect`, `/model NAME`,
`/memory`, `/tasks`, `/reset`.

The window is served only to this computer (`http://127.0.0.1:8765`) and needs a secret
token the launcher puts in its address, so other programs and websites can't drive Jay.
Without a terminal, messages that would have been printed go to `data/jay.log`.

## How Jay works

Jay is a language model with tools, not a set of rules. For each thing you say it decides
what to do, does it, and tells you. Each step is printed as it happens
(`> update_task {...}`), so you can always see what it did and why.

- **Short, casual requests.** Say "repu full ga plan cheyyi" or "I'm tired, push things".
  Jay fills in the details from your schedule, your memories and the time of day, and only
  asks when a wrong guess would cost you something.
- **It runs the schedule.** Every task has a deadline, a planned start, an estimate and a
  priority. Jay adds, moves, re-prioritises and drops tasks on its own judgment and tells
  you what changed. You are reminded at each planned start.
- **It re-plans.** New work, a task running late, "I'm not well tomorrow": Jay reshuffles
  the rest around hard deadlines straight away.
- **It checks in.** Every `JARVIS_CHECKIN_MINUTES` (45 by default) Jay looks at the
  schedule unprompted, re-plans if something slipped, and speaks only if it matters.
- **It learns.** Jay records what gets finished, when, how late, and what gets postponed
  (`/patterns` shows the numbers), and plans from that. Once a day at startup it reviews
  recent conversations and saves lasting patterns about you as memories (`/memory`).
- **It follows the internet.** For anything current it searches the web instead of relying
  on what the model already knows; `/news` and the briefing cover the fields you follow.
- **It works jobs through.** Given a job, Jay plans the steps, carries them out, tries
  another route when one fails, and reports at the end instead of stopping to ask.
- **It has continuity.** A new session starts from the end of the last conversation, so
  "that test" or "the thing from yesterday" still means something.
- **It knows your material.** "Learn this file" (text, code or PDF) or "note this down"
  puts it in a personal knowledge base. Jay searches it before answering questions about
  your own notes and names the source. This is keyword search over passages (a simple form
  of retrieval-augmented generation), stored in `data/jarvis.db`.
- **It greets you.** On the first start of each day it gives a short briefing unasked
  (`JARVIS_MORNING_BRIEF=0` turns that off).

## Watching things for you

Jay can keep an eye on things and tell you **only when something is new** (a Windows
notification and a line in its window). Just say it: "keep an eye on backend internships,
check daily", "tell me when the college notices page changes", "watch for news on GenAI".
Kinds: news on a topic, job postings for a search, a web page, or email matching a Gmail
search (needs Google). Each check is compared with the last, so nothing is reported twice;
a check that fails is retried within half an hour and you're warned once. `list watches` and
`stop watching ...` work by voice. One watch is already set up: Java / Spring Boot internships, daily.

Also unprompted: an **end-of-day summary** at 22:30 (`JARVIS_NIGHT_TIME`), and a **daily
backup** of Jay's memory, profile and voiceprints to `data\backups` (last 7 days kept).

## Study and coding help

- `/quiz [topic]`: one question at a time from your own notes, with a score at the end.
- `/revise`: a revision plan for your upcoming exams, added to your schedule.
- "git status of my KVG project" / "what changed in ByteVault": read-only `git` status, log and diff.
- "what's this error on my screen?": Jay looks at the screen and explains it.

## The window, extras

- **Today timeline** (right side): your planned work as blocks from 6 am to midnight, with a marker for now.
- **Result cards**: weather, headlines, currency and similar answers also appear as a card.
- **Mini mode** (the corner-square button, or "Jay, go into mini mode"): a small always-on-top
  panel in the bottom-right corner; "back to the full window" restores it.
- **Themes** (☰ → Theme): Arc cyan, Ember, Emerald, Ice white, Crimson.
- **Global shortcut** `Ctrl+Alt+J` (`JARVIS_HOTKEY`) summons Jay from any app without speaking.
- **Crash recovery**: if Jay's process dies, it restarts itself within a few seconds, quietly,
  and a window you left open reconnects.

## Phone and app connections

- **Alerts on your phone (free, no account):** say "Jay, set up phone alerts". Jay makes a private topic
  and tells you to install the **ntfy** app and subscribe to it. After that, reminders, things a watch
  found and the end-of-day summary all arrive on your phone, even when the laptop window is closed.
- **"Message me that" / "send me that file":** `message_phone` sends text (and, with your approval, a file
  from the laptop) to Telegram or ntfy.
- **Telegram two-way chat** (below) also accepts **voice notes**: record one in Telegram and Jay
  transcribes it (with your Groq key) and answers in the chat.
- **Email without Google Cloud:** `JARVIS_MAIL_USER` and `JARVIS_MAIL_PASSWORD` (an app password) in `.env`
  give Jay `mail_list`, `mail_read` (neither marks mail as read) and `mail_send` (you approve each one).
- **Calendar without Google Cloud:** put your calendar's secret iCal address(es) in `JARVIS_ICS_URLS`;
  Jay reads events, repeating classes and all-day items with `calendar_feed`. A college timetable link works too.
- Everything above is owner-only: another voice asking for it is refused.

## Phone access (Telegram)

Needs a bot token from you (see "Things only you can do"). Once paired, you can message Jay
from your phone for reminders, questions, quick lookups and schedule changes. Only the paired
chat is answered, and anything that needs an approval (commands, sending messages, files) is
refused from the phone: those stay on the laptop.

## What kind of system this is

Jay is an *LLM agent*: a pretrained language model (Gemini or Groq, reached over their
APIs) wrapped in our own software, which gives it tools, memory, a schedule, a knowledge
base, a voice and a window. The model is not trained by us; everything that makes Jay
yours (what it knows about you, how it plans, what it can do) lives in this project.

## Setup

Copy `.env.example` to `.env`. Jay needs at least one brain:

| Brain | Cost | What to do |
|---|---|---|
| `gemini` | Free tier | Key from https://aistudio.google.com/apikey -> `GEMINI_API_KEY` |
| `groq` | Free tier | Key from https://console.groq.com/keys -> `GROQ_API_KEY` |
| `openrouter` | Free models | Key -> `OPENROUTER_API_KEY`, and pick a model in `JARVIS_OPENROUTER_MODEL` |
| `claude` | Paid | Key from https://console.anthropic.com/ -> `ANTHROPIC_API_KEY` |
| `ollama` | Free, offline | Install https://ollama.com, then `ollama pull qwen3:8b` (or a smaller model; set `JARVIS_OLLAMA_MODEL`) |

`JARVIS_PROVIDER` is the brain it starts on. When a brain is unavailable (no key, rate
limit, offline) it moves down `JARVIS_FALLBACK_ORDER` and tells you it switched. Model
names for each brain are in `.env`; provider line-ups change, so if one reports "model not
found", update the name there.

**Voice**
- *Wake word:* "Hey Jay" and "Stop Jay" are spotted on the laptop by `JARVIS_WAKE_MODEL`.
- *Requests:* with a Groq key, what you say after waking goes to Groq's Whisper
  (`whisper-large-v3`). Without one, a local model is used (`JARVIS_WHISPER_MODEL`).
- *Speaking:* Microsoft's online neural voice (`JARVIS_VOICE_EN`, Indian English by
  default). Offline it falls back to the Windows voice. Speech stops the moment you say
  "Stop Jay", though over loud speaker output the microphone may need a second try.
- At startup it measures the room's background noise for half a second, so stay quiet. If
  it triggers too easily or not at all, set `JARVIS_MIC_THRESHOLD` (try 0.02-0.05). If it
  mishears its name, add the mishearing to `JARVIS_WAKE_ALIASES`.

**Google (optional, for calendar and email)**
1. In https://console.cloud.google.com/ create a project and enable the *Google Calendar API* and *Gmail API*.
2. Configure the OAuth consent screen (External, add yourself as a test user).
3. Create credentials -> OAuth client ID -> *Desktop app*, download the JSON.
4. Save it as `data/google_credentials.json` and restart.
5. The first calendar or email request opens a browser to sign in once.

**Start with Windows (optional)**: `powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1`
(add `-Remove` to undo).

## What it can do

| Area | Examples |
|---|---|
| Schedule | "plan tomorrow", "I have an OS exam Thursday", "move gym to evening", "I'm done with DBMS" |
| PC control | "open Chrome", "find my resume PDF", "how's my battery", "type this into Notepad", "press ctrl+s" |
| Screen | "what's this error on my screen?" (needs a brain that can see images: gemini or claude) |
| Media | "pause", "next song", "volume to 30", "mute" |
| Web and news | "what's new in backend this week", "read this page and summarise it", `/news` |
| Weather, maths | "do I need an umbrella tomorrow?", "18% of 2450 plus 399" |
| Calendar, email | "what's on tomorrow", "any unread mail from my professor", `/deadlines` (needs Google) |
| Apps and windows | "open Chrome", "launch settings", "minimise WhatsApp", "close Notepad", "what's open?" (any app in the Start menu, by its everyday name) |
| Laptop | "lock the screen", "brightness to 40", "put it to sleep", "copy that to my clipboard", "shut down" (sleep, shutdown and restart need approval) |
| Timers and routines | "ten minute timer for the tea", "remind me every weekday at 7:30 to run", "every Monday at 9, team call"; reminders also pop up as Windows notifications |
| Quick facts | "latest AI news", "who was Alan Turing?", "250 dollars in rupees", "play lo-fi music on YouTube", "directions from home to college" |
| History | "what did I tell you about my interview?": Jay searches every past conversation, not just saved memories |
| Your notes | "learn this file", "note that Ravi sends the spec Monday", "what are the deadlock conditions?" |
| WhatsApp | `/messages` or "any messages?": lists unread chats from the chat list (no read ticks), summarises, suggests replies. "Reply to Amma that I'll be late" shows the recipient and exact text on an approval card before sending |
| Dashboards | `/dashboards`: checks the pages you want watched and reports only what changed |
| College ERP | `/erp`: opens the portal in its own Edge window, you sign in, it reads courses, exams and notices |
| Jobs | `/jobs`: finds and shortlists openings in your domain; drafts answers for you to submit yourself |
| Memory | Saves lasting facts and patterns about you; `/memory` lists them |
| Briefing | `/brief`: weather, schedule, calendar, email, what's new in your fields |

## Things only you can do

These need your accounts or a download, so Jay can't do them for you.

**1. Gmail and Calendar** (unlocks mail briefs, deadline-finding in college mail, day planning with your events, email watches)
Follow the Google steps above, then say "Jay, any unread mail?". Once it works, try
"watch my email from the college" and "find my exam dates in my mail".

**2. College ERP**
1. Say "Jay, open my ERP portal" and give it the address when asked (it remembers).
2. A separate Edge window opens: sign in there yourself, once.
3. Then "check my attendance", "any new notices?", and "watch the ERP notices page, check every few hours".

**3a. Phone alerts and email/calendar without Google Cloud (quickest wins)**
1. Say "Jay, set up phone alerts" and follow the two lines it gives you (install ntfy, subscribe).
2. Gmail: Google Account → Security → 2-Step Verification on → App passwords → create one for "Jay". Put in `.env`:
   `JARVIS_MAIL_USER=you@gmail.com` and `JARVIS_MAIL_PASSWORD=<the 16-character app password>`.
   (Outlook/Hotmail work the same way; other providers also set `JARVIS_MAIL_IMAP` and `JARVIS_MAIL_SMTP`.)
3. Calendar: Google Calendar → Settings → your calendar → "Secret address in iCal format" → copy it into
   `JARVIS_ICS_URLS=` (several can be separated by commas). Never share that link: anyone with it can read your calendar.
4. Restart Jay, then say "any unread mail?" and "what's on my calendar this week?".

**3b. Calendar (pick one; the first takes two minutes)**
- *Read your calendar now, no setup:* sign in to Outlook in Jay's browser once (say "Jay, open my portals"), then ask
  "what's on my Outlook calendar this week?". Jay reads the page. Answer Yes to "Stay signed in?".
- *Google Calendar, read-only (recommended for daily briefs):* calendar.google.com, Settings, your calendar, Integrate
  calendar, copy **Secret address in iCal format**, put it in `.env` as `JARVIS_ICS_URLS=<link>`, restart Jay.
  To bring an Outlook/college calendar in, subscribe to it from Google Calendar (Other calendars, From URL) using
  Outlook's Settings, Calendar, Shared calendars, **Publish a calendar**, ICS link (your college may block this).
- *Read and create events (Google):* follow the Google OAuth steps earlier in this README.
- *Timetable PDF:* say `/timetable D:/path/timetable.pdf`. Jay reads it, adds classes, labs and exams to its schedule
  and saves your weekly pattern. `/getknow` makes Jay interview you (hobbies, routine, goals) one question at a time.

**Changing your mind mid-request.** If Jay misheard you or you want something else, just say or type the new request while it is working: Jay drops the old one (silently) and starts the new one at once. Say "Stop Jay" to drop it without a new request. While Jay is *speaking*, only "Stop Jay" is heard, so it never mistakes its own voice for you.

**Your schedule in other calendars.** Jay's schedule is saved to `data\jay_schedule.ics` (also `/exportcalendar`). Import it into Google Calendar (Settings, Import & export) or open it with Outlook/your phone.

**Stopping Jay.** Say "Stop Jay", or press the shortcut (Ctrl+Alt+J) while Jay is talking or working: it stops at once.

**3. Phone access**
1. In Telegram, message **@BotFather**, send `/newbot`, pick a name, and copy the token it gives you.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...` and restart Jay.
3. Jay shows a pairing code in its window (also in the activity log). Open your new bot and send `/pair <code>`.

**4. A faster brain (optional, a small monthly cost)**
1. Create an API key at https://console.anthropic.com/ and add a little credit.
2. In `.env`: `ANTHROPIC_API_KEY=...`, `JARVIS_CLAUDE_MODEL=claude-haiku-5-5`,
   `JARVIS_PROVIDER=claude`, `JARVIS_FALLBACK_ORDER=claude,gemini,groq,ollama`. Restart Jay.
3. Replies should drop to about 1 to 1.5 seconds. Jay adapts its settings to the small model
   automatically. (This path hasn't been tested with a real key.)

**5. An offline brain (optional)**
1. Install Ollama from https://ollama.com, then pull a small model that supports tools, e.g.
   `ollama pull qwen3:4b` (check the exact name at https://ollama.com/library; a 4 GB graphics card
   can't run bigger models well).
2. In `.env`: set `JARVIS_OLLAMA_MODEL` to the name you pulled. Jay falls back to it when the internet or the free quota is down.

**6. Tell Jay about yourself**
Your city, goals and daily routine are still empty in ☰ → About you. Fill them in, or just say
"I live in ..., my goal this semester is ..., I usually wake at ...".

## Supervision

- Jay changes your task list and its own memories freely.
- PowerShell commands, writing files, typing or pressing keys in apps, browser clicks,
  sending email and creating calendar events are shown to you and need a typed `y`.
  Approval is never accepted by voice.
- It never submits an application, form or payment by itself, and never handles passwords.
- Web pages, emails and screen contents are treated as information, never as instructions.

**WhatsApp setup.** The first time, Jay's Edge window shows a QR code: on your phone open
WhatsApp > Linked devices > Link a device and scan it. You stay linked in Jay's own browser
profile. To unlink, remove the device from Linked devices on your phone.

- Listing unread chats reads only the chat list, so nobody sees read ticks.
- Opening a chat (to read it fully or to reply) does mark it read.
- A name that matches several chats, or none, is refused rather than guessed.
- What people write to you is treated as information; Jay doesn't act on instructions inside messages.

**Messaging caveat.** Jay uses WhatsApp Web the way you would. WhatsApp, Instagram and
similar services do not permit automated use, and accounts that look automated can be
restricted. Keep it to reading and the occasional approved reply; do not use it for bulk
messaging.

## The browser

For sites that need your login, Jay opens a separate, visible Microsoft Edge window with
its own profile (`data/browser_profile`). You sign in there by hand once and stay signed
in. It can open pages, read them and list links freely; every click and everything it
types needs your typed `y`.

## Guards against poisoned pages and mail

Anything Jay reads from outside (web pages, mail, WhatsApp, documents) is treated as data, never as orders.
On top of that, these always ask you first, however Jay was persuaded:

- Opening a site Jay has not opened before, and any address on this laptop or your network (`browser_open`,
  `check_site`, and watching a page). Your ERP, LMS and Outlook addresses, and sites you approved once, open freely.
- `open_app` for programs and scripts (.exe, .bat, .ps1, .lnk and similar), shell names like cmd or powershell,
  `\\server\share` paths, `ms-...:` style links, and web links that carry a query string or a long address.
  Plain apps (notepad), documents, folders and short links such as youtube.com open at once.
- Sending any file to your phone. Keys, tokens, cookies and `.env` can never be sent, read or changed by Jay.
- Telegram pairing: five wrong codes pause pairing for 15 minutes. Jay's window token is no longer written
  to the log, and is compared in constant time.

`/allow ...` standing permissions (see Supervision) can relax these, and are logged.

## Skills

Jay keeps step-by-step skills and reads one before a job it covers ("use the weekly review skill"). 27 ship with it:
the 20 from OpenJarvis (Stanford, Apache-2.0) plus, adapted for Jay's own tools, four from Hermes Agent
(NousResearch, MIT: `systematic-debugging`, `email-inbox-triage`, `weekly-review-planning`,
`document-to-action-items`) and three written for you: `dsa-practice-coach`, `assignment-and-exam-tracker`,
`job-application-pack`. Add more by saying "install this skill" with a link to a `SKILL.md`, or drop a folder into
`data\skills`. Jay refuses a skill that tries to give it orders. Skills only add instructions; anything they ask for
still goes through the normal tools and approvals.

## Privacy

What Jay reads (your requests, schedule, screen, pages, mail) is sent to whichever brain is
active; Google may use free-tier prompts to improve its products. Voice audio goes to Groq
for recognition, and reply text goes to Microsoft's speech service. Set `JARVIS_STT=local`
and `JARVIS_TTS=sapi` to keep voice on the laptop.

## Token use

- The free and local brains only resend the last `JARVIS_HISTORY_TURNS` turns.
- Google tools are left out of requests until Google is set up.
- Screenshots are shrunk before they are sent.
- Check-ins are skipped when there are no tasks; set `JARVIS_CHECKIN_MINUTES=0` to stop them.
- Each brain can list several models; when one runs out of free quota or stalls, the next
  is used, and if the whole brain is unavailable the next brain takes over at once.
- Groq allows about 8,000 tokens a minute on the free plan, so it starts with the everyday
  tools only and switches on files, system, browser or Google tools when a request needs them.
- Mid-task, a free-tier per-minute limit is waited out (20s, then 40s) instead of dropping the task.

## Layout

| Path | What it is |
|---|---|
| `jarvis/__main__.py` | Start-up options; the terminal front end (`--console`) |
| `jarvis/session.py` | The running assistant: worker, approvals, voice modes, reminders, check-ins, event stream |
| `jarvis/ui.py`, `jarvis/ui.html` | The window: local server and the page |
| `jarvis/agent.py` | Jay's instructions, routines, brain switching, check-in and daily review |
| `jarvis/providers.py` | Tool loops for Claude, Gemini/Groq/OpenRouter, and Ollama |
| `jarvis/tools/` | `pc.py`, `web.py`, `browser.py`, `personal.py` (memory, schedule, patterns, calculator), `knowledge.py`, `google.py`, `registry.py` |
| `jarvis/watches.py` | Things Jay watches by itself, daily backups, git tools |
| `jarvis/telegram.py` | The phone link |
| `jarvis/profile.py`, `jarvis/speakers.py` | Your profile; who-is-speaking recognition |
| `jarvis/store.py` | SQLite: memories, schedule, activity log, work-pattern statistics (`data/jarvis.db`) |
| `jarvis/voice.py` | Microphone, wake word, speech recognition, English and Telugu speech |
| `tests/test_core.py` | Offline tests: `.venv\Scripts\python.exe -m unittest discover -s tests` |

To add an ability, write a function with `@reg.tool(...)` in a file under `jarvis/tools/`
and call its `register` from `jarvis/tools/__init__.py`. Every brain picks it up.
