"""Shared utilities: logging, JSON-with-tolerance, paths."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-12s | %(message)s"


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%H:%M:%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def prompts_dir() -> Path:
    return Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    return (prompts_dir() / f"{name}.md").read_text()


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", re.MULTILINE)


def parse_json_tolerant(raw: str) -> dict:
    """Parse JSON from an LLM response, tolerating code fences and stray prose.

    Strategy:
    1. Try strict json.loads on the trimmed string.
    2. If that fails, look for a ```json ... ``` block.
    3. If that fails, find the outermost { ... } in the string and try that.
    4. Raise ValueError with a sample of the raw text.
    """
    s = raw.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    m = _FENCE_RE.search(s)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = s[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    sample = s[:400] + ("..." if len(s) > 400 else "")
    raise ValueError(f"could not parse JSON from response. raw start: {sample!r}")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"warning: skipping malformed jsonl line in {path}", file=sys.stderr)
    return out


def tail_jsonl(path: Path, from_offset: int) -> tuple[list[dict], int]:
    """Read new lines from `path` starting at byte offset `from_offset`.
    Returns (records, new_offset). If the file doesn't exist or shrank, resets."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < from_offset:
        from_offset = 0
    if size == from_offset:
        return [], from_offset
    with path.open("rb") as f:
        f.seek(from_offset)
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    # If the file ends mid-line, keep the trailing partial line for next time.
    if not text.endswith("\n"):
        last_newline = text.rfind("\n")
        if last_newline == -1:
            return [], from_offset  # whole chunk is partial, wait
        consumed = last_newline + 1
        text = text[:consumed]
    else:
        consumed = len(raw)
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, from_offset + consumed


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
