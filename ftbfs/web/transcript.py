"""Turn a backend's transcript.jsonl into console entries.

Understands the claude stream-json events, opencode's --format json
events and the fake backend's lines; anything else is shown raw, so a new
backend is visible before it gets a dedicated renderer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT = 4000


@dataclass
class Entry:
    kind: str  # start | text | thinking | tool | result | final | raw
    title: str
    body: str = ""
    error: bool = False


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = text if isinstance(text, str) else json.dumps(text, indent=1)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters]"


def _tool_input(name: str, args: dict, cwd: str | None) -> str:
    def rel(p):
        return p[len(cwd) + 1:] if cwd and str(p).startswith(cwd + "/") \
            else p

    if name in ("Read", "Write") and "file_path" in args:
        return rel(args["file_path"])
    if name == "Edit" and "file_path" in args:
        return (f"{rel(args['file_path'])}\n--- old\n"
                f"{args.get('old_string', '')}\n+++ new\n"
                f"{args.get('new_string', '')}")
    if name in ("Grep", "Glob"):
        where = rel(args["path"]) if args.get("path") else ""
        return f"{args.get('pattern', '')}  {where}".strip()
    if name == "Bash":
        return args.get("command", "")
    return json.dumps(args, indent=1)


# opencode tool names and argument keys, in the claude vocabulary that
# _tool_input renders.
_OC_TOOLS = {"read": "Read", "write": "Write", "edit": "Edit",
             "grep": "Grep", "glob": "Glob", "bash": "Bash",
             "list": "List", "webfetch": "WebFetch"}
_OC_ARGS = {"filePath": "file_path", "oldString": "old_string",
            "newString": "new_string"}


def _opencode_tool(part: dict, state: dict) -> list[Entry]:
    name = _OC_TOOLS.get(part.get("tool"), part.get("tool", "?"))
    st = part.get("state") or {}
    args = {_OC_ARGS.get(k, k): v for k, v in (st.get("input") or {}).items()}
    out = [Entry("tool", name, _clip(_tool_input(name, args,
                                                 state.get("cwd"))))]
    if st.get("status") == "error":
        out.append(Entry("result", f"{name} result",
                         _clip(str(st.get("error", "")), 1500), error=True))
    elif "output" in st:
        out.append(Entry("result", f"{name} result",
                         _clip(str(st["output"]), 1500)))
    return out


def _opencode(e: dict, state: dict) -> list[Entry] | None:
    """Entries for an opencode event (or the ftbfs_* lines the backend
    writes around them); None when the line is not one of those."""
    kind, part = e.get("type"), e.get("part") or {}
    if kind == "ftbfs_start":
        state["cwd"] = e.get("cwd")
        perm = e.get("permission") or {}
        tools = ", ".join(k for k, v in perm.items()
                          if v != "deny" and k != "*") or "none"
        return [Entry("start", f"session start: {e.get('model', '?')}",
                      f"tools: {tools}")]
    if kind == "ftbfs_end":
        bits = [f"{e.get('steps', '?')} steps"]
        if e.get("cost") is not None:
            bits.append(f"${e['cost']:.4f}")
        if e.get("duration_s"):
            bits.append(f"{e['duration_s']:.0f} s")
        err = not e.get("ok")
        return [Entry("final", "finished" + (" with error" if err else ""),
                      ", ".join(bits) + (f"\n{e.get('error')}" if err
                                         else ""), error=err)]
    if kind == "text" and "part" in e:
        text = part.get("text", "")
        return [Entry("text", "assistant", _clip(text))] \
            if text.strip() else []
    if kind == "reasoning":
        text = part.get("text", "")
        return [Entry("thinking", "thinking", _clip(text))] \
            if text.strip() else []
    if kind == "tool_use":
        return _opencode_tool(part, state)
    if kind == "error":
        err = e.get("error") or {}
        msg = (err.get("data") or {}).get("message") or json.dumps(err)
        return [Entry("final", f"error: {err.get('name', '?')}",
                      _clip(msg), error=True)]
    if kind in ("step_start", "step_finish"):
        return []  # bookkeeping (tokens, cost), totalled in ftbfs_end
    return None


def _result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else
                         str(c) for c in content)
    return str(content)


def parse_line(line: str, state: dict) -> list[Entry]:
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return [Entry("raw", "output", _clip(line))] if line.strip() else []
    kind = e.get("type")
    oc = _opencode(e, state)
    if oc is not None:
        return oc
    if kind == "system" and e.get("subtype") == "init":
        state["cwd"] = e.get("cwd")
        tools = ", ".join(e.get("tools") or []) or "none"
        return [Entry("start", f"session start: {e.get('model', '?')}",
                      f"tools: {tools}")]
    if kind == "assistant" and "message" in e:
        out = []
        for c in e["message"].get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(Entry("text", "assistant", _clip(c["text"])))
            elif c.get("type") == "thinking" and c.get("thinking"):
                out.append(Entry("thinking", "thinking",
                                 _clip(c["thinking"])))
            elif c.get("type") == "tool_use":
                name = c.get("name", "?")
                state.setdefault("tools", {})[c.get("id")] = name
                out.append(Entry("tool", name, _clip(_tool_input(
                    name, c.get("input") or {}, state.get("cwd")))))
        return out
    if kind == "assistant":  # fake backend
        return [Entry("text", "assistant", _clip(e.get("text", "")))]
    if kind == "user" and "message" in e:
        out = []
        for c in e["message"].get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                name = state.get("tools", {}).get(c.get("tool_use_id"),
                                                  "tool")
                out.append(Entry("result", f"{name} result",
                                 _clip(_result_text(c.get("content")),
                                       1500),
                                 error=bool(c.get("is_error"))))
        return out
    if kind == "result":
        cost = e.get("total_cost_usd")
        bits = [f"{e.get('num_turns', '?')} turns"]
        if cost is not None:
            bits.append(f"${cost:.4f}")
        if e.get("duration_ms"):
            bits.append(f"{e['duration_ms'] / 1000:.0f} s")
        err = bool(e.get("is_error"))
        return [Entry("final", "finished" + (" with error" if err else ""),
                      ", ".join(bits) + ("\n" + _clip(e.get("result", ""))
                                         if err else ""), error=err)]
    if kind in ("rate_limit_event", "stream_event", "system"):
        return []  # bookkeeping (token estimates, limits), not activity
    return [Entry("raw", kind or "event", _clip(line, 800))]


def read(path: Path, offset: int = 0,
         state: dict | None = None) -> tuple[list[Entry], int]:
    """Entries from byte `offset` on; returns the new offset. Only whole
    lines are consumed, so a line being written is picked up next time."""
    state = state if state is not None else {}
    entries: list[Entry] = []
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return entries, offset
    end = data.rfind(b"\n") + 1
    for raw in data[:end].splitlines():
        entries += parse_line(raw.decode(errors="replace"), state)
    return entries, offset + end
