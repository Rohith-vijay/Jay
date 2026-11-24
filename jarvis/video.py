"""Faceless video maker: a storyboard in, a finished .mp4 and thumbnail out.

    scenes (narration + on-screen text + picture)  ->  voiceover (word-timed)  ->  slow-zoom pictures with
    burned-in captions that follow the voice, an optional music bed  ->  mp4 (H.264 + AAC) + thumbnail

Everything is local except the voice (Microsoft's neural voices through edge-tts, the engine Jay
already speaks with) and, if you set PEXELS_API_KEY in .env, searching Pexels for free stock photos
for scenes that name a picture to find. A scene's picture can be:
  * a file on this PC (an image you made or own),
  * a short search phrase, looked up on Pexels when a key is set,
  * nothing, which gives a dark gradient with the on-screen text. That always works and is plain,
    so for lore and history channels a Pexels key or your own images make a big difference.

Only pictures you own or that are licensed for use go in. Jay does not scrape images from the web.
A background track is mixed in at low volume if you put a file at data/video_assets/music.mp3.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .security import safe_get
from .tools.registry import Registry

VERTICAL, HORIZONTAL = (1080, 1920), (1920, 1080)
SAMPLE_RATE = 44100
PAD_SECONDS = 0.35
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
HEADLINE_FONTS = ("impact.ttf", "arialbd.ttf", "segoeuib.ttf")
CAPTION_FONTS = ("segoeuib.ttf", "arialbd.ttf", "impact.ttf")
PALETTES = [((18, 24, 48), (70, 40, 90)), ((10, 30, 40), (20, 80, 90)), ((40, 20, 20), (110, 50, 30)),
            ((20, 20, 30), (60, 60, 90)), ((15, 35, 25), (60, 90, 50))]


@dataclass
class Scene:
    narration: str
    on_screen: str = ""
    image: str = ""                       # a file path, or a search phrase, or empty


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class VideoResult:
    path: str
    thumbnail: str
    seconds: float
    scenes: int
    notes: list = field(default_factory=list)


def font(names, size):
    from PIL import ImageFont

    for name in names:
        try:
            return ImageFont.truetype(str(FONT_DIR / name), size)
        except OSError:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ planning
def auto_scenes(script: str, title: str = "", target_chars: int = 190) -> list[Scene]:
    """Split a written script into scenes of a few sentences each, with the title on the first."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", script.strip()) if s.strip()]
    scenes, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target_chars:
            scenes.append(Scene(current))
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        scenes.append(Scene(current))
    if scenes and title:
        scenes[0].on_screen = title
    return scenes


def caption_phrases(words: list[Word], max_words: int = 4, max_chars: int = 26) -> list[list[Word]]:
    phrases, current = [], []
    for w in words:
        current.append(w)
        if len(current) >= max_words or len(" ".join(x.text for x in current)) >= max_chars or re.search(r"[.,!?;:]$", w.text):
            phrases.append(current)
            current = []
    if current:
        phrases.append(current)
    return phrases


def even_words(text: str, seconds: float) -> list[Word]:
    """Fallback timing when the voice service gives none: spread the words across the audio."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [max(len(t), 2) for t in tokens]
    total, at, words = sum(weights), 0.0, []
    for token, weight in zip(tokens, weights):
        step = seconds * weight / total
        words.append(Word(token, at, at + step))
        at += step
    return words


# ------------------------------------------------------------------ voice
async def _speak(text: str, voice: str, rate: str, mp3: str) -> list[Word]:
    import edge_tts

    words: list[Word] = []
    with open(mp3, "wb") as out:
        async for message in edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary").stream():
            if message["type"] == "audio":
                out.write(message["data"])
            elif message["type"] == "WordBoundary":
                start = message["offset"] / 1e7
                words.append(Word(message["text"], start, start + message["duration"] / 1e7))
    return words


def decode_audio(path: str):
    """Any audio file -> mono float32 samples at SAMPLE_RATE."""
    import av
    import numpy as np

    chunks = []
    with av.open(path) as container:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for piece in resampler.resample(frame):
                chunks.append(piece.to_ndarray().reshape(-1))
        for piece in resampler.resample(None):
            chunks.append(piece.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype("float32") if chunks else np.zeros(0, dtype="float32")


# ------------------------------------------------------------------ pictures
def pexels_photo(query: str, vertical: bool, cache: Path) -> str | None:
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key or not query.strip():
        return None
    target = cache / (re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:50] + ("-v" if vertical else "-h") + ".jpg")
    if target.exists():
        return str(target)
    try:
        found = safe_get("https://api.pexels.com/v1/search", headers={"Authorization": key}, timeout=20,
                         params={"query": query, "per_page": 5, "orientation": "portrait" if vertical else "landscape"}).json()
        photos = found.get("photos") or []
        if not photos:
            return None
        image = safe_get(photos[0]["src"]["large2x"], timeout=30)
        image.raise_for_status()
        cache.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image.content)
        return str(target)
    except Exception:
        return None


def gradient(size, seed: str):
    import numpy as np
    from PIL import Image

    w, h = size
    h256 = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    top, bottom = PALETTES[h256 % len(PALETTES)]
    t = np.linspace(0, 1, h, dtype="float32")[:, None, None]
    img = (np.array(top, "float32") * (1 - t) + np.array(bottom, "float32") * t).repeat(w, axis=1)
    yy, xx = np.mgrid[0:h, 0:w].astype("float32")
    vignette = 1 - 0.55 * (((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 2
    return Image.fromarray(np.clip(img * vignette[..., None], 0, 255).astype("uint8"))


def prepare_base(path: str | None, size, seed: str):
    """A picture covering the frame with room to zoom, darkened a little so text reads."""
    import numpy as np
    from PIL import Image

    w, h = size
    zoomed = (int(w * 1.18), int(h * 1.18))
    if path:
        try:
            image = Image.open(path).convert("RGB")
            scale = max(zoomed[0] / image.width, zoomed[1] / image.height)
            image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
            left, top = (image.width - zoomed[0]) // 2, (image.height - zoomed[1]) // 2
            image = image.crop((left, top, left + zoomed[0], top + zoomed[1]))
            return Image.fromarray((np.asarray(image, dtype="float32") * 0.62).astype("uint8"))
        except Exception:
            pass
    return gradient(zoomed, seed)


# ------------------------------------------------------------------ drawing
def wrap_words(draw, words: list[str], fnt, max_width: int) -> list[list[str]]:
    lines, line = [], []
    for word in words:
        trial = " ".join([*line, word])
        if line and draw.textlength(trial, font=fnt) > max_width:
            lines.append(line)
            line = [word]
        else:
            line.append(word)
    if line:
        lines.append(line)
    return lines


def caption_layer(phrase: list[str], current: int, size, vertical: bool):
    """RGBA image of one caption phrase with the spoken word highlighted, and where to paste it."""
    from PIL import Image, ImageDraw

    w, h = size
    fnt = font(CAPTION_FONTS, int(w / (13 if vertical else 24)))
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = wrap_words(probe, phrase, fnt, int(w * 0.86))
    line_h = int(fnt.size * 1.25)
    layer = Image.new("RGBA", (w, line_h * len(lines) + 20), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    index = 0
    for row, line in enumerate(lines):
        total = draw.textlength(" ".join(line), font=fnt)
        x = (w - total) / 2
        for word in line:
            colour = (255, 214, 64, 255) if index == current else (255, 255, 255, 255)
            draw.text((x, row * line_h + 8), word, font=fnt, fill=colour, stroke_width=max(3, fnt.size // 12), stroke_fill=(0, 0, 0, 255))
            x += draw.textlength(word + " ", font=fnt)
            index += 1
    top = int(h * (0.66 if vertical else 0.80)) - layer.height // 2
    return layer, (0, top)


def headline_layer(text: str, size, vertical: bool):
    from PIL import Image, ImageDraw

    w, h = size
    fnt = font(HEADLINE_FONTS, int(w / (9 if vertical else 18)))
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = wrap_words(probe, text.upper().split(), fnt, int(w * 0.84))
    line_h = int(fnt.size * 1.15)
    layer = Image.new("RGBA", (w, line_h * len(lines) + 20), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for row, line in enumerate(lines):
        total = draw.textlength(" ".join(line), font=fnt)
        draw.text(((w - total) / 2, row * line_h + 8), " ".join(line), font=fnt, fill=(255, 255, 255, 255),
                  stroke_width=max(4, fnt.size // 14), stroke_fill=(0, 0, 0, 255))
    return layer, (0, int(h * (0.22 if vertical else 0.32)) - layer.height // 2)


def faded(layer, alpha: float):
    """The same layer at a fraction of its opacity (cached in 10 steps)."""
    if alpha >= 0.99:
        return layer
    band = layer.getchannel("A").point(lambda v: int(v * round(alpha * 10) / 10))
    out = layer.copy()
    out.putalpha(band)
    return out


def make_thumbnail(title: str, base, path: str) -> None:
    from PIL import Image, ImageDraw

    w, h = 1280, 720
    canvas = base.copy()
    scale = max(w / canvas.width, h / canvas.height)
    canvas = canvas.resize((int(canvas.width * scale) + 1, int(canvas.height * scale) + 1))
    canvas = canvas.crop(((canvas.width - w) // 2, (canvas.height - h) // 2, (canvas.width - w) // 2 + w, (canvas.height - h) // 2 + h))
    fnt = font(HEADLINE_FONTS, 120)
    draw = ImageDraw.Draw(canvas)
    lines = wrap_words(draw, title.upper().split(), fnt, int(w * 0.88))[:3]
    y = (h - len(lines) * 135) // 2
    for line in lines:
        text = " ".join(line)
        draw.text(((w - draw.textlength(text, font=fnt)) / 2, y), text, font=fnt, fill=(255, 214, 64),
                  stroke_width=9, stroke_fill=(0, 0, 0))
        y += 135
    canvas.convert("RGB").save(path, quality=90)


# ------------------------------------------------------------------ the render
class VideoMaker:
    def __init__(self, data_dir: Path, voice: str | None = None, rate: str = "+0%", progress=lambda text: None):
        self.dir = data_dir / "videos"
        self.assets = data_dir / "video_assets"
        self.voice = voice or os.getenv("JARVIS_VIDEO_VOICE") or os.getenv("JARVIS_VOICE_EN", "en-GB-RyanNeural")
        self.rate, self.progress = rate, progress
        self.stop = threading.Event()

    def make(self, title: str, scenes: list[Scene], vertical: bool = True, name: str | None = None) -> VideoResult:
        import av
        import numpy as np
        from PIL import Image

        if not scenes:
            raise ValueError("There is nothing to narrate.")
        self.dir.mkdir(parents=True, exist_ok=True)
        slug = (name or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "video") + datetime.now().strftime("-%m%d-%H%M")
        size, fps = (VERTICAL, 30) if vertical else (HORIZONTAL, 24)
        notes: list[str] = []
        work = Path(tempfile.mkdtemp(prefix="jay-video-"))

        # 1. voice for every scene, with word times
        voiced = []
        for i, scene in enumerate(scenes, 1):
            self.progress(f"voice {i}/{len(scenes)}")
            mp3 = str(work / f"s{i}.mp3")
            try:
                words = asyncio.run(_speak(scene.narration, self.voice, self.rate, mp3))
            except Exception as exc:
                raise RuntimeError(f"The voice service failed ({type(exc).__name__}: {exc}). Are you online?") from exc
            samples = decode_audio(mp3)
            seconds = len(samples) / SAMPLE_RATE
            if not words:
                words = even_words(scene.narration, seconds)
            voiced.append((scene, samples, words, seconds))

        music = self._music(sum(v[3] + PAD_SECONDS for v in voiced))

        # 2. pictures
        bases = []
        for i, (scene, _, _, _) in enumerate(voiced, 1):
            self.progress(f"pictures {i}/{len(scenes)}")
            path = scene.image if scene.image and Path(scene.image).is_file() else pexels_photo(scene.image, vertical, self.assets / "cache")
            if scene.image and not path:
                notes.append(f"scene {i}: no picture found for '{scene.image[:40]}'; used a plain background")
            bases.append(prepare_base(path, size, f"{title}-{i}"))

        # 3. frames + audio, scene by scene
        out_path = str(self.dir / f"{slug}.mp4")
        total_seconds = 0.0
        container = av.open(out_path, "w")
        vs = container.add_stream("libx264", rate=fps)
        vs.width, vs.height, vs.pix_fmt = size[0], size[1], "yuv420p"
        vs.options = {"crf": "23", "preset": "veryfast"}
        aus = container.add_stream("aac", rate=SAMPLE_RATE)
        aus.layout = "stereo"
        frame_no = audio_pos = 0
        try:
            for i, ((scene, samples, words, seconds), base) in enumerate(zip(voiced, bases), 1):
                if self.stop.is_set():
                    raise RuntimeError("Stopped.")
                self.progress(f"rendering scene {i}/{len(voiced)}")
                duration = seconds + PAD_SECONDS
                frames = max(1, int(round(duration * fps)))
                phrases = caption_phrases(words)
                caption_cache: dict = {}
                headline = headline_layer(scene.on_screen, size, vertical) if scene.on_screen else None
                headline_cache: dict = {}
                for f in range(frames):
                    t = f / fps
                    progress = f / max(frames - 1, 1)
                    zoom = 1.0 + 0.15 * progress if i % 2 else 1.15 - 0.15 * progress          # slow push in, then pull out
                    bw, bh = base.size                                                          # 1.18 x the frame
                    zoom = max(zoom, 1.0)
                    cw, ch = bw / zoom, bh / zoom
                    x0, y0 = max((bw - cw) / 2, 0.0), max((bh - ch) / 2, 0.0)
                    img = base.resize(size, Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))
                    if headline is not None:
                        alpha = min(1.0, t / 0.35, max(0.0, (min(duration, 4.5) - t) / 0.35))
                        if alpha > 0:
                            key = round(alpha * 10)
                            layer = headline_cache.setdefault(key, faded(headline[0], alpha))
                            img.paste(layer, headline[1], layer)
                    for pi, phrase in enumerate(phrases):
                        if phrase[0].start - 0.05 <= t <= phrase[-1].end + 0.25:
                            current = max((wi for wi, w in enumerate(phrase) if w.start - 0.03 <= t), default=0)
                            key = (pi, current)
                            if key not in caption_cache:
                                caption_cache[key] = caption_layer([w.text for w in phrase], current, size, vertical)
                            layer, pos = caption_cache[key]
                            img.paste(layer, pos, layer)
                            break
                    # a short fade in and out of each scene keeps the cuts soft
                    edge = min(t, duration - t) / 0.18
                    arr = np.asarray(img, dtype="uint8")
                    if edge < 1:
                        arr = (arr.astype("float32") * max(edge, 0.25)).astype("uint8")
                    frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
                    frame.pts = frame_no
                    for packet in vs.encode(frame):
                        container.mux(packet)
                    frame_no += 1
                # this scene's audio: voice, then a pad of silence, with the music underneath
                n = int(round(duration * SAMPLE_RATE))
                voice = np.zeros(n, dtype="float32")
                voice[:min(n, len(samples))] = samples[:n]
                if music is not None:
                    voice += music[audio_pos:audio_pos + n] if audio_pos + n <= len(music) else np.pad(music[audio_pos:], (0, n - len(music[audio_pos:])))[:n]
                voice = np.clip(voice, -1, 1)
                for start in range(0, n, 1024):
                    chunk = voice[start:start + 1024]
                    aframe = av.AudioFrame.from_ndarray(np.stack([chunk, chunk]).astype("float32"), format="fltp", layout="stereo")
                    aframe.sample_rate = SAMPLE_RATE
                    aframe.pts = audio_pos + start
                    for packet in aus.encode(aframe):
                        container.mux(packet)
                audio_pos += n
                total_seconds += duration
            for packet in vs.encode(None):
                container.mux(packet)
            for packet in aus.encode(None):
                container.mux(packet)
        except BaseException:
            container.close()
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise
        container.close()
        thumb = str(self.dir / f"{slug}.jpg")
        make_thumbnail(title, bases[0], thumb)
        for leftover in work.glob("*"):
            leftover.unlink(missing_ok=True)
        work.rmdir()
        return VideoResult(out_path, thumb, round(total_seconds, 1), len(voiced), notes)

    def _music(self, seconds: float):
        import numpy as np

        for candidate in (os.getenv("JARVIS_VIDEO_MUSIC", ""), str(self.assets / "music.mp3")):
            if candidate and Path(candidate).is_file():
                try:
                    track = decode_audio(candidate)
                except Exception:
                    continue
                if len(track):
                    need = int(seconds * SAMPLE_RATE) + SAMPLE_RATE
                    track = np.tile(track, need // len(track) + 1)[:need] * 0.10
                    fade = min(len(track), SAMPLE_RATE * 2)
                    track[-fade:] *= np.linspace(1, 0, fade, dtype="float32")
                    return track
        return None


# ------------------------------------------------------------------ Jay's side
class Videos:
    """Background video jobs. A finished video is attached to its draft and sent to the phone."""

    def __init__(self, data_dir: Path, queue=None, phone=None, notify=None):
        self.data_dir, self.queue, self.phone, self.notify = data_dir, queue, phone, notify
        self.jobs: dict[int, dict] = {}
        self._next = 1

    def start(self, title: str, scenes: list[Scene], vertical: bool, content_id: int | None = None, voice: str | None = None) -> str:
        job_id = self._next
        self._next += 1
        maker = VideoMaker(self.data_dir, voice=voice, progress=lambda text, j=job_id: self.jobs[j].__setitem__("now", text))
        self.jobs[job_id] = {"title": title, "status": "running", "now": "starting", "started": datetime.now().strftime("%H:%M"),
                             "maker": maker, "content": content_id}
        threading.Thread(target=self._run, args=(job_id, maker, title, scenes, vertical, content_id), daemon=True).start()
        words = sum(len(s.narration.split()) for s in scenes)
        return (f"Making video #{job_id} '{title}' ({len(scenes)} scenes, about {words / 2.5:.0f} seconds of speech, "
                f"{'vertical' if vertical else 'horizontal'}). It renders in the background and goes to your phone when done.")

    def _run(self, job_id, maker, title, scenes, vertical, content_id) -> None:
        job = self.jobs[job_id]
        try:
            result = maker.make(title, scenes, vertical)
        except Exception as exc:
            job.update(status="failed", now=f"{type(exc).__name__}: {exc}")
            self._announce(f"The video '{title}' failed: {str(exc)[:200]}")
            return
        job.update(status="done", now=result.path, result=result)
        attached = ""
        if content_id and self.queue is not None:
            self.queue.attach_media(content_id, result.path, result.thumbnail)
            attached = f" It's attached to draft #{content_id}."
        extra = (" Notes: " + "; ".join(result.notes) + ".") if result.notes else ""
        self._announce(f"Video '{title}' is ready: {result.seconds:.0f} seconds, {result.scenes} scenes.{attached}{extra}",
                       result.path)

    def _announce(self, text: str, video: str = "") -> None:
        if self.notify:
            try:
                self.notify("Jay · video", text)
            except Exception:
                pass
        if self.phone is not None and self.phone.available():
            size = os.path.getsize(video) if video and os.path.isfile(video) else 0
            if video and size and size <= 49 * 1024 * 1024:
                self.phone.send_video("Jay · video", text, video)
            else:
                self.phone.send("Jay · video", text + (f"\nIt's too big to send here ({size // 1048576} MB). File: {video}" if video else ""))

    def status(self) -> str:
        return "\n".join(f"#{i} {j['title']}: {j['status']} ({j['now']}), started {j['started']}" for i, j in self.jobs.items()) or "No videos yet."

    def stop(self, job_id: int) -> str:
        job = self.jobs.get(job_id)
        if not job:
            return f"No video #{job_id}."
        job["maker"].stop.set()
        return f"Stopping video #{job_id}."


def register(reg: Registry, videos: Videos, queue) -> None:
    @reg.tool(
        "make_video",
        "Make a faceless narrated video with captions from a storyboard, in the background. Give the scenes "
        "yourself: each has `narration` (what the voice says, a few sentences), optional `on_screen` (a few big "
        "words, e.g. a heading) and optional `image` (a path to an image file on the PC, or a short search phrase "
        "for free stock photos). If you give content_id and no scenes, the draft's script is split into scenes. "
        "Use vertical for Shorts and Reels, horizontal for normal YouTube videos. The finished video is attached "
        "to the draft and sent to the user's phone.",
        {"title": {"type": "string"},
         "scenes": {"type": "array", "items": {"type": "object", "properties": {
             "narration": {"type": "string"}, "on_screen": {"type": "string"}, "image": {"type": "string"}},
             "required": ["narration"], "additionalProperties": False}},
         "format": {"type": "string", "enum": ["vertical", "horizontal"]},
         "content_id": {"type": "integer", "description": "The draft this video is for."},
         "voice": {"type": "string", "description": "Optional edge-tts voice name, e.g. en-US-GuyNeural."}},
        required=("title",),
    )
    def make_video(title: str, scenes: list | None = None, format: str = "", content_id: int = 0, voice: str = "") -> str:
        item = queue.get(content_id) if content_id else None
        if content_id and item is None:
            return f"No draft #{content_id}."
        if scenes:
            plan = [Scene(s["narration"], s.get("on_screen", ""), s.get("image", "")) for s in scenes]
        elif item is not None and item["script"].strip():
            plan = auto_scenes(item["script"], title)
        else:
            return "Give the scenes, or a content_id whose draft has a script."
        vertical = (format == "vertical") if format else bool(item and item["kind"] in ("short", "reel", "story"))
        return videos.start(title, plan, vertical, content_id or None, voice or None)

    @reg.tool("video_status", "How the video renders are going.")
    def video_status() -> str:
        return videos.status()

    @reg.tool("video_stop", "Stop a video render by its number.", {"id": {"type": "integer"}}, required=("id",))
    def video_stop(id: int) -> str:
        return videos.stop(id)
