"""Jay builds software by itself: build, test, review, improve, repeat until the target is met.

You give one command ("build a task tracker with a React front end and a FastAPI back end that ...")
and Jay works in its own project folder without asking about every step:

    round 1:  a coding agent writes the project and runs it
              Jay runs the tests itself (the agent can't just claim they pass)
              a separate reviewer reads the code and the test output and scores it against your target
    round 2+: if the target isn't met, the review's issues go back to the agent, which fixes and
              improves; tests and review run again ... until the target is met, or a limit is reached.

Limits (all adjustable in .env): rounds, tokens (your Anthropic credit) and time. When the build
stops, Jay says honestly whether the target was met and sends you the result on your phone.

Where the code runs. Everything the agent runs happens in the project folder only. If Docker is
running, commands run inside a container that can see nothing but that folder (best). If not, a
restricted local runner is used: one program per command from a short list, nothing outside the
folder, and none of your API keys in its environment. Local mode cannot stop code the agent
writes from doing something harmful when it runs, so Docker is the safer choice for big builds.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .security import check_file_allowed, strip_credentials
from .tools.registry import Registry

DEFAULT_PROJECTS = r"D:\jay_projects"
IMAGE = os.getenv("JARVIS_BUILD_IMAGE", "nikolaik/python-nodejs:python3.12-nodejs22")
ALLOWED_PROGRAMS = {"python", "python3", "py", "pip", "pip3", "node", "npm", "git", "pytest", "yarn", "pnpm",
                    "uv", "tsc", "vite", "eslint", "prettier"}
BLOCKED_GIT = {"push", "remote", "config", "credential", "clone", "fetch", "pull"}
SECRETISH = re.compile(r"(?i)(key|token|secret|passw|credential|cookie|session|auth)")
OUT_LIMIT = 8000
STEP_CAP = 60              # tool calls the agent may make in one round


# ------------------------------------------------------------------ where commands run
class Sandbox:
    """Files and commands confined to one project folder."""

    def __init__(self, root: Path, use_docker: bool | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.docker = docker_ready() if use_docker is None else use_docker

    def path(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise PermissionError("That path is outside the project folder.")
        check_file_allowed(target)
        return target

    def write(self, relative: str, content: str) -> str:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {relative} ({len(content)} characters)."

    def read(self, relative: str) -> str:
        target = self.path(relative)
        if target.stat().st_size > 200_000:
            return "File is larger than 200 KB; read part of it with a command instead."
        return target.read_text(encoding="utf-8", errors="replace")

    def tree(self, limit: int = 300) -> str:
        skip = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".jay", ".pytest_cache"}
        lines = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part in skip for part in rel.parts) or not p.is_file():
                continue
            lines.append(f"{rel.as_posix()} ({p.stat().st_size} bytes)")
            if len(lines) >= limit:
                lines.append("... more files not shown")
                break
        return "\n".join(lines) or "(empty)"

    # -- commands
    def _clean_env(self) -> dict:
        return {k: v for k, v in os.environ.items() if not SECRETISH.search(k)}

    def check_command(self, command: str) -> list[str]:
        """Local mode only: split into one allowed program with arguments, or raise PermissionError."""
        if re.search(r"[&|;<>`\n]|\$\(", command):
            raise PermissionError("Run one plain command at a time (no &&, |, ;, > or backticks).")
        parts = shlex.split(command)
        if not parts:
            raise PermissionError("Empty command.")
        program = Path(parts[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")
        if program not in ALLOWED_PROGRAMS:
            raise PermissionError(f"'{parts[0]}' is not an allowed program. Allowed: {', '.join(sorted(ALLOWED_PROGRAMS))}.")
        if program == "git" and len(parts) > 1 and parts[1] in BLOCKED_GIT:
            raise PermissionError(f"git {parts[1]} is not allowed.")
        if program in ("npm", "pnpm", "yarn") and len(parts) > 1 and parts[1] in ("publish", "login", "adduser", "token", "exec"):
            raise PermissionError(f"{program} {parts[1]} is not allowed.")
        for arg in parts[1:]:
            if ".." in Path(arg).parts:
                raise PermissionError("Paths with .. are not allowed.")
            if re.match(r"^([A-Za-z]:|/|\\\\)", arg):
                resolved = Path(arg).resolve()
                if resolved != self.root and self.root not in resolved.parents:
                    raise PermissionError("Absolute paths outside the project are not allowed.")
        exe = shutil.which(parts[0])
        if not exe:
            raise PermissionError(f"'{parts[0]}' is not installed on this PC.")
        return [exe, *parts[1:]]

    def run(self, command: str, timeout: int = 300) -> tuple[int, str]:
        if self.docker:
            argv = ["docker", "run", "--rm", "-v", f"{self.root}:/work", "-w", "/work", "-e", "HOME=/tmp",
                    "--memory", "3g", "--cpus", "2", "--pids-limit", "512", IMAGE, "sh", "-c", command]
            env = None
        else:
            argv, env = self.check_command(command), self._clean_env()
        try:
            proc = subprocess.Popen(argv, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            return 127, f"Could not start: {exc}"
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            out, _ = proc.communicate()
            return 124, f"(stopped after {timeout} seconds)\n" + _tail(out)
        return proc.returncode, _tail(out)


def _tail(text: str, limit: int = OUT_LIMIT) -> str:
    text = strip_credentials(text or "")
    return text if len(text) <= limit else f"[...{len(text) - limit} earlier characters dropped]\n" + text[-limit:]


def docker_ready() -> bool:
    if os.getenv("JARVIS_BUILD_SANDBOX", "auto").lower() == "off" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


# ------------------------------------------------------------------ the loop
@dataclass
class Limits:
    rounds: int = int(os.getenv("JARVIS_BUILD_ROUNDS", "6"))
    tokens: int = int(os.getenv("JARVIS_BUILD_TOKENS", "2000000"))
    minutes: int = int(os.getenv("JARVIS_BUILD_MINUTES", "90"))
    pass_score: int = int(os.getenv("JARVIS_BUILD_PASS_SCORE", "8"))


@dataclass
class BuildResult:
    met: bool
    rounds: int
    score: int
    summary: str
    path: str
    reason: str                       # why it stopped
    tests: str = ""
    tokens: int = 0
    issues: list = field(default_factory=list)


AGENT_SYSTEM = """You are a senior full-stack engineer working alone and unsupervised in one project folder.
Build exactly what the brief asks, working end to end, then run it and test it yourself.
- Use write_file to create or replace files, read_file and list_files to look, and run to execute \
commands (install, build, test, start scripts). Paths are relative to the project folder; use forward slashes.
- Write real tests (pytest, or the framework's test runner) that check the acceptance criteria, and make \
them pass. A project with no tests is not finished.
- Keep a README.md saying how to install, run and test it, and put the exact test command in `finish`.
- Never put secrets in files. Never try to reach outside the project folder.
- Be economical: do not re-read files you just wrote, and do not repeat a failing command unchanged.
- When you believe the work for this round is complete, call finish."""

REVIEW_SYSTEM = """You are a strict code reviewer deciding whether a software project meets its brief.
You are given the brief, the acceptance criteria, the files, and the latest test output.
Judge honestly: missing features, failing or shallow tests, bugs, insecure code (injection, secrets, no input \
validation), poor structure, missing README. Reply with ONLY a JSON object:
{"met": true|false, "score": 0-10, "issues": ["specific problem to fix", ...], "improvements": ["optional upgrade", ...]}
"met" is true only if every acceptance criterion works and the tests pass."""

TOOLS = [
    {"name": "write_file", "description": "Create or overwrite a file in the project.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "read_file", "description": "Read a file in the project.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_files", "description": "List the project's files.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "run", "description": "Run one command in the project folder (install, build, test). Returns exit code and output.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "finish", "description": "Call when this round's work is complete.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "What was built or changed."},
         "test_command": {"type": "string", "description": "The exact command that runs the tests."}},
         "required": ["summary"]}},
]


class Builder:
    def __init__(self, sandbox: Sandbox, client=None, model: str | None = None, limits: Limits | None = None,
                 progress=lambda text: None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.sandbox, self.client, self.progress = sandbox, client, progress
        self.model = model or os.getenv("JARVIS_BUILD_MODEL") or os.getenv("JARVIS_CLAUDE_MODEL", "claude-opus-5-5")
        self.limits = limits or Limits()
        self.tokens = 0
        self.stop = threading.Event()

    # -- one model call
    def _call(self, system: str, messages: list, tools: list | None = None, max_tokens: int = 16000):
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = self.client.messages.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage:
            self.tokens += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
        return resp

    def _out_of_budget(self, started: float) -> str | None:
        if self.stop.is_set():
            return "stopped by you"
        if self.tokens >= self.limits.tokens:
            return f"token budget reached ({self.limits.tokens:,})"
        if time.monotonic() - started > self.limits.minutes * 60:
            return f"time limit reached ({self.limits.minutes} minutes)"
        return None

    # -- the agent's round
    def _tool(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            if name == "write_file":
                return self.sandbox.write(args["path"], args["content"]), False
            if name == "read_file":
                return self.sandbox.read(args["path"]), False
            if name == "list_files":
                return self.sandbox.tree(), False
            if name == "run":
                code, out = self.sandbox.run(args["command"])
                return f"exit code {code}\n{out}", code != 0
        except PermissionError as exc:
            return f"Refused: {exc}", True
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True
        return f"Unknown tool {name}.", True

    def _agent_round(self, brief: str, started: float) -> tuple[str, str]:
        """Let the agent work until it calls finish. Returns (its summary, the test command it named)."""
        messages = [{"role": "user", "content": brief}]
        summary, test_command = "", ""
        for _ in range(STEP_CAP):
            if self._out_of_budget(started):
                break
            resp = self._call(AGENT_SYSTEM, messages, TOOLS)
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                summary = summary or "\n".join(b.text for b in resp.content if b.type == "text").strip()
                break
            results, done = [], False
            for block in calls:
                if block.name == "finish":
                    summary = block.input.get("summary", "")
                    test_command = block.input.get("test_command", "") or test_command
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Noted."})
                    done = True
                    continue
                if block.name == "run":
                    self.progress(f"running: {block.input.get('command', '')[:80]}")
                text, is_error = self._tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text[:OUT_LIMIT * 2],
                                "is_error": is_error})
            messages.append({"role": "user", "content": results})
            if done:
                break
        return summary, test_command

    # -- review
    def _review(self, goal: str, acceptance: str, tests: str) -> dict:
        files, used = [], 0
        for line in self.sandbox.tree().splitlines():
            rel = line.split(" (")[0]
            if used > 60_000 or not rel or rel.endswith(("lock.json", ".lock", ".png", ".jpg", ".svg", ".ico")):
                continue
            try:
                body = self.sandbox.read(rel)[:12_000]
            except Exception:
                continue
            files.append(f"=== {rel}\n{body}")
            used += len(body)
        prompt = (f"BRIEF:\n{goal}\n\nACCEPTANCE CRITERIA:\n{acceptance or '(none given; judge against the brief)'}\n\n"
                  f"LATEST TEST OUTPUT:\n{tests or '(no tests were run)'}\n\nFILES:\n" + "\n\n".join(files))
        resp = self._call(REVIEW_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=4000)
        text = "".join(b.text for b in resp.content if b.type == "text")
        match = re.search(r"\{.*\}", text, re.S)
        try:
            verdict = json.loads(match.group(0)) if match else {}
        except ValueError:
            verdict = {}
        verdict.setdefault("met", False)
        verdict["score"] = int(verdict.get("score", 0) or 0)
        verdict["issues"] = [str(i) for i in verdict.get("issues", [])][:12]
        verdict["improvements"] = [str(i) for i in verdict.get("improvements", [])][:8]
        return verdict

    def _log(self, text: str) -> None:
        folder = self.sandbox.root / ".jay"
        folder.mkdir(exist_ok=True)
        with open(folder / "build_log.md", "a", encoding="utf-8") as handle:
            handle.write(f"\n## {datetime.now():%Y-%m-%d %H:%M}\n{text}\n")

    # -- the whole job
    def build(self, goal: str, acceptance: str = "", test_command: str = "") -> BuildResult:
        started = time.monotonic()
        feedback, summary, tests, verdict = "", "", "", {"met": False, "score": 0, "issues": []}
        rounds = 0
        reason = "round limit reached"
        for rounds in range(1, self.limits.rounds + 1):
            reason = self._out_of_budget(started)
            if reason:
                rounds -= 1
                break
            self.progress(f"round {rounds}: building")
            brief = (f"BRIEF:\n{goal}\n\nACCEPTANCE CRITERIA:\n{acceptance or '(use your judgement from the brief)'}\n\n"
                     f"Project folder contents now:\n{self.sandbox.tree()}\n")
            if feedback:
                brief += (f"\nThis is round {rounds}. The last round did not meet the target. Review feedback to fix "
                          f"and then improve on:\n{feedback}\n\nLatest test output:\n{tests}\n")
            summary, named = self._agent_round(brief, started)
            test_command = test_command or named
            self.progress(f"round {rounds}: testing")
            if test_command:
                code, out = self.sandbox.run(test_command, timeout=600)
                tests = f"$ {test_command}\nexit code {code}\n{out}"
                tests_ok = code == 0
            else:
                tests, tests_ok = "(no test command was given)", False
            self.progress(f"round {rounds}: reviewing")
            verdict = self._review(goal, acceptance, tests)
            self._log(f"Round {rounds}: tests {'passed' if tests_ok else 'FAILED'}; score {verdict['score']}/10; "
                      f"met={verdict['met']}\nAgent: {summary}\nIssues: {verdict['issues']}\n"
                      f"Improvements: {verdict['improvements']}")
            if tests_ok and verdict["met"] and verdict["score"] >= self.limits.pass_score:
                reason = "target met"
                return BuildResult(True, rounds, verdict["score"], summary, str(self.sandbox.root), reason,
                                   tests, self.tokens, verdict["issues"])
            feedback = "\n".join(f"- FIX: {i}" for i in verdict["issues"]) + "\n" + \
                       "\n".join(f"- IMPROVE: {i}" for i in verdict["improvements"])
            if not tests_ok:
                feedback = "- FIX: the tests are failing or missing (see output below)\n" + feedback
        else:
            reason = "round limit reached"
        return BuildResult(False, rounds, verdict["score"], summary, str(self.sandbox.root), reason or "stopped",
                           tests, self.tokens, verdict["issues"])


# ------------------------------------------------------------------ Jay's side: jobs and tools
def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"


class Builds:
    """Background build jobs, one thread each, reported to the phone when they finish."""

    def __init__(self, projects_dir: Path, phone=None, notify=None, client=None):
        self.projects_dir = Path(os.getenv("JARVIS_PROJECTS_DIR") or projects_dir)
        self.phone, self.notify, self.client = phone, notify, client
        self.jobs: dict[str, dict] = {}
        self._builders: dict[str, Builder] = {}

    def start(self, name: str, goal: str, acceptance: str = "", test_command: str = "") -> str:
        key = slug(name)
        if self.jobs.get(key, {}).get("status") == "running":
            return f"'{key}' is already being built."
        sandbox = Sandbox(self.projects_dir / key)
        builder = Builder(sandbox, client=self.client,
                          progress=lambda text, k=key: self.jobs[k].__setitem__("now", text))
        self._builders[key] = builder
        self.jobs[key] = {"status": "running", "now": "starting", "path": str(sandbox.root),
                          "started": datetime.now().strftime("%H:%M"), "docker": sandbox.docker}
        threading.Thread(target=self._run, args=(key, builder, goal, acceptance, test_command), daemon=True).start()
        mode = "in a Docker container" if sandbox.docker else "with the restricted local runner (Docker isn't running)"
        return (f"Started building '{key}' by myself in {sandbox.root}, {mode}. "
                f"Up to {builder.limits.rounds} rounds of build, test, review and improve. "
                "I'll message your phone when it's done; ask build_status any time.")

    def _run(self, key: str, builder: Builder, goal: str, acceptance: str, test_command: str) -> None:
        try:
            result = builder.build(goal, acceptance, test_command)
            self.jobs[key].update(status="met" if result.met else "stopped", now=result.reason,
                                  score=result.score, rounds=result.rounds, tokens=result.tokens)
            headline = (f"'{key}' is built and passed its tests and review (score {result.score}/10, "
                        f"{result.rounds} round{'s' if result.rounds != 1 else ''})."
                        if result.met else
                        f"'{key}' isn't finished: {result.reason}, after {result.rounds} rounds, score {result.score}/10.")
            more = ""
            if result.issues and not result.met:
                more = " Still open: " + "; ".join(result.issues[:3]) + "."
            text = f"{headline}{more} It's in {result.path}. Used about {result.tokens:,} tokens."
        except Exception as exc:
            self.jobs[key].update(status="failed", now=f"{type(exc).__name__}: {exc}")
            text = f"The build of '{key}' failed: {type(exc).__name__}: {str(exc)[:200]}"
        self._announce(text)

    def _announce(self, text: str) -> None:
        if self.notify:
            try:
                self.notify("Jay · build finished", text)
            except Exception:
                pass
        if self.phone is not None and self.phone.available():
            self.phone.send("Jay · build finished", text)
            self.phone.send_voice(text)

    def status(self, name: str = "") -> str:
        if not self.jobs:
            return "No builds yet."
        rows = [self.jobs[slug(name)]] if name and slug(name) in self.jobs else list(self.jobs.values())
        names = [slug(name)] if name and slug(name) in self.jobs else list(self.jobs)
        return "\n".join(f"{n}: {j['status']} ({j['now']}), started {j['started']}, {j['path']}"
                         for n, j in zip(names, rows))

    def stop(self, name: str) -> str:
        builder = self._builders.get(slug(name))
        if not builder:
            return f"No build called '{name}'."
        builder.stop.set()
        return f"Stopping '{slug(name)}' at the end of its current step."


def register(reg: Registry, builds: Builds) -> None:
    limits = Limits()

    @reg.tool(
        "build_app",
        "Build a complete software project by yourself from one description, working in the background: "
        "a coding agent writes it, runs the tests, a reviewer scores it against the target, and it keeps "
        "fixing and improving until the target is met or a limit is reached. Use it for 'build me an "
        "app/website/API/tool that ...'. Give clear acceptance criteria (what must work). Returns at once; "
        "the result is sent to the user's phone.",
        {"name": {"type": "string", "description": "Short project name."},
         "goal": {"type": "string", "description": "Everything the project should be and do, including the tech stack if the user named one."},
         "acceptance": {"type": "string", "description": "The checks that mean it is done, as a short list."},
         "test_command": {"type": "string", "description": "Optional: the command that runs the tests."}},
        required=("name", "goal"),
        confirm=lambda a: (f"Build '{a['name']}' by itself: up to {limits.rounds} rounds of build, test, review and "
                           f"improve, using your Anthropic credit (budget {limits.tokens:,} tokens)"),
    )
    def build_app(name: str, goal: str, acceptance: str = "", test_command: str = "") -> str:
        return builds.start(name, goal, acceptance, test_command)

    @reg.tool("build_status", "How the background builds are going (or one build, by name).",
              {"name": {"type": "string"}})
    def build_status(name: str = "") -> str:
        return builds.status(name)

    @reg.tool("build_stop", "Stop a background build.", {"name": {"type": "string"}}, required=("name",))
    def build_stop(name: str) -> str:
        return builds.stop(name)
