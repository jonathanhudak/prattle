"""Mermaid sketch artist."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..utils import load_prompt, parse_json_tolerant
from .base import Agent, AgentContext

log = logging.getLogger("agent.mermaid")

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(s: str) -> str:
    s = s.strip().lower().replace(" ", "_")
    s = _SLUG_RE.sub("", s) or "diagram"
    return s[:60]


def _index_path(sketches_dir: Path) -> Path:
    return sketches_dir / "_index.json"


def read_index(sketches_dir: Path) -> list[dict]:
    p = _index_path(sketches_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def write_index(sketches_dir: Path, entries: list[dict]) -> None:
    sketches_dir.mkdir(parents=True, exist_ok=True)
    _index_path(sketches_dir).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


class MermaidAgent(Agent):
    name = "mermaid"

    def describe_for_router(self) -> str:
        return (
            "**mermaid** — maintains mermaid diagrams (flowchart / mindmap / "
            "sequence / class / timeline). Use when something is being described "
            "visually: a flow, hierarchy, sequence, system layout. Stable diagram "
            "ids let the artist iterate on the same diagram across turns."
        )

    def handle(self, ctx: AgentContext) -> None:
        existing = read_index(ctx.sketches_dir)
        existing_summary = [
            {"id": d["id"], "title": d.get("title", ""), "type": d.get("type", "")}
            for d in existing
        ]

        system = load_prompt("mermaid")
        user = (
            f"Router instruction: {ctx.instruction}\n\n"
            f"Recent transcript window:\n```\n{ctx.transcript_window}\n```\n\n"
            f"Existing diagrams (reuse ids to update): "
            f"{json.dumps(existing_summary, ensure_ascii=False)}\n\n"
            f"Produce diagrams per the schema."
        )

        raw = ctx.backend.call(system, user)
        try:
            data = parse_json_tolerant(raw)
        except ValueError as e:
            log.warning("could not parse mermaid agent JSON: %s", e)
            return

        diagrams = data.get("diagrams") or []
        if not diagrams:
            log.info("mermaid: no diagrams (%s)", data.get("thinking", ""))
            return

        ctx.sketches_dir.mkdir(parents=True, exist_ok=True)
        by_id: dict[str, dict] = {d["id"]: d for d in existing}
        for d in diagrams:
            did = slugify(d.get("id", "diagram"))
            title = d.get("title", did)
            dtype = d.get("type", "flowchart")
            code = (d.get("code") or "").strip()
            if not code:
                continue
            mmd_path = ctx.sketches_dir / f"{did}.mmd"
            mmd_path.write_text(code + "\n", encoding="utf-8")
            by_id[did] = {"id": did, "title": title, "type": dtype, "file": mmd_path.name}
            log.info("mermaid: wrote %s (%s, %d chars)", mmd_path.name, dtype, len(code))

        write_index(ctx.sketches_dir, list(by_id.values()))
        _write_diagrams_md(ctx.session_root, list(by_id.values()), ctx.sketches_dir)


def _write_diagrams_md(session_root: Path, entries: list[dict], sketches_dir: Path) -> None:
    """Write diagrams.md with all diagrams as ```mermaid code fences for Obsidian."""
    lines = ["# Diagrams\n"]
    for entry in entries:
        title = entry.get("title", entry["id"])
        mmd_path = sketches_dir / entry["file"]
        if not mmd_path.exists():
            continue
        code = mmd_path.read_text(encoding="utf-8").strip()
        lines.append(f"## {title}\n")
        lines.append(f"```mermaid\n{code}\n```\n")
    out = session_root / "diagrams.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("mermaid: updated diagrams.md (%d diagram(s))", len(entries))
