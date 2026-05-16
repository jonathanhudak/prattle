"""Pluggable LLM backend registry."""
from __future__ import annotations

from typing import Type

from .agent_sdk import AgentSdkBackend
from .base import Backend
from .claude_code import ClaudeCodeBackend
from .hermes import HermesBackend


BACKENDS: dict[str, Type[Backend]] = {
    "claude_code": ClaudeCodeBackend,
    "agent_sdk": AgentSdkBackend,
    "hermes": HermesBackend,
}


def make_backend(name: str, settings: dict) -> Backend:
    if name not in BACKENDS:
        raise ValueError(
            f"unknown backend {name!r}. available: {sorted(BACKENDS)}"
        )
    return BACKENDS[name](settings)
