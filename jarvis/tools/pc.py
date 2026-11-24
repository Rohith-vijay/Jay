"""Tools that act on this Windows PC."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ..security import check_file_allowed, check_url, command_touches_secrets
from .registry import Registry, ToolImage

MAX_READ = 100_000

# Windows virtual-key codes for the media keys found on most keyboards.
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "mute_toggle": VK_VOLUME_MUTE}
KEYEVENTF_KEYUP = 0x0002


def press_key(vk: int, times: int = 1) -> None:
    import ctypes

    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


KEY_NAMES = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def parse_combo(combo: str) -> list[int]:
    """'ctrl+shift+s' -> virtual-key codes, in press order."""
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in KEY_NAMES:
            codes.append(KEY_NAMES[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"Unknown key '{part}'. Use names like ctrl, alt, shift, win, enter, tab, esc, f5, a, 1.")
    return codes


def press_combo(codes: list[int]) -> None:
    import ctypes

    for vk in codes:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(codes):
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def type_unicode(text: str) -> None:
    """Type text into the focused window, any script (English, Telugu, ...)."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def send(scan: int, flags: int, vk: int = 0) -> None:
        event = INPUT(type=1, u=_U(ki=KEYBDINPUT(vk, scan, flags, 0, None)))
        ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            send(0, 0, 0x0D), send(0, KEYEVENTF_KEYUP, 0x0D)
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):          # one event per UTF-16 unit
            unit = int.from_bytes(data[i:i + 2], "little")
            send(unit, 0x0004), send(unit, 0x0004 | KEYEVENTF_KEYUP)


# Things that run code when "opened". Opening a plain document, folder, app name or short web link is
# instant; these ask first, because text from a web page or an email could try to start them.
RISKY_EXTENSIONS = {".exe", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
                    ".wsf", ".wsh", ".hta", ".msi", ".msp", ".lnk", ".jar", ".reg", ".cpl", ".dll", ".pif",
                    ".appx", ".msix", ".application", ".gadget", ".url", ".inf", ".sct"}
RISKY_NAMES = {"cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "regedit", "rundll32", "regsvr32",
               "msiexec", "bash", "wsl", "python", "pythonw", "py", "node", "certutil", "bitsadmin", "schtasks"}


def open_risk(target: str) -> str | None:
    """Why opening this needs the user's yes (None = fine to open straight away)."""
    t = os.path.expandvars(str(target)).strip().strip('"')
    if not t:
        return None
    if t.startswith("\\\\"):
        return f"Open the network path {t} (this can hand your Windows sign-in to another computer)"
    scheme = re.match(r"^([a-z][a-z0-9+.-]+):", t, re.I)
    if scheme:
        kind = scheme.group(1).lower()
        if kind in ("http", "https"):
            link = urlparse(t)
            host = link.hostname or ""
            plain = (not link.query and not link.fragment and not link.username and len(link.path) <= 80
                     and len(host) <= 60 and host.count(".") <= 4)
            if plain and check_url(t) is None:
                return None
            return f"Open the link {t[:300]} in your browser"
        if kind == "mailto":
            return None
        return f"Open {t[:300]} (a {kind}: link, which can start other programs)"
    name = Path(t).name.lower()
    if Path(name).suffix in RISKY_EXTENSIONS or name in RISKY_NAMES:
        return f"Run {t}"
    return None


def _path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def register(reg: Registry, data_dir: Path) -> None:
    @reg.tool(
        "open_app",
        "Open an application, file, folder or URL on the user's PC, the same way the Windows "
        "Run dialog would. Examples: 'notepad', 'chrome', 'calc', 'C:\\\\Users\\\\me\\\\report.pdf', "
        "'https://example.com'.",
        {"target": {"type": "string", "description": "Program name, full path, or URL."}},
        required=("target",),
        confirm=lambda a: open_risk(a.get("target", "")),
    )
    def open_app(target: str) -> str:
        os.startfile(os.path.expandvars(target))  # noqa: S606 - Windows only, by design
        return f"Opened {target}."

    @reg.tool(
        "run_powershell",
        "Run a PowerShell command on the user's PC and return its output. Use for anything the "
        "other tools cannot do (system settings, processes, installs). The user is asked to "
        "approve every command, so give one clear command rather than many small ones.",
        {"command": {"type": "string", "description": "The PowerShell command to run."}},
        required=("command",),
        confirm=lambda a: (("WARNING, this names a secrets file (keys, tokens, cookies). " if command_touches_secrets(a["command"]) else "")
                           + f"Run PowerShell: {a['command']}"),
    )
    def run_powershell(command: str) -> str:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=120, errors="replace",
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return f"exit code {proc.returncode}\n{out}" + (f"\nstderr:\n{err}" if err else "")

    @reg.tool(
        "list_directory",
        "List the files and folders inside a directory.",
        {"path": {"type": "string", "description": "Directory path. '~' means the home folder."}},
        required=("path",),
    )
    def list_directory(path: str) -> str:
        entries = sorted(_path(path).iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        lines = [f"{'[dir] ' if e.is_dir() else '      '}{e.name}" for e in entries[:500]]
        more = f"\n... and {len(entries) - 500} more" if len(entries) > 500 else ""
        return "\n".join(lines) + more if lines else "(empty)"

    @reg.tool(
        "search_files",
        "Find files by name under a folder, searching sub-folders too.",
        {
            "root": {"type": "string", "description": "Folder to search in."},
            "pattern": {"type": "string", "description": "Glob such as '*.pdf' or '*invoice*'."},
        },
        required=("root", "pattern"),
    )
    def search_files(root: str, pattern: str) -> str:
        hits = []
        for p in _path(root).rglob(pattern):
            hits.append(str(p))
            if len(hits) >= 200:
                hits.append("... stopped at 200 results")
                break
        return "\n".join(hits) or "No matches."

    @reg.tool(
        "read_file",
        "Read a text file or a PDF (for example the user's resume) from the user's PC.",
        {"path": {"type": "string"}},
        required=("path",),
    )
    def read_file(path: str) -> str:
        check_file_allowed(_path(path))
        p = _path(path)
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
        size = p.stat().st_size
        if size > MAX_READ:
            return (f"File is {size:,} bytes, larger than the {MAX_READ:,} byte limit. "
                    "Tell the user and ask how they want to proceed.")
        return p.read_text(encoding="utf-8", errors="replace")

    @reg.tool(
        "write_file",
        "Create or overwrite a text file on the user's PC. The user is asked to approve it.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        required=("path", "content"),
        confirm=lambda a: (f"{'Overwrite' if _path(a['path']).exists() else 'Create'} file "
                           f"{a['path']} ({len(a['content'])} characters)"),
    )
    def write_file(path: str, content: str) -> str:
        check_file_allowed(_path(path))
        p = _path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {p}."

    @reg.tool(
        "edit_file",
        "Change part of a text file: replace one exact piece of text with another, leaving the rest of the "
        "file untouched. Safer than write_file for small changes, because it cannot lose the rest. Read the "
        "file first and copy `old` exactly; it must appear once. The user is asked to approve it.",
        {"path": {"type": "string"},
         "old": {"type": "string", "description": "The exact text to replace (must occur exactly once)."},
         "new": {"type": "string", "description": "What to put there instead (may be empty to delete)."}},
        required=("path", "old", "new"),
        confirm=lambda a: f"Edit {a['path']}: replace {len(a['old'])} characters with {len(a['new'])}",
    )
    def edit_file(path: str, old: str, new: str) -> str:
        p = _path(path)
        check_file_allowed(p)
        if not old:
            return "`old` is empty; say which text to replace."
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return "That text isn't in the file. Read the file again and copy it exactly."
        if count > 1:
            return f"That text appears {count} times; include more around it so it matches once."
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Edited {p}."

    @reg.tool("system_status", "Report CPU, memory, disk and battery status of the user's PC.")
    def system_status() -> str:
        import psutil

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        lines = [
            f"CPU: {psutil.cpu_percent(interval=0.5)}% of {psutil.cpu_count()} cores",
            f"Memory: {mem.percent}% used ({mem.used / 2**30:.1f} / {mem.total / 2**30:.1f} GB)",
            f"System disk: {disk.percent}% used ({disk.free / 2**30:.0f} GB free)",
        ]
        battery = psutil.sensors_battery()
        if battery:
            state = "charging" if battery.power_plugged else "on battery"
            lines.append(f"Battery: {battery.percent:.0f}% ({state})")
        return "\n".join(lines)

    @reg.tool(
        "look_at_screen",
        "See what is currently on the user's screen. Returns a picture of the screen for you to "
        "look at. Use it when the user asks about something they are looking at.")
    def look_at_screen() -> ToolImage:
        import io

        from PIL import ImageGrab

        shot = ImageGrab.grab().convert("RGB")
        shot.thumbnail((1568, 1568))  # smaller picture, far fewer tokens
        buf = io.BytesIO()
        shot.save(buf, "JPEG", quality=80)
        return ToolImage("Screenshot of the user's screen, taken just now.", buf.getvalue())

    @reg.tool(
        "media_control",
        "Control whatever is playing music or video on the PC, and mute.",
        {"action": {"type": "string", "enum": sorted(MEDIA_KEYS)}},
        required=("action",),
    )
    def media_control(action: str) -> str:
        press_key(MEDIA_KEYS[action])
        return f"Sent '{action}'."

    @reg.tool(
        "set_volume",
        "Set the PC's master volume to a percentage (0-100).",
        {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
        required=("percent",),
    )
    def set_volume(percent: int) -> str:
        # Each volume key press moves Windows volume by 2%: drop to zero, then step up.
        press_key(VK_VOLUME_DOWN, times=50)
        press_key(VK_VOLUME_UP, times=round(percent / 2))
        return f"Volume set to about {round(percent / 2) * 2}%."

    @reg.tool(
        "type_text",
        "Type text into whichever app window is currently in front, as if the user typed it "
        "(works for English and Telugu). Open or focus the right app first. The user approves it.",
        {"text": {"type": "string"}},
        required=("text",),
        confirm=lambda a: f"Type into the active window: {a['text']}",
    )
    def type_text(text: str) -> str:
        type_unicode(text)
        return f"Typed {len(text)} characters."

    @reg.tool(
        "press_keys",
        "Press a key or shortcut in the app that is in front, e.g. 'enter', 'ctrl+s', 'alt+tab', "
        "'win+d'. The user approves it.",
        {"keys": {"type": "string", "description": "Keys joined with +."}},
        required=("keys",),
        confirm=lambda a: f"Press {a['keys']} in the active window",
    )
    def press_keys(keys: str) -> str:
        press_combo(parse_combo(keys))
        return f"Pressed {keys}."

    @reg.tool("take_screenshot", "Save a screenshot of the screen to a PNG file and return its path.")
    def take_screenshot() -> str:
        from PIL import ImageGrab

        folder = data_dir / "screenshots"
        folder.mkdir(exist_ok=True)
        out = folder / f"screen-{datetime.now():%Y%m%d-%H%M%S}.png"
        ImageGrab.grab().save(out)
        return f"Saved screenshot to {out}."
