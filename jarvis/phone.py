"""Reaching the user's phone first: reminders, things a watch found, the day summary, "text me that".

Two channels, either or both:
  * Telegram: the paired bot chat (see telegram.py), which also lets you send Jay messages.
  * ntfy: a free push-notification service that needs no account. Jay makes a private random
    topic name; you install the ntfy app and subscribe to that topic. Anyone who knew the topic
    could read the alerts, so it is long and random and never shown outside the laptop.
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

import requests

from .security import check_file_allowed

NTFY_SERVER = "https://ntfy.sh"


def _to_opus(source: Path, target: Path) -> None:
    """mp3 -> ogg/opus, the format Telegram plays as a voice note."""
    import av

    with av.open(str(source)) as inp, av.open(str(target), "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000, frame_size=960)
        for frame in inp.decode(audio=0):
            for piece in resampler.resample(frame):
                out.mux(stream.encode(piece))
        for piece in resampler.resample(None):
            out.mux(stream.encode(piece))
        out.mux(stream.encode(None))


class Phone:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "ntfy_topic"
        self.telegram = None                        # set by the session once the bot is running
        self.voice_name = os.getenv("JARVIS_VOICE_EN", "en-GB-RyanNeural")

    # --- ntfy -------------------------------------------------------------------------------
    @property
    def topic(self) -> str:
        env = os.getenv("NTFY_TOPIC", "").strip()
        if env:
            return env
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def create_topic(self) -> str:
        topic = self.topic
        if not topic:
            topic = "jay-" + secrets.token_urlsafe(14).replace("_", "x").replace("-", "y").lower()
            self.path.write_text(topic, encoding="utf-8")
        return topic

    # --- sending ----------------------------------------------------------------------------
    def channels(self) -> list[str]:
        found = []
        if self.telegram is not None and getattr(self.telegram, "chat_id", None):
            found.append("telegram")
        if self.topic:
            found.append("ntfy")
        return found

    def available(self) -> bool:
        return bool(self.channels())

    def send(self, title: str, text: str, file_path: str = "") -> list[str]:
        """Send to every working channel; returns the channels that accepted it."""
        done = []
        if self.telegram is not None and getattr(self.telegram, "chat_id", None):
            try:
                self.telegram.send(self.telegram.chat_id, f"{title}\n{text}" if title else text)
                if file_path:
                    self.telegram.send_file(self.telegram.chat_id, file_path)
                done.append("telegram")
            except Exception:
                pass
        if self.topic:
            try:
                headers = {"Title": (title or "Jay").encode("ascii", "ignore").decode() or "Jay"}
                resp = requests.post(f"{NTFY_SERVER}/{self.topic}", data=text.encode("utf-8"),
                                     headers=headers, timeout=15)
                if resp.ok:
                    done.append("ntfy")
            except Exception:
                pass
        return done

    def send_video(self, title: str, text: str, video_path: str) -> list[str]:
        """The finished video to the phone, playable in the chat, with a note."""
        done = []
        tg = self.telegram
        if tg is not None and getattr(tg, "chat_id", None):
            try:
                tg.send_video(tg.chat_id, video_path, f"{title}\n{text}")
                done.append("telegram")
            except Exception:
                pass
        if not done:
            done = self.send(title, text)
        return done

    def send_voice(self, text: str) -> bool:
        """Speak `text` and send it to the Telegram chat as a voice note (an audio file if opus isn't available)."""
        tg = self.telegram
        if tg is None or not getattr(tg, "chat_id", None):
            return False
        import asyncio
        import tempfile

        folder = Path(tempfile.mkdtemp(prefix="jay-voice-"))
        try:
            import edge_tts

            mp3 = folder / "update.mp3"
            asyncio.run(edge_tts.Communicate(text[:2500], self.voice_name, rate="+0%").save(str(mp3)))
            try:
                ogg = folder / "update.ogg"
                _to_opus(mp3, ogg)
                tg.send_audio(tg.chat_id, str(ogg), voice=True)
            except Exception:
                tg.send_audio(tg.chat_id, str(mp3), voice=False)
            return True
        except Exception:
            return False
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def register(self, reg) -> None:
        @reg.tool(
            "message_phone",
            "Send a message (and optionally a file from the PC) to the user's phone, for when they ask "
            "to be messaged something or want a file on their phone.",
            {"text": {"type": "string"},
             "file_path": {"type": "string", "description": "Optional file on the PC to send along (Telegram only)."},
             "voice": {"type": "boolean", "description": "Also send it as a spoken voice note (good for updates on work done while they were away)."}},
            required=("text",),
            confirm=lambda a: f"Send the file {a['file_path']} to your phone" if a.get("file_path") else None,
        )
        def message_phone(text: str, file_path: str = "", voice: bool = False) -> str:
            if not self.available():
                return ("The phone isn't linked yet. Use setup_phone_alerts for notifications, or the "
                        "Telegram link described in the README.")
            if file_path and not Path(file_path).is_file():
                return f"There is no file at {file_path}."
            if file_path:
                check_file_allowed(file_path)                 # never send keys, tokens or cookies off the laptop
            sent = self.send("Jay", text, file_path)
            if voice and self.send_voice(text):
                sent.append("voice note")
            return f"Sent to your phone ({', '.join(sent)})." if sent else "Couldn't reach the phone just now."

        @reg.tool(
            "setup_phone_alerts",
            "Set up free phone notifications (ntfy, no account needed) and return what the user has to do "
            "on their phone to receive them.",
        )
        def setup_phone_alerts() -> str:
            topic = self.create_topic()
            ok = self.send("Jay", "Phone alerts are on. You'll get reminders and new finds here.")
            return (f"Phone alerts topic: {topic}\n"
                    "On the phone: install the 'ntfy' app (Play Store or App Store), tap +, "
                    f"choose 'Subscribe to topic', type {topic} and leave the server as ntfy.sh. "
                    + ("A test alert was just sent." if ok else "The test alert could not be sent; check the internet."))

    def send_async(self, title: str, text: str) -> None:
        """For alerts raised in the background: never delay or break what Jay is doing."""
        if self.available():
            threading.Thread(target=self.send, args=(title, text[:1500]), daemon=True).start()
