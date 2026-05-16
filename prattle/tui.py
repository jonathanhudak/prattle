"""Textual TUI for prattle.

Layout:

    ┌─ prattle — <session> · listener:● router:claude_code ─────┐
    │ TRANSCRIPT                       │ ROUTER                    │
    │ 12:34:56  okay so i'm thinking   │ 12:35:01  dispatch (3)    │
    │           about how to...        │    → obsidian: capture    │
    │ 12:35:04  the router agent...    │    → mermaid: flow        │
    │                                  │    → html: synthesize     │
    │                                  ├───────────────────────────┤
    │                                  │ AGENTS                    │
    │                                  │  obsidian  notes.md 2.1K  │
    │                                  │  mermaid   1 diagram      │
    │                                  │  html      output.html    │
    ├──────────────────────────────────┴───────────────────────────┤
    │ › chat:  _                                                    │
    │ q quit · s synthesize · p pause · n new session · o open html │
    └──────────────────────────────────────────────────────────────┘

Runs the listener and router in background threads. Polls the filesystem to
update the transcript and per-agent status panels. Log records from the
listener / router / agents are routed into the TUI via a custom logging handler.

Chat input at the bottom lets you inject messages to the router while running.
Press n to start a new named session.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .utils import append_jsonl, now_iso, read_jsonl, tail_jsonl

# Textual is imported lazily so non-TUI commands don't need it.


_LOG_QUEUE: "queue.Queue[logging.LogRecord]" = queue.Queue()


class _QueueLogHandler(logging.Handler):
    """Push log records to the TUI's queue."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_QUEUE.put_nowait(record)
        except queue.Full:
            pass


def _format_record(record: logging.LogRecord) -> tuple[str, str]:
    """Return (channel, formatted_line). channel ∈ {'transcript','router','agent','other'}."""
    name = record.name
    ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
    msg = record.getMessage()
    if name == "listener":
        if msg.startswith("» "):
            return "transcript", f"[dim]{ts}[/dim]  {msg[2:]}"
        return "router", f"[dim]{ts}[/dim] [yellow]listener[/yellow] {msg}"
    if name == "router":
        return "router", f"[dim]{ts}[/dim] [cyan]router[/cyan] {msg}"
    if name.startswith("agent."):
        short = name.split(".", 1)[1]
        return "agent", f"[dim]{ts}[/dim] [magenta]{short}[/magenta] {msg}"
    if name.startswith("backend."):
        return "router", f"[dim]{ts}[/dim] [yellow]{name}[/yellow] {msg}"
    return "other", f"[dim]{ts}[/dim] [white]{name}[/white] {msg}"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}K"
    return f"{n/1024/1024:.1f}M"


def _agent_status(cfg: Config) -> list[tuple[str, str]]:
    """Return (label, value) pairs describing each agent's current output state."""
    rows: list[tuple[str, str]] = []

    notes = cfg.notes_path
    if notes.exists():
        size = notes.stat().st_size
        text = notes.read_text(encoding="utf-8")
        sections = text.count("\n## ")
        if text.startswith("## "):
            sections += 1
        age = int(time.time() - notes.stat().st_mtime)
        rows.append(
            (
                "obsidian",
                f"notes.md  {_format_bytes(size)} · {sections} section(s) · {age}s ago",
            )
        )
    else:
        rows.append(("obsidian", "(no notes yet)"))

    sketches = cfg.sketches_dir
    idx = sketches / "_index.json"
    if idx.exists():
        try:
            entries = json.loads(idx.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            entries = []
        if entries:
            titles = ", ".join(e.get("title", e.get("id", "?")) for e in entries[:3])
            extra = "" if len(entries) <= 3 else f" +{len(entries)-3} more"
            rows.append(("mermaid", f"{len(entries)} diagram(s): {titles}{extra}"))
        else:
            rows.append(("mermaid", "(none yet)"))
    else:
        rows.append(("mermaid", "(none yet)"))

    html = cfg.html_path
    if html.exists():
        size = html.stat().st_size
        age = int(time.time() - html.stat().st_mtime)
        rows.append(("html", f"output.html  {_format_bytes(size)} · {age}s ago"))
    else:
        rows.append(("html", "(not synthesized yet)"))

    return rows


CSS = """
Screen {
    background: $surface;
}

#main {
    height: 1fr;
}

#left, #right {
    border: solid $primary 20%;
    padding: 0 1;
    height: 1fr;
}

#left {
    width: 60%;
}

#right {
    width: 40%;
}

#right-top, #right-bot {
    height: 1fr;
    border-top: dashed $primary 30%;
    padding: 0 1;
}

#right-top {
    border-top: none;
}

.panel-title {
    color: $accent;
    text-style: bold;
    padding-bottom: 1;
}

#agents-table {
    background: $panel;
    padding: 1 1;
}

.agent-row {
    padding: 0 1;
}

.agent-name {
    color: $accent;
    text-style: bold;
}

#chat-bar {
    height: 3;
    border-top: solid $primary 30%;
    padding: 0 1;
    align: left middle;
}

#chat-label {
    width: auto;
    content-align: left middle;
    color: $accent;
    padding: 0 1 0 0;
}

#chat-input {
    width: 1fr;
    border: none;
    background: $surface;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: $accent;
    color: $background;
    content-align: center middle;
}
"""


def _import_textual():
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Footer, Header, Input, RichLog, Static
    except ImportError:
        return None
    return {
        "App": App,
        "ComposeResult": ComposeResult,
        "Horizontal": Horizontal,
        "Vertical": Vertical,
        "Header": Header,
        "Footer": Footer,
        "RichLog": RichLog,
        "Static": Static,
        "Input": Input,
    }


def _make_session_name(name: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return f"prattle-sessions/{ts}-{slug}"


def build_app_class(cfg: Config):
    """Build the TUI App class against a config. Returns the class (not instance).

    Lifted out of run_tui so it's testable via Textual's `App.run_test()` pilot.
    """
    tx = _import_textual()
    if tx is None:
        raise RuntimeError("textual not installed")

    App = tx["App"]
    ComposeResult = tx["ComposeResult"]
    Horizontal = tx["Horizontal"]
    Vertical = tx["Vertical"]
    Header = tx["Header"]
    Footer = tx["Footer"]
    RichLog = tx["RichLog"]
    Static = tx["Static"]
    Input = tx["Input"]

    from .listener import _PAUSED as _LISTENER_PAUSED, run_listener
    from .router import _CHAT_TRIGGER, run_router, synthesize_once

    class TalkstreamTUI(App):
        TITLE = "prattle"
        SUB_TITLE = ""
        ENABLE_COMMAND_PALETTE = False
        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+s", "synthesize", "HTML"),
            ("ctrl+p", "toggle_pause", "Pause"),
            ("ctrl+n", "new_session", "New session"),
            ("ctrl+o", "open_html", "Open HTML"),
            ("ctrl+r", "reload", "Reload"),
            ("escape", "cancel_mode", "Cancel"),
        ]
        CSS = CSS

        paused: bool = False
        _listener_thread: threading.Thread | None = None
        _router_thread: threading.Thread | None = None
        _stop_event: threading.Event
        _last_offset: int = 0
        _input_mode: str = "chat"  # "chat" | "new_session"

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical():
                with Horizontal(id="main"):
                    with Vertical(id="left"):
                        yield Static("[bold]TRANSCRIPT[/bold]", classes="panel-title")
                        yield RichLog(
                            id="transcript", wrap=True, highlight=False, markup=True,
                            max_lines=2000, auto_scroll=True,
                        )
                    with Vertical(id="right"):
                        with Vertical(id="right-top"):
                            yield Static("[bold]ROUTER[/bold]", classes="panel-title")
                            yield RichLog(
                                id="router-log", wrap=True, highlight=False, markup=True,
                                max_lines=1500, auto_scroll=True,
                            )
                        with Vertical(id="right-bot"):
                            yield Static("[bold]AGENTS[/bold]", classes="panel-title")
                            yield Static(id="agents-table")
                with Horizontal(id="chat-bar"):
                    yield Static("[bold cyan]› chat:[/bold cyan]", id="chat-label")
                    yield Input(
                        placeholder="message to router  (^n new session  ·  ^p pause  ·  ^s html  ·  ^q quit)",
                        id="chat-input",
                    )
            yield Static(id="status-bar")
            yield Footer()

        def on_mount(self) -> None:
            self._stop_event = threading.Event()
            self.sub_title = cfg.get("obsidian", "session_name", default="session")

            # Route logs into TUI queue
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            for h in list(root.handlers):
                root.removeHandler(h)
            root.addHandler(_QueueLogHandler())

            self._render_status_bar()
            self._refresh_agents()

            # Replay any existing transcript records so reopening mid-session shows context
            if cfg.transcript_path.exists():
                existing = read_jsonl(cfg.transcript_path)
                tlog = self.query_one("#transcript", RichLog)
                for r in existing[-200:]:
                    ts = (r.get("t", "") or "")[11:19]
                    source = r.get("source", "")
                    text = r.get("text", "")
                    if source == "chat":
                        tlog.write(f"[dim]{ts}[/dim]  [bold yellow]you ›[/bold yellow] {text[7:]}")
                    else:
                        tlog.write(f"[dim]{ts}[/dim]  {text}")
                self._last_offset = cfg.transcript_path.stat().st_size

            self._spawn_workers()

            self.set_interval(0.2, self._drain_log_queue)
            self.set_interval(1.0, self._refresh_agents)
            self.set_interval(0.5, self._tail_transcript)

        # ---- workers ----

        def _spawn_workers(self) -> None:
            stop = self._stop_event

            def _listener() -> None:
                try:
                    run_listener(cfg, stop_event=stop)
                except Exception:
                    logging.getLogger("listener").exception("listener crashed")

            def _router() -> None:
                try:
                    run_router(cfg, stop_event=stop)
                except Exception:
                    logging.getLogger("router").exception("router crashed")

            self._listener_thread = threading.Thread(
                target=_listener, name="listener", daemon=True
            )
            self._router_thread = threading.Thread(
                target=_router, name="router", daemon=True
            )
            self._listener_thread.start()
            self._router_thread.start()

        def _stop_workers(self, timeout: float = 3.0) -> None:
            """Signal workers to stop and wait briefly."""
            self._stop_event.set()
            if self._router_thread and self._router_thread.is_alive():
                self._router_thread.join(timeout=timeout)
            if self._listener_thread and self._listener_thread.is_alive():
                self._listener_thread.join(timeout=timeout)

        # ---- updates ----

        def _drain_log_queue(self) -> None:
            transcript = self.query_one("#transcript", RichLog)
            router = self.query_one("#router-log", RichLog)
            drained = 0
            while drained < 50:
                try:
                    rec = _LOG_QUEUE.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                channel, line = _format_record(rec)
                if channel == "transcript":
                    transcript.write(line)
                else:
                    router.write(line)

        def _tail_transcript(self) -> None:
            """Watch the JSONL file directly (catch anything not emitted via logging)."""
            new_records, new_offset = tail_jsonl(cfg.transcript_path, self._last_offset)
            self._last_offset = new_offset
            _ = new_records  # primary path is the log queue

        def _refresh_agents(self) -> None:
            rows = _agent_status(cfg)
            lines: list[str] = []
            for name, val in rows:
                lines.append(f"[bold cyan]{name:<10}[/bold cyan] {val}")
            self.query_one("#agents-table", Static).update("\n".join(lines))

        def _render_status_bar(self) -> None:
            backend = cfg.get("router", "backend", default="?")
            paused = " · [PAUSED]" if self.paused else ""
            session = cfg.get("obsidian", "session_name", default="?")
            self.query_one("#status-bar", Static).update(
                f"listener: ● · backend: {backend} · session: {session}{paused}"
            )

        # ---- chat ----

        def on_input_submitted(self, event: "Input.Submitted") -> None:
            text = event.value.strip()
            event.input.clear()
            if not text:
                return
            if self._input_mode == "new_session":
                self._start_new_session(text)
            else:
                self._send_chat(text)

        def _send_chat(self, message: str) -> None:
            cfg.session_root.mkdir(parents=True, exist_ok=True)
            record = {"t": now_iso(), "text": f"[Chat] {message}", "source": "chat"}
            append_jsonl(cfg.transcript_path, record)
            _CHAT_TRIGGER.set()
            ts = datetime.now().strftime("%H:%M:%S")
            self.query_one("#transcript", RichLog).write(
                f"[dim]{ts}[/dim]  [bold yellow]you ›[/bold yellow] {message}"
            )
            rlog = self.query_one("#router-log", RichLog)
            rlog.write(f"[dim]{ts}[/dim] [yellow]chat[/yellow] triggering dispatch")

        def _start_new_session(self, name: str) -> None:
            rlog = self.query_one("#router-log", RichLog)
            ts = datetime.now().strftime("%H:%M:%S")

            # Stop existing workers
            self._stop_workers(timeout=3.0)

            # Compute new session path
            new_session_name = _make_session_name(name)
            cfg.raw.setdefault("obsidian", {})["session_name"] = new_session_name

            # Reset state
            self._stop_event = threading.Event()
            self._last_offset = 0
            self.paused = False
            _LISTENER_PAUSED.clear()
            cfg.raw["router"]["min_new_chars"] = cfg.get("router", "min_new_chars", default=60)
            self.sub_title = new_session_name
            self._input_mode = "chat"
            self._update_chat_label()

            # Clear transcript display
            self.query_one("#transcript", RichLog).clear()
            rlog.write(f"[dim]{ts}[/dim] [green]new session[/green] {new_session_name}")

            self._render_status_bar()
            self._spawn_workers()

        def _update_chat_label(self) -> None:
            label = self.query_one("#chat-label", Static)
            inp = self.query_one("#chat-input", Input)
            if self._input_mode == "new_session":
                label.update("[bold yellow]› session name:[/bold yellow]")
                inp.placeholder = "enter a name for this session, then press Enter"
            else:
                label.update("[bold cyan]› chat:[/bold cyan]")
                inp.placeholder = "message to router  (^n new session  ·  ^p pause  ·  ^s html  ·  ^q quit)"

        # ---- actions ----

        def action_synthesize(self) -> None:
            log = logging.getLogger("agent.html")
            log.info("manual synthesize requested")
            t = threading.Thread(target=synthesize_once, args=(cfg,), daemon=True)
            t.start()

        def action_toggle_pause(self) -> None:
            self.paused = not self.paused
            if self.paused:
                cfg.raw["router"]["min_new_chars"] = 10_000_000
                _LISTENER_PAUSED.set()
            else:
                cfg.raw["router"]["min_new_chars"] = cfg.get("router", "min_new_chars", default=60)
                _LISTENER_PAUSED.clear()
            logging.getLogger("router").info(
                "pause → %s", "PAUSED" if self.paused else "RUNNING"
            )
            self._render_status_bar()

        def action_open_html(self) -> None:
            path = cfg.html_path
            if not path.exists():
                logging.getLogger("agent.html").info("output.html not yet written")
                return
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            try:
                subprocess.Popen([opener, str(path)])
                logging.getLogger("agent.html").info("opening %s", path)
            except FileNotFoundError:
                logging.getLogger("agent.html").info(
                    "couldn't find %s; open %s manually", opener, path
                )

        def action_reload(self) -> None:
            self._refresh_agents()
            self._render_status_bar()

        def action_new_session(self) -> None:
            self._input_mode = "new_session"
            self._update_chat_label()
            self.query_one("#chat-input", Input).focus()

        def action_cancel_mode(self) -> None:
            if self._input_mode != "chat":
                self._input_mode = "chat"
                self._update_chat_label()

    return TalkstreamTUI


def run_tui(cfg: Config) -> int:
    """Launch the Textual TUI."""
    if _import_textual() is None:
        print(
            "textual not installed. `pip install textual` to use the TUI.",
            file=sys.stderr,
        )
        return 2
    AppCls = build_app_class(cfg)
    AppCls().run()
    return 0
