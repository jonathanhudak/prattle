"""Agent base + the context dispatched to each agent."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..backends.base import Backend


@dataclass
class AgentContext:
    """Everything an agent needs to do its job."""

    instruction: str  # the router's brief for this agent
    transcript_window: str  # recent transcript text
    full_transcript: str  # accumulated transcript so far (may be truncated)
    session_root: Path
    notes_path: Path
    sketches_dir: Path
    html_path: Path
    backend: "Backend"
    session_name: str


class Agent(ABC):
    """Sub-agent interface.

    `describe_for_router()` returns the bullet the router prompt sees when listing
    available agents. Keep it crisp — the router uses it to decide whether to
    dispatch.
    """

    name: str = "base"

    def __init__(self, settings: dict) -> None:
        self.settings = settings

    @abstractmethod
    def describe_for_router(self) -> str: ...

    @abstractmethod
    def handle(self, ctx: AgentContext) -> None: ...
