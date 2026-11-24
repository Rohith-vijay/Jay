"""More control of the laptop: launch any installed app by name, manage windows, clipboard,
screen brightness, lock / sleep / shut down, and Windows notifications."""
from __future__ import annotations

import ctypes
import difflib
import json
import os
import subprocess
import time
from ctypes import wintypes

from .registry import Registry

user32 = ctypes.windll.user32 if os.name == "nt" else None
WM_CLOSE = 0x0010
SW_MINIMIZE, SW_RESTORE, SW_MAXIMIZE = 6, 9, 3
_apps_cache: dict = {"at": 0.0, "apps": []}


def powershell(command: str, timeout: int = 30) -> str:
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=timeout, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 and proc.stderr.strip():
        raise RuntimeError(proc.stderr.strip().splitlines()[0])
    return proc.stdout


def installed_apps() -> list[dict]:
    """Everything in the Start menu (desktop programs and Store apps), cached for ten minutes."""
    if time.time() - _apps_cache["at"] > 600 or not _apps_cache["apps"]:
        raw = powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
        apps = json.loads(raw) if raw.strip() else []
        _apps_cache.update(at=time.time(), apps=apps if isinstance(apps, list) else [apps])
    return _apps_cache["apps"]


def best_app(name: str, apps: list[dict]) -> dict | None:
    """Pick the installed app the user most likely means ('chrome' -> 'Google Chrome')."""
    wanted = name.lower().strip()
    names = {a["Name"].lower(): a for a in apps}
    if wanted in names:
        return names[wanted]
    starts = [a for n, a in names.items() if n.startswith(wanted) or n.split(" ")[-1] == wanted]
    if starts:
        return min(starts, key=lambda a: len(a["Name"]))
    contains = [a for n, a in names.items() if wanted in n]
    if contains:
        return min(contains, key=lambda a: len(a["Name"]))
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


def top_windows() -> list[tuple[int, str]]:
    """Visible, titled top-level windows: (handle, title)."""
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):    # skip owned pop-ups
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.strip() and buf.value not in ("Program Manager", "Windows Input Experience"):
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def find_window(title: str) -> tuple[int, str]:
    wanted = title.lower().strip()
    matches = [(h, t) for h, t in top_windows() if wanted in t.lower()]
    if not matches:
        raise ValueError(f"No open window matches '{title}'. Use list_windows to see what's open.")
    return min(matches, key=lambda m: len(m[1]))


def notify(title: str, message: str) -> None:
    """A Windows notification in the corner of the screen (shown even when Jay's window is closed)."""
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")  # noqa: E731
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text>"
        f"<text>{esc(message)}</text></binding></visual><audio src=\"ms-winsoundevent:Notification.Reminder\"/></toast>');"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


POWER = {
    "lock": ("Lock the screen", lambda: user32.LockWorkStation()),
    "sleep": ("Put the laptop to sleep", lambda: subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Windows.Forms;"
         "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"])),
    "shutdown": ("Shut the laptop down in 60 seconds", lambda: subprocess.Popen(["shutdown", "/s", "/t", "60"])),
    "restart": ("Restart the laptop in 60 seconds", lambda: subprocess.Popen(["shutdown", "/r", "/t", "60"])),
    "cancel_shutdown": ("Cancel a pending shutdown or restart", lambda: subprocess.Popen(["shutdown", "/a"])),
}


def register(reg: Registry) -> None:
    @reg.tool(
        "launch_app",
        "Open any installed app by its everyday name ('spotify', 'vs code', 'whatsapp', 'calculator', "
        "'settings'), matching what's in the Start menu. Use open_app instead for files, folders and web links.",
        {"name": {"type": "string"}},
        required=("name",),
    )
    def launch_app(name: str) -> str:
        app = best_app(name, installed_apps())
        if app is None:
            close = difflib.get_close_matches(name.lower(), [a["Name"].lower() for a in installed_apps()], n=4, cutoff=0.4)
            hint = f" Closest installed: {', '.join(close)}." if close else ""
            return f"No installed app matches '{name}'.{hint}"
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"])
        return f"Opened {app['Name']}."

    @reg.tool("list_windows", "List the windows open on the screen right now (their titles).")
    def list_windows() -> str:
        return "\n".join(title for _, title in top_windows()) or "No windows are open."

    @reg.tool(
        "window_control",
        "Bring a window to the front, minimise, maximise or close it, found by part of its title "
        "(see list_windows). Closing asks the app to close normally, so it can ask to save; the "
        "user approves a close.",
        {"title": {"type": "string"}, "action": {"type": "string", "enum": ["focus", "minimise", "maximise", "close"]}},
        required=("title", "action"),
        confirm=lambda a: f"Close the window '{a['title']}'" if a.get("action") == "close" else None,
    )
    def window_control(title: str, action: str) -> str:
        hwnd, full = find_window(title)
        if action == "close":
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif action == "minimise":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif action == "maximise":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        else:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.keybd_event(0x12, 0, 0, 0)          # a tap of Alt lets Windows hand over focus
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)
        return f"{action.title()}d '{full}'." if action != "focus" else f"Brought '{full}' to the front."

    @reg.tool("read_clipboard", "Read the text currently copied to the clipboard.")
    def read_clipboard() -> str:
        text = powershell("Get-Clipboard -Raw").rstrip("\r\n")
        return text[:20_000] if text else "The clipboard is empty (or holds something that isn't text)."

    @reg.tool(
        "write_clipboard",
        "Put text on the clipboard so the user can paste it anywhere (a drafted reply, a summary, a command).",
        {"text": {"type": "string"}},
        required=("text",),
    )
    def write_clipboard(text: str) -> str:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                       input=text, text=True, encoding="utf-8", timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return f"Copied {len(text)} characters to the clipboard."

    @reg.tool(
        "set_brightness",
        "Set the laptop screen brightness, 0-100.",
        {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
        required=("percent",),
    )
    def set_brightness(percent: int) -> str:
        powershell("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
                   f".WmiSetBrightness(1, {int(percent)})")
        return f"Brightness set to {percent}%."

    @reg.tool(
        "power_action",
        "Lock the screen, put the laptop to sleep, shut down or restart (both after a 60-second "
        "warning), or cancel a pending shutdown. The user approves everything except locking.",
        {"action": {"type": "string", "enum": list(POWER)}},
        required=("action",),
        confirm=lambda a: None if a.get("action") in ("lock", "cancel_shutdown") else POWER[a["action"]][0],
    )
    def power_action(action: str) -> str:
        POWER[action][1]()
        return POWER[action][0] + ": done."

    @reg.tool(
        "notify",
        "Show a Windows notification in the corner of the screen, for something the user should "
        "see even if they're in another app.",
        {"title": {"type": "string"}, "message": {"type": "string"}},
        required=("title", "message"),
    )
    def notify_tool(title: str, message: str) -> str:
        notify(title, message)
        return "Notification shown."
