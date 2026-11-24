"""The owner's profile: the core facts Jay should always know, kept apart from the
everyday memories so they are never pushed out or lost. Stored in data/profile.json."""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = {                       # key: (label, hint shown in the window)
    "name": ("Name", "e.g. Rohith"),
    "call_me": ("Call me", "how Jay addresses you, e.g. boss"),
    "about": ("About me", "a line or two about who you are"),
    "education": ("Studies", "college, course, year, graduating when"),
    "work": ("Work and skills", "domain, skills, projects, internships"),
    "location": ("Location", "city, and home / college areas"),
    "goals": ("Goals", "what you're working towards right now"),
    "interests": ("Interests", "topics, sports, music, news you follow"),
    "routine": ("Daily routine", "when you wake, study, work out, sleep"),
    "timetable": ("Weekly timetable", "classes, labs, fixed slots by day (or tell Jay to learn your timetable PDF)"),
    "hobbies": ("Hobbies", "what you do for fun and to unwind"),
    "personality": ("How Jay reads you", "Jay's working picture of your character, habits and blind spots"),
    "people": ("People", "family and friends Jay should know, and what you call them"),
    "preferences": ("How Jay should work", "tone, detail, things to always or never do"),
}


class Profile:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "profile.json"
        self.values: dict[str, str] = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.values = {k: str(v) for k, v in saved.items() if k in FIELDS and str(v).strip()}

    def update(self, changes: dict) -> None:
        for key, value in changes.items():
            if key not in FIELDS:
                raise ValueError(f"Unknown profile field '{key}'. Fields: {', '.join(FIELDS)}")
            value = str(value or "").strip()[:700]
            if value:
                self.values[key] = value
            else:
                self.values.pop(key, None)
        self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=1), encoding="utf-8")

    def as_text(self) -> str:
        if not self.values:
            return "- (not filled in yet; when the owner tells you about themselves, save it with update_profile)"
        clip = lambda v: v if len(v) <= 420 else v[:417] + "..."      # full text stays stored; the prompt stays small
        return "\n".join(f"- {FIELDS[k][0]}: {clip(v)}" for k, v in self.values.items())

    def form(self) -> list[dict]:
        return [{"key": k, "label": label, "hint": hint, "value": self.values.get(k, "")}
                for k, (label, hint) in FIELDS.items()]
