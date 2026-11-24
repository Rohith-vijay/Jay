"""Talk to Jay from your phone through a private Telegram bot.

Set TELEGRAM_BOT_TOKEN in .env (from @BotFather). Jay then shows a pairing code once; send
"/pair <code>" to your bot from your phone and that chat becomes the only one Jay answers.
Messages from any other chat are ignored. Requests from the phone can't approve actions
(those still need the laptop), so anything needing approval is declined there.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

import requests

API = "https://api.telegram.org/bot{token}/{method}"
PHONE_HELP = (
    "Hi, it's Jay. Message me anything, by text or voice note, and I'll get on with it and reply here.\n\n"
    "When something needs your OK I'll send buttons here: Allow, Allow for a while, or Deny.\n\n"
    "/allow <kind> [only <word>] [for 8h]  stop asking about a kind of action (mail, files, browser, shell...)\n"
    "/grants   see what you've allowed\n/revoke <number|all>   take it back\n"
    "/stop   drop what I'm doing and refuse anything waiting\n/status   what I'm working on\n"
    "/queue   drafts waiting for you · /draft N full text · /approve N · /reject N · /posted N\n"
    "/video N make the video · /publish N post it now · /allow publish for 7d  let scheduled posts go out\n"
    "/youtube  /instagram   analyse a channel and plan what to post\n"
    "/guide  /commands  /tour  /setup  /autopilot   learn what I can do, check what's connected, switch on routines")


class TelegramLink:
    def __init__(self, token: str, data_dir: Path, on_message, on_info, on_approval=None, on_command=None, on_content=None):
        self.token, self.on_message, self.on_info = token, on_message, on_info
        self.on_approval = on_approval            # (approval id, 'y' | 'g' | 'n') -> text to show on the message
        self.on_command = on_command              # (text) -> reply, handled at once, never queued behind other work
        self.on_content = on_content              # (draft id, 'a' | 'r') -> text, for the Approve / Reject buttons
        self.asked: dict[int, int] = {}           # approval id -> message id, so the buttons can be removed
        self.path = data_dir / "telegram.json"
        saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.chat_id: int | None = saved.get("chat_id")
        self.code = f"{random.SystemRandom().randint(0, 999999):06d}"
        self.offset = 0
        self.wrong, self.locked_until = 0, 0.0
        self.stopped = threading.Event()

    def _call(self, method: str, **params):
        resp = requests.post(API.format(token=self.token, method=method), json=params, timeout=40)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram error"))
        return data["result"]

    def send(self, chat_id: int, text: str) -> None:
        for start in range(0, len(text) or 1, 4000):         # Telegram's limit is 4096 characters
            self._call("sendMessage", chat_id=chat_id, text=text[start:start + 4000] or "(no reply)")

    def ask(self, approval_id: int, summary: str, always_label: str = "") -> None:
        """Put an approval on the phone as a message with buttons. The answer arrives as a callback."""
        buttons = [[{"text": "✅ Allow", "callback_data": f"ap:{approval_id}:y"}]]
        if always_label:
            buttons[0].append({"text": f"♾ {always_label}", "callback_data": f"ap:{approval_id}:g"})
        buttons[0].append({"text": "❌ Deny", "callback_data": f"ap:{approval_id}:n"})
        sent = self._call("sendMessage", chat_id=self.chat_id, text=f"Jay needs your OK:\n{summary[:3500]}",
                          reply_markup={"inline_keyboard": buttons})
        self.asked[approval_id] = sent["message_id"]

    def ask_content(self, item_id: int, card: str) -> None:
        """A draft post or video, with Approve and Reject buttons."""
        buttons = [[{"text": "✅ Approve", "callback_data": f"ct:{item_id}:a"},
                    {"text": "❌ Reject", "callback_data": f"ct:{item_id}:r"}]]
        self._call("sendMessage", chat_id=self.chat_id, text=card[:3900] + "\n\nReply here to ask for changes.",
                   reply_markup={"inline_keyboard": buttons})

    def settle(self, approval_id: int, outcome: str) -> None:
        """An approval was answered (here or on the laptop): drop its buttons and say what happened."""
        message = self.asked.pop(approval_id, None)
        if message and self.chat_id:
            try:
                self._call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message,
                           reply_markup={"inline_keyboard": []})
                self._call("sendMessage", chat_id=self.chat_id, text=outcome)
            except Exception:
                pass

    def handle_callback(self, callback: dict) -> None:
        chat = callback.get("message", {}).get("chat", {}).get("id")
        if chat != self.chat_id:
            return                                           # only the paired chat can answer
        parts = (callback.get("data") or "").split(":")
        if len(parts) == 3 and parts[0] == "ct" and parts[1].isdigit() and self.on_content is not None:
            outcome = self.on_content(int(parts[1]), parts[2]) or "Done."
            try:
                self._call("editMessageReplyMarkup", chat_id=self.chat_id,
                           message_id=callback["message"]["message_id"], reply_markup={"inline_keyboard": []})
                self._call("answerCallbackQuery", callback_query_id=callback["id"], text=outcome[:150])
                self._call("sendMessage", chat_id=self.chat_id, text=outcome)
            except Exception:
                pass
            return
        if len(parts) != 3 or parts[0] != "ap" or not parts[1].isdigit() or self.on_approval is None:
            return
        outcome = self.on_approval(int(parts[1]), parts[2]) or "Done."
        try:
            self._call("answerCallbackQuery", callback_query_id=callback["id"], text=outcome[:150])
        except Exception:
            pass

    def send_file(self, chat_id: int, path: str) -> None:
        with open(path, "rb") as handle:
            resp = requests.post(API.format(token=self.token, method="sendDocument"),
                                 data={"chat_id": chat_id}, files={"document": handle}, timeout=120)
        if not resp.json().get("ok"):
            raise RuntimeError("Telegram refused the file")

    def send_video(self, chat_id: int, path: str, caption: str = "") -> None:
        """A video the phone can play inline (up to 50 MB); a plain file if Telegram won't take it as video."""
        with open(path, "rb") as handle:
            resp = requests.post(API.format(token=self.token, method="sendVideo"),
                                 data={"chat_id": chat_id, "caption": caption[:900], "supports_streaming": "true"},
                                 files={"video": handle}, timeout=300)
        if not resp.json().get("ok"):
            self.send_file(chat_id, path)

    def send_audio(self, chat_id: int, path: str, voice: bool) -> None:
        """A spoken update: a round voice note (ogg/opus) when `voice`, otherwise an audio file (mp3)."""
        method, field = ("sendVoice", "voice") if voice else ("sendAudio", "audio")
        with open(path, "rb") as handle:
            resp = requests.post(API.format(token=self.token, method=method),
                                 data={"chat_id": chat_id}, files={field: handle}, timeout=120)
        if not resp.json().get("ok"):
            raise RuntimeError("Telegram refused the audio")

    def transcribe_voice(self, file_id: str) -> str:
        """A voice note from the phone, turned into text with the same Groq Whisper Jay uses by voice."""
        import os

        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            return ""
        info = self._call("getFile", file_id=file_id)
        audio = requests.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}", timeout=60).content
        resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                             headers={"Authorization": f"Bearer {key}"},
                             files={"file": ("voice.ogg", audio)},
                             data={"model": os.getenv("JARVIS_STT_MODEL", "whisper-large-v3"), "language": "en"},
                             timeout=60)
        return (resp.json().get("text") or "").strip() if resp.ok else ""

    def handle(self, message: dict) -> None:
        chat = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        voice = message.get("voice") or message.get("audio")
        if chat and chat == self.chat_id and voice and not text:
            try:
                text = self.transcribe_voice(voice["file_id"])
            except Exception:
                text = ""
            if not text:
                self.send(chat, "I couldn't make out that voice note. Try typing it, or send it again.")
                return
            self.send(chat, f"Heard: {text}")
        if not chat or not text:
            return
        if self.chat_id is None:
            if self.locked_until > time.monotonic():
                return                                        # too many wrong codes: say nothing for a while
            if text.startswith("/pair ") and text != f"/pair {self.code}":
                self.wrong += 1
                if self.wrong >= 5:
                    self.wrong, self.locked_until = 0, time.monotonic() + 900
                    self.on_info("Someone sent several wrong pairing codes to the Telegram bot; pairing is paused for 15 minutes.")
            if text == f"/pair {self.code}":
                self.chat_id = chat
                self.path.write_text(json.dumps({"chat_id": chat}), encoding="utf-8")
                self.send(chat, "Paired. This chat can now talk to Jay.")
                self.on_info("Phone linked through Telegram.")
            else:
                self.send(chat, "This is a private assistant. Send the pairing code shown on the laptop: /pair <code>")
            return
        if chat != self.chat_id:
            return                                            # someone else found the bot: ignore them
        if text in ("/start", "/help"):
            self.send(chat, PHONE_HELP)
            return
        if text.startswith("/") and self.on_command is not None:
            reply = self.on_command(text)
            if reply is not None:
                self.send(chat, reply)
                return
        self.on_message(text, chat)

    def run(self) -> None:
        try:
            me = self._call("getMe")
        except Exception as exc:
            self.on_info(f"Telegram link not started: {exc}")
            return
        if self.chat_id is None:
            self.on_info(f"Phone link: open @{me.get('username')} in Telegram and send  /pair {self.code}")
        backoff = 5
        while not self.stopped.is_set():
            try:
                updates = self._call("getUpdates", offset=self.offset, timeout=30,
                                     allowed_updates=["message", "callback_query"])
                backoff = 5
            except Exception:
                self.stopped.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                try:
                    if update.get("callback_query"):
                        self.handle_callback(update["callback_query"])
                    else:
                        self.handle(update.get("message") or {})
                except Exception:
                    pass
            time.sleep(0.2)
