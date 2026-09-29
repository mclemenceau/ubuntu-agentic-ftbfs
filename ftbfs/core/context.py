"""What a stage sees while it runs: units, upstream results, agents, files.

Stages should only touch the outside world through this object so every
action is traced (events, prompts, transcripts, cost ledger).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agents.base import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    ToolPolicy,
    validate,
)
from .stage import UnitType

if TYPE_CHECKING:
    from ..builder.base import BuildResult
    from ..builder.pool import BuilderPool
    from ..db import DB
    from .pipeline import StageSpec


@dataclass(frozen=True)
class Paths:
    root: Path  # project dir (config, rules)
    work: Path  # per-unit artifacts
    cache: Path  # downloads shared across runs


class Units:
    """The selected items and the package/cluster units derived from them.

    The selection decides which units run. Cluster *membership* is global
    when a db is given: every active, classified item of the cluster, so
    a cluster's inputs (and cached LLM results) do not depend on how the
    run was filtered or sampled.
    """

    def __init__(self, items: list, db: DB | None = None):
        self.items: dict[str, dict] = {r["id"]: dict(r) for r in items}
        self.known: dict[str, dict] = dict(self.items)
        self.db = db
        self._members: dict[str, list[str]] = {}

    def of(self, utype: UnitType) -> list[str]:
        if utype == UnitType.ITEM:
            return list(self.items)
        key = self._key(utype)
        return sorted({i[key] for i in self.items.values() if i.get(key)})

    @staticmethod
    def _key(utype: UnitType) -> str:
        return "source" if utype == UnitType.PACKAGE else "cluster_id"

    def row(self, item_id: str) -> dict:
        return self.known[item_id]

    def children(self, utype: UnitType, uid: str) -> list[str]:
        if utype == UnitType.ITEM:
            return [uid]
        if utype == UnitType.CLUSTER and self.db is not None:
            return self._cluster_members(uid)
        key = self._key(utype)
        return [i for i, row in self.items.items() if row.get(key) == uid]

    def _cluster_members(self, cluster_id: str) -> list[str]:
        if cluster_id not in self._members:
            from ..filters import all_items

            rows = all_items(self.db, "WHERE i.cluster_id = ? AND"
                             " i.lifecycle != 'gone'", (cluster_id,))
            for r in rows:
                self.known.setdefault(r["id"], dict(r))
            self._members[cluster_id] = [r["id"] for r in rows]
        return self._members[cluster_id]

    def set_cluster(self, item_id: str, cluster_id: str | None) -> None:
        for row in (self.items.get(item_id), self.known.get(item_id)):
            if row is not None:
                row["cluster_id"] = cluster_id
        self._members.clear()

    def related(self, from_type: UnitType, uid: str,
                to_type: UnitType) -> list[str]:
        if from_type == to_type:
            return [uid]
        out: list[str] = []
        for item in self.children(from_type, uid):
            if to_type == UnitType.ITEM:
                target = item
            else:
                target = self.known[item].get(self._key(to_type))
            if target and target not in out:
                out.append(target)
        return out


@dataclass
class Ledger:
    usage: dict[str, int] = field(default_factory=dict)
    cost: float | None = None
    backend: str | None = None
    model: str | None = None

    def add(self, result: AgentResult, share: float) -> None:
        for k, v in result.usage_dict().items():
            self.usage[k] = self.usage.get(k, 0) + round(v * share)
        if result.cost is not None:
            self.cost = (self.cost or 0.0) + result.cost * share
        self.backend, self.model = result.backend, result.model


class Context:
    def __init__(self, *, db: DB, run_id: int | None, spec: StageSpec,
                 units: Units, backends: dict[str, AgentBackend],
                 paths: Paths, attempts: dict[str, int],
                 unit_types: dict[str, UnitType],
                 builders: BuilderPool | None = None):
        self.db = db
        self.builders = builders
        self.unit_types = unit_types
        self.run_id = run_id
        self.spec = spec
        self.units = units
        self.backends = backends
        self.paths = paths
        self.attempts = attempts
        self.ledger: dict[str, Ledger] = {}
        self._lock = threading.Lock()

    @property
    def stage(self):
        return self.spec.stage

    @property
    def options(self) -> dict[str, Any]:
        return self.spec.options

    # -- data -------------------------------------------------------------

    def item(self, item_id: str) -> dict:
        return self.units.row(item_id)

    def update_item(self, item_id: str, **fields) -> None:
        """Denormalize stage output onto the item row (and the in-memory
        unit, so later stages in this run see it)."""
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE item SET {cols} WHERE id=?",
                        (*fields.values(), item_id))
        self.units.items[item_id].update(fields)
        if "cluster_id" in fields:
            self.units.set_cluster(item_id, fields["cluster_id"])

    def package(self, source: str) -> dict | None:
        row = self.db.one("SELECT * FROM package WHERE source=?", (source,))
        return dict(row) if row else None

    def upstream(self, unit_id: str, stage: str) -> dict | None:
        """Latest result of an upstream stage for this unit, as
        {"status": ..., **data}; None when absent."""
        from .scheduler import latest_result

        dep_type = self.unit_types[stage]
        targets = self.units.related(self.stage.unit, unit_id, dep_type)
        if len(targets) != 1:
            return None
        row = latest_result(self.db, targets[0], stage)
        if row is None:
            return None
        return {**json.loads(row["data"]), "status": row["status"]}

    def result(self, unit_id: str, stage: str) -> dict | None:
        """Latest result of `stage` for exactly this unit id (any unit
        type), as {**data, "status": ...}; None when absent."""
        from .scheduler import latest_result

        row = latest_result(self.db, unit_id, stage)
        if row is None:
            return None
        return {**json.loads(row["data"]), "status": row["status"]}

    def feedback(self, unit_id: str) -> list[dict]:
        """Loop-back payloads sent to this stage for this unit, oldest
        first (e.g. review findings, verify failure excerpt)."""
        rows = self.db.query(
            "SELECT payload FROM event WHERE unit=? AND stage=?"
            " AND type='loop_back' ORDER BY id",
            (unit_id, self.spec.name),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- files ------------------------------------------------------------

    def workdir(self, unit_id: str) -> Path:
        unit = self.stage.unit
        if unit == UnitType.ITEM:
            item = self.item(unit_id)
            base = self.paths.work / item["source"] / item["version"]
            base = base / item["arch"]
        elif unit == UnitType.PACKAGE:
            base = self.paths.work / unit_id / "_package"
        else:
            safe = re.sub(r"[^\w.:+-]", "_", unit_id)
            base = self.paths.work / "_clusters" / safe
        path = base / self.spec.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def attempt_dir(self, unit_id: str) -> Path:
        path = self.workdir(unit_id) / f"attempt-{self.attempts[unit_id]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def event(self, type_: str, unit: str | None = None, **payload) -> int:
        return self.db.event(type_, run_id=self.run_id, unit=unit,
                             stage=self.spec.name, **payload)

    # -- builds -----------------------------------------------------------

    def can_build(self, arch: str) -> bool:
        return self.builders is not None and self.builders.supports(arch)

    def build(self, unit_id: str, dsc: Path, arch: str, dist: str,
              build_dir: Path, extra_repos: list[str], /,
              **event) -> BuildResult:
        """sbuild on whichever builder has a free slot for `arch`.
        The stage options `parallel`, `timeout` and `idle_timeout`
        apply; a builder's own `parallel` takes precedence. A builder
        that cannot build (host down) is taken out of the pool and the
        build goes to another one."""
        from ..builder import local
        from ..builder.base import BuilderUnavailable

        if self.builders is None:
            raise RuntimeError("no builders configured")
        opts = self.options
        while True:
            with self.builders.slot(arch) as b:
                self.event("build_start", unit=unit_id, where=b.name,
                           arch=arch, **event)
                try:
                    res = b.build(
                        dsc, arch, dist, build_dir, extra_repos,
                        parallel=b.parallel or opts.get("parallel", 8),
                        timeout=opts.get("timeout", 4 * 3600),
                        idle_timeout=opts.get("idle_timeout",
                                              local.IDLE_TIMEOUT))
                except BuilderUnavailable as e:
                    self.event("build_end", unit=unit_id, where=b.name,
                               ok=False, error=str(e))
                    self.builders.mark_down(b.name, str(e))
                    self.event("builder_down", unit=unit_id,
                               builder=b.name, error=str(e))
                    continue
                except Exception as e:
                    self.event("build_end", unit=unit_id, where=b.name,
                               ok=False, error=repr(e))
                    raise
            break
        res.builder = b.name
        self.event("build_end", unit=unit_id, where=b.name, ok=res.ok,
                   exit=res.exit_code, duration_s=round(res.duration_s))
        return res

    # -- agents -----------------------------------------------------------

    def run_agent(self, unit_ids: str | list[str], prompt: str, *,
                  tool_policy: ToolPolicy | None = None,
                  output_schema: dict | None = None,
                  max_turns: int | None = None,
                  cwd: Path | None = None,
                  attempt_dir: Path | None = None,
                  system: str | None = None) -> AgentResult:
        """Run the stage's configured agent. With several unit ids (a
        packed batch) the cost is split evenly between them."""
        spec = self.spec.agent
        if spec is None:
            raise RuntimeError(f"{self.spec.name} is not an agent stage")
        uids = [unit_ids] if isinstance(unit_ids, str) else list(unit_ids)
        backend = self.backends[spec.backend]
        tier = spec.tier
        loops = max(len(self.feedback(u)) for u in uids)
        if (spec.escalate_after_loops is not None
                and loops >= spec.escalate_after_loops):
            tier = spec.escalate_tier
        adir = attempt_dir or self.attempt_dir(uids[0])
        full_prompt = prompt
        if output_schema and not backend.native_schema:
            full_prompt += backend.schema_instructions(output_schema)
        req = AgentRequest(
            prompt=full_prompt,
            cwd=cwd or adir,
            attempt_dir=adir,
            tier=tier,
            tool_policy=tool_policy or ToolPolicy.none(),
            max_turns=max_turns or spec.max_turns,
            output_schema=output_schema,
            timeout=spec.timeout,
            system=system,
            max_budget_usd=spec.max_budget_usd,
            effort=spec.effort,
        )
        self.event("agent_call_start", unit=uids[0], units=uids,
                   backend=backend.name, model=backend.model_for(tier),
                   tier=tier, attempt_dir=str(adir))
        try:
            result = backend.run(req)
        except Exception as e:
            self.event("agent_call_end", unit=uids[0], ok=False,
                       error=repr(e))
            raise
        if result.ok and output_schema is not None:
            errors = (validate(result.data, output_schema)
                      if result.data is not None else ["no JSON found"])
            if errors and backend.native_schema:
                # The backend already enforced the schema; a mismatch
                # here means a degenerate answer. Fail (and retry later)
                # rather than "repairing" it with a smaller model.
                result.ok = False
                result.error = f"schema validation failed: {errors}"
            elif errors:
                result = self._repair(backend, req, result, errors)
        with self._lock:
            for u in uids:
                self.ledger.setdefault(u, Ledger()).add(
                    result, 1 / len(uids)
                )
        (adir / "usage.json").write_text(json.dumps({
            "backend": result.backend, "model": result.model,
            "usage": result.usage_dict(), "cost": result.cost,
            "duration_s": round(result.duration_s, 2), "ok": result.ok,
            "error": result.error,
        }, indent=1))
        if result.data is not None:
            (adir / "result.json").write_text(
                json.dumps(result.data, indent=1)
            )
        self.event("agent_call_end", unit=uids[0], ok=result.ok,
                   error=result.error, cost=result.cost,
                   usage=result.usage_dict(),
                   duration_s=round(result.duration_s, 2))
        return result

    def _repair(self, backend: AgentBackend, req: AgentRequest,
                bad: AgentResult, errors: list[str]) -> AgentResult:
        """One retry that shows the answer and the schema errors. It runs
        on the same tier: a smaller model would be rewriting, and then be
        credited with, the content of a bigger model's answer."""
        repair_dir = req.attempt_dir / "repair"
        prompt = (
            "Your previous answer did not match the required JSON schema."
            f"\nErrors: {errors}\nPrevious answer:\n{bad.text}"
            + backend.schema_instructions(req.output_schema)
        )
        fixed = backend.run(AgentRequest(
            prompt=prompt, cwd=req.cwd, attempt_dir=repair_dir,
            tier=req.tier, output_schema=req.output_schema,
            system=req.system or "Reply with JSON only.",
            timeout=req.timeout,
        ))
        fixed.usage.input_tokens += bad.usage.input_tokens
        fixed.usage.output_tokens += bad.usage.output_tokens
        if bad.cost is not None:
            fixed.cost = (fixed.cost or 0.0) + bad.cost
        still = (validate(fixed.data, req.output_schema)
                 if fixed.data is not None else ["no JSON found"])
        if still:
            fixed.ok = False
            fixed.error = f"schema validation failed: {still}"
        return fixed

