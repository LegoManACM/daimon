"""Claude Code through the Claude Agent SDK. The SDK runs the real `claude` CLI, so with `auth: subscription`
(the default) runs count against your Claude plan, like your own sessions, and use your ~/.claude settings.

Options (config.yaml harness entry or the worker's harness_options):
  auth:            subscription (default: ANTHROPIC_API_KEY is hidden from the CLI) | api_key
  cli_path:        the claude executable (default: the one on PATH, else the SDK's bundled copy)
  setting_sources: which Claude Code settings and CLAUDE.md files load (default [user, project, local])
  permission_mode: default acceptEdits; every other action goes through our consent rules
  model, max_turns, max_budget_usd, effort, allowed_tools, disallowed_tools: passed through
"""
from __future__ import annotations

import shutil
import sys
import time
from typing import Any

from .. import gate
from . import Driver, Job, Outcome

RATE_LIMIT_FALLBACK_SECONDS = 15 * 60


class ClaudeSdk(Driver):
    lends_tools = True

    def build_options(self, job: Job) -> Any:
        from claude_agent_sdk import (ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, SdkMcpTool,
                                      create_sdk_mcp_server)
        o = self.options

        def lend(tool: gate.Tool) -> SdkMcpTool:
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": await tool.run(args)}]}
            return SdkMcpTool(tool.name, tool.description, tool.schema or {"type": "object", "properties": {}}, handler)

        async def can_use_tool(name: str, args: dict[str, Any], _context: Any) -> Any:
            if name.startswith("mcp__daimon__") or job.approve is None:
                return PermissionResultAllow()
            allowed, note = await job.approve(name, args)
            if allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message="The user declined this." + (f' They said: "{note}"' if note else ""))

        env = {} if o.get("auth", "subscription") == "api_key" else {"ANTHROPIC_API_KEY": ""}
        kwargs: dict[str, Any] = dict(
            cwd=str(job.workspace),
            resume=job.session,
            model=job.model if o.get("model") is None else o["model"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": job.system},
            mcp_servers={"daimon": create_sdk_mcp_server("daimon", tools=[lend(t) for t in job.tools])},
            permission_mode=o.get("permission_mode", "acceptEdits"),
            setting_sources=o.get("setting_sources", ["user", "project", "local"]),
            can_use_tool=can_use_tool,
            env=env,
            cli_path=o.get("cli_path") or shutil.which("claude"),
            disallowed_tools=["AskUserQuestion", *(o.get("disallowed_tools") or [])],
            allowed_tools=list(o.get("allowed_tools") or []),
        )
        for key in ("max_turns", "max_budget_usd", "effort"):
            if o.get(key) is not None:
                kwargs[key] = o[key]
        return ClaudeAgentOptions(**kwargs)

    async def run(self, job: Job) -> Outcome:
        try:
            from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient, RateLimitEvent, ResultMessage,
                                          SystemMessage, TextBlock, ToolUseBlock)
        except ImportError as e:  # name the interpreter: `daimon` is often a uv tool with its own environment
            return Outcome("", error=f"can't import claude_agent_sdk in {sys.executable} ({e}). Reinstall daimon so it "
                           "picks up its dependencies: `uv tool install -e <Daimon repo> --force`, or "
                           f"`uv pip install --python \"{sys.executable}\" claude-agent-sdk`", started=False)
        session, text, started, retry_at = job.session, "", False, None
        try:
            async with ClaudeSDKClient(self.build_options(job)) as client:
                await client.query(job.prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        session = msg.data.get("session_id") or session
                    elif isinstance(msg, AssistantMessage):
                        started = True
                        session = msg.session_id or session
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                text = block.text
                            elif isinstance(block, ToolUseBlock) and not block.name.startswith("mcp__daimon__"):
                                job.event(f"{block.name} {gate.short(block.input)}")
                    elif isinstance(msg, RateLimitEvent) and msg.rate_limit_info.status == "rejected":
                        retry_at = msg.rate_limit_info.resets_at or time.time() + RATE_LIMIT_FALLBACK_SECONDS
                    elif isinstance(msg, ResultMessage):
                        session = msg.session_id or session
                        if msg.is_error:
                            if msg.api_error_status == 429 or retry_at:
                                retry_at = retry_at or time.time() + RATE_LIMIT_FALLBACK_SECONDS
                            error = "; ".join(msg.errors or []) or msg.result or msg.subtype
                            return Outcome(text, session, msg.total_cost_usd or 0.0, error=error,
                                           retry_at=retry_at, started=started)
                        return Outcome(msg.result or text, session, msg.total_cost_usd or 0.0)
                    if job.stop():  # it asked main a question: end the turn, resume later with the answer
                        await client.interrupt()
                        return Outcome(text, session)
        except Exception as e:  # noqa: BLE001 - CLI missing, process died, session not found...
            return Outcome(text, session, error=f"{type(e).__name__}: {e}", retry_at=retry_at, started=started)
        return Outcome(text, session, error="the session ended without a result", retry_at=retry_at, started=started)
