"""Hermes backend — STUB.

Replace the body of `call()` with however you invoke your Hermes agent.

Common patterns:

1. CLI subprocess (mirrors the claude_code backend):

        proc = subprocess.run(
            ["hermes", "ask", "--system", system_prompt, user_prompt],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

2. HTTP endpoint:

        import requests
        r = requests.post(self.settings["endpoint"],
                          json={"system": system_prompt, "prompt": user_prompt},
                          timeout=60)
        r.raise_for_status()
        return r.json()["text"]

3. stdin pipe to a long-running process — see if your Hermes wrapper supports
   this; if so, hold the process open in __init__ and write/readline per call.

The contract is: take system_prompt + user_prompt, return the assistant's raw
text. The router parses JSON out of that text using `parse_json_tolerant`.
"""
from __future__ import annotations

from .base import Backend


class HermesBackend(Backend):
    name = "hermes"

    def call(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError(
            "HermesBackend is a stub. Open prattle/backends/hermes.py and "
            "implement `call()` with however you invoke Hermes (CLI, HTTP, etc). "
            "For now, set router.backend = \"claude_code\" in config.toml."
        )
