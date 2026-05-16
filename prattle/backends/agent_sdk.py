"""Claude Agent SDK backend — STUB with a clear wiring path.

When you're ready to swap the `claude -p` subprocess for the Agent SDK (and
gain real subagent semantics, streaming, and tool use), enable this backend in
config.toml:

    [router]
    backend = "agent_sdk"

    [backends.agent_sdk]
    model = "claude-sonnet-4-5"
    api_key_env = "ANTHROPIC_API_KEY"

Then fill in `call()` below.

## Why a separate backend?

The CLI backend (`claude_code.py`) shells out to `claude -p "<prompt>"` per
turn. That's the path the user requested first ("use it with Claude Code to
start out"). It works today, but each call is a fresh process and the
sub-agent/tool affordances of Claude Code aren't exposed to the router.

Migrating to the Agent SDK lets the router and each sub-agent be a long-lived
agent with:
- streaming responses (lower perceived latency)
- proper subagent invocation (real `Task` calls instead of subprocess hacks)
- tool definitions the agents can call (e.g. an `apply_obsidian_update` tool
  instead of returning JSON for a Python adapter to parse)
- ACP-style routing if your stack uses it

## Sketch (Python — claude-agent-sdk)

    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    class AgentSdkBackend(Backend):
        def __init__(self, settings):
            super().__init__(settings)
            self.opts = ClaudeAgentOptions(
                model=settings.get("model", "claude-sonnet-4-5"),
                # ACP / MCP servers, tools, etc. go here
            )

        async def _call_async(self, system, user):
            async with ClaudeSDKClient(options=self.opts) as client:
                await client.query(user, system_prompt=system)
                chunks = []
                async for msg in client.receive_response():
                    chunks.append(msg.text)
                return "".join(chunks)

        def call(self, system, user):
            import asyncio
            return asyncio.run(self._call_async(system, user))

For now this stub raises a clear error so the user is pointed at this file.
"""
from __future__ import annotations

from .base import Backend


class AgentSdkBackend(Backend):
    name = "agent_sdk"

    def call(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError(
            "AgentSdkBackend is a stub. See prattle/backends/agent_sdk.py for "
            "a wiring sketch using `claude-agent-sdk`. For now, keep "
            "router.backend = \"claude_code\" in config.toml."
        )
