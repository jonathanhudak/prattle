"""Claude Code CLI backend.

Spawns `claude -p "<prompt>"` per call. We pass the system prompt via
`--append-system-prompt` and the user prompt as the positional `-p` argument.

This is intentionally simple — one call, one process. For higher throughput you'd
keep a session open, but for a PoC the latency is dominated by the model anyway.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from .base import Backend

log = logging.getLogger("backend.claude_code")


class ClaudeCodeBackend(Backend):
    name = "claude_code"

    def call(self, system_prompt: str, user_prompt: str) -> str:
        binary = self.settings.get("binary", "claude")
        extra_args: list[str] = list(self.settings.get("extra_args", []))
        timeout = float(self.settings.get("timeout_seconds", 60))

        resolved = shutil.which(binary)
        if not resolved:
            raise RuntimeError(
                f"claude CLI not found on PATH (looked for {binary!r}). "
                "Install Claude Code and ensure `claude --version` works."
            )

        cmd: list[str] = [
            resolved,
            *extra_args,
            "--append-system-prompt",
            system_prompt,
            "-p",
            user_prompt,
        ]
        log.debug("invoking: %s ... (prompt %d chars)", " ".join(cmd[:5]), len(user_prompt))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI timed out after {timeout}s") from e

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (exit {proc.returncode}): "
                f"stderr={proc.stderr.strip()[:400]}"
            )

        return proc.stdout
