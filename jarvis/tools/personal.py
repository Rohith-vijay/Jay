"""Long-term memory, the user's schedule, learned work patterns, and a calculator."""
from __future__ import annotations

import ast
import math
import operator
from datetime import datetime, timedelta

from ..store import PRIORITIES, REPEATS, UNSET, Store, describe_task
from ..profile import FIELDS as PROFILE_FIELDS
from ..profile import Profile
from .registry import Registry

WHEN = "Local time as ISO 8601, e.g. 2026-11-16T09:30."

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {"pi": math.pi, "e": math.e}
_FUNCS = {n: getattr(math, n) for n in ("sqrt", "log", "log10", "log2", "exp", "sin", "cos", "tan",
                                         "floor", "ceil", "factorial")} | {"abs": abs, "round": round,
                                                                          "min": min, "max": max}


def calculate_expression(expression: str):
    """Evaluate arithmetic safely (no names or calls beyond a fixed maths list)."""
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _NAMES:
            return _NAMES[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
                and not node.keywords:
            return _FUNCS[node.func.id](*(ev(a) for a in node.args))
        raise ValueError("only numbers, + - * / // % **, brackets and basic maths functions are allowed")

    return ev(ast.parse(expression.replace("^", "**"), mode="eval").body)


def register(reg: Registry, store: Store, data_dir) -> None:
    # ---- memory ----------------------------------------------------------
    @reg.tool(
        "remember",
        "Save a lasting fact about the user (preferences, people, routines, projects, interests, "
        "patterns you notice in how they work) so it is available in future conversations.",
        {
            "fact": {"type": "string", "description": "One self-contained sentence."},
            "category": {"type": "string",
                         "description": "Short label, e.g. 'preference', 'person', 'work', 'interest', 'pattern'."},
        },
        required=("fact",),
    )
    def remember(fact: str, category: str = "general") -> str:
        return f"Saved as memory #{store.add_fact(fact, category)}."

    @reg.tool(
        "update_profile",
        "Update the owner's profile: the core facts you always keep in mind. Fields: " + ", ".join(PROFILE_FIELDS)
        + ". 'personality' is your own evidence-based picture of them (strengths, habits, blind spots), "
        "revised as you learn. Pass the whole new value for a field (merge in what's already there; "
        "an empty value clears it).",
        {"field": {"type": "string", "enum": list(PROFILE_FIELDS)}, "value": {"type": "string"}},
        required=("field", "value"),
    )
    def update_profile(field: str, value: str) -> str:
        profile = Profile(data_dir)
        profile.update({field: value})
        return f"Profile updated: {PROFILE_FIELDS[field][0]} = {value or '(cleared)'}"

    @reg.tool(
        "recall",
        "Search saved long-term memories by keyword. Memories saved earlier in this same "
        "conversation only show up here, not in your starting notes.",
        {"query": {"type": "string"}},
        required=("query",),
    )
    def recall(query: str) -> str:
        rows = store.search_facts(query)
        return "\n".join(f"#{r['id']} [{r['category']}] {r['fact']}" for r in rows) or "Nothing saved about that."

    @reg.tool(
        "forget",
        "Delete a saved memory by its number (for example when it is out of date).",
        {"memory_id": {"type": "integer"}},
        required=("memory_id",),
    )
    def forget(memory_id: int) -> str:
        return "Deleted." if store.delete_fact(memory_id) else f"No memory #{memory_id}."

    # ---- schedule --------------------------------------------------------
    task_fields = {
        "title": {"type": "string"},
        "due": {"type": "string", "description": "Hard deadline. " + WHEN},
        "start": {"type": "string",
                  "description": "When the user should start working on it; they are reminded then. " + WHEN},
        "minutes": {"type": "integer", "description": "Estimated minutes of work."},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "repeat": {"type": "string", "enum": list(REPEATS),
                   "description": "For routines and recurring reminders, e.g. 'daily' for a 7am gym "
                                  "reminder. Each time it comes round, it moves itself to the next one."},
    }

    @reg.tool(
        "add_task",
        "Add something to the user's schedule, or a reminder. 'due' is the deadline; 'start' is the "
        "planned time to work on it. The user is reminded out loud at 'start' (or at 'due' if no "
        "start is set). Use 'repeat' for routines ('every morning', 'each Monday').",
        task_fields, required=("title",),
    )
    def add_task(title: str, due: str | None = None, start: str | None = None,
                 minutes: int | None = None, priority: str = "normal", repeat: str | None = None) -> str:
        task_id = store.add_task(title, due, start, minutes, priority, repeat)
        return "Added: " + describe_task(store.get_task(task_id))

    @reg.tool(
        "set_timer",
        "Start a countdown timer, e.g. 'ten minute timer for the tea'. The user is alerted when it ends.",
        {"minutes": {"type": "number", "description": "Length in minutes (0.5 = thirty seconds)."},
         "label": {"type": "string", "description": "What it's for."}},
        required=("minutes",),
    )
    def set_timer(minutes: float, label: str = "Timer") -> str:
        if not 0 < minutes <= 24 * 60:
            raise ValueError("A timer must be between a few seconds and a day long.")
        ends = datetime.now() + timedelta(minutes=minutes)
        task_id = store.add_task(f"⏱ {label}", start=ends.isoformat(timespec="seconds"))
        store._run("UPDATE tasks SET start = ? WHERE id = ?", (ends.isoformat(timespec="seconds"), task_id))
        return f"Timer set: {label}, ends at {ends:%H:%M:%S}."

    @reg.tool(
        "search_history",
        "Search everything said in past conversations with the user, by keyword, newest first. Use "
        "it for 'what did I tell you about…', 'what did we decide last week…', or to recover "
        "context that isn't in your memories.",
        {"query": {"type": "string"}},
        required=("query",),
    )
    def search_history(query: str) -> str:
        rows = store.search_log(query)
        return "\n".join(f"{r['ts']} {r['role']}: {' '.join(r['text'].split())[:300]}" for r in rows) \
            or "Nothing in past conversations matches."

    @reg.tool(
        "update_task",
        "Change a task: rename it, move its planned start or deadline, change its estimate or "
        "priority. Use this to reschedule and re-plan. Only the fields you pass are changed; "
        "pass an empty string for 'due' or 'start' to clear it.",
        {"task_id": {"type": "integer"}, **task_fields}, required=("task_id",),
    )
    def update_task(task_id: int, title=UNSET, due=UNSET, start=UNSET, minutes=UNSET, priority=UNSET,
                    repeat=UNSET) -> str:
        due = None if due == "" else due
        start = None if start == "" else start
        repeat = None if repeat == "" else repeat
        if not store.update_task(task_id, title, due, start, minutes, priority, repeat):
            return f"No task #{task_id}."
        return "Updated: " + describe_task(store.get_task(task_id))

    @reg.tool(
        "list_tasks",
        "List the user's schedule: open tasks with their planned start, estimate, priority and deadline.",
        {"include_done": {"type": "boolean"}},
    )
    def list_tasks(include_done: bool = False) -> str:
        return "\n".join(describe_task(t) for t in store.list_tasks(include_done)) or "No tasks."

    @reg.tool(
        "complete_task",
        "Mark a task as done by its number.",
        {"task_id": {"type": "integer"}},
        required=("task_id",),
    )
    def complete_task(task_id: int) -> str:
        return "Marked done." if store.complete_task(task_id) else f"No task #{task_id}."

    @reg.tool(
        "delete_task",
        "Remove a task that is no longer needed (not done, just dropped).",
        {"task_id": {"type": "integer"}},
        required=("task_id",),
    )
    def delete_task(task_id: int) -> str:
        return "Deleted." if store.delete_task(task_id) else f"No task #{task_id}."

    @reg.tool(
        "get_work_patterns",
        "Numbers about how the user actually works, measured from their history: how often "
        "deadlines are missed and by how much, what time of day work gets done, how real "
        "durations compare with estimates, what keeps getting postponed, when they are active. "
        "Check this before planning or rescheduling so plans fit their real habits.")
    def get_work_patterns() -> str:
        return store.work_patterns()

    # ---- calculator ------------------------------------------------------
    @reg.tool(
        "calculate",
        "Work out arithmetic exactly. Use it for any calculation instead of doing it in your head.",
        {"expression": {"type": "string", "description": "e.g. '(1250 * 0.18) + 40' or 'sqrt(2) * 10'."}},
        required=("expression",),
    )
    def calculate(expression: str) -> str:
        return str(calculate_expression(expression))
