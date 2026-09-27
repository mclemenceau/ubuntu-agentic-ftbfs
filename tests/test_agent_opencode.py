"""opencode backend without calling opencode: a fake `opencode` executable
emits --format json events like the real CLI (1.18)."""

import json
import stat
import textwrap

import pytest

from ftbfs.agents import make_backend
from ftbfs.agents.base import AgentRequest, PolicyNotSupported, ToolPolicy
from ftbfs.agents.opencode import OpencodeBackend, permission
from ftbfs.web import transcript

FAKE = """\
#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
conf = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
def emit(kind, **part):
    print(json.dumps({"type": kind, "part": part}), flush=True)
def step_end(cost, reason="stop"):
    emit("step_finish", reason=reason, cost=cost, tokens={
        "input": 100, "output": 10, "reasoning": 5,
        "cache": {"read": 50, "write": 20}})
if "sleep" in prompt:
    time.sleep(30)
if "fail" in prompt:
    print(json.dumps({"type": "error", "error": {
        "name": "APIError", "data": {"message": "429 rate limited"}}}),
        flush=True)
    sys.exit(1)
if "spend" in prompt:
    for _ in range(50):
        emit("step_start")
        step_end(0.1, "tool-calls")
        time.sleep(0.05)
emit("step_start")
emit("text", text="Let me look.")
emit("tool_use", tool="read", state={"status": "completed",
     "input": {"filePath": "debian/rules"}, "output": "#!/usr/bin/make"})
step_end(0.001, "tool-calls")
emit("step_start")
if "empty" not in prompt:
    emit("text", text="```json\\n" + json.dumps({
        "prompt": prompt, "argv": sys.argv[1:], "conf": conf,
        "env": {k: v for k, v in os.environ.items()
                if k.startswith(("XDG_", "OPENCODE_"))}}) + "\\n```")
step_end(0.002)
"""


@pytest.fixture
def backend(tmp_path):
    exe = tmp_path / "opencode"
    exe.write_text(textwrap.dedent(FAKE))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return OpencodeBackend(
        tiers={"small": "openrouter/anthropic/claude-haiku-4.5",
               "medium": "openrouter/anthropic/claude-sonnet-5"},
        state_dir=tmp_path / "state", binary=str(exe))


def req(tmp_path, **kw):
    return AgentRequest(prompt=kw.pop("prompt", "hello"), cwd=tmp_path,
                        attempt_dir=tmp_path / "a1", **kw)


def test_permission_is_an_allowlist():
    assert permission(ToolPolicy.none()) == {
        "*": "deny", "external_directory": "deny"}
    p = permission(ToolPolicy(read=True, edit=True,
                              bash_allow=("quilt",)))
    assert list(p)[0] == "*"  # later rules win: the deny comes first
    assert p["read"] == p["grep"] == p["edit"] == "allow"
    assert p["bash"] == {"*": "deny", "quilt": "allow",
                         "quilt *": "allow"}
    assert "webfetch" not in p
    assert p["external_directory"] == "deny"
    with pytest.raises(PolicyNotSupported):
        permission(ToolPolicy(bash_allow=("rm *",)))


def test_isolated_config_and_env(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/me/opencode.json")
    r = backend.run(req(tmp_path, system="be terse", effort="low",
                        max_turns=5))
    assert r.ok, r.error
    agent = r.data["conf"]["agent"]["ftbfs"]
    assert agent["model"] == "openrouter/anthropic/claude-haiku-4.5"
    assert agent["prompt"] == "be terse"
    assert agent["variant"] == "low" and agent["steps"] == 5
    assert agent["permission"] == {"*": "deny",
                                   "external_directory": "deny"}
    conf = r.data["conf"]
    assert conf["mcp"] == {} and conf["plugin"] == []
    assert conf["enabled_providers"] == ["openrouter"]
    env = r.data["env"]
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "state" / "opencode")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert "OPENCODE_CONFIG" not in env
    argv = r.data["argv"]
    assert argv[:4] == ["run", "--pure", "--format", "json"]
    assert argv[argv.index("--agent") + 1] == "ftbfs"
    assert "--title" in argv  # no LLM call to make one up


def test_unset_options_are_left_to_opencode(backend, tmp_path):
    agent = backend.config(req(tmp_path))["agent"]["ftbfs"]
    assert not {"prompt", "variant", "steps"} & set(agent)


def test_prompt_on_stdin_answer_from_last_step(backend, tmp_path):
    big = "x" * 300_000  # over the 128 KiB argv string limit
    r = backend.run(req(tmp_path, prompt=big))
    assert r.ok and r.data["prompt"] == big
    assert r.cost == pytest.approx(0.003)
    assert r.usage.input_tokens == 200 and r.usage.output_tokens == 30
    assert r.usage.cache_read_tokens == 100
    assert r.usage.cache_write_tokens == 40
    assert r.model == "openrouter/anthropic/claude-haiku-4.5"
    assert (tmp_path / "a1" / "prompt.md").read_text() == big
    assert (tmp_path / "a1" / "pid").exists()
    cmd = json.loads((tmp_path / "a1" / "command.json").read_text())
    assert cmd["config"]["agent"]["ftbfs"]["model"] == r.model


def test_transcript_is_bookended_and_renders(backend, tmp_path):
    backend.run(req(tmp_path, tool_policy=ToolPolicy.read_only()))
    path = tmp_path / "a1" / "transcript.jsonl"
    kinds = [json.loads(x)["type"] for x in path.read_text().splitlines()]
    assert kinds[0] == "ftbfs_start" and kinds[-1] == "ftbfs_end"
    entries, _ = transcript.read(path)
    assert [e.kind for e in entries] == [
        "start", "text", "tool", "result", "text", "final"]
    assert entries[0].body == "tools: read, grep, glob, list"
    assert entries[2].title == "Read" and entries[2].body == "debian/rules"
    assert entries[-1].body.startswith("2 steps, $0.0030")
    assert not entries[-1].error


def test_api_error_is_reported(backend, tmp_path):
    r = backend.run(req(tmp_path, prompt="fail"))
    assert not r.ok and r.error == "APIError: 429 rate limited"
    entries, _ = transcript.read(tmp_path / "a1" / "transcript.jsonl")
    assert entries[-1].error and "429" in entries[-1].body


def test_empty_answer_is_an_error(backend, tmp_path):
    r = backend.run(req(tmp_path, prompt="empty"))
    assert not r.ok and r.error == "empty answer" and r.data is None


def test_budget_kills_the_agent(backend, tmp_path):
    r = backend.run(req(tmp_path, prompt="spend", max_budget_usd=0.25))
    assert not r.ok and r.error.startswith("budget exceeded: $0.3")
    assert r.cost < 1.0


def test_timeout_kills_the_agent(backend, tmp_path):
    r = backend.run(req(tmp_path, prompt="sleep", timeout=1))
    assert not r.ok and r.error == "timeout after 1s"


def test_registered_with_state_dir(tmp_path):
    b = make_backend("opencode", {"state_dir": tmp_path,
                                  "tiers": {"small": "p/m"}})
    assert isinstance(b, OpencodeBackend)
    assert b.config_home == tmp_path / "opencode"


def test_dedicated_key_is_a_file_reference(backend, tmp_path):
    key = tmp_path / "or.key"
    key.write_text("sk-or-v1-secret\n")
    backend.options["api_keys"] = {"openrouter": str(key)}
    r = backend.run(req(tmp_path))
    assert r.ok, r.error
    assert r.data["conf"]["provider"] == {
        "openrouter": {"options": {"apiKey": f"{{file:{key}}}"}}}
    for f in (tmp_path / "a1").iterdir():
        assert "sk-or-v1-secret" not in f.read_text()


def test_missing_key_file_fails_without_fallback(backend, tmp_path):
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    for path in (tmp_path / "absent.key", empty):
        backend.options["api_keys"] = {"openrouter": str(path)}
        r = backend.run(req(tmp_path))
        assert not r.ok and r.error == (
            f"missing API key file: openrouter: {path}")
        assert not (tmp_path / "a1" / "pid").exists()  # never spawned
