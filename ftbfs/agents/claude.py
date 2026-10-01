# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Claude Code headless backend: `claude -p`.

Calls are isolated from the user's interactive setup (no CLAUDE.md,
settings, MCP servers, skills or session files), which keeps them
reproducible and cheap: with a stage-provided system prompt and no tools
the fixed overhead is ~400 input tokens instead of ~23k.

Events are streamed (--output-format stream-json) into transcript.jsonl
as they arrive, so a running agent can be followed live.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time

from .base import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    PolicyNotSupported,
    ToolPolicy,
    Usage,
)


def tool_flags(policy: ToolPolicy) -> list[str]:
    """Translate the abstract policy into --tools / --allowedTools."""
    tools: list[str] = []
    allowed: list[str] = []
    if policy.read:
        tools += ["Read", "Grep", "Glob"]
    if policy.edit:
        tools += ["Edit", "Write"]
    if policy.bash_allow:
        tools.append("Bash")
        for prefix in policy.bash_allow:
            if not prefix or any(c in prefix for c in "()*"):
                raise PolicyNotSupported(f"bad bash prefix {prefix!r}")
            allowed.append(f"Bash({prefix}:*)")
    if policy.network:
        tools += ["WebFetch", "WebSearch"]
    flags = ["--tools", ",".join(tools)]
    # Tools not explicitly allowed are denied in print mode (there is
    # nobody to ask), so this is a hard allowlist.
    allowed += [t for t in tools if t != "Bash"]
    if allowed:
        flags += ["--allowedTools", *allowed]
    if policy.edit:
        flags += ["--permission-mode", "acceptEdits"]
    return flags


class ClaudeBackend(AgentBackend):
    name = "claude"
    native_schema = True

    def command(self, req: AgentRequest) -> list[str]:
        cmd = [
            self.options.get("binary", "claude"), "-p",
            "--model", self.model_for(req.tier),
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            *tool_flags(req.tool_policy),
        ]
        if req.system:
            cmd += ["--system-prompt", req.system]
        if req.output_schema:
            cmd += ["--json-schema", json.dumps(req.output_schema)]
        if req.effort:
            cmd += ["--effort", req.effort]
        if req.max_budget_usd:
            cmd += ["--max-budget-usd", str(req.max_budget_usd)]
        if req.max_turns:
            cmd += ["--max-turns", str(req.max_turns)]
        return cmd

    def run(self, req: AgentRequest) -> AgentResult:
        req.attempt_dir.mkdir(parents=True, exist_ok=True)
        (req.attempt_dir / "prompt.md").write_text(req.prompt)
        transcript = req.attempt_dir / "transcript.jsonl"
        cmd = self.command(req)
        (req.attempt_dir / "command.json").write_text(json.dumps(cmd))
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=req.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        (req.attempt_dir / "pid").write_text(str(proc.pid))
        timer = threading.Timer(req.timeout, _kill, [proc])
        timer.start()
        final: dict | None = None
        try:
            proc.stdin.write(req.prompt)
            proc.stdin.close()
            with transcript.open("w") as out:
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        final = event
            stderr = proc.stderr.read()
            proc.wait()
        finally:
            timer.cancel()
        duration = time.monotonic() - start
        model = self.model_for(req.tier)
        if final is None:
            killed = proc.returncode in (-signal.SIGKILL, -signal.SIGTERM)
            error = (f"timeout after {req.timeout}s" if killed
                     else f"exit {proc.returncode}: {stderr.strip()[-500:]}")
            return AgentResult(False, "", None, Usage(), None, model,
                               self.name, duration, transcript, error)
        return self.parse_result(final, model, duration, transcript)

    def parse_result(self, final: dict, model: str, duration: float,
                     transcript) -> AgentResult:
        u = final.get("usage") or {}
        usage = Usage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_write_tokens=u.get("cache_creation_input_tokens", 0),
        )
        models = list((final.get("modelUsage") or {}).keys())
        text = final.get("result") or ""
        data = final.get("structured_output")
        if data is None and text:
            data = self.extract_json(text)
        is_error = bool(final.get("is_error"))
        # The CLI can flag an error (e.g. structured-output retry limit)
        # even though a structured answer was delivered; that answer is
        # still validated by the caller, so keep it.
        if is_error and final.get("structured_output") is not None:
            is_error = False
        return AgentResult(
            ok=not is_error,
            text=text,
            data=data,
            usage=usage,
            cost=final.get("total_cost_usd"),
            model=models[0] if models else model,
            backend=self.name,
            duration_s=duration,
            transcript_path=transcript,
            error=_error_text(final) if is_error else None,
        )


def _error_text(final: dict) -> str:
    """The CLI reports API failures (429 rate limit, ...) with subtype
    "success"; the status and message are what explain them."""
    parts = [final.get("subtype") or "error"]
    if final.get("api_error_status"):
        parts.append(f"API {final['api_error_status']}")
    if final.get("terminal_reason"):
        parts.append(final["terminal_reason"])
    msg = (final.get("result") or "").strip()
    if msg:
        parts.append(msg[:300])
    return ": ".join(parts)


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
