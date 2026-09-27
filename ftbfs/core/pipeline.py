"""pipeline.toml: the declarative DAG of stages.

    [stage.<name>]
    after   = ["other", ...]        # dependencies (must be ok)
    when    = "triage.fixable != 'no'"   # optional predicate
    gate    = "manual"              # optional human approval
    enabled = true                  # default true
    agent   = { backend = "claude", tier = "medium", max_turns = 30,
                escalate_after_loops = 1, escalate_tier = "large" }
    on_fail = { goto = "dev", max_loops = 2 }
    # any other key is passed to the stage as an option
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .expr import compile_expr
from .stage import Kind, Stage

_KNOWN_KEYS = {"after", "when", "gate", "enabled", "agent", "on_fail"}


class PipelineError(ValueError):
    pass


@dataclass
class AgentSpec:
    backend: str
    tier: str = "small"
    max_turns: int | None = None
    escalate_after_loops: int | None = None
    escalate_tier: str = "large"
    max_budget_usd: float | None = None
    timeout: int = 900
    effort: str | None = None


@dataclass
class OnFail:
    goto: str
    max_loops: int = 2


@dataclass
class StageSpec:
    name: str
    stage: Stage
    after: list[str] = field(default_factory=list)
    when: str | None = None
    gate: str | None = None
    agent: AgentSpec | None = None
    on_fail: OnFail | None = None
    options: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Pipeline:
    specs: dict[str, StageSpec]  # topologically ordered
    hash: str

    def __iter__(self):
        return iter(self.specs.values())

    def __getitem__(self, name: str) -> StageSpec:
        return self.specs[name]

    def ancestors(self, name: str) -> list[str]:
        seen: list[str] = []
        todo = list(self.specs[name].after)
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.append(n)
                todo.extend(self.specs[n].after)
        return [n for n in self.specs if n in seen]  # DAG order

    def descendants(self, name: str) -> list[str]:
        return [n for n in self.specs if name in self.ancestors(n)]


def load(path: Path, registry: dict[str, type[Stage]],
         default_backend: str) -> Pipeline:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    return build(raw, registry, default_backend)


def build(raw: dict, registry: dict[str, type[Stage]],
          default_backend: str) -> Pipeline:
    stages_raw = {
        name: conf
        for name, conf in raw.get("stage", {}).items()
        if conf.get("enabled", True)
    }
    specs: dict[str, StageSpec] = {}
    for name, conf in stages_raw.items():
        if name not in registry:
            raise PipelineError(
                f"stage {name!r} is not registered; known: "
                f"{sorted(registry)}"
            )
        stage = registry[name]()
        spec = StageSpec(
            name=name,
            stage=stage,
            after=list(conf.get("after", [])),
            when=conf.get("when"),
            gate=conf.get("gate"),
            options={k: v for k, v in conf.items() if k not in _KNOWN_KEYS},
        )
        if spec.gate not in (None, "manual"):
            raise PipelineError(f"{name}: gate must be 'manual' if set")
        if spec.when:
            compile_expr(spec.when)
        if stage.kind == Kind.AGENT:
            agent = dict(conf.get("agent", {}))
            agent.setdefault("backend", default_backend)
            spec.agent = AgentSpec(**agent)
        elif "agent" in conf:
            raise PipelineError(f"{name}: 'agent' set on a {stage.kind} "
                                "stage")
        if stage.kind == Kind.OUTWARD and spec.gate != "manual":
            spec.gate = "manual"
            spec.notes.append("outward stage: manual gate enforced")
        if "on_fail" in conf:
            spec.on_fail = OnFail(**conf["on_fail"])
        specs[name] = spec

    for spec in specs.values():
        for dep in spec.after:
            if dep not in specs:
                raise PipelineError(
                    f"{spec.name}: depends on unknown or disabled "
                    f"stage {dep!r}"
                )
    ordered = _toposort(specs)
    pipeline = Pipeline(specs=ordered, hash=_hash(stages_raw))
    for spec in pipeline:
        if spec.on_fail and spec.on_fail.goto not in pipeline.ancestors(
            spec.name
        ):
            raise PipelineError(
                f"{spec.name}: on_fail.goto {spec.on_fail.goto!r} must be "
                "an upstream stage"
            )
    return pipeline


def _toposort(specs: dict[str, StageSpec]) -> dict[str, StageSpec]:
    ordered: dict[str, StageSpec] = {}
    visiting: set[str] = set()

    def visit(name: str, chain: list[str]) -> None:
        if name in ordered:
            return
        if name in visiting:
            raise PipelineError(f"cycle: {' -> '.join([*chain, name])}")
        visiting.add(name)
        for dep in specs[name].after:
            visit(dep, [*chain, name])
        visiting.discard(name)
        ordered[name] = specs[name]

    for name in specs:
        visit(name, [])
    return ordered


def _hash(raw: dict) -> str:
    blob = json.dumps(raw, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
