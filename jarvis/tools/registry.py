"""One tool registry shared by every model provider."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator

from ..security import UNTRUSTED_TOOLS, mark_untrusted, strip_credentials

MAX_OUTPUT = 20_000
LOOP_REPEATS = 3          # the same call giving the same answer this many times in a row is a loop
LOOP_WINDOW = 120.0       # seconds

# Tools that are only needed now and then. Brains with a small token allowance get the
# everyday tools up front and switch a group on (with the enable_tools tool) when needed.
TOOL_GROUPS = {
    "files": ("list_directory", "search_files", "read_file", "write_file", "edit_file"),
    "system": ("run_powershell", "system_status", "take_screenshot", "type_text", "press_keys"),
    "browser": ("browser_open", "browser_read", "browser_links", "browser_click", "browser_type",
                "browser_press", "check_site", "open_portals", "browser_tabs", "browser_switch"),
    "coding": ("git_status", "git_diff"),
    "knowledge": ("read_document", "learn_document", "learn_folder", "save_note", "list_knowledge", "remove_knowledge"),
    "whatsapp": ("whatsapp_unread", "whatsapp_read", "whatsapp_send"),
    "windows": ("list_windows", "window_control", "read_clipboard", "write_clipboard", "set_brightness",
                "power_action", "notify"),
    "google": ("calendar_list_events", "calendar_create_event", "gmail_list", "gmail_read", "gmail_send"),
    "mail": ("mail_list", "mail_read", "mail_send", "calendar_feed"),
    "phone": ("message_phone", "setup_phone_alerts"),
    "creator": ("make_video", "video_status", "video_stop", "content_create", "content_revise", "content_get",
                "content_list", "publish_content", "build_app", "build_status", "build_stop",
                "instagram_setup", "youtube_setup"),
    "automation": ("grant_permission", "list_permissions", "add_routine", "list_routines", "remove_routine",
                   "install_skill", "use_skill", "list_skills"),
}
GROUP_HELP = {
    "files": "find, list, read and write files on the PC",
    "system": "run PowerShell, system status, see the screen, type and press keys in apps",
    "browser": "drive the assistant's own browser window (ERP portal, dashboards, messaging and job sites)",
    "whatsapp": "read the user's WhatsApp chats and send replies",
    "windows": "manage open windows, clipboard, brightness, lock/sleep/shutdown, notifications",
    "coding": "git status and uncommitted changes of the user's code projects",
    "knowledge": "add documents and notes to the user's knowledge base (searching it is always available)",
    "google": "Google Calendar and Gmail",
    "mail": "the user's inbox (read, search, send) and calendar feeds / timetable",
    "phone": "send a message or file to the user's phone; set up phone notifications",
    "creator": "make videos, write and publish content (Instagram, YouTube), build apps in the project folder",
    "automation": "permissions, recurring routines, and installable skills",
}
_GROUP_OF = {name: group for group, names in TOOL_GROUPS.items() for name in names}

# Things only the owner may do. When someone else's voice (or an unknown voice) is
# speaking, these are refused, whatever the brain decides.
PRIVATE_TOOLS = {
    "whatsapp_unread", "whatsapp_read", "whatsapp_send",
    "gmail_list", "gmail_read", "gmail_send", "calendar_list_events", "calendar_create_event",
    "run_powershell", "list_directory", "search_files", "read_file", "write_file", "edit_file",
    "type_text", "press_keys", "take_screenshot", "look_at_screen",
    "browser_open", "browser_read", "browser_links", "browser_click", "browser_type", "browser_press", "check_site",
    "open_portals", "browser_tabs", "browser_switch",
    "update_task", "complete_task", "delete_task", "forget", "learn_document", "remove_knowledge",
    "forget_voice", "set_owner_voice", "read_document", "learn_folder",
    "list_windows", "window_control", "read_clipboard", "write_clipboard", "power_action", "search_history",
    "mail_list", "mail_read", "mail_send", "calendar_feed", "message_phone", "setup_phone_alerts",
    "update_profile", "add_watch", "remove_watch", "git_status", "git_diff", "check_setup", "publish_content", "make_video", "video_status", "video_stop", "content_create", "content_list", "content_get", "content_revise", "add_routine", "list_routines", "remove_routine",
    "build_app", "build_stop", "build_status", "instagram_account", "instagram_posts", "instagram_insights", "youtube_channel", "youtube_videos", "youtube_insights", "youtube_video_report", "grant_permission", "list_permissions",
}
ConfirmCallback = Callable[[str], bool]


@dataclass
class ToolImage:
    """Return this from a tool to hand the model a picture along with text."""
    text: str
    jpeg: bytes


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    jpeg: bytes | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    # If set, returns a one-line summary of the action; the user must approve it first.
    confirm: Callable[[dict], str] | None = None
    tags: frozenset = frozenset()
    # If set and it returns False, the tool is not offered to the model (saves tokens).
    enabled: Callable[[], bool] | None = None


class Superseded(Exception):
    """The user gave a newer request while this one was still being worked on: drop this one."""


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.remote = False                     # True while answering a request that came from the phone
        self.tools_run = 0                      # tools actually started (tells whether a request has done anything yet)
        self.generation = 0                     # raised whenever a new request replaces the one in progress
        self._serial = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jay-serial")
        self.cancelled = threading.Event()      # set when the user says stop: no further actions run
        self.speaker_is_owner = True            # False while a non-owner's voice is being answered
        self.on_result = None                   # the app shows some results as cards
        self.grants = None                      # standing permissions (set by build_registry)
        self.last_confirm = (None, "")          # (tool, summary) of the approval being asked, for 'always allow'
        self._recent: dict = {}                 # loop guard: call -> (times in a row, last output, when)

    def tool(self, name: str, description: str, properties: dict | None = None,
             required: tuple[str, ...] = (), confirm=None, tags: tuple[str, ...] = (), enabled=None):
        schema = {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        }

        def decorator(func):
            self._tools[name] = Tool(name, description, schema, func, confirm, frozenset(tags), enabled)
            return func

        return decorator

    def tools(self, exclude_tags: tuple[str, ...] = (), groups: set[str] | None = None) -> list[Tool]:
        """Tools to offer. groups=None offers everything; otherwise only the everyday tools
        plus the named occasional groups."""
        skip = set(exclude_tags)
        return [t for t in self._tools.values()
                if not (t.tags & skip) and (t.enabled is None or t.enabled())
                and (groups is None or _GROUP_OF.get(t.name) is None or _GROUP_OF[t.name] in groups)]

    def available_groups(self) -> list[str]:
        offered = {t.name for t in self.tools()}
        return [g for g, names in TOOL_GROUPS.items() if offered & set(names)]

    def is_serial(self, name: str) -> bool:
        """Tools bound to one thread (the browser and WhatsApp drive a single window), so a step
        that calls several of them must run them one after another, never at the same time."""
        return _GROUP_OF.get(name) in ("browser", "whatsapp")

    def needs_approval(self, name: str, args) -> bool:
        tool = self._tools.get(name)
        if tool is None or tool.confirm is None or not isinstance(args, dict):
            return False
        try:
            return tool.confirm(args) is not None
        except Exception:
            return True

    def run_serial(self, fn, *args):
        """Run something on the one thread that owns the browser (it can only be driven from there)."""
        return self._serial.submit(fn, *args).result()

    def execute(self, name: str, args: dict, confirm_cb: ConfirmCallback, gen: int | None = None) -> ToolResult:
        if gen is not None and gen != self.generation:
            return ToolResult("That request was replaced by a newer one. Do nothing.")
        result = self._execute(name, args, confirm_cb)
        if self.on_result is not None:
            try:
                self.on_result(name, args, result)
            except Exception:
                pass
        return result

    def _execute(self, name: str, args: dict, confirm_cb: ConfirmCallback) -> ToolResult:
        if self.cancelled.is_set():
            return ToolResult("The user said stop. Do nothing further and reply with just: Stopped.")
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'.", True)
        if (name in PRIVATE_TOOLS or name.startswith("mcp_")) and not self.speaker_is_owner:
            return ToolResult("Refused: only the owner can do this, and the person speaking is not the owner "
                              "(by voice). Tell them politely that the owner needs to ask for it.", True)
        if not isinstance(args, dict):
            return ToolResult("Tool input must be a JSON object.", True)
        # Some models send null for options they mean to leave out.
        args = {k: v for k, v in args.items() if v is not None}
        errors = [e.message for e in Draft202012Validator(tool.parameters).iter_errors(args)]
        if errors:
            return ToolResult("Invalid tool input: " + "; ".join(errors), True)
        key = name + json.dumps(args, sort_keys=True, default=str)
        seen = self._recent.get(key)
        if seen and seen[0] >= LOOP_REPEATS and time.monotonic() - seen[2] < LOOP_WINDOW:
            return ToolResult(f"Loop guard: you have called {name} with these same inputs {seen[0]} times and got the "
                              "same answer each time. Stop repeating it: use what you already have, try something "
                              "different, or tell the user what is blocking you.", True)
        summary = tool.confirm(args) if tool.confirm is not None else None
        if summary is not None:                 # a confirm function may return None for harmless cases
            granted = self.grants.allows(name, summary, remote=self.remote) if self.grants is not None else None
            if granted is not None:
                self.grants.used(granted, name, summary)         # the user said yes to this kind of thing already
            else:
                self.last_confirm = (name, summary)
                if not confirm_cb(summary):
                    return ToolResult("The user declined this action. Do not retry it unless they ask.")
        self.tools_run += 1
        try:
            if self.is_serial(name):
                out = self.run_serial(lambda: tool.func(**args))
            else:
                out = tool.func(**args)
        except Exception as exc:  # tool failures go back to the model, not the user
            return ToolResult(f"{type(exc).__name__}: {exc}", True)
        if isinstance(out, ToolImage):
            return ToolResult(strip_credentials(out.text), False, out.jpeg)
        out = strip_credentials(str(out))              # the model never sees keys or tokens
        count, last, _ = self._recent.get(key, (0, None, 0.0))
        self._recent[key] = (count + 1 if out == last else 1, out, time.monotonic())
        if len(self._recent) > 200:
            self._recent.clear()
        if name in UNTRUSTED_TOOLS or name.startswith("mcp_"):
            out = mark_untrusted(name, out)            # outside text that gives orders is flagged as data
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n[output truncated: {len(out) - MAX_OUTPUT} more characters]"
        return ToolResult(out or "(no output)")
