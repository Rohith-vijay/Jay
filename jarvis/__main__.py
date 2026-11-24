"""Run with:  python -m jarvis  [--console] [--handsfree] [--voice] [--brief] [--local]

By default Jay opens in its own window. --console keeps everything in the terminal.
"""
from __future__ import annotations

import argparse
import sys
import threading

from .config import Config
from .session import Session


def run_console(session: Session, args) -> None:
    """Terminal front end: prints the event stream and reads typed lines."""
    name = session.cfg.name

    def printer() -> None:
        seen = 0
        while not session.stopped.is_set():
            for event in session.events_after(seen, timeout=1):
                seen = event["id"]
                kind = event["kind"]
                if kind == "step":
                    print(f"   > {event['text']}", flush=True)
                elif kind == "jay":
                    print(f"\n{name}: {event['text']}\n", flush=True)
                elif kind == "user" and event.get("source") == "voice":
                    print(f"You said: {event['text']}", flush=True)
                elif kind == "info":
                    print(event["text"], flush=True)
                elif kind == "mode":
                    print(f"  ({name} is listening)" if event["awake"] else f'  (standby: say "Hey {name}")', flush=True)
                elif kind == "approval":
                    print(f"\n  Approval needed: {event['summary']}\n  Allow? [y/N] ", end="", flush=True)

    threading.Thread(target=printer, daemon=True).start()
    session.start(brief=args.brief)
    print(f"{name} online ({session.agent.provider_name}). Type /help for commands; empty line to talk when voice is on.")
    for line in sys.stdin:
        line = line.rstrip("\r\n").lstrip("﻿").strip()
        pending = session.pending_approval()
        if pending is not None:      # approvals are typed, never spoken
            session.answer_approval(pending, line.lower() in {"y", "yes"})
        elif line in {"/quit", "/exit"}:
            break
        elif line == "/voice":
            session.set_speaking(not session.speak_replies)
            print("Voice on. Press Enter on an empty line to talk." if session.speak_replies else "Voice off.")
        elif line == "/handsfree":
            session.set_handsfree(session.handsfree_stop is None)
            print(f'Hands-free on. Say "{name}, ..."' if session.handsfree_stop else "Hands-free off.")
        elif not line:
            if session.voice and not session.handsfree_stop:
                session.talk_once()
        else:
            session.submit(line)
    session.stop()
    session.stopped.wait(15)
    print("Goodbye.")


def supervise() -> None:
    """Run Jay in a child process and restart it if it crashes (not when it's quit on purpose)."""
    import subprocess
    import time

    child_args = [a for a in sys.argv[1:]]
    crashes: list[float] = []
    while True:
        code = subprocess.call([sys.executable, "-m", "jarvis", "--child", *child_args])
        if code == 0:
            return                                   # quit normally (or another copy is already running)
        now = time.time()
        crashes = [t for t in crashes if now - t < 600] + [now]
        if len(crashes) > 5:
            return                                   # crashing over and over: stop rather than loop forever
        time.sleep(3)
        child_args = [a for a in child_args if a != "--brief"] + ["--standby"]   # come back quietly


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal assistant.")
    parser.add_argument("--console", action="store_true", help="stay in the terminal instead of opening the window")
    parser.add_argument("--no-window", action="store_true", help="start the app without opening a browser window")
    parser.add_argument("--standby", action="store_true",
                        help="start hidden, just listening for the wake word (used at Windows start-up)")
    parser.add_argument("--no-voice", action="store_true", help="don't listen or speak; typing only")
    parser.add_argument("--quit-on-close", action="store_true",
                        help="closing the window stops Jay instead of sending it to standby")
    parser.add_argument("--voice", action="store_true", help="(console) speak replies")
    parser.add_argument("--handsfree", action="store_true", help="(console) listen for the wake word")
    parser.add_argument("--brief", action="store_true", help="start with a briefing")
    parser.add_argument("--local", action="store_true", help="start on the local Ollama model")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.console and not args.child:
        return supervise()

    cfg = Config.load()
    if sys.stdout is None:      # started without a console (pythonw): keep a log file instead
        log = cfg.data_dir / "jay.log"
        try:
            if log.exists() and log.stat().st_size > 3_000_000:      # keep the last run's log, start a fresh one
                log.replace(cfg.data_dir / "jay.log.1")
        except OSError:
            pass
        sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
    else:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.local:
        cfg.provider = "ollama"

    session = Session(cfg)
    if args.console:
        if args.handsfree:
            session.set_handsfree(True)
        elif args.voice:
            session.set_speaking(True)
        run_console(session, args)
    else:
        from .ui import serve

        serve(session, cfg.data_dir, open_browser=not (args.no_window or args.standby),
              quit_on_close=args.quit_on_close, handsfree=not args.no_voice, brief=args.brief)


if __name__ == "__main__":
    main()
