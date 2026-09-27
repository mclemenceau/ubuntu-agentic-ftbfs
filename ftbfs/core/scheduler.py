"""Runs the pipeline DAG over the selected units.

There is no hard-coded state machine. For each stage, in DAG order, a unit
runs when:
  1. every `after` dependency has an ok result,
  2. the stage's `when` predicate holds,
  3. the stage's own eligible() hook agrees,
  4. its manual gate (if any) is approved,
  5. there is no final cached result for the same inputs hash.

The inputs hash covers the stage version and options, the agent spec, the
unit's own data, its direct dependencies' results and any loop-back or
retry request. Changing any of them re-runs that stage and, transitively,
everything downstream of it; nothing else.

Passes repeat until nothing runs, which is how on_fail loops work: a
failing stage records a loop_back event for its `goto` target, which
changes that target's inputs hash, so the next pass re-runs it.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import MISSING, dataclass, fields

from ..agents.base import AgentBackend
from ..db import DB, now
from .context import Context, Paths, Units
from .expr import evaluate
from .pipeline import Pipeline, StageSpec
from .stage import FINAL, Kind, StageResult, Status

MAX_PASSES = 50
MAX_ERRORS_PER_INPUTS = 3

DEFAULT_CONCURRENCY = {
    Kind.DETERMINISTIC: 8,
    Kind.AGENT: 4,
    Kind.BUILD: 2,
    Kind.OUTWARD: 1,
}


def latest_result(db: DB, unit_id: str, stage: str):
    return db.one(
        "SELECT * FROM stage_result WHERE unit_id=? AND stage=?"
        " ORDER BY id DESC LIMIT 1",
        (unit_id, stage),
    )


@dataclass
class Decision:
    """Why a unit does or does not run for a stage (for `ftbfs why`)."""

    run: bool
    reason: str
    inputs_hash: str | None = None


class Cancelled(Exception):
    pass


class Scheduler:
    def __init__(self, db: DB, pipeline: Pipeline, units: Units,
                 backends: dict[str, AgentBackend], paths: Paths,
                 concurrency: dict[str, int] | None = None,
                 run_id: int | None = None):
        self.db = db
        self.pipeline = pipeline
        self.units = units
        self.backends = backends
        self.paths = paths
        self.concurrency = {**DEFAULT_CONCURRENCY,
                            **{Kind(k): v
                               for k, v in (concurrency or {}).items()}}
        self.run_id = run_id

    # -- decisions --------------------------------------------------------

    def _dep_results(self, spec: StageSpec, uid: str,
                     names: list[str]) -> dict[str, dict | None]:
        """Latest results of other stages, mapped onto this unit.

        When a unit maps to several dependency units (e.g. a package
        depending on per-item results) the dependency counts as ok if any
        of them is ok; its data is the per-unit mapping.
        """
        out: dict[str, dict | None] = {}
        for name in names:
            dep = self.pipeline[name]
            targets = self.units.related(spec.stage.unit, uid,
                                         dep.stage.unit)
            rows = [(t, latest_result(self.db, t, name)) for t in targets]
            rows = [(t, r) for t, r in rows if r is not None]
            if not rows:
                out[name] = None
            elif len(targets) == 1:
                r = rows[0][1]
                out[name] = {**json.loads(r["data"]),
                             "status": r["status"], "id": r["id"]}
            else:
                ok = any(r["status"] == Status.OK for _, r in rows)
                out[name] = {
                    "status": Status.OK if ok else rows[-1][1]["status"],
                    "id": [r["id"] for _, r in rows],
                    "units": {t: {**json.loads(r["data"]),
                                  "status": r["status"]}
                              for t, r in rows},
                }
        return out

    def _unit_data(self, spec: StageSpec, uid: str) -> dict:
        if spec.stage.unit == "item":
            row = self.units.items[uid]
            keys = ("id", "state", "build_id", "log_url", "pocket")
            return {k: row.get(k) for k in keys}
        return {"id": uid}

    def _nonce(self, spec: StageSpec, uid: str) -> int | None:
        row = self.db.one(
            "SELECT MAX(id) AS id FROM event WHERE unit=? AND stage=?"
            " AND type IN ('loop_back', 'retry')",
            (uid, spec.name),
        )
        return row["id"] if row else None

    def inputs_hash(self, spec: StageSpec, uid: str, ctx: Context,
                    deps: dict[str, dict | None]) -> str:
        blob = {
            "stage_version": spec.stage.version,
            "options": spec.options,
            "agent": _non_default(spec.agent),
            "unit": self._unit_data(spec, uid),
            "deps": {
                name: None if d is None else _stable_hash(
                    {k: v for k, v in d.items() if k != "id"}
                )
                for name, d in deps.items()
            },
            "nonce": self._nonce(spec, uid),
            "stage_inputs": spec.stage.inputs(ctx, uid),
        }
        return _stable_hash(blob)

    def decide(self, spec: StageSpec, uid: str, ctx: Context) -> Decision:
        deps = self._dep_results(spec, uid, spec.after)
        for name, d in deps.items():
            if d is None:
                return Decision(False, f"waiting for {name}")
            if d["status"] != Status.OK:
                return Decision(False, f"{name} is {d['status']}")
        if spec.when:
            env = self._dep_results(
                spec, uid, self.pipeline.ancestors(spec.name)
            )
            env = {k: v for k, v in env.items() if v is not None}
            if spec.stage.unit == "item":
                env["item"] = self.units.items[uid]
            if not evaluate(spec.when, env):
                return Decision(False, f"when is false: {spec.when}")
        reason = spec.stage.eligible(ctx, uid)
        if reason:
            return Decision(False, f"not eligible: {reason}")
        if spec.gate == "manual":
            gate = self.db.one(
                "SELECT decision FROM gate WHERE unit_id=? AND stage=?",
                (uid, spec.name),
            )
            if gate is None or gate["decision"] == "pending":
                return Decision(False, "waiting for manual approval")
            if gate["decision"] == "rejected":
                return Decision(False, "rejected at gate")
        ih = self.inputs_hash(spec, uid, ctx, deps)
        prev = self.db.query(
            "SELECT status FROM stage_result WHERE unit_id=? AND stage=?"
            " AND inputs_hash=? ORDER BY id DESC",
            (uid, spec.name, ih),
        )
        if prev and prev[0]["status"] in FINAL:
            return Decision(False, f"cached ({prev[0]['status']})", ih)
        errors = sum(1 for r in prev if r["status"] == Status.ERROR)
        if errors >= MAX_ERRORS_PER_INPUTS:
            return Decision(False, f"gave up after {errors} errors", ih)
        return Decision(True, "ready", ih)

    def _context(self, spec: StageSpec, attempts: dict[str, int]) -> Context:
        return Context(db=self.db, run_id=self.run_id, spec=spec,
                       units=self.units, backends=self.backends,
                       paths=self.paths, attempts=attempts,
                       unit_types={s.name: s.stage.unit
                                   for s in self.pipeline})

    def explain(self, uid_for: dict[str, str]) -> list[tuple[str, str]]:
        """Decision per stage for one item (and its package/cluster)."""
        out = []
        for spec in self.pipeline:
            uid = uid_for.get(spec.stage.unit)
            if uid is None:
                out.append((spec.name, f"no {spec.stage.unit} unit"))
                continue
            ctx = self._context(spec, {uid: 0})
            out.append((spec.name, self.decide(spec, uid, ctx).reason))
        return out

    # -- execution --------------------------------------------------------

    def run(self, only: list[str] | None = None,
            until: str | None = None) -> dict[str, dict[str, int]]:
        """Run passes until quiescent. Returns counts per stage/status."""
        names = list(self.pipeline.specs)
        if until:
            names = [*self.pipeline.ancestors(until), until]
        if only:
            names = [n for n in names if n in only]
        totals: dict[str, dict[str, int]] = {}
        for _ in range(MAX_PASSES):
            did_work = False
            for name in names:
                spec = self.pipeline[name]
                counts = self._run_stage(spec)
                for status, n in counts.items():
                    per = totals.setdefault(name, {})
                    per[status] = per.get(status, 0) + n
                did_work |= bool(counts)
            if not did_work:
                break
        return totals

    def _check_control(self) -> None:
        if self.run_id is None:
            return
        while True:
            row = self.db.one("SELECT control FROM run WHERE id=?",
                              (self.run_id,))
            control = row["control"] if row else None
            if control == "cancel":
                raise Cancelled()
            if control != "pause":
                return
            time.sleep(2)

    def _run_stage(self, spec: StageSpec) -> dict[str, int]:
        uids = self.units.of(spec.stage.unit)
        probe = self._context(spec, dict.fromkeys(uids, 0))
        ready: list[tuple[str, str]] = []
        for uid in uids:
            d = self.decide(spec, uid, probe)
            if d.run:
                ready.append((uid, d.inputs_hash))
            elif d.reason == "waiting for manual approval":
                self._ensure_pending_gate(spec, uid)
        if not ready:
            return {}
        attempts = {
            uid: 1 + self.db.one(
                "SELECT COUNT(*) AS n FROM stage_result"
                " WHERE unit_id=? AND stage=?", (uid, spec.name))["n"]
            for uid, _ in ready
        }
        ctx = self._context(spec, attempts)
        hashes = dict(ready)
        size = max(1, spec.stage.batch_size)
        batches = [
            [u for u, _ in ready[i:i + size]]
            for i in range(0, len(ready), size)
        ]
        ctx.event("stage_start", units=len(ready), batches=len(batches))
        counts: dict[str, int] = {}
        workers = self.concurrency[spec.stage.kind]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for batch in batches:
                self._check_control()
                for u in batch:
                    ctx.event("unit_start", unit=u, attempt=attempts[u])
                futures[pool.submit(self._run_batch, ctx, batch)] = batch
            for fut in as_completed(futures):
                for res in fut.result():
                    self._record(spec, ctx, res, hashes[res.unit_id],
                                 attempts[res.unit_id])
                    counts[res.status] = counts.get(res.status, 0) + 1
        ctx.event("stage_end", counts=counts)
        return counts

    def _run_batch(self, ctx: Context, batch: list[str]) -> list:
        try:
            results = ctx.stage.run(ctx, batch)
            by_unit = {r.unit_id: r for r in results}
            missing = [u for u in batch if u not in by_unit]
            if missing:
                raise RuntimeError(f"stage returned no result for {missing}")
            return [by_unit[u] for u in batch]
        except Exception as e:
            tb = traceback.format_exc()
            return [
                StageResult(u, Status.ERROR,
                            {"error": repr(e), "traceback": tb[-4000:]})
                for u in batch
            ]

    def _record(self, spec: StageSpec, ctx: Context, res: StageResult,
                inputs_hash: str, attempt: int) -> None:
        ledger = ctx.ledger.get(res.unit_id)
        if ledger:
            res.usage = res.usage or ledger.usage
            res.cost = res.cost if res.cost is not None else ledger.cost
            res.backend = res.backend or ledger.backend
            res.model = res.model or ledger.model
        rid = self.db.execute(
            "INSERT INTO stage_result (run_id, unit_type, unit_id, stage,"
            " stage_version, inputs_hash, attempt, status, data, artifacts,"
            " backend, model, usage, cost, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, spec.stage.unit, res.unit_id, spec.name,
             spec.stage.version, inputs_hash, attempt, res.status,
             json.dumps(res.data), json.dumps(res.artifacts), res.backend,
             res.model, json.dumps(res.usage) if res.usage else None,
             res.cost, now()),
        ).lastrowid
        ctx.event("unit_end", unit=res.unit_id, status=res.status,
                  result_id=rid, cost=res.cost,
                  error=res.data.get("error") if res.status == "error"
                  else None)
        if res.status == Status.FAIL and spec.on_fail:
            self._loop_back(spec, ctx, res, rid)

    def _loop_back(self, spec: StageSpec, ctx: Context, res: StageResult,
                   result_id: int) -> None:
        target = spec.on_fail.goto
        # Loops are counted per (unit, target, source stage).
        target_unit = self.units.related(
            spec.stage.unit, res.unit_id, self.pipeline[target].stage.unit
        )
        for uid in target_unit:
            n = self.db.one(
                "SELECT COUNT(*) AS n FROM event WHERE unit=? AND stage=?"
                " AND type='loop_back' AND json_extract(payload, '$.from')"
                " = ?", (uid, target, spec.name))["n"]
            if n >= spec.on_fail.max_loops:
                ctx.event("loop_exhausted", unit=uid, target=target,
                          loops=n)
                continue
            self.db.event("loop_back", run_id=self.run_id, unit=uid,
                          stage=target, **{"from": spec.name,
                                           "loop": n + 1,
                                           "result_id": result_id,
                                           "data": res.data})

    def _ensure_pending_gate(self, spec: StageSpec, uid: str) -> None:
        with self.db.transaction():
            exists = self.db.one(
                "SELECT 1 FROM gate WHERE unit_id=? AND stage=?",
                (uid, spec.name),
            )
            if exists:
                return
            self.db.execute(
                "INSERT INTO gate VALUES (?, ?, 'pending', NULL, NULL, ?)",
                (uid, spec.name, now()),
            )
        self.db.event("gate_wait", run_id=self.run_id, unit=uid,
                      stage=spec.name)


def _non_default(obj) -> dict | None:
    """Dataclass fields that differ from their defaults: adding a new
    optional field to a spec must not invalidate every cached result."""
    if obj is None:
        return None
    out = {}
    for f in fields(obj):
        default = (f.default if f.default is not MISSING
                   else f.default_factory() if f.default_factory
                   is not MISSING else MISSING)
        value = getattr(obj, f.name)
        if value != default:
            out[f.name] = value
    return out


def _stable_hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]

