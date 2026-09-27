"""Claude backend without calling Claude: a fake `claude` executable
emits stream-json like the real CLI."""

import json
import stat
import textwrap

import pytest

from ftbfs.agents.base import AgentRequest, PolicyNotSupported, ToolPolicy
from ftbfs.agents.claude import ClaudeBackend, tool_flags

FAKE = """\
#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
if "sleep" in prompt:
    time.sleep(30)
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": []}}),
      flush=True)
schema = json.loads(args[args.index("--json-schema") + 1]) \\
    if "--json-schema" in args else None
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "done", "structured_output": {"echo": prompt, "args": args}
        if schema else None,
    "usage": {"input_tokens": 10, "output_tokens": 5,
              "cache_read_input_tokens": 3,
              "cache_creation_input_tokens": 2},
    "modelUsage": {"claude-haiku-4-5-20251001": {}},
    "total_cost_usd": 0.001}), flush=True)
"""


@pytest.fixture
def backend(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text(textwrap.dedent(FAKE))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return ClaudeBackend(tiers={"small": "haiku", "medium": "sonnet"},
                         binary=str(exe))


def req(tmp_path, **kw):
    return AgentRequest(prompt=kw.pop("prompt", "hello"), cwd=tmp_path,
                        attempt_dir=tmp_path / "a1", **kw)


def test_tool_flags():
    assert tool_flags(ToolPolicy.none()) == ["--tools", ""]
    flags = tool_flags(ToolPolicy(read=True, edit=True,
                                  bash_allow=("quilt", "dpkg-source")))
    assert flags[:2] == ["--tools", "Read,Grep,Glob,Edit,Write,Bash"]
    assert "Bash(quilt:*)" in flags and "Bash(dpkg-source:*)" in flags
    assert flags[-2:] == ["--permission-mode", "acceptEdits"]
    with pytest.raises(PolicyNotSupported):
        tool_flags(ToolPolicy(bash_allow=("rm (*)",)))


def test_lean_isolated_command(backend, tmp_path):
    cmd = backend.command(req(tmp_path, system="be terse",
                              output_schema={"type": "object"},
                              max_budget_usd=0.5, effort="low"))
    for flag in ("--strict-mcp-config", "--no-session-persistence",
                 "--disable-slash-commands"):
        assert flag in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--system-prompt") + 1] == "be terse"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5"
    assert cmd[cmd.index("--effort") + 1] == "low"


def test_run_streams_transcript_and_parses_result(backend, tmp_path):
    r = backend.run(req(tmp_path, output_schema={"type": "object"}))
    assert r.ok and r.data["echo"] == "hello"
    assert r.cost == 0.001 and r.model == "claude-haiku-4-5-20251001"
    assert r.usage.input_tokens == 10 and r.usage.cache_read_tokens == 3
    lines = (tmp_path / "a1" / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(x)["type"] for x in lines] == [
        "system", "assistant", "result"]
    assert (tmp_path / "a1" / "prompt.md").read_text() == "hello"
    assert (tmp_path / "a1" / "pid").exists()


def test_timeout_kills_the_agent(backend, tmp_path):
    r = backend.run(req(tmp_path, prompt="sleep", timeout=1))
    assert not r.ok and "timeout" in r.error


def test_structured_answer_survives_cli_error_flag(backend):
    final = {"type": "result", "is_error": True,
             "subtype": "error_max_structured_output_retries",
             "structured_output": {"a": 1}, "usage": {},
             "total_cost_usd": 0.01}
    r = backend.parse_result(final, "m", 1.0, None)
    assert r.ok and r.data == {"a": 1}
    final["structured_output"] = None
    assert not backend.parse_result(final, "m", 1.0, None).ok
