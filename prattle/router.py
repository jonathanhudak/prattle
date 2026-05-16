"""Router daemon.

Tails `transcript.jsonl`, debounces, and dispatches new chunks to specialist
sub-agents. The router itself is an LLM call: it decides *which* agents to
invoke for each round. Each agent is then a separate LLM call with its own
focused prompt.

State (last seen byte offset, last router call time, last full transcript
length) is persisted to `<session>/.router_state.json` so restarts pick up
where they left off.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .agents import AGENTS, AgentContext, make_enabled_agents
from .backends import make_backend
from .backends.base import Backend
from .config import Config
from .utils import load_prompt, now_iso, parse_json_tolerant, tail_jsonl

log = logging.getLogger("router")

# Set from the TUI when the user sends a chat message, to bypass debounce.
_CHAT_TRIGGER: threading.Event = threading.Event()


def _state_path(cfg: Config) -> Path:
    return cfg.router_state_path


def _load_state(cfg: Config) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {"offset": 0, "last_router_call_ts": 0.0, "last_processed_len": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"offset": 0, "last_router_call_ts": 0.0, "last_processed_len": 0}


def _save_state(cfg: Config, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _render_router_prompt(agents: dict) -> str:
    tmpl = load_prompt("router")
    block = "\n\n".join(a.describe_for_router() for a in agents.values())
    return tmpl.replace("{AGENTS_BLOCK}", block)


@dataclass
class TranscriptBuffer:
    """Rolling buffer of transcript chunks."""

    records: list[dict]

    def text(self, max_chars: int | None = None) -> str:
        joined = " ".join(r.get("text", "").strip() for r in self.records).strip()
        if max_chars and len(joined) > max_chars:
            return "..." + joined[-max_chars:]
        return joined

    def window_text(self, n_records: int = 8) -> str:
        return " ".join(r.get("text", "").strip() for r in self.records[-n_records:]).strip()

    def total_chars(self) -> int:
        return sum(len(r.get("text", "")) for r in self.records)


def _dispatch(
    cfg: Config,
    backend: Backend,
    agents: dict,
    buf: TranscriptBuffer,
) -> None:
    if not buf.records:
        return

    # Surface any recent user chat injections prominently.
    recent_chat = [
        r for r in buf.records[-20:]
        if r.get("source") == "chat"
    ]
    chat_section = ""
    if recent_chat:
        msgs = "\n".join(f"  • {r['text']}" for r in recent_chat)
        chat_section = (
            f"\n\nUser injections (typed directly into TUI — treat as high-priority guidance):\n{msgs}\n"
        )

    system = _render_router_prompt(agents)
    user = (
        f"New transcript window:\n```\n{buf.window_text(12)}\n```\n\n"
        f"Full transcript so far (truncated, most recent first wins):\n"
        f"```\n{buf.text(max_chars=cfg.get('router', 'context_chars', default=4000))}\n```"
        f"{chat_section}\n\n"
        f"Decide which agents to dispatch."
    )

    log.info("router: invoking (%d new chars total)", buf.total_chars())
    try:
        raw = backend.call(system, user)
    except Exception as e:
        log.error("router: backend call failed: %s", e)
        return

    try:
        decision = parse_json_tolerant(raw)
    except ValueError as e:
        log.warning("router: could not parse decision JSON: %s", e)
        return

    dispatches = decision.get("dispatches") or []
    thinking = decision.get("thinking", "")
    log.info("router: %d dispatch(es) — %s", len(dispatches), thinking)

    if not dispatches:
        return

    seen: set[str] = set()
    for d in dispatches:
        name = d.get("agent")
        if not name or name in seen:
            continue
        seen.add(name)
        agent = agents.get(name)
        if agent is None:
            log.warning("router: unknown agent %r in dispatch", name)
            continue
        instruction = d.get("instruction", "")
        ctx = AgentContext(
            instruction=instruction,
            transcript_window=buf.window_text(12),
            full_transcript=buf.text(max_chars=cfg.get("router", "context_chars", default=4000)),
            session_root=cfg.session_root,
            notes_path=cfg.notes_path,
            sketches_dir=cfg.sketches_dir,
            html_path=cfg.html_path,
            backend=backend,
            session_name=cfg.get("obsidian", "session_name", default="session"),
        )
        try:
            agent.handle(ctx)
        except Exception as e:
            log.exception("agent %s failed: %s", name, e)


def run_router(
    cfg: Config,
    *,
    once: bool = False,
    poll_seconds: float = 0.5,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Tail the transcript file and dispatch indefinitely.

    If `once=True`, processes whatever is in the transcript file right now,
    runs the router, and exits. Useful for testing.

    If `stop_event` is provided, the loop exits when it is set (no signal handler
    installed — caller owns the event). Otherwise installs SIGINT/SIGTERM handlers.
    """
    backend = make_backend(cfg.get("router", "backend", default="claude_code"),
                          cfg.get("backends", cfg.get("router", "backend"), default={}) or {})
    agents = make_enabled_agents(cfg)
    if not agents:
        log.error("no agents enabled; nothing to do.")
        return 2
    log.info("router: backend=%s, agents=%s", backend.name, sorted(agents.keys()))

    cfg.session_root.mkdir(parents=True, exist_ok=True)

    state = _load_state(cfg)
    offset = int(state.get("offset", 0))
    last_call = float(state.get("last_router_call_ts", 0.0))
    last_processed_len = int(state.get("last_processed_len", 0))

    # Load existing records up to current offset so the rolling buffer has context
    buf = TranscriptBuffer(records=[])
    if cfg.transcript_path.exists() and offset > 0:
        # Re-read everything we've already consumed for context; cheap for typical sessions.
        from .utils import read_jsonl
        buf.records = read_jsonl(cfg.transcript_path)
        log.info("router: loaded %d prior transcript records for context", len(buf.records))

    debounce = float(cfg.get("router", "debounce_seconds", default=12))

    _own_stop = stop_event is None
    if stop_event is None:
        stop_event = threading.Event()

    def _on_sigint(*_: Any) -> None:
        log.info("router: stop requested")
        stop_event.set()  # type: ignore[union-attr]

    if _own_stop:
        signal.signal(signal.SIGINT, _on_sigint)
        signal.signal(signal.SIGTERM, _on_sigint)

    while not stop_event.is_set():
        # Re-read each iteration so TUI pause (cfg mutation) takes effect immediately.
        min_new_chars = int(cfg.get("router", "min_new_chars", default=60))

        new_records, new_offset = tail_jsonl(cfg.transcript_path, offset)
        if new_records:
            buf.records.extend(new_records)
            offset = new_offset

        # Chat message injected from TUI — bypass debounce for immediate dispatch.
        chat_triggered = _CHAT_TRIGGER.is_set()
        if chat_triggered:
            _CHAT_TRIGGER.clear()

        total_len = buf.total_chars()
        new_chars = total_len - last_processed_len
        now = time.time()
        ready = (
            new_chars >= min_new_chars and (now - last_call) >= debounce
        ) or (chat_triggered and new_chars > 0)

        if ready:
            _dispatch(cfg, backend, agents, buf)
            last_call = now
            last_processed_len = total_len
            _save_state(
                cfg,
                {
                    "offset": offset,
                    "last_router_call_ts": last_call,
                    "last_processed_len": last_processed_len,
                },
            )
            if once:
                return 0

        if once:
            log.info("router: --once and not ready (new_chars=%d, gap=%.1fs)",
                     new_chars, now - last_call)
            _save_state(cfg, {"offset": offset, "last_router_call_ts": last_call,
                               "last_processed_len": last_processed_len})
            return 0

        time.sleep(poll_seconds)

    _save_state(
        cfg,
        {
            "offset": offset,
            "last_router_call_ts": last_call,
            "last_processed_len": last_processed_len,
        },
    )
    return 0


def replay_transcript(cfg: Config, jsonl_path: Path) -> int:
    """Replay a pre-recorded transcript JSONL through the router.

    Copies the records into the session transcript file (overwrites!) and then
    runs the router once. Useful for prompt iteration without a mic.
    """
    if not jsonl_path.exists():
        log.error("transcript file not found: %s", jsonl_path)
        return 2

    cfg.session_root.mkdir(parents=True, exist_ok=True)
    target = cfg.transcript_path
    target.write_text(jsonl_path.read_text(encoding="utf-8"), encoding="utf-8")
    # Reset router state so the replay reprocesses from scratch
    _save_state(cfg, {"offset": 0, "last_router_call_ts": 0.0, "last_processed_len": 0})
    log.info("router: replaying %s → %s", jsonl_path, target)

    # Force one immediate dispatch by setting min_new_chars=0 and debounce=0 inline.
    # We do this by mutating the in-memory config.
    cfg.raw["router"]["min_new_chars"] = 0
    cfg.raw["router"]["debounce_seconds"] = 0
    return run_router(cfg, once=True, poll_seconds=0.1)


def synthesize_once(cfg: Config) -> int:
    """Force a single HTML synthesis pass, ignoring cooldown."""
    from .agents.html import HtmlAgent

    agents = make_enabled_agents(cfg)
    html_agent = agents.get("html") or HtmlAgent({"enabled": True, "cooldown_seconds": 0})
    # Bypass cooldown
    html_agent.settings = dict(html_agent.settings or {})
    html_agent.settings["cooldown_seconds"] = 0

    backend = make_backend(
        cfg.get("router", "backend", default="claude_code"),
        cfg.get("backends", cfg.get("router", "backend"), default={}) or {},
    )
    ctx = AgentContext(
        instruction="Roll the current state into a polished HTML doc.",
        transcript_window="",
        full_transcript="",
        session_root=cfg.session_root,
        notes_path=cfg.notes_path,
        sketches_dir=cfg.sketches_dir,
        html_path=cfg.html_path,
        backend=backend,
        session_name=cfg.get("obsidian", "session_name", default="session"),
    )
    html_agent.handle(ctx)
    return 0
