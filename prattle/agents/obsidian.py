"""Obsidian markdown capture agent.

Maintains notes.md by appending or replacing h2 sections. Idempotent across runs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..utils import load_prompt, parse_json_tolerant
from .base import Agent, AgentContext

log = logging.getLogger("agent.obsidian")


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(md: str) -> dict[str, str]:
    """Split a markdown doc into {h2_heading: body_including_heading}.

    Anything before the first h2 is stored under the empty-string key.
    """
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(md))
    if not matches:
        return {"": md}
    if matches[0].start() > 0:
        sections[""] = md[: matches[0].start()]
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections[heading] = md[start:end]
    return sections


def _join_sections(sections: dict[str, str]) -> str:
    parts: list[str] = []
    if "" in sections and sections[""].strip():
        parts.append(sections[""].rstrip())
    for heading, body in sections.items():
        if heading == "":
            continue
        parts.append(body.rstrip())
    return ("\n\n".join(parts).rstrip() + "\n") if parts else ""


def apply_updates(current_md: str, updates: list[dict]) -> str:
    """Pure function: take current markdown + a list of updates, return new markdown.

    Each update: {"section": str, "content": str, "mode": "append" | "replace"}
    """
    sections = _split_sections(current_md)
    for upd in updates:
        heading = (upd.get("section") or "").strip()
        if not heading:
            continue
        content = (upd.get("content") or "").rstrip()
        mode = upd.get("mode", "append")
        existing = sections.get(heading)
        if existing is None:
            # New section
            sections[heading] = f"## {heading}\n\n{content}\n"
            continue
        if mode == "replace":
            sections[heading] = f"## {heading}\n\n{content}\n"
        else:  # append
            sections[heading] = existing.rstrip() + "\n\n" + content + "\n"
    return _join_sections(sections)


class ObsidianAgent(Agent):
    name = "obsidian"

    def describe_for_router(self) -> str:
        return (
            "**obsidian** — captures the conversation as an evolving markdown doc. "
            "Use when a substantive idea, decision, question, or fact should be "
            "recorded. Operates on h2 sections in `notes.md`."
        )

    def handle(self, ctx: AgentContext) -> None:
        notes_path: Path = ctx.notes_path
        current = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""

        system = load_prompt("obsidian")
        user = (
            f"Router instruction: {ctx.instruction}\n\n"
            f"Recent transcript window:\n```\n{ctx.transcript_window}\n```\n\n"
            f"Current notes.md (truncate-aware):\n```markdown\n{current[-6000:]}\n```\n\n"
            f"Produce updates per the schema."
        )

        raw = ctx.backend.call(system, user)
        try:
            data = parse_json_tolerant(raw)
        except ValueError as e:
            log.warning("could not parse obsidian agent JSON: %s", e)
            return

        updates = data.get("updates") or []
        if not updates:
            log.info("obsidian: no updates (%s)", data.get("thinking", ""))
            return

        new_md = apply_updates(current, updates)
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(new_md, encoding="utf-8")
        log.info(
            "obsidian: applied %d update(s) → %s (%d chars)",
            len(updates),
            notes_path.name,
            len(new_md),
        )
