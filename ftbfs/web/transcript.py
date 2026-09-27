"""Turn a backend's transcript.jsonl into console entries.

Understands the claude stream-json events and the fake backend's lines;
anything else is shown raw, so a new backend is visible before it gets
a dedicated renderer.
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
