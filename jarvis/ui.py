"""Jay's window: a small web app served only to this computer.

Only requests from this machine, carrying the secret token that the launcher puts
in the window's address, are accepted, so other programs and web pages can't
drive the assistant.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .session import Session

PORT = 8765
PAGE = Path(__file__).with_name("ui.html")


def telemetry() -> dict:
    """Live laptop figures for the display's side panels."""
    import psutil

    battery = psutil.sensors_battery()
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "memory": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage(Path.home().anchor).percent,
        "battery": round(battery.percent) if battery else None,
        "charging": bool(battery and battery.power_plugged),
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
        "processes": len(psutil.pids()),
        "uptime": int(time.time() - psutil.boot_time()),
    }


class WindowWatch:
    """Knows whether a window is open: an open page always has an events request waiting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.waiting = 0
        self.last_seen: float | None = None     # None until a window has connected once
        self._polls: dict[int, float] = {}      # requests waiting right now -> when each started
        self._next = 0
        self._closed_at = 0.0                   # when the window was last told to close

    def enter(self) -> int:
        with self._lock:
            self._next += 1
            self._polls[self._next] = self.last_seen = time.monotonic()
            self.waiting = len(self._polls)
            return self._next

    def leave(self, poll: int | None = None) -> None:
        with self._lock:
            started = self._polls.pop(poll, None) if poll is not None else None
            if poll is None and self._polls:
                started = self._polls.pop(next(iter(self._polls)))
            self.waiting = len(self._polls)
            if started is None or started > self._closed_at:
                self.last_seen = time.monotonic()

    def order_closed(self) -> None:
        """The window has been told to close. A request it left hanging no longer counts as 'open'."""
        with self._lock:
            self._closed_at = time.monotonic()
            self.last_seen = self._closed_at - 60 if self.last_seen is not None else None

    def is_open(self) -> bool:
        with self._lock:
            if any(started > self._closed_at for started in self._polls.values()):
                return True
            # A page between two polls has nothing waiting for an instant; allow for that.
            return self.last_seen is not None and time.monotonic() - self.last_seen < 3

    def closed_for(self) -> float:
        """Seconds since the last window went away (0 while one is open or none has opened yet)."""
        with self._lock:
            live = any(started > self._closed_at for started in self._polls.values())
            if live or self.last_seen is None:
                return 0.0
            return time.monotonic() - self.last_seen

    def forgive(self) -> None:
        """Restart the count, e.g. after the laptop wakes from sleep."""
        with self._lock:
            if self.last_seen is not None:
                self.last_seen = time.monotonic()


def watch_window(session: Session, window: WindowWatch, on_closed, grace: float = 8, tick: float = 2) -> None:
    """Call on_closed() each time the window has been closed for `grace` seconds."""
    previous = time.monotonic()
    handled = True                          # nothing to react to until a window has been open
    while not session.stopped.wait(tick):
        now = time.monotonic()
        if now - previous > tick * 4:       # the clock jumped: the laptop was asleep
            window.forgive()
        previous = now
        if window.is_open():
            handled = False
        elif not handled and window.closed_for() > grace:
            handled = True
            on_closed()


def _same(given: str, token: str) -> bool:
    """Compare tokens in constant time, so the answer can't be worked out a character at a time."""
    import hmac
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8"))


def make_handler(session: Session, token: str, window: WindowWatch | None = None):
    window = window or WindowWatch()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:   # keep the log quiet
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode())

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._local():
                return self._send(403, b"forbidden", "text/plain")
            if url.path == "/":
                if not _same(query.get("t", [""])[0], token):
                    return self._send(403, b"Open Jay from jarvis.bat.", "text/plain")
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, page.encode(), "text/html")
            if not _same(self.headers.get("X-Jay-Token", ""), token):
                return self._send(403, b"forbidden", "text/plain")
            if url.path == "/api/events":
                after = int(query.get("after", ["0"])[0])
                if "nowait" in query:               # history for a window that has just opened
                    return self._json(session.events[after:])
                poll = window.enter()
                try:
                    return self._json(session.events_after(after, timeout=8))
                finally:
                    window.leave(poll)
            if url.path == "/api/levels":
                return self._json(session.levels())
            if url.path == "/api/telemetry":
                return self._json(telemetry())
            if url.path == "/api/icon.png":
                icon = Path(__file__).resolve().parent.parent / "assets" / "jay.png"
                if icon.exists():
                    return self._send(200, icon.read_bytes(), "image/png")
                return self._send(404, b"", "text/plain")
            if url.path == "/api/state":
                return self._json({**session.state(), "window_open": window.is_open()})
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if not self._local() or not _same(self.headers.get("X-Jay-Token", ""), token):
                return self._send(403, b"forbidden", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            path = urlparse(self.path).path
            store = session.agent.store
            if path == "/api/send":
                session.submit(str(body.get("text", "")))
            elif path == "/api/talk":
                session.talk_once()
            elif path == "/api/approve":
                session.answer_approval(int(body.get("id", 0)), bool(body.get("allow")))
            elif path == "/api/voice":
                # Starting the microphone takes a moment; don't hold the request open for it.
                if "handsfree" in body:
                    threading.Thread(target=session.set_handsfree, args=(bool(body["handsfree"]),), daemon=True).start()
                if "speak" in body:
                    threading.Thread(target=session.set_speaking, args=(bool(body["speak"]),), daemon=True).start()
            elif path == "/api/task":
                task_id = int(body.get("id", 0))
                store.complete_task(task_id) if body.get("action") == "done" else store.delete_task(task_id)
                session.emit("refresh")
            elif path == "/api/model":
                session.submit(f"/model {body.get('name', '')}")
            elif path == "/api/fps":
                session.fps = int(body.get("fps", 0))      # reported by the page, for checking smoothness
            elif path == "/api/profile":
                session.save_profile(body.get("values") or {})
            elif path == "/api/enrol":
                name = str(body.get("name", "")).strip()[:40]
                if name:
                    threading.Thread(target=session.start_enrol, args=(name.title(),), daemon=True).start()
            elif path == "/api/enrol_cancel":
                session.cancel_enrol()
            elif path == "/api/voice_remove":
                session.speakers.remove(str(body.get("name", "")))
                session.emit("refresh")
            elif path == "/api/voice_owner":
                session.speakers.set_owner(str(body.get("name", "")))
                session.emit("refresh")
            elif path == "/api/mini":
                session.set_mini(bool(body.get("on")))
            elif path == "/api/closed":
                window.order_closed()               # the page is about to close itself
            elif path == "/api/wake":
                session.wake()
            elif path == "/api/stop":
                session.stand_down(close_window=bool(body.get("close")))
            elif path == "/api/quit":
                session.stop()
            else:
                return self._send(404, b"not found", "text/plain")
            self._json({"ok": True})

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows would otherwise let a second copy of Jay listen on the same port alongside the first.
    allow_reuse_address = False

    def server_bind(self) -> None:
        import socket

        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        """A window closing mid-request is normal; don't fill the log with it."""
        import sys

        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def watch_hotkey(combo: str, on_press, stopped: threading.Event) -> None:
    """A system-wide shortcut (e.g. ctrl+alt+j) that summons Jay from any app."""
    import ctypes
    from ctypes import wintypes

    mods = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
    parts = [x.strip() for x in combo.split("+") if x.strip()]
    flags = sum(mods[x] for x in parts[:-1] if x in mods) | 0x4000      # MOD_NOREPEAT
    key = parts[-1].upper()
    vk = ord(key) if len(key) == 1 else {"SPACE": 0x20, "F12": 0x7B, "F11": 0x7A}.get(key)
    if not vk or not ctypes.windll.user32.RegisterHotKey(None, 0xA11, flags, vk):
        print(f"Hotkey {combo} could not be registered (another app may be using it).", flush=True)
        return
    msg = wintypes.MSG()
    while not stopped.is_set() and ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:                                         # WM_HOTKEY
            on_press()


def set_mini(on: bool) -> bool:
    """Shrink Jay's window to a small floating panel in the bottom-right corner (always on top),
    or put it back where it was. Done with Windows itself, so it works for Edge's app window."""
    import ctypes
    from ctypes import wintypes

    from .tools.system import top_windows

    user32 = ctypes.windll.user32
    # Declare the types: without them, the "always on top" marker (-1) is passed too narrow and
    # Windows silently rejects the call.
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    hwnd = next((h for h, title in top_windows() if title == "Jay"), None)
    if hwnd is None:
        return False
    state = set_mini.__dict__.setdefault("saved", {})
    rect = wintypes.RECT()
    if on:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        state["rect"] = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)             # SPI_GETWORKAREA
        w, h = 420, 520
        user32.ShowWindow(hwnd, 9)                                                  # restore if maximised
        return bool(user32.SetWindowPos(hwnd, wintypes.HWND(-1), work.right - w - 16, work.bottom - h - 16, w, h, 0x0040))   # topmost
    else:
        x, y, w, h = state.get("rect", (120, 80, 1280, 800))
        return bool(user32.SetWindowPos(hwnd, wintypes.HWND(-2), x, y, w, h, 0x0040))   # no longer topmost


def open_window(url: str) -> None:
    """Open the page as its own app-style window in Edge, or in the default browser."""
    try:
        subprocess.Popen(f'start "" msedge --app="{url}"', shell=True)
    except OSError:
        webbrowser.open(url)


def serve(session: Session, data_dir: Path, open_browser: bool = True, quit_on_close: bool = False,
          handsfree: bool = True, brief: bool = False) -> None:
    """Run Jay with its window.

    Jay keeps running in the background listening for the wake word; the window opens
    when it is called and closing the window sends it back to standby. With
    quit_on_close, closing the window stops Jay completely instead.
    """
    token_file = data_dir / "ui_token"
    window = WindowWatch()
    # The access token is kept between runs, so a window left open across a restart or crash
    # reconnects by itself instead of sitting there offline. Delete data/ui_token to change it.
    token = token_file.read_text().strip() if token_file.exists() else ""
    if len(token) < 24 or not all(c.isalnum() or c in "-_" for c in token):
        token = secrets.token_urlsafe(24)
    try:
        server = QuietServer(("127.0.0.1", PORT), make_handler(session, token, window))
    except OSError:
        # Jay is already running: just bring its window back (the page wakes it when it loads).
        if token_file.exists() and open_browser:
            open_window(f"http://127.0.0.1:{PORT}/?t={token_file.read_text().strip()}")
        return
    token_file.write_text(token)
    url = f"http://127.0.0.1:{PORT}/?t={token}"
    print(f"Jay's window is at http://127.0.0.1:{PORT}/ (its access token is kept in data/ui_token)", flush=True)

    opened_at = [0.0]

    def show_window() -> None:
        """Open the window if it isn't open (and wasn't just asked to open)."""
        if not window.is_open() and time.monotonic() - opened_at[0] > 4:
            opened_at[0] = time.monotonic()
            open_window(url)

    session.on_wake = show_window
    if session.cfg.hotkey:
        def hotkey_pressed() -> None:
            # While Jay is working or talking the shortcut stops it (a sure alternative to "Stop Jay").
            if session.status in ("thinking", "speaking", "waiting"):
                session.stand_down(close_window=False)
            else:
                session.wake()

        threading.Thread(target=watch_hotkey, args=(session.cfg.hotkey, hotkey_pressed, session.stopped),
                         daemon=True).start()
    session.on_close_window = window.order_closed
    session.on_mini = set_mini
    session.start(brief=brief, handsfree=handsfree)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    on_closed = session.stop if quit_on_close else (lambda: session.stand_down(close_window=False, sound=False))
    threading.Thread(target=watch_window, args=(session, window, on_closed), daemon=True).start()
    if open_browser:
        show_window()
    # The worker normally finishes on its own; don't let a stuck request keep the process alive.
    while not session.stopped.wait(1):
        if session.quitting.is_set() and session.stopped.wait(20) is False:
            break
    server.shutdown()
