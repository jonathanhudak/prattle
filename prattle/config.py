"""Config loader. Reads config.toml from the project root (or a path you pass)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from .utils import repo_root


DEFAULTS: dict[str, Any] = {
    "obsidian": {
        "vault_path": str(Path.home() / "Documents" / "Obsidian"),
        "session_name": "prattle-sessions/default",
    },
    "listener": {
        "chunk_seconds": 6.0,
        "input_device": None,
        "sample_rate": 16000,
        "parakeet_model": "mlx-community/parakeet-tdt-0.6b-v3",
    },
    "router": {
        "backend": "claude_code",
        "debounce_seconds": 12,
        "min_new_chars": 60,
        "context_chars": 4000,
    },
    "backends": {
        "claude_code": {
            "binary": "claude",
            "extra_args": [],
            "timeout_seconds": 60,
        },
        "hermes": {"endpoint": ""},
    },
    "agents": {
        "obsidian": {"enabled": True},
        "mermaid": {"enabled": True},
        "html": {"enabled": True, "cooldown_seconds": 60},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def session_root(self) -> Path:
        vault = Path(self.get("obsidian", "vault_path"))
        session = self.get("obsidian", "session_name")
        return vault / session

    @property
    def transcript_path(self) -> Path:
        return self.session_root / "transcript.jsonl"

    @property
    def notes_path(self) -> Path:
        return self.session_root / "notes.md"

    @property
    def sketches_dir(self) -> Path:
        return self.session_root / "sketches"

    @property
    def html_path(self) -> Path:
        return self.session_root / "output.html"

    @property
    def router_state_path(self) -> Path:
        return self.session_root / ".router_state.json"

    @property
    def chat_path(self) -> Path:
        return self.session_root / "chat.jsonl"

    @property
    def log_path(self) -> Path:
        return self.session_root / "session.log"


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = repo_root() / "config.toml"
        if not path.exists():
            # fall back to defaults + warn
            print(
                f"warning: no config.toml at {path}; using defaults. "
                f"cp config.example.toml config.toml to customize.",
                file=sys.stderr,
            )
            return Config(raw=DEFAULTS, path=path)
    with path.open("rb") as f:
        user = tomllib.load(f)
    merged = _deep_merge(DEFAULTS, user)
    return Config(raw=merged, path=path)
