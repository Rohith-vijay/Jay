"""Model backends. Each one runs the tool loop for a single user turn."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Callable

from .config import Config
from .tools.registry import GROUP_HELP, ConfirmCallback, Registry, Superseded

MAX_STEPS = 40
Transcript = list[tuple[str, str]]          # (role, text) pairs, provider-neutral
ToolNotice = Callable[[str], None]
TOO_MANY_STEPS = "I stopped after too many steps without finishing. Want me to continue?"
NO_VISION = "\n[This model cannot see images. Tell the user to switch to a brain that can, e.g. /model gemini or /model claude.]"


def _short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def describe_call(name: str, args) -> str:
    """One line for the on-screen trace, e.g.  add_task {"title": "Call mom"}"""
    try:
        shown = json.dumps(args, ensure_ascii=False) if args else ""
    except TypeError:
        shown = str(args)
    return _short(f"{name} {shown}".strip())


class ProviderUnavailable(Exception):
    """The backend cannot be reached or is not configured.

    partial is True when tools already ran in this turn, so the turn must not
    be replayed on another backend (actions would happen twice).
    """
    partial = False


def _seed_messages(transcript: Transcript) -> list[dict]:
    if not transcript:
        return []
    lines = "\n".join(f"{role}: {text}" for role, text in transcript)
    return [
        {"role": "user", "content": f"Earlier conversation, for context:\n{lines}"},
        {"role": "assistant", "content": "Understood, I have that context."},
    ]


# --------------------------------------------------------------------------- Claude
class ClaudeProvider:
    name = "claude"

    def __init__(self, cfg: Config, registry: Registry, system: str, client=None):
        import anthropic

        self._anthropic = anthropic
        self.cfg, self.registry, self.system = cfg, registry, system
        # The small, fast model takes the basic web tools and has no server-side refusal fallback.
        self._small = "haiku" in cfg.claude_model
        self.client = client or anthropic.Anthropic()
        self.messages: list[dict] = []
        # Claude gets Anthropic's hosted web tools instead of the local "web" ones.
        self.tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in registry.tools(exclude_tags=("web",))
        ] + [
            {"type": "web_search_20250305" if self._small else "web_search_20260209", "name": "web_search", "max_uses": 5},
            {"type": "web_fetch_20250910" if self._small else "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
        ]

    def seed(self, transcript: Transcript) -> None:
        self.messages = _seed_messages(transcript)

    def _request(self):
        kwargs = dict(
            model=self.cfg.claude_model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": self.cfg.effort},
        )
        if self.cfg.fallbacks and not self._small:
            # If a safety classifier declines, the API retries on a fallback model.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        a = self._anthropic
        try:
            return self.client.beta.messages.create(**kwargs)
        except a.AuthenticationError as exc:
            raise ProviderUnavailable("API key is missing or invalid (set ANTHROPIC_API_KEY in .env).") from exc
        except a.PermissionDeniedError as exc:
            raise ProviderUnavailable(f"API key lacks permission: {exc.message}") from exc
        except a.NotFoundError as exc:
            raise ProviderUnavailable(f"model '{self.cfg.claude_model}' was not found.") from exc
        except a.RateLimitError as exc:
            raise ProviderUnavailable("rate limit reached.") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailable(f"server error ({exc.status_code}).") from exc
            raise
        except a.APIConnectionError as exc:
            raise ProviderUnavailable("cannot reach the API (offline?).") from exc
        except TypeError as exc:
            # No credentials at all surfaces as a TypeError when the SDK builds auth headers.
            if "api_key" in str(exc) or "auth" in str(exc).lower():
                raise ProviderUnavailable("no credentials found (set ANTHROPIC_API_KEY in .env).") from exc
            raise

    def turn(self, user_text: str, confirm_cb: ConfirmCallback, notice: ToolNotice) -> str:
        self.messages.append({"role": "user", "content": user_text})
        ran_tools = False
        for _ in range(MAX_STEPS):
            try:
                resp = self._request()
            except ProviderUnavailable as problem:
                problem.partial = ran_tools
                if not ran_tools:
                    self.messages.pop()
                raise
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()

            if resp.stop_reason == "refusal":
                if not ran_tools:
                    self.messages.pop()  # drop the turn so the history stays valid
                return "I can't help with that request."
            if resp.stop_reason == "max_tokens":
                # Output was cut off; any tool call in it may be incomplete, so run nothing.
                self.messages.append({"role": "assistant", "content": text or "(reply cut off)"})
                return (text + "\n\n" if text else "") + "[My reply hit the length limit and was cut off.]"

            # Keep the full content (thinking and tool blocks included), not just the text.
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue  # a hosted tool is still working; re-send to let it finish
            if resp.stop_reason != "tool_use":
                return text

            if text:
                notice(f"says: {_short(text)}")
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                notice(describe_call(block.name, block.input))
                res = self.registry.execute(block.name, block.input, confirm_cb)
                content: list = [{"type": "text", "text": res.text}]
                if res.jpeg:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(res.jpeg).decode()}})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": res.is_error})
            ran_tools = True
            self.messages.append({"role": "user", "content": results})
        return TOO_MANY_STEPS


# ------------------------------------------------- shared history for chat-style APIs
class _TurnHistory:
    """Keeps history as whole turns so old ones can be dropped to save tokens."""

    def __init__(self, cfg: Config, system: str):
        self.cfg, self.system = cfg, system
        self._seed: list = []
        self.turns: list[list] = []

    def seed(self, transcript: Transcript) -> None:
        self._seed, self.turns = _seed_messages(transcript), []

    @property
    def messages(self) -> list:
        recent = self.turns[-self.cfg.history_turns:]
        seed = self._seed if len(self.turns) <= self.cfg.history_turns else []
        return [{"role": "system", "content": self.system}, *seed, *(m for turn in recent for m in turn)]


# --------------------------------------------- OpenAI-compatible (Gemini, Groq, ...)
ENABLE_TOOLS = "enable_tools"
PRESETS = {
    # The lite models answer in a second or two; the full one is the reserve when their quota runs out.
    "gemini": dict(base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                   key_env="GEMINI_API_KEY", model_env="JARVIS_GEMINI_MODEL",
                   default_model="gemini-3.5-flash-lite,gemini-flash-lite-latest", vision=True,
                   lean=True),     # fewer tools per request answers noticeably faster
    # Groq's free plan allows about 8,000 tokens a minute, so it gets the lean tool set.
    "groq": dict(base_url="https://api.groq.com/openai/v1",
                 key_env="GROQ_API_KEY", model_env="JARVIS_GROQ_MODEL",
                 default_model="openai/gpt-oss-120b,openai/gpt-oss-20b", vision=False, lean=True),
    "openrouter": dict(base_url="https://openrouter.ai/api/v1",
                       key_env="OPENROUTER_API_KEY", model_env="JARVIS_OPENROUTER_MODEL",
                       default_model="", vision=False, lean=True),
}


RATE_LIMIT_WAITS = (20, 40)   # seconds

# A reply that only promises an action ("Let me check the volume settings, one moment") without doing it.
STALLING = re.compile(r"\b(let me (?!know)(check|look|see|find|open|try|pull|have a look|take a look)|"
                      r"one moment|just a moment|hold on|give me a (moment|second)|i'?ll (check|look|find|open|try)|"
                      r"i will (check|look|find|open|try)|checking (now|that)|looking into)\b", re.I)


class OpenAICompatProvider(_TurnHistory):
    _sleep = staticmethod(time.sleep)

    def __init__(self, cfg: Config, registry: Registry, system: str, preset: str, client=None):
        super().__init__(cfg, system)
        import openai

        self._openai = openai
        p = PRESETS[preset]
        self.name, self.registry, self.vision = preset, registry, p["vision"]
        # Several models may be listed, comma separated. Free quotas are per model, so when
        # one runs out the next is used.
        self.models = [m.strip() for m in (os.getenv(p["model_env"]) or p["default_model"]).split(",") if m.strip()]
        if not self.models:
            raise ProviderUnavailable(f"no model chosen (set {p['model_env']} in .env).")
        self.model = self.models[0]
        self._model_cool: dict[str, float] = {}
        if client is None:
            key = os.getenv(p["key_env"])
            if not key:
                raise ProviderUnavailable(f"no API key (set {p['key_env']} in .env).")
            # No silent SDK retries: they made a brain with an exhausted quota hang for minutes.
            # Failures surface at once, so the next model or brain takes over immediately;
            # waiting out a limit mid-task is done visibly in _request_patiently.
            # No kept-alive connections: after a quiet minute a pooled connection is often dead (the router
            # has dropped it), and a request sent down it hangs until it times out (20-30 s) before the
            # retry on a fresh one. A new connection per request costs about 0.15 s instead.
            import httpx

            http = httpx.Client(limits=httpx.Limits(max_keepalive_connections=0, max_connections=12),
                                timeout=httpx.Timeout(20.0, connect=6.0))
            client = openai.OpenAI(api_key=key, base_url=p["base_url"], http_client=http, max_retries=0)
        self.client = client
        # Lean brains (small per-minute token allowance) start with the everyday tools only.
        self.groups: set[str] | None = set() if p.get("lean") else None

    @property
    def tools(self) -> list[dict]:
        offered = [
            {"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self.registry.tools(groups=self.groups)
        ]
        hidden = [] if self.groups is None else [g for g in self.registry.available_groups() if g not in self.groups]
        if hidden:
            offered.append({"type": "function", "function": {
                "name": ENABLE_TOOLS,
                "description": "Switch on a set of extra tools you need for this request. Sets: "
                               + "; ".join(f"{g} = {GROUP_HELP[g]}" for g in hidden) + ".",
                "parameters": {"type": "object", "properties": {"group": {"type": "string", "enum": hidden}},
                               "required": ["group"], "additionalProperties": False}}})
        return offered

    def _usable_models(self) -> list[str]:
        now = time.monotonic()
        return [m for m in self.models if self._model_cool.get(m, 0.0) <= now] or self.models[:1]

    def _next_model(self, cool_for: float = 60.0) -> bool:
        """Benched the current model for a while (a stall or a used-up quota) and move to one that
        is available. It comes back on its own afterwards, so one bad minute doesn't strand the
        assistant on a slower model for the rest of the session."""
        self._model_cool[self.model] = time.monotonic() + cool_for
        usable = [m for m in self._usable_models() if m != self.model]
        if usable:
            self.model = usable[0]
        return bool(usable)

    # Measured speed (seconds per model call, smoothed) and a "don't bother until" time after a
    # rate limit or stall; the agent uses them to pick the quickest brain that is actually up.
    latency: float | None = None
    cool_until: float = 0.0

    def _note_latency(self, seconds: float) -> None:
        self.latency = seconds if self.latency is None else 0.6 * self.latency + 0.4 * seconds

    def _cool(self, seconds: float) -> None:
        self.cool_until = time.monotonic() + seconds

    def _request(self, attempts: int = 3):
        o = self._openai
        self.model = self._usable_models()[0]
        started = time.monotonic()
        try:
            reply = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools)
            took = time.monotonic() - started
            self._note_latency(took)
            self.model_time = getattr(self, "model_time", 0.0) + took
            return reply
        except o.APITimeoutError as exc:
            if self._next_model(45):           # this model is stalling; try the next one
                return self._request(attempts)
            self._cool(20)
            raise ProviderUnavailable("no answer in time.") from exc
        except o.BadRequestError as exc:
            # Some hosts reject the whole reply when the model writes a malformed tool call
            # (e.g. null for an optional field). Asking again usually produces a valid one.
            if "tool" in str(exc).lower() and "fail" in str(exc).lower():
                if attempts > 1:
                    return self._request(attempts - 1)
                raise ProviderUnavailable("the model kept producing invalid tool calls.") from exc
            raise
        except o.AuthenticationError as exc:
            raise ProviderUnavailable("API key was rejected.") from exc
        except o.PermissionDeniedError as exc:
            raise ProviderUnavailable("API key lacks permission for this model.") from exc
        except o.NotFoundError as exc:
            raise ProviderUnavailable(f"model '{self.model}' was not found.") from exc
        except o.RateLimitError as exc:
            if self._next_model():             # this model's quota is used up; try the next one
                return self._request(attempts)
            try:
                wait = float(getattr(exc, "response", None).headers.get("retry-after", 30))
            except (AttributeError, TypeError, ValueError):
                wait = 30.0
            self._cool(min(max(wait, 5.0), 90.0))
            problem = ProviderUnavailable("free-tier rate limit reached.")
            problem.rate_limited = True
            raise problem from exc
        except o.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailable(f"server error ({exc.status_code}).") from exc
            if exc.status_code in (408, 413):       # e.g. a free tier's per-minute size cap: try another brain
                self._cool(60)
                raise ProviderUnavailable("this request is over the model's free-tier size limit.") from exc
            raise
        except o.APIConnectionError as exc:
            raise ProviderUnavailable("cannot reach the API (offline?).") from exc

    def _request_patiently(self, ran_tools: bool, notice: ToolNotice):
        """Mid-task, a per-minute limit is waited out rather than abandoning half-done work."""
        for wait in (*RATE_LIMIT_WAITS, None):
            try:
                return self._request()
            except ProviderUnavailable as problem:
                if not (ran_tools and getattr(problem, "rate_limited", False)) or wait is None:
                    raise
                notice(f"free-tier limit reached; waiting {wait}s before continuing")
                self._sleep(wait)

    _active_turn: list | None = None

    def abandon(self) -> None:
        """A newer request replaces the one in progress: take it out of the history at once, so the
        next request starts clean while the old one winds down on its own."""
        turn, self._active_turn = self._active_turn, None
        if turn is not None and turn in self.turns:
            self.turns.remove(turn)

    def _run_calls(self, parsed, confirm_cb, gen: int | None = None) -> list:
        """Run one step's tool calls. Ones that need no approval run at the same time;
        anything that asks the user runs on its own, in order."""
        from concurrent.futures import ThreadPoolExecutor

        from .tools.registry import ToolResult

        results: list = [None] * len(parsed)
        quick = []
        for i, (call, name, args) in enumerate(parsed):
            if name == ENABLE_TOOLS and self.groups is not None:
                group = (args or {}).get("group")
                if group in self.registry.available_groups():
                    self.groups.add(group)
                    results[i] = ToolResult(f"The {group} tools are now available. Go ahead and use them.")
                else:
                    results[i] = ToolResult(f"No such tool set: {group}.")
            elif self.registry.needs_approval(name, args) or len(parsed) == 1 or self.registry.is_serial(name):
                results[i] = self.registry.execute(name, args, confirm_cb, gen)
            else:
                quick.append(i)
        if quick:
            with ThreadPoolExecutor(max_workers=min(6, len(quick))) as pool:
                futures = {i: pool.submit(self.registry.execute, parsed[i][1], parsed[i][2], confirm_cb, gen) for i in quick}
            for i, future in futures.items():
                results[i] = future.result()
        return results

    def turn(self, user_text: str, confirm_cb: ConfirmCallback, notice: ToolNotice) -> str:
        turn: list = [{"role": "user", "content": user_text}]
        self.turns.append(turn)
        self._active_turn = turn
        gen = self.registry.generation
        ran_tools = False
        nudged = False
        last_result = ""      # if the model does something and then says nothing, report what the tool said
        for _ in range(MAX_STEPS):
            try:
                msg = self._request_patiently(ran_tools, notice).choices[0].message
            except ProviderUnavailable as problem:
                problem.partial = ran_tools
                if not ran_tools and turn in self.turns:
                    self.turns.remove(turn)
                raise
            if gen != self.registry.generation:
                raise Superseded()              # the user has asked for something else meanwhile
            calls = msg.tool_calls or []
            entry: dict = {"role": "assistant", "content": msg.content or ""}
            if calls:
                # Dump each call whole: some providers attach extra fields that must be sent back.
                entry["tool_calls"] = [c.model_dump(exclude_none=True) for c in calls]
            turn.append(entry)
            if not calls:
                spoken = (msg.content or "").strip()
                if not ran_tools and not nudged and STALLING.search(spoken) and len(spoken) < 240:
                    # It said it would do something ("let me check...") but called no tool: make it act.
                    nudged = True
                    turn.append({"role": "user", "content": (
                        "(App note: you said you would do something but called no tool, so nothing happened. "
                        "Do it now with a tool call, or answer directly. Never reply with only 'one moment' "
                        "or 'let me check'.)")})
                    continue
                return spoken or last_result

            reasoning = getattr(msg, "reasoning", None)   # some models return their reasoning
            if reasoning:
                notice(f"thinking: {_short(reasoning)}")
            if msg.content and msg.content.strip():
                notice(f"says: {_short(msg.content)}")
            images: list[bytes] = []
            parsed = []
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = None  # rejected by the registry as "must be a JSON object"
                notice(describe_call(call.function.name, args))
                parsed.append((call, call.function.name, args))
            results = self._run_calls(parsed, confirm_cb, gen)
            if gen != self.registry.generation:
                raise Superseded()
            for (call, name, args), res in zip(parsed, results):
                text = res.text
                if res.jpeg:
                    if self.vision:
                        images.append(res.jpeg)
                    else:
                        text += NO_VISION
                turn.append({"role": "tool", "tool_call_id": call.id, "content": text})
            ran_tools = True
            last_result = next((r.text.strip()[:240] for r in reversed(results) if r.text.strip()), last_result)
            if images:  # tool messages can't carry pictures in this API; attach them as a user message
                turn.append({"role": "user", "content": [
                    {"type": "text", "text": "Image returned by the tool above:"},
                    *({"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.standard_b64encode(j).decode()}}
                      for j in images)]})
        return TOO_MANY_STEPS


# --------------------------------------------------------------------------- Ollama
class OllamaProvider(_TurnHistory):
    name = "ollama"

    def __init__(self, cfg: Config, registry: Registry, system: str, client=None):
        super().__init__(cfg, system)
        import ollama

        self.registry = registry
        self.client = client or ollama.Client()
        self.tools = [
            {"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in registry.tools()
        ]

    def _request(self):
        try:
            return self.client.chat(model=self.cfg.ollama_model, messages=self.messages, tools=self.tools)
        except ConnectionError as exc:
            raise ProviderUnavailable("not running (install Ollama and run 'ollama serve').") from exc
        except Exception as exc:
            if type(exc).__name__ == "ResponseError":
                raise ProviderUnavailable(
                    f"{exc}. If the model is missing, run: ollama pull {self.cfg.ollama_model}") from exc
            if "connect" in str(exc).lower():
                raise ProviderUnavailable("not running (install Ollama and run 'ollama serve').") from exc
            raise

    def turn(self, user_text: str, confirm_cb: ConfirmCallback, notice: ToolNotice) -> str:
        turn: list = [{"role": "user", "content": user_text}]
        self.turns.append(turn)
        ran_tools = False
        for _ in range(MAX_STEPS):
            try:
                msg = self._request().message
            except ProviderUnavailable as problem:
                problem.partial = ran_tools
                if not ran_tools:
                    self.turns.pop()
                raise
            turn.append(msg)
            if not msg.tool_calls:
                return (msg.content or "").strip()
            for call in msg.tool_calls:
                name = call.function.name
                notice(describe_call(name, dict(call.function.arguments or {})))
                res = self.registry.execute(name, dict(call.function.arguments or {}), confirm_cb)
                turn.append({"role": "tool", "content": res.text + (NO_VISION if res.jpeg else ""),
                             "tool_name": name})
            ran_tools = True
        return TOO_MANY_STEPS


PROVIDERS: dict[str, Callable] = {
    "claude": ClaudeProvider,
    "ollama": OllamaProvider,
    **{name: (lambda cfg, reg, system, _n=name: OpenAICompatProvider(cfg, reg, system, _n)) for name in PRESETS},
}
