"""Skills: small instruction files that teach Jay how to do a job well.

Adapted from OpenJarvis (Stanford, Apache-2.0, https://github.com/open-jarvis/OpenJarvis).
A skill is either
  * a SKILL.md file in the open agentskills.io format (YAML front matter with `name` and
    `description`, then the instructions in markdown), or
  * an OpenJarvis .toml skill (a list of steps), which is turned into plain instructions.

Jay lists the skills it has, and calls `use_skill` to read one before doing that kind of job.
Skills live in `jarvis/skills_builtin` (shipped) and `data/skills` (yours). A skill is only
text for the model to follow: it cannot run anything by itself, and anything it asks for goes
through the normal tools and approval cards.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .security import safe_get, scan_injection
from .tools.registry import Registry

BUILTIN_DIR = Path(__file__).with_name("skills_builtin")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_BYTES = 60_000

# What an OpenJarvis step's tool is called in Jay.
TOOL_WORDS = {
    "file_read": "read_file",
    "file_write": "write_file",
    "shell_exec": "run_powershell",
    "web_search": "web_search",
    "http_request": "fetch_page",
    "pdf_extract": "read_document",
    "memory_search": "recall (and search_knowledge for their notes)",
    "memory_store": "remember",
    "memory_index": "learn_document",
    "code_interpreter": "run_powershell (run the Python with `python`)",
}


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: str            # "built-in" or "yours"


def _placeholders(text: str) -> list[str]:
    return re.findall(r"\{([a-z_][a-z0-9_]*)\}", text)


def toml_to_instructions(data: dict) -> str:
    """Turn an OpenJarvis step list into numbered plain-English instructions."""
    steps = data.get("skill", {}).get("steps", [])
    outputs = {s.get("output_key") for s in steps if s.get("output_key")}
    needed: list[str] = []
    for step in steps:
        for key in _placeholders(step.get("arguments_template", "")):
            if key not in outputs and key not in needed:
                needed.append(key)
    lines = []
    if needed:
        lines.append("Inputs (take them from the user's request, and ask if one is missing): "
                     + ", ".join(needed) + ".")
    for number, step in enumerate(steps, 1):
        template = step.get("arguments_template", "")
        text = re.sub(r"\{([a-z_][a-z0-9_]*)\}", lambda m: f"<{m.group(1)}>", template)
        tool = step.get("tool_name", "")
        keep = f" Keep the result as <{step['output_key']}>." if step.get("output_key") else ""
        if tool in ("think", "llm_call"):
            thought = re.search(r'"(?:thought|prompt)":\s*"(.*)"\s*}?$', text, re.S)
            lines.append(f"{number}. Work this out yourself (no tool needed): {thought.group(1) if thought else text}.{keep}")
        else:
            lines.append(f"{number}. Use {TOOL_WORDS.get(tool, tool)} with {text}.{keep}")
    return "\n".join(lines)


def parse_skill_md(text: str) -> tuple[dict, str]:
    """(front matter, body) of a SKILL.md. Raises ValueError if it isn't a valid skill."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text.lstrip("﻿"), re.S)
    if not match:
        raise ValueError("a SKILL.md must start with --- front matter containing name and description")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("the front matter is not a set of fields")
    name, description = str(meta.get("name", "")).strip(), str(meta.get("description", "")).strip()
    if not NAME_RE.match(name) or len(name) > 64:
        raise ValueError("name must be lower-case words joined by hyphens (up to 64 characters)")
    if not description:
        raise ValueError("description is missing")
    return {"name": name, "description": description[:1024]}, match.group(2).strip()


def _load_file(path: Path, source: str) -> Skill | None:
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            meta = data["skill"]
            return Skill(meta["name"], meta.get("description", ""), toml_to_instructions(data), source)
        meta, body = parse_skill_md(path.read_text(encoding="utf-8"))
        return Skill(meta["name"], meta["description"], body, source)
    except Exception:
        return None                                      # a broken file never stops Jay starting


class SkillBook:
    def __init__(self, data_dir: Path):
        self.user_dir = data_dir / "skills"
        self.skills: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        found: dict[str, Skill] = {}
        for folder, source in ((BUILTIN_DIR, "built-in"), (self.user_dir, "yours")):    # yours wins
            if not folder.is_dir():
                continue
            for path in sorted([*folder.glob("*.toml"), *folder.glob("*.md"), *folder.glob("*/SKILL.md")]):
                skill = _load_file(path, source)
                if skill:
                    found[skill.name] = skill
        self.skills = found

    def catalog(self, width: int = 1024) -> str:
        """One line per skill, for the instructions Jay starts with."""
        return "\n".join(f"- {s.name}: {s.description[:width]}" for s in self.skills.values())

    def install(self, url: str) -> Skill:
        """Fetch one SKILL.md from the web, check it, and keep it. Raises ValueError/PermissionError."""
        url = re.sub(r"^https://github\.com/([^/]+/[^/]+)/blob/", r"https://raw.githubusercontent.com/\1/", url.strip())
        if not url.startswith("https://"):
            raise ValueError("Skills can only be installed from an https:// link.")
        resp = safe_get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; personal-assistant)"})
        resp.raise_for_status()
        if len(resp.content) > MAX_SKILL_BYTES:
            raise ValueError("That file is too large to be a skill.")
        meta, body = parse_skill_md(resp.text)
        reasons = scan_injection(resp.text)
        if reasons:
            raise PermissionError("Not installed: the skill " + "; ".join(reasons)
                                  + ". If you trust it, save it yourself in data\\skills.")
        folder = self.user_dir / meta["name"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(resp.text, encoding="utf-8")
        self.reload()
        return self.skills[meta["name"]]


def register(reg: Registry, book: SkillBook) -> None:
    @reg.tool(
        "use_skill",
        "Read the step-by-step instructions of one of your skills (listed in your instructions) and "
        "then follow them with your other tools. Call this before starting a job a skill covers.",
        {"name": {"type": "string", "description": "The skill's name, e.g. 'meeting-notes'."}},
        required=("name",),
    )
    def use_skill(name: str) -> str:
        skill = book.skills.get(name.strip().lower())
        if skill is None:
            return f"No skill called '{name}'. Available: {', '.join(book.skills) or 'none'}."
        return f"Skill: {skill.name}\n{skill.description}\n\n{skill.body}"

    @reg.tool("list_skills", "List the skills you have, with what each is for.")
    def list_skills() -> str:
        return book.catalog() or "No skills installed."

    @reg.tool(
        "install_skill",
        "Install a new skill from the web: the link must point to a SKILL.md file (agentskills.io format), "
        "for example a raw GitHub link. The user is asked to approve it, and it is refused if the text "
        "tries to give you orders.",
        {"url": {"type": "string", "description": "https link to a SKILL.md file."}},
        required=("url",),
        confirm=lambda a: f"Install a skill from {a['url']}",
    )
    def install_skill(url: str) -> str:
        skill = book.install(url)
        return f"Installed skill '{skill.name}': {skill.description}"
