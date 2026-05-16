"""Sub-agent registry.

Each agent is responsible for one kind of output (markdown notes, mermaid diagrams,
HTML synthesis). The router decides which to invoke; this package executes them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Type

from .base import Agent, AgentContext
from .html import HtmlAgent
from .mermaid import MermaidAgent
from .obsidian import ObsidianAgent


AGENTS: dict[str, Type[Agent]] = {
    "obsidian": ObsidianAgent,
    "mermaid": MermaidAgent,
    "html": HtmlAgent,
}


def make_enabled_agents(config) -> dict[str, Agent]:
    """Instantiate agents whose `enabled` flag in config is true."""
    out: dict[str, Agent] = {}
    for name, cls in AGENTS.items():
        settings = config.get("agents", name, default={}) or {}
        if settings.get("enabled", True):
            out[name] = cls(settings)
    return out


__all__ = [
    "AGENTS",
    "Agent",
    "AgentContext",
    "make_enabled_agents",
    "ObsidianAgent",
    "MermaidAgent",
    "HtmlAgent",
]
