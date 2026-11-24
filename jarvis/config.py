"""Settings, read from environment variables / the .env file next to this project."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Config:
    name: str
    provider: str              # which brain to start on
    fallback_order: list[str]  # brains to try, in order, when one is unavailable
    history_turns: int         # past turns resent to chat-style brains (token saver)
    claude_model: str
    effort: str                # low | medium | high | xhigh | max
    fallbacks: bool            # server-side refusal fallback (Claude API beta)
    ollama_model: str
    checkin_minutes: int       # how often it reviews the schedule unprompted; 0 = never
    morning_brief: bool        # greet and brief on the first start of each day
    digest_speak: bool         # read the morning and evening summaries aloud (when you are at the laptop)
    reminder_mode: str         # digest = planned blocks only appear in the morning and evening summaries; each = ring at every start
    evening_after: str         # HH:MM: evening summary from this time on a college day
    evening_other: str         # HH:MM: ... and on other days
    leetcode_user: str         # public LeetCode username, to check the day's problem was really solved
    github_user: str           # public GitHub username, to check coding blocks
    brief_time: str            # HH:MM when the day's plan is pushed to the phone; empty = off
    weekly_time: str           # HH:MM on Sundays when the week's summary is sent
    night_time: str            # HH:MM for the end-of-day summary; empty = off
    hotkey: str                # global shortcut that opens Jay, e.g. ctrl+alt+j; empty = off
    languages: list[str]       # languages spoken to it: ["en"] or ["en", "te"]
    merge_seconds: int         # a spoken addition within this many seconds joins the request in progress
    active_seconds: int        # after a reply, how long it keeps listening without "Hey Jay"
    wake_model: str            # small local model that spots "Hey Jay" / "Stop Jay"
    stt: str                   # speech recognition: auto | groq | local
    stt_vocab: str             # words and names the speech recogniser should expect (improves spelling)
    stt_model: str             # Groq speech model
    whisper_model: str         # local speech model (used when Groq isn't)
    tts: str                   # speech output: auto | edge | sapi
    voice_en: str
    voice_te: str
    voice_rate: str            # e.g. "+4%" faster, "-5%" slower
    voice_pitch: str           # e.g. "-3Hz" slightly deeper
    address: str               # how Jay addresses the user, e.g. "sir"; empty = by name or not at all
    tts_rate: int
    wake_names: list[str]      # words that wake the assistant in hands-free mode
    mic_threshold: float       # 0 = measure the room at startup
    wake_anyone: bool          # let any voice wake Jay, not just recognised ones
    data_dir: Path

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Config":
        load_dotenv(ROOT / ".env")
        data = data_dir or Path(os.getenv("JARVIS_DATA_DIR", ROOT / "data"))
        data.mkdir(parents=True, exist_ok=True)
        name = os.getenv("JARVIS_NAME", "Jay")
        order = _list("JARVIS_FALLBACK_ORDER", "gemini,groq,claude,ollama")
        return cls(
            name=name,
            provider=os.getenv("JARVIS_PROVIDER", order[0] if order else "gemini").lower(),
            fallback_order=order,
            history_turns=max(1, int(os.getenv("JARVIS_HISTORY_TURNS", "12"))),
            claude_model=os.getenv("JARVIS_CLAUDE_MODEL", "claude-opus-5-5"),
            effort=os.getenv("JARVIS_EFFORT", "medium"),
            fallbacks=_flag("JARVIS_FALLBACKS", True),
            ollama_model=os.getenv("JARVIS_OLLAMA_MODEL", "qwen3:8b"),
            checkin_minutes=max(0, int(os.getenv("JARVIS_CHECKIN_MINUTES", "45"))),
            morning_brief=_flag("JARVIS_MORNING_BRIEF", True),
            digest_speak=_flag("JARVIS_DIGEST_SPEAK", True),
            reminder_mode=os.getenv("JARVIS_REMINDER_MODE", "digest").strip().lower(),
            evening_after=os.getenv("JARVIS_EVENING_AFTER", "19:30").strip(),
            evening_other=os.getenv("JARVIS_EVENING_OTHER", "17:30").strip(),
            leetcode_user=os.getenv("JARVIS_LEETCODE_USER", "").strip(),
            github_user=os.getenv("JARVIS_GITHUB_USER", "").strip(),
            brief_time=os.getenv("JARVIS_BRIEF_TIME", "07:00").strip(),
            weekly_time=os.getenv("JARVIS_WEEKLY_TIME", "20:00").strip(),
            night_time=os.getenv("JARVIS_NIGHT_TIME", "22:30").strip(),
            hotkey=os.getenv("JARVIS_HOTKEY", "ctrl+alt+j").strip().lower(),
            languages=_list("JARVIS_LANGUAGES", "en") or ["en"],
            merge_seconds=max(0, int(os.getenv("JARVIS_MERGE_SECONDS", "12"))),
            active_seconds=max(5, int(os.getenv("JARVIS_ACTIVE_SECONDS", "25"))),
            wake_model=os.getenv("JARVIS_WAKE_MODEL", "base.en"),
            stt=os.getenv("JARVIS_STT", "auto").lower(),
            stt_vocab=os.getenv("JARVIS_STT_VOCAB", "LeetCode, NeetCode, Spring Boot, WhatsApp, Telegram, Gemini, Groq, ntfy, timetable").strip(),
            stt_model=os.getenv("JARVIS_STT_MODEL", "whisper-large-v3"),
            whisper_model=os.getenv("JARVIS_WHISPER_MODEL", "small"),
            tts=os.getenv("JARVIS_TTS", "auto").lower(),
            voice_en=os.getenv("JARVIS_VOICE_EN", "en-GB-RyanNeural"),
            voice_te=os.getenv("JARVIS_VOICE_TE", "te-IN-MohanNeural"),
            voice_rate=os.getenv("JARVIS_VOICE_RATE", "+4%"),
            voice_pitch=os.getenv("JARVIS_VOICE_PITCH", "-3Hz"),
            address=os.getenv("JARVIS_ADDRESS", "").strip(),
            tts_rate=int(os.getenv("JARVIS_TTS_RATE", "185")),
            wake_names=list(dict.fromkeys([name.lower(), *_list("JARVIS_WAKE_ALIASES", "jai,jey,jae,jaye,j,jaya,jake,jade,jaay,jah")])),
            mic_threshold=float(os.getenv("JARVIS_MIC_THRESHOLD", "0")),
            wake_anyone=_flag("JARVIS_WAKE_ANYONE", False),
            data_dir=data,
        )
