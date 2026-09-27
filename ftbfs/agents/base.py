"""Backend-neutral contract for headless coding agents.

Stages describe *what* they need (tier, tools, output schema); a backend
maps that onto a concrete CLI (claude, opencode, ...). Every backend must:
  - write the exact prompt to <attempt_dir>/prompt.md
  - stream events to <attempt_dir>/transcript.jsonl while running
  - enforce the ToolPolicy, or refuse it (never silently widen it)
  - report usage; cost when the backend knows it
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Tier = Literal["small", "medium", "large"]


@dataclass(frozen=True)
class ToolPolicy:
    read: bool = True
    edit: bool = False
    bash_allow: tuple[str, ...] = ()  # command prefixes, e.g. "quilt"
    network: bool = False

    @classmethod
    def read_only(cls) -> ToolPolicy:
        return cls()

    @classmethod
    def none(cls) -> ToolPolicy:
        return cls(read=False)


@dataclass
class AgentRequest:
    prompt: str
    cwd: Path
    attempt_dir: Path
    tier: Tier = "small"
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy.none)
    max_turns: int = 1
    output_schema: dict | None = None  # JSON schema for the final answer
    timeout: int = 600
    # Replaces the backend's default (large, agentic) system prompt. Lean
    # prompts cut fixed overhead per call by ~20x for tool-less stages.
    system: str | None = None
    max_budget_usd: float | None = None  # hard stop for runaway sessions
    # Reasoning effort (low|medium|high|...): thinking tokens are most of
    # the output cost; backends without such a knob ignore it.
    effort: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class AgentResult:
    ok: bool
    text: str
    data: Any | None
    usage: Usage
    cost: float | None
    model: str
    backend: str
    duration_s: float
    transcript_path: Path
    error: str | None = None

    def usage_dict(self) -> dict:
        return asdict(self.usage)


class PolicyNotSupported(RuntimeError):
    pass


class AgentBackend(ABC):
    name: str
    # True when the backend enforces output_schema itself; otherwise the
    # schema is spelled out in the prompt and validated afterwards.
    native_schema: bool = False

    def __init__(self, tiers: dict[str, str] | None = None, **options):
        self.tiers = tiers or {}
        self.options = options

    def model_for(self, tier: Tier) -> str:
        try:
            return self.tiers[tier]
        except KeyError:
            raise ValueError(
                f"backend {self.name}: no model configured for tier {tier}"
            ) from None

    @abstractmethod
    def run(self, req: AgentRequest) -> AgentResult: ...

    # Shared helpers --------------------------------------------------------

    @staticmethod
    def schema_instructions(schema: dict) -> str:
        return (
            "\n\nWhen done, reply with ONLY a JSON object (no prose, no code"
            " fence) that validates against this JSON schema:\n"
            + json.dumps(schema, indent=1)
        )

    @staticmethod
    def extract_json(text: str) -> Any | None:
        """Parse a JSON answer, tolerating a surrounding code fence."""
        text = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
        start = min(
            (i for i in (text.find("{"), text.find("[")) if i >= 0),
            default=-1,
        )
        if start < 0:
            return None
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except json.JSONDecodeError:
                return None


def validate(data: Any, schema: dict) -> list[str]:
    """Minimal JSON-schema check: types, required, enum, nested items.

    Enough for the flat schemas stages use; keeps us free of a
    jsonschema dependency.
    """
    errors: list[str] = []
    _validate(data, schema, "$", errors)
    return errors


_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _validate(data, schema, path, errors):
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(
            isinstance(data, _TYPES[x])
            and not (x in ("integer", "number") and isinstance(data, bool))
            for x in types
        ):
            errors.append(f"{path}: expected {t}")
            return
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} not in {schema['enum']}")
    if isinstance(data, str | list):
        lo, hi = ("minLength", "maxLength") if isinstance(data, str) \
            else ("minItems", "maxItems")
        if lo in schema and len(data) < schema[lo]:
            errors.append(f"{path}: shorter than {schema[lo]}")
        if hi in schema and len(data) > schema[hi]:
            errors.append(f"{path}: longer than {schema[hi]}")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: missing {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                _validate(data[key], sub, f"{path}.{key}", errors)
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            _validate(item, schema["items"], f"{path}[{i}]", errors)
