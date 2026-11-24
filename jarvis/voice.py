"""Speech in and out.

Standby:  the microphone is checked on this laptop only, by a tiny local model, for the
          wake word ("Hey Jay"). Nothing you say is sent anywhere until then.
Awake:    what you say goes to Groq's hosted Whisper for accurate transcription
          (or a local model when there is no Groq key).
Out:      Microsoft neural voice via edge-tts, or the Windows voice offline. Speech can be
          cut off mid-sentence ("Stop Jay").

Everything heavy is imported lazily so text mode works without the voice packages.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import time
import unicodedata
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .config import Config

SAMPLE_RATE = 16_000
VOICE_RATE = 24_000           # the speech voice's own sample rate
BLOCK = 1_600                 # 0.1 s of audio per block
TELUGU = re.compile(r"[ఀ-౿]")
STOP_WORDS = {"stop", "cancel", "quiet", "silence"}


def speakable(text: str) -> str:
    """Strip markdown and URLs so the speech engine doesn't read symbols aloud."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    return re.sub(r"[*_#`>|]", "", text).strip()


@dataclass
class Heard:
    text: str
    confidence: float      # average log-probability; closer to 0 is more confident
    no_speech: float       # chance the clip was not speech at all


def pick_transcript(english: Heard, telugu: Heard, margin: float = 0.08) -> str:
    """Choose between the same clip heard as English and as Telugu (only used when Telugu is enabled).

    English wins ties: forcing Telugu on English speech still scores fairly well,
    while forcing English on Telugu speech scores much worse.
    """
    if min(english.no_speech, telugu.no_speech) > 0.6:
        return ""                                   # background noise, not speech
    return telugu.text if telugu.confidence > english.confidence + margin else english.text


def is_telugu(text: str) -> bool:
    return bool(TELUGU.search(text))


def _bare(word: str) -> str:
    """Lower-case with accents, vowel signs and punctuation removed, for comparing words."""
    return "".join(c for c in unicodedata.normalize("NFD", word.lower())
                   if unicodedata.category(c)[0] not in "MP")


def _words(text: str) -> list[str]:
    return [w for w in (_bare(m.group()) for m in re.finditer(r"[^\s,.!?:;]+", text)) if w]


GREETINGS = {"hey", "hi", "ok", "okay", "hello", "yo", "oi", "so", "and", "um", "uh"}


def extract_command(text: str, names: list[str], strict: bool = False) -> str | None:
    """If the wake word is in text, return what follows it ('' if nothing does); else None.

    strict: the name must open the sentence ("Jay, ..." or "Hey Jay ..."), so a name that
    merely comes up in conversation or on a video ("Jai Hind", "the Jay Shetty podcast")
    doesn't count.
    """
    wake = {_bare(n) for n in names}
    words = list(re.finditer(r"[^\s,.!?:;]+", text))
    for i, word in enumerate(words):
        if _bare(word.group()) in wake:
            if strict and not (i == 0 or (i == 1 and _bare(words[0].group()) in GREETINGS)):
                return None
            return re.sub(r"^[\W_]+", "", text[word.end():]).strip()   # drop punctuation and stray marks
        if strict and i >= 1:
            return None
    return None


def is_stop(text: str, names: list[str]) -> bool:
    """'Stop Jay', 'Jay stop', or a bare 'stop' / 'cancel'. Longer sentences that merely contain
    the word (e.g. 'the bus stop') don't count unless the assistant is named. The small
    listening model often mishears a short command ("stob", "stuff"), so in a short
    utterance a word that sounds close to a stop word counts too."""
    import difflib

    words = _words(text)
    if not words:
        return False
    named = bool({_bare(n) for n in names} & set(words))
    if STOP_WORDS & set(words):
        return named or len(words) <= 2
    if len(words) <= 3:
        close = any(difflib.get_close_matches(w, STOP_WORDS, n=1, cutoff=0.75) for w in words if len(w) >= 3)
        return close and (named or len(words) <= 2)
    return False


def normalize(audio, target: float = 0.5, max_gain: float = 30.0):
    """Bring a quietly spoken clip up to a normal level (never louder than `max_gain` times),
    so speech recognition and voice checking work without having to raise your voice."""
    import numpy as np

    audio = np.asarray(audio, dtype="float32")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 1e-4 or peak >= target:
        return audio
    return audio * min(target / peak, max_gain)


def to_wav(audio) -> bytes:
    """float32 samples in [-1, 1] -> 16-bit mono WAV bytes."""
    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def play_mp3(path: str, stop: threading.Event | None = None) -> None:
    """Play an audio file with Windows' built-in player until it ends or `stop` is set."""
    import ctypes

    mci = ctypes.windll.winmm.mciSendStringW
    alias = f"jay{threading.get_ident()}"
    mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    try:
        mci(f"play {alias}", None, 0, None)
        state = ctypes.create_unicode_buffer(32)
        while True:
            mci(f"status {alias} mode", state, 32, None)
            if state.value != "playing" or (stop is not None and stop.is_set()):
                break
            time.sleep(0.05)
        mci(f"stop {alias}", None, 0, None)
    finally:
        mci(f"close {alias}", None, 0, None)


def chime(kind: str = "wake") -> None:
    """A short tone so the user knows the wake word (or stop) was heard."""
    def beep() -> None:
        try:
            import winsound

            for freq, ms in ((880, 70), (1320, 90)) if kind == "wake" else ((660, 70), (440, 110)):
                winsound.Beep(freq, ms)
        except Exception:
            pass

    threading.Thread(target=beep, daemon=True).start()


class _Reopen(Exception):
    """Internal: close the microphone and open it again (on another device)."""


class UtteranceDetector:
    """Cuts a stream of audio blocks into utterances by loudness.

    Speech starts after `start_blocks` loud blocks in a row and ends after
    `end_blocks` quiet ones (or at `max_blocks`).
    """

    def __init__(self, threshold: float, start_blocks: int = 2, end_blocks: int = 8,
                 max_blocks: int = 300, pre_roll: int = 3, adaptive_floor: float | None = None,
                 overlap_blocks: int = 0):
        # adaptive_floor: follow the room's background noise (never below this level), so a quiet
        # voice is still heard in a quiet room and a fan or street noise doesn't trigger it.
        self.threshold, self.start_blocks = threshold, start_blocks
        self.adaptive_floor, self._noise = adaptive_floor, None
        self.overlap_blocks = overlap_blocks     # when a long stretch is cut, the next piece starts with the tail of this one
        self.end_blocks, self.max_blocks = end_blocks, max_blocks
        # Must hold at least the blocks that triggered the start, or the first word is clipped.
        self._pre: deque = deque(maxlen=max(pre_roll, start_blocks))
        self.reset()

    def reset(self) -> None:
        self.active = False
        self._loud_run = self._quiet_run = 0
        self._chunks: list = []
        self._pre.clear()

    def feed(self, block):
        """Feed one block; returns the finished utterance (numpy array) or None."""
        import numpy as np

        level = float(np.sqrt(np.mean(np.square(block))))
        loud = level > self.threshold
        if self.adaptive_floor is not None and not self.active and not loud:
            self._noise = level if self._noise is None else 0.97 * self._noise + 0.03 * level
            self.threshold = max(self.adaptive_floor, 2.5 * self._noise)
        if not self.active:
            self._pre.append(block)
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= self.start_blocks:
                self.active, self._chunks, self._quiet_run = True, list(self._pre), 0
            return None
        self._chunks.append(block)
        self._quiet_run = 0 if loud else self._quiet_run + 1
        if self._quiet_run >= self.end_blocks or len(self._chunks) >= self.max_blocks:
            audio = np.concatenate(self._chunks)
            if self.overlap_blocks and self._quiet_run < self.end_blocks:
                self._chunks = self._chunks[-self.overlap_blocks:]      # a word on the cut still arrives whole
                self._quiet_run = 0
                return audio
            if self.adaptive_floor is not None and self._quiet_run < self.end_blocks:
                # Never went quiet: that was constant noise (a fan, a video), not speech. Learn it.
                self._noise = float(np.sqrt(np.mean(np.square(audio))))
                self.threshold = max(self.adaptive_floor, 2.5 * self._noise)
            self.reset()
            return audio
        return None


def route_utterance(mode: str, heard_locally: Callable[[], str], heard_accurately: Callable[[], str],
                    names: list[str], known_voice: Callable[[], bool] = lambda: True) -> tuple[str, str] | None:
    """Decide what one finished utterance means. Returns (event, text) or None to ignore it.

    mode "standby": only the wake word matters, and only the local model listens.
    mode "busy":    the assistant is thinking or talking; only "stop" matters (local model).
    mode "active":  a conversation is under way; what a known voice says is for the assistant.
    known_voice():  whether this sounds like someone Jay knows; anyone may say stop, but only
                    a known voice can wake Jay or keep a conversation going.
    """
    if mode in ("standby", "busy"):
        text = heard_locally()
        if is_stop(text, names):
            return ("stop", text)
        if mode == "busy":
            return None
        rest = extract_command(text, names, strict=True)
        if rest is None or not known_voice():
            return None                              # not for us; it never left this laptop
        if not rest:
            return ("wake", "")
        accurate = heard_accurately()                # the local model is rough; re-hear the request properly
        better = extract_command(accurate, names)
        return ("wake", better if better else rest)
    text = heard_accurately()
    if not text:
        return None
    if is_stop(text, names):
        return ("stop", text)
    if not known_voice():
        return None                                  # someone else talking nearby, or a video
    rest = extract_command(text, names, strict=True)
    if rest is not None:                             # "Hey Jay, ..." said again mid-conversation
        return ("command", rest) if rest else None
    return ("command", text)


class Voice:
    def __init__(self, cfg: Config, measure_room: bool = True):
        import numpy  # noqa: F401 - fail early if voice packages are missing
        import sounddevice  # noqa: F401

        self.cfg = cfg
        self.on_problem: Callable[[str], None] = print     # the app points this at its own display
        self.level = 0.0          # loudness of Jay's voice right now, 0-1 (drives the display)
        self.known_voice: Callable = lambda audio: True    # the app replaces this with speaker checking
        self.mic_level = 0.0      # loudness at the microphone right now, 0-1
        self.last_peak = 1.0      # loudest sample seen during the last push-to-talk recording
        self._voice_until = 0.0   # until when Jay's own voice may still be in the room (echo)
        self._scanned = 0.0
        self.silent_for = 0.0     # seconds the microphone has delivered pure silence (muted or taken by another app)
        self.heartbeat = 0.0      # when the microphone loop last read audio (to spot a dead listener)
        self._speak_lock = threading.Lock()
        self._stop_speaking = threading.Event()
        self._local: dict = {}
        self._groq = None
        if cfg.stt in ("auto", "groq") and os.getenv("GROQ_API_KEY"):
            import openai

            import httpx

            # A fresh connection per request: a pooled one that went stale during a quiet minute would hang
            # the listening thread until it timed out, which is what made some requests arrive 20+ seconds late.
            http = httpx.Client(limits=httpx.Limits(max_keepalive_connections=0),
                                timeout=httpx.Timeout(8.0, connect=4.0))
            self._groq = openai.OpenAI(api_key=os.environ["GROQ_API_KEY"],
                                       base_url="https://api.groq.com/openai/v1", http_client=http, max_retries=0)
        elif cfg.stt == "groq":
            raise RuntimeError("JARVIS_STT=groq needs GROQ_API_KEY in .env")
        self.threshold = cfg.mic_threshold or (self._measure_room() if measure_room else 0.004)

    # ---- hearing -------------------------------------------------------------
    device = None             # None = the Windows default microphone; otherwise a device number found to be live

    def _stream(self):
        import sounddevice as sd

        return sd.InputStream(device=self.device, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                              blocksize=BLOCK)

    def find_live_input(self, skip: int | None = None) -> int | None:
        """When the default microphone delivers only silence (muted, or driver trouble), look for any
        other input that hears something: a headset, a webcam's microphone. Returns its number, or None."""
        import numpy as np
        import sounddevice as sd

        best, loudest = None, 5e-4        # real microphones always carry some noise; a flat trace is not one
        for index, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] < 1 or sd.query_hostapis(info["hostapi"])["name"] != "MME" or index == skip:
                continue
            name = info["name"].lower()
            if "mapper" in name or "stereo mix" in name or "tranScreen".lower() in name:
                continue                                  # a mixer or a virtual device, not a real microphone
            try:
                with sd.InputStream(device=index, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                    blocksize=BLOCK) as stream:
                    level = max(float(abs(stream.read(BLOCK)[0]).max()) for _ in range(4))
            except Exception:
                continue
            if level > loudest:
                best, loudest = index, level
        return best

    def _measure_room(self) -> float:
        """Listen for half a second and set the speech threshold above the background noise."""
        import numpy as np

        with self._stream() as stream:
            levels = [float(np.sqrt(np.mean(np.square(stream.read(BLOCK)[0])))) for _ in range(5)]
        return max(0.004, 2.5 * float(np.median(levels)))

    def _model(self, name: str):
        if name not in self._local:
            from faster_whisper import WhisperModel

            self._local[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return self._local[name]

    def hear_locally(self, audio) -> str:
        """Quick, rough, private transcription on this laptop: enough to spot 'Hey Jay' and 'Stop'."""
        # No hint about the name: with one, the model "hears" Jay in Jai, Jay Shetty and the like.
        segments, _ = self._model(self.cfg.wake_model).transcribe(audio, language="en", beam_size=1, vad_filter=True)
        # Drop segments the model itself thinks are not speech: on plain noise it can
        # otherwise "hear" the hint phrase and wake by mistake.
        return " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()

    def warm_up(self) -> None:
        """Load the wake-word model now (the first use downloads it) instead of at the first word."""
        self._model(self.cfg.wake_model)

    def _stt_prompt(self) -> str:
        """A hint for the recogniser: the wake word and the names this user actually says."""
        vocab = getattr(self.cfg, "stt_vocab", "")
        return f"Hey {self.cfg.name}." + (f" {vocab}." if vocab else "")

    def transcribe(self, audio) -> str:
        """Accurate speech -> text for requests."""
        languages = self.cfg.languages
        if self._groq is not None:
            try:
                wav = to_wav(audio)

                def hear(language: str, prompt: str) -> Heard:
                    r = self._groq.audio.transcriptions.create(
                        model=self.cfg.stt_model, file=("speech.wav", wav, "audio/wav"),
                        response_format="verbose_json", language=language, prompt=prompt)
                    segs = [s if isinstance(s, dict) else s.__dict__ for s in (r.segments or [])]
                    if not segs:
                        return Heard("", -9.0, 1.0)
                    return Heard(r.text.strip(),
                                 sum(s["avg_logprob"] for s in segs) / len(segs),
                                 sum(s["no_speech_prob"] for s in segs) / len(segs))

                if "te" not in languages:
                    english = hear("en", self._stt_prompt())
                    return "" if english.no_speech > 0.6 else english.text
                # Two languages: Whisper's own guess is unreliable on short clips, so hear it
                # both ways and keep the more confident one.
                with ThreadPoolExecutor(2) as pool:
                    english = pool.submit(hear, "en", self._stt_prompt())
                    telugu = pool.submit(hear, "te", "జే.")
                    return pick_transcript(english.result(), telugu.result())
            except Exception as exc:
                self.on_problem(f"Online speech recognition was slow or failed ({type(exc).__name__}); using the local model.")
        name = self.cfg.whisper_model if self.cfg.whisper_model in self._local else self.cfg.wake_model
        segments, _ = self._model(name).transcribe(
            audio, vad_filter=True, language=None if "te" in languages else "en")
        return " ".join(s.text.strip() for s in segments).strip()

    def record(self, wait_seconds: float = 8):
        """Record one utterance (it stops by itself when you go quiet). Returns the audio, or None."""
        detector = UtteranceDetector(self.threshold, end_blocks=8, adaptive_floor=max(0.005, self.threshold / 3))
        deadline = time.monotonic() + wait_seconds
        self.last_peak = 0.0
        with self._stream() as stream:
            while detector.active or time.monotonic() < deadline:
                block = stream.read(BLOCK)[0].flatten()
                self.last_peak = max(self.last_peak, float(abs(block).max()))
                self.mic_level = min(1.0, float((block * block).mean() ** 0.5) * 9)
                audio = detector.feed(block)
                if audio is not None:
                    return audio
        return None

    def listen(self, wait_seconds: float = 8) -> str:
        """Push-to-talk: record one utterance and transcribe it. The audio is kept in last_audio."""
        self.last_audio = self.record(wait_seconds)
        return self.transcribe(self.last_audio) if self.last_audio is not None else ""

    def run_handsfree(self, on_event: Callable[..., None], mode: Callable[[], str],
                      stop: threading.Event, on_enrol_audio: Callable | None = None) -> None:
        """Listen continuously and report ('wake' | 'command' | 'stop', text, audio) events.

        `mode()` says what the assistant is doing right now: "standby", "active", "busy",
        or "enrol" (recording voice samples, which go to on_enrol_audio instead).

        The microphone is read on this thread and nothing else happens on it: finished
        utterances go to worker threads for recognition, so Jay keeps hearing while it works.
        While Jay is busy (thinking or talking) speech is cut into short pieces, so "Stop Jay"
        is noticed within a second or two instead of after Jay has finished talking. If the
        microphone fails (unplugged, taken by another app) it is reopened by itself.
        """
        import queue as queue_

        floor = max(0.003, self.threshold / 3)
        calm = UtteranceDetector(self.threshold, end_blocks=8, adaptive_floor=floor)
        busy = UtteranceDetector(self.threshold, start_blocks=2, end_blocks=3, max_blocks=22,
                                 overlap_blocks=10)
        jobs: queue_.Queue = queue_.Queue()          # wake words, requests, voice samples
        stops: queue_.Queue = queue_.Queue()         # "stop" checks run on their own thread, never held up

        def handle(audio, heard_as: str) -> None:
            audio = normalize(audio)          # a normal speaking voice is often quiet at a laptop microphone
            if heard_as == "enrol":
                if on_enrol_audio is not None:
                    on_enrol_audio(audio)
                return
            try:
                event = route_utterance(heard_as, lambda: self.hear_locally(audio),
                                        lambda: self.transcribe(audio), self.cfg.wake_names,
                                        known_voice=lambda: self.known_voice(audio))
            except Exception as exc:
                self.on_problem(f"Listening problem: {type(exc).__name__}: {exc}")
                return
            if event:
                on_event(*event, audio)

        def worker(source: queue_.Queue) -> None:
            while not stop.is_set():
                try:
                    audio, heard_as = source.get(timeout=0.5)
                except queue_.Empty:
                    continue
                handle(audio, heard_as)

        for source in (jobs, stops):
            threading.Thread(target=worker, args=(source,), daemon=True).start()

        was_busy = False
        problem_reported = False
        while not stop.is_set():
            try:
                with self._stream() as stream:
                    problem_reported = False
                    while not stop.is_set():
                        block = stream.read(BLOCK)[0].flatten()
                        self.heartbeat = time.monotonic()
                        self.mic_level = min(1.0, float((block * block).mean() ** 0.5) * 9)
                        # A working microphone always has a little noise; a flat zero means it is muted
                        # (a hardware key, a privacy switch) or another app has taken it.
                        self.silent_for = self.silent_for + BLOCK / SAMPLE_RATE if float(abs(block).max()) < 1e-4 else 0.0
                        if self.silent_for > 20 and time.monotonic() - self._scanned > 60:
                            self._scanned = time.monotonic()
                            other = self.find_live_input(skip=self.device)
                            if other is not None:
                                self.device = other
                                self.on_problem("The microphone was silent; switched to another input that can hear.")
                                raise _Reopen()
                            if self.device is not None:          # the one we switched to went quiet too: back to Windows' default
                                self.device = None
                                self.on_problem("The other input went silent as well; back to the default microphone.")
                                raise _Reopen()
                        now = mode()
                        # While Jay's own voice is playing (even a short "On it."), only "stop" is listened for,
                        # so Jay never mistakes its echo for a command.
                        is_busy = now == "busy" or time.monotonic() < self._voice_until
                        if is_busy != was_busy:                 # switching between the two ears
                            calm.reset()
                            busy.reset()
                            was_busy = is_busy
                        audio = (busy if is_busy else calm).feed(block)
                        if audio is None:
                            continue
                        if now == "enrol":
                            jobs.put((audio, "enrol"))
                        elif is_busy:
                            stops.put((audio, "busy"))
                        else:
                            jobs.put((audio, now))
            except _Reopen:
                self.silent_for = 0.0
                calm.reset()
                busy.reset()
                continue
            except Exception as exc:
                if not problem_reported:
                    self.on_problem(f"Microphone problem ({type(exc).__name__}: {exc}); retrying.")
                    problem_reported = True
                calm.reset()
                busy.reset()
                if stop.wait(2.0):
                    return

    # ---- speaking ------------------------------------------------------------
    def stop_speaking(self) -> None:
        self._stop_speaking.set()

    def _communicate(self, text: str):
        import edge_tts

        voice = self.cfg.voice_te if is_telugu(text) else self.cfg.voice_en
        return edge_tts.Communicate(text, voice, rate=self.cfg.voice_rate, pitch=self.cfg.voice_pitch)

    def _speak_streaming(self, text: str) -> None:
        """Play the voice while it is still being generated: sound starts after the first few
        hundred milliseconds of audio instead of after the whole reply is ready."""
        import asyncio
        import queue as queue_

        import av
        import numpy as np
        import sounddevice as sd

        chunks: queue_.Queue = queue_.Queue()
        failure: list = []

        def produce() -> None:
            codec = av.CodecContext.create("mp3", "r")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=VOICE_RATE)

            def decoded(data):
                for packet in codec.parse(data):
                    for frame in codec.decode(packet):
                        for out in resampler.resample(frame):
                            chunks.put(out.to_ndarray().reshape(-1).astype("float32"))

            async def run() -> None:
                async for message in self._communicate(text).stream():
                    if self._stop_speaking.is_set():
                        return
                    if message["type"] == "audio":
                        decoded(message["data"])
                decoded(None)

            try:
                asyncio.run(run())
            except Exception as exc:                 # reported to the player, which falls back
                failure.append(exc)
            finally:
                chunks.put(None)

        threading.Thread(target=produce, daemon=True).start()
        played = False
        pending = np.zeros(0, dtype="float32")
        with sd.OutputStream(samplerate=VOICE_RATE, channels=1, dtype="float32", latency="low") as out:
            finished = False
            while not finished and not self._stop_speaking.is_set():
                try:
                    chunk = chunks.get(timeout=8)
                except queue_.Empty:
                    break
                if chunk is None:
                    finished = True
                else:
                    pending = np.concatenate([pending, chunk])
                # Write in small slices so a stop is noticed within ~40 ms and the level stays live.
                while len(pending) >= 1024 or (finished and len(pending)):
                    if self._stop_speaking.is_set():
                        break
                    piece, pending = pending[:1024], pending[1024:]
                    self.level = min(1.0, float(np.sqrt(np.mean(np.square(piece)))) * 4.5)
                    self._voice_until = time.monotonic() + 0.8
                    out.write(piece.reshape(-1, 1))
                    played = True
        self.level = 0.0
        if failure and not played:
            raise failure[0]

    # Short phrases kept ready in memory, so they play the instant they're needed.
    def prepare(self, phrases: list[str]) -> None:
        def build() -> None:
            import asyncio

            import av
            import numpy as np

            async def pcm(text: str):
                data = b"".join([m["data"] async for m in self._communicate(text).stream() if m["type"] == "audio"])
                codec = av.CodecContext.create("mp3", "r")
                resampler = av.AudioResampler(format="flt", layout="mono", rate=VOICE_RATE)
                parts = [o.to_ndarray().reshape(-1) for p in codec.parse(data) + codec.parse(None)
                         for f in codec.decode(p) for o in resampler.resample(f)]
                return np.concatenate(parts).astype("float32") if parts else None

            for phrase in phrases:
                try:
                    audio = asyncio.run(pcm(phrase))
                    if audio is not None:
                        self._ready[phrase] = audio
                except Exception:
                    pass

        self._ready: dict = getattr(self, "_ready", {})
        threading.Thread(target=build, daemon=True).start()

    def say_ready(self, phrase: str) -> bool:
        """Play a prepared phrase at once. Returns False if it isn't prepared yet."""
        audio = getattr(self, "_ready", {}).get(phrase)
        if audio is None:
            return False
        import numpy as np
        import sounddevice as sd

        with self._speak_lock:
            self._stop_speaking.clear()
            with sd.OutputStream(samplerate=VOICE_RATE, channels=1, dtype="float32", latency="low") as out:
                for i in range(0, len(audio), 1024):
                    if self._stop_speaking.is_set():
                        break
                    piece = audio[i:i + 1024]
                    self.level = min(1.0, float(np.sqrt(np.mean(np.square(piece)))) * 4.5)
                    self._voice_until = time.monotonic() + 0.8
                    out.write(piece.reshape(-1, 1))
            self.level = 0.0
        return True

    def _speak_edge(self, text: str) -> None:
        import asyncio

        path = os.path.join(tempfile.gettempdir(), f"jay-speech-{threading.get_ident()}.mp3")
        asyncio.run(self._communicate(text).save(path))
        try:
            if not self._stop_speaking.is_set():
                play_mp3(path, self._stop_speaking)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _speak_sapi(self, text: str) -> None:
        import pyttsx3

        # A fresh engine per utterance avoids pyttsx3's second-call hang on Windows.
        engine = pyttsx3.init()
        engine.setProperty("rate", self.cfg.tts_rate)
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def speak(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        with self._speak_lock:
            self._stop_speaking.clear()
            if self.cfg.tts in ("auto", "edge"):
                try:
                    return self._speak_streaming(text)
                except Exception:
                    pass
                try:
                    return self._speak_edge(text)        # older, slower path
                except Exception as exc:
                    self.on_problem(f"Online voice failed ({type(exc).__name__}); using the Windows voice.")
                    if self.cfg.tts == "edge":
                        return
            self._speak_sapi(text)   # offline fallback
