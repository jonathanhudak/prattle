"""Backend interface. A backend turns (system_prompt, user_prompt) into text."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Backend(ABC):
    name: str = "base"

    def __init__(self, settings: dict) -> None:
        self.settings = settings

    @abstractmethod
    def call(self, system_prompt: str, user_prompt: str) -> str:
        """Synchronous one-shot call. Returns the model's raw text output."""
        ...
