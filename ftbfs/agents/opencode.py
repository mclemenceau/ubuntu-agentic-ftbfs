# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""opencode headless backend: `opencode run --format json`.

Models are `provider/model` ids (e.g. openrouter/anthropic/claude-haiku-4.5).
Credentials come from `api_keys` ({provider: key file}) when set, so the
pipeline's spend is kept apart from interactive use; the key is passed as
a `{file:...}` reference and never appears in the config, transcripts or
database. Providers without an entry use opencode's own credentials
(`opencode auth login`).

Calls are isolated from the user's interactive setup: a private config
home (no global config, MCP servers, plugins or skills), no project
config, no ~/.claude rules. The whole configuration is one inline agent,
`ftbfs`, whose permissions are an explicit allowlist built from the
ToolPolicy; tools it does not allow are not offered to the model at all,
and nothing outside the working directory can be touched.

opencode has no structured output, so the schema goes in the prompt and
the answer is validated (and repaired) by the caller. There is no budget
flag either: cost is summed from the step events and the agent is killed
once it exceeds max_budget_usd.

The transcript is opencode's raw event stream between two lines written
here, `ftbfs_start` (model, permissions) and `ftbfs_end` (outcome).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .base import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    PolicyNotSupported,
    ToolPolicy,
    Usage,
)

AGENT = "ftbfs"

# Environment variables that would pull the user's own config back in.
_LEAKY_ENV = ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR",
              "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION")


def permission(policy: ToolPolicy) -> dict:
    """Translate the abstract policy into an opencode permission map.

    Everything is denied, then the policy's tools are allowed. Later
    rules win in opencode, so the order of the keys matters.
    """
    perm: dict = {"*": "deny"}
    if policy.read:
        perm |= {t: "allow" for t in ("read", "grep", "glob", "list")}
    if policy.edit:
        perm["edit"] = "allow"  # covers edit, write and patch
    if policy.bash_allow:
        bash = {"*": "deny"}
        for prefix in policy.bash_allow:
            if not prefix or any(c in prefix for c in "*?()"):
                raise PolicyNotSupported(f"bad bash prefix {prefix!r}")
            bash[prefix] = "allow"
            bash[f"{prefix} *"] = "allow"
        perm["bash"] = bash
    if policy.network:
        perm |= {"webfetch": "allow", "websearch": "allow"}
    perm["external_directory"] = "deny"
    return perm


class OpencodeBackend(AgentBackend):
    name = "opencode"
    native_schema = False

    @property
    def key_files(self) -> dict[str, Path]:
        return {provider: Path(path).expanduser().resolve()
                for provider, path in
                (self.options.get("api_keys") or {}).items()}

    def missing_keys(self) -> list[str]:
        """Key files that are absent or empty: never fall back silently
        to the user's own credentials."""
        return [f"{provider}: {path}"
                for provider, path in self.key_files.items()
                if not (path.is_file() and path.read_text().strip())]

    @property
    def config_home(self) -> Path:
        # opencode keeps plugin dependencies in its config home, so it is
        # persistent (and private) rather than per call.
        return self.state_dir / "opencode"

    def config(self, req: AgentRequest) -> dict:
        model = self.model_for(req.tier)
        agent: dict = {
            "mode": "primary",
            "model": model,
            "permission": permission(req.tool_policy),
        }
        if req.system:
            agent["prompt"] = req.system
        if req.effort:
            agent["variant"] = req.effort
        if req.max_turns:
            agent["steps"] = req.max_turns
        providers = sorted({m.split("/", 1)[0]
                            for m in self.tiers.values() if m})
        conf = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "lsp": False,
            "formatter": False,
            "mcp": {},
            "plugin": [],
            "instructions": [],
            "enabled_providers": providers,
            "small_model": model,
            "default_agent": AGENT,
            "agent": {AGENT: agent},
        }
        if self.key_files:
            conf["provider"] = {
                provider: {"options": {"apiKey": f"{{file:{path}}}"}}
                for provider, path in self.key_files.items()}
        return conf

    def env(self, req: AgentRequest) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _LEAKY_ENV}
        env |= {
            "XDG_CONFIG_HOME": str(self.config_home.resolve()),
            "OPENCODE_CONFIG_CONTENT": json.dumps(self.config(req)),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
        return env

    def command(self, req: AgentRequest) -> list[str]:
        # The prompt goes on stdin: a single argv string is capped at
        # 128 KiB. A title avoids an extra LLM call to generate one.
        title = "ftbfs " + "/".join(req.attempt_dir.parts[-4:])
        return [self.options.get("binary", "opencode"), "run", "--pure",
                "--format", "json", "--thinking", "--agent", AGENT,
                "--title", title]

    def run(self, req: AgentRequest) -> AgentResult:
        req.attempt_dir.mkdir(parents=True, exist_ok=True)
        missing = self.missing_keys()
        if missing:
            return AgentResult(
                False, "", None, Usage(), None, self.model_for(req.tier),
                self.name, 0.0, req.attempt_dir / "transcript.jsonl",
                "missing API key file: " + ", ".join(missing))
        self.config_home.mkdir(parents=True, exist_ok=True)
        (req.attempt_dir / "prompt.md").write_text(req.prompt)
        transcript = req.attempt_dir / "transcript.jsonl"
        cmd = self.command(req)
        conf = self.config(req)
        (req.attempt_dir / "command.json").write_text(json.dumps(
            {"argv": cmd, "config": conf}, indent=1))
        model = self.model_for(req.tier)
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=req.cwd, env=self.env(req), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        (req.attempt_dir / "pid").write_text(str(proc.pid))
        killed: list[str] = []
        timer = threading.Timer(req.timeout, _kill,
                                [proc, killed, f"timeout after "
                                 f"{req.timeout}s"])
        timer.start()
        run = _Run()
        try:
            proc.stdin.write(req.prompt)
            proc.stdin.close()
            with transcript.open("w") as out:
                out.write(json.dumps({
                    "type": "ftbfs_start", "backend": self.name,
                    "model": model, "cwd": str(req.cwd),
                    "permission": conf["agent"][AGENT]["permission"],
                }) + "\n")
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    run.feed(line)
                    if (req.max_budget_usd is not None
                            and run.cost > req.max_budget_usd):
                        _kill(proc, killed,
                              f"budget exceeded: ${run.cost:.4f} >"
                              f" ${req.max_budget_usd}")
                stderr = proc.stderr.read()
                proc.wait()
                duration = time.monotonic() - start
                error = self._error(proc, run, killed, stderr)
                out.write(json.dumps({
                    "type": "ftbfs_end", "ok": error is None,
                    "error": error, "steps": run.steps,
                    "cost": run.cost, "duration_s": round(duration, 2),
                }) + "\n")
        finally:
            timer.cancel()
        text = run.answer()
        return AgentResult(
            ok=error is None,
            text=text,
            data=self.extract_json(text) if error is None else None,
            usage=run.usage,
            cost=run.cost,
            model=model,
            backend=self.name,
            duration_s=duration,
            transcript_path=transcript,
            error=error,
        )

    @staticmethod
    def _error(proc, run: _Run, killed: list[str],
               stderr: str) -> str | None:
        if killed:
            return killed[0]
        if run.errors:
            return "; ".join(run.errors)[:500]
        if proc.returncode != 0:
            return f"exit {proc.returncode}: {stderr.strip()[-500:]}"
        if not run.answer().strip():
            return "empty answer"
        return None


class _Run:
    """What the event stream says so far: usage, cost, the answer."""

    def __init__(self):
        self.usage = Usage()
        self.cost = 0.0
        self.steps = 0
        self.errors: list[str] = []
        self._texts: list[str] = []  # text parts of the current step

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "step_start":
            self._texts = []
        elif kind == "text":
            self._texts.append(part.get("text", ""))
        elif kind == "step_finish":
            self.steps += 1
            self.cost += part.get("cost") or 0.0
            t = part.get("tokens") or {}
            cache = t.get("cache") or {}
            self.usage.input_tokens += t.get("input", 0)
            self.usage.output_tokens += (t.get("output", 0)
                                         + t.get("reasoning", 0))
            self.usage.cache_read_tokens += cache.get("read", 0)
            self.usage.cache_write_tokens += cache.get("write", 0)
        elif kind == "error":
            err = event.get("error") or {}
            data = err.get("data") or {}
            self.errors.append(": ".join(
                str(x) for x in (err.get("name"), data.get("message"))
                if x) or json.dumps(err)[:300])

    def answer(self) -> str:
        """The final answer: the text of the last step."""
        return "".join(self._texts)


def _kill(proc: subprocess.Popen, killed: list[str], why: str) -> None:
    if not killed:
        killed.append(why)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
