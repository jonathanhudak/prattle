"""prattle CLI.

Subcommands:
    listen       — mic → transcript.jsonl
    route        — tail transcript.jsonl → router → agents (long-running)
    run          — listener + router in one process (typical use)
    replay FILE  — pump a prerecorded JSONL through the router once
    synthesize   — force one HTML synthesis pass
    devices      — list audio input devices
    transcribe FILE.wav — offline ASR over a WAV file (writes transcript.jsonl)
    doctor       — sanity-check the install (config, model, backend reachable)
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load_config
from .utils import setup_logging

log = logging.getLogger("cli")


def _make_session_name(name: str) -> str:
    """Compute a timestamped session folder name from a human-readable name."""
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return f"prattle-sessions/{ts}-{slug}"


def _apply_session_name(cfg: "Config", name: str) -> None:
    """Override the session_name in cfg with a fresh timestamped folder."""
    cfg.raw.setdefault("obsidian", {})["session_name"] = _make_session_name(name)


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m prattle",
        description="Continuous-listening multi-agent companion.",
    )
    p.add_argument("--config", type=Path, default=None, help="Path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listen", help="run the continuous mic listener")
    sub.add_parser("route", help="run the router daemon (tails transcript.jsonl)")
    _run = sub.add_parser("run", help="listener + router in one process (headless)")
    _run.add_argument("name", nargs="?", default=None, help="session name (creates timestamped folder)")
    _tui = sub.add_parser("tui", help="listener + router with a Textual TUI")
    _tui.add_argument("name", nargs="?", default=None, help="session name (creates timestamped folder)")
    r = sub.add_parser("replay", help="replay a prerecorded transcript through the router")
    r.add_argument("jsonl", type=Path)
    sub.add_parser("synthesize", help="force one HTML synthesis pass")
    sub.add_parser("devices", help="list audio input devices")
    t = sub.add_parser("transcribe", help="offline ASR over a WAV file")
    t.add_argument("wav", type=Path)
    sub.add_parser("doctor", help="check config + backend + model availability")
    return p


def _cmd_run(cfg: Config) -> int:
    """Listener and router in the same process, each in a thread.

    The listener thread owns the mic. The router thread tails the file. They
    coordinate only through the filesystem.
    """
    from .listener import run_listener
    from .router import run_router

    listener_rc: list[int | None] = [None]
    router_rc: list[int | None] = [None]

    def _listener_target() -> None:
        try:
            listener_rc[0] = run_listener(cfg)
        except Exception:
            log.exception("listener crashed")
            listener_rc[0] = 1

    def _router_target() -> None:
        try:
            router_rc[0] = run_router(cfg)
        except Exception:
            log.exception("router crashed")
            router_rc[0] = 1

    t1 = threading.Thread(target=_listener_target, name="listener", daemon=False)
    t2 = threading.Thread(target=_router_target, name="router", daemon=False)
    t1.start()
    # tiny stagger so the listener prints its setup logs first
    time.sleep(0.5)
    t2.start()

    try:
        t1.join()
    except KeyboardInterrupt:
        pass
    t2.join(timeout=2.0)

    return max(listener_rc[0] or 0, router_rc[0] or 0)


def _cmd_doctor(cfg: Config) -> int:
    """Sanity check the installation."""
    ok = True
    print(f"config path:       {cfg.path}")
    print(f"session root:      {cfg.session_root}")
    print(f"transcript path:   {cfg.transcript_path}")
    print(f"notes path:        {cfg.notes_path}")
    print(f"backend:           {cfg.get('router', 'backend')}")

    backend_name = cfg.get("router", "backend", default="claude_code")
    if backend_name == "claude_code":
        binary = cfg.get("backends", "claude_code", "binary", default="claude")
        path = shutil.which(binary)
        if path:
            print(f"claude binary:     {path}  ✓")
        else:
            print(f"claude binary:     NOT FOUND on PATH ({binary!r})  ✗")
            ok = False
    elif backend_name == "hermes":
        print("hermes backend selected — implement prattle/backends/hermes.py.call() before running.")

    try:
        import parakeet_mlx  # type: ignore # noqa: F401
        print("parakeet-mlx:      installed  ✓")
    except ImportError:
        print("parakeet-mlx:      NOT installed (replay-only mode possible)")
    try:
        import sounddevice  # type: ignore # noqa: F401
        print("sounddevice:       installed  ✓")
    except ImportError:
        print("sounddevice:       NOT installed")
        ok = False
    try:
        import textual  # type: ignore # noqa: F401
        print("textual:           installed  ✓")
    except ImportError:
        print("textual:           NOT installed (TUI unavailable)")

    return 0 if ok else 1


def main() -> None:
    args = _make_parser().parse_args()
    setup_logging(verbose=args.verbose)
    cfg = load_config(args.config)

    if args.cmd == "listen":
        from .listener import run_listener
        sys.exit(run_listener(cfg))
    elif args.cmd == "route":
        from .router import run_router
        sys.exit(run_router(cfg))
    elif args.cmd == "run":
        if getattr(args, "name", None):
            _apply_session_name(cfg, args.name)
        sys.exit(_cmd_run(cfg))
    elif args.cmd == "tui":
        if getattr(args, "name", None):
            _apply_session_name(cfg, args.name)
        from .tui import run_tui
        sys.exit(run_tui(cfg))
    elif args.cmd == "replay":
        from .router import replay_transcript
        sys.exit(replay_transcript(cfg, args.jsonl))
    elif args.cmd == "synthesize":
        from .router import synthesize_once
        sys.exit(synthesize_once(cfg))
    elif args.cmd == "devices":
        from .listener import list_devices
        sys.exit(list_devices())
    elif args.cmd == "transcribe":
        from .listener import transcribe_file
        sys.exit(transcribe_file(cfg, args.wav))
    elif args.cmd == "doctor":
        sys.exit(_cmd_doctor(cfg))
    else:
        log.error("unknown command %s", args.cmd)
        sys.exit(2)
