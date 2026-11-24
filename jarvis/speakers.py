"""Who is speaking: on-laptop speaker recognition for a handful of people.

Each enrolled person has a voiceprint (an averaged speaker embedding from a few short
recordings). A new utterance is compared with every voiceprint; the closest one above
a threshold is the speaker. Everything runs locally with a small ONNX model
(NVIDIA TitaNet-small, via sherpa-onnx); no audio leaves the laptop for this.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             + MODEL_NAME)
SAMPLE_RATE = 16_000
MIN_SECONDS = 1.0          # shorter clips are too unreliable to say exactly who it is
GATE_MIN_SECONDS = 0.5     # ... but enough to tell a known voice from a stranger or a video
GATE_THRESHOLD = 0.22      # "sounds like someone I know" (wake word, follow-ups)
ADAPT_THRESHOLD = 0.6      # sure enough to refine that person's voiceprint
MAX_PEOPLE = 6

ENROL_LINES = [
    "Hey Jay, this is my voice, and I'd like you to recognise it.",
    "The quick brown fox jumps over the lazy dog by the river.",
    "Remind me to finish my assignment before Friday evening.",
    "What's the weather looking like for tomorrow morning?",
]


@dataclass
class Match:
    name: str | None        # None = not one of the enrolled people
    score: float


class Speakers:
    def __init__(self, data_dir: Path, threshold: float = 0.5):
        self.path = data_dir / "voiceprints.json"
        self.model_path = data_dir / "models" / MODEL_NAME
        self.threshold = threshold
        self._extractor = None
        self._lock = threading.Lock()
        self.prints: dict[str, list[float]] = {}
        self.owner: str | None = None
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.prints, self.owner = saved.get("prints", {}), saved.get("owner")

    # ---- model -------------------------------------------------------------
    def _model(self):
        if self._extractor is None:
            import sherpa_onnx

            if not self.model_path.exists():
                self.model_path.parent.mkdir(parents=True, exist_ok=True)
                partial = self.model_path.with_suffix(".part")
                urllib.request.urlretrieve(MODEL_URL, partial)
                partial.replace(self.model_path)
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model_path), num_threads=2)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return self._extractor

    def embed(self, audio):
        import numpy as np

        with self._lock:
            extractor = self._model()
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, np.asarray(audio, dtype="float32"))
            stream.input_finished()
            vector = np.array(extractor.compute(stream), dtype="float32")
        return vector / (np.linalg.norm(vector) or 1.0)

    # ---- people ------------------------------------------------------------
    @property
    def enrolled(self) -> list[str]:
        return list(self.prints)

    def _save(self) -> None:
        self.path.write_text(json.dumps({"owner": self.owner, "prints": self.prints}), encoding="utf-8")

    def enrol(self, name: str, clips: list) -> None:
        """Store a voiceprint for `name` from a few recordings. The first person enrolled is the owner."""
        import numpy as np

        name = name.strip().title()
        if not name:
            raise ValueError("A name is needed.")
        usable = [c for c in clips if len(c) >= SAMPLE_RATE * MIN_SECONDS]
        if len(usable) < 2:
            raise ValueError("Not enough clear speech was recorded; try again somewhere quieter.")
        if name not in self.prints and len(self.prints) >= MAX_PEOPLE:
            raise ValueError(f"Up to {MAX_PEOPLE} voices can be recognised; remove one first.")
        mean = np.mean([self.embed(c) for c in usable], axis=0)
        self.prints[name] = (mean / np.linalg.norm(mean)).round(5).tolist()
        if self.owner is None:
            self.owner = name
        self._save()

    def remove(self, name: str) -> bool:
        key = next((n for n in self.prints if n.lower() == name.strip().lower()), None)
        if key is None:
            return False
        del self.prints[key]
        if self.owner == key:
            self.owner = next(iter(self.prints), None)
        self._save()
        return True

    def set_owner(self, name: str) -> bool:
        key = next((n for n in self.prints if n.lower() == name.strip().lower()), None)
        if key is None:
            return False
        self.owner = key
        self._save()
        return True

    def best(self, audio, min_seconds: float = GATE_MIN_SECONDS):
        """(closest enrolled name, score, embedding) for any clip long enough to judge, else None."""
        import numpy as np

        if not self.prints or len(audio) < SAMPLE_RATE * min_seconds:
            return None
        vector = self.embed(audio)
        scores = {name: float(vector @ np.asarray(p, dtype="float32")) for name, p in self.prints.items()}
        name = max(scores, key=scores.get)
        return name, round(scores[name], 3), vector

    def is_known(self, audio) -> bool:
        """Does this sound like any enrolled person? Lenient, for waking up and follow-ups."""
        found = self.best(audio)
        return found is not None and found[1] >= GATE_THRESHOLD

    def identify(self, audio) -> Match:
        """Whose voice is this? Returns the best match, or name=None if it's nobody enrolled."""
        found = self.best(audio, MIN_SECONDS)
        if found is None:
            return Match(None, 0.0)
        name, score, vector = found
        if score >= ADAPT_THRESHOLD and len(audio) >= SAMPLE_RATE * 1.5:
            self._adapt(name, vector)
        return Match(name if score >= self.threshold else None, score)

    def _adapt(self, name: str, vector) -> None:
        """Nudge a voiceprint towards a confident new sample, so it keeps up with this
        microphone, this room and how the person sounds day to day."""
        import numpy as np

        mixed = 0.92 * np.asarray(self.prints[name], dtype="float32") + 0.08 * vector
        self.prints[name] = (mixed / np.linalg.norm(mixed)).round(5).tolist()
        self._save()
