# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

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
from ..builder.pool import BuilderPool, NoBuilder, make_pool
from ..db import DB, now
from .context import Context, Paths, Units, approval_event
from .expr import evaluate
from .pipeline import Pipeline, StageSpec
from .stage import FINAL, Kind, StageResult, Status

MAX_PASSES = 50
# How upstream results feed an inputs hash: 1, the whole result; 2,
# without the producing stage's volatile fields. `ftbfs rehash` moves
# stored hashes from one to the next (ftbfs/rehash.py).
HASH_RECIPE = 2
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
    # Approved gate seen for the first time: the inputs to bind it to.
    gate_inputs: str | None = None


class Cancelled(Exception):
    """The run was cancelled. `counts` are the results recorded by the
    stage it stopped in, `totals` those of the whole run so far."""

    def __init__(self, counts: dict[str, int] | None = None):
        super().__init__()
        self.counts = counts or {}
        self.totals: dict[str, dict[str, int]] = {}


class Scheduler:
    def __init__(self, db: DB, pipeline: Pipeline, units: Units,
                 backends: dict[str, AgentBackend], paths: Paths,
                 concurrency: dict[str, int] | None = None,
                 run_id: int | None = None,
                 builders: BuilderPool | None = None,
                 identity: dict[str, str] | None = None):
        self.db = db
        self.pipeline = pipeline
        self.identity = identity or {}
        self.units = units
        self.backends = backends
        self.paths = paths
        self.concurrency = {**DEFAULT_CONCURRENCY,
                            **{Kind(k): v
                               for k, v in (concurrency or {}).items()}}
        self.run_id = run_id
        self.recipe = HASH_RECIPE
        self.builders = builders or make_pool(
            {}, self.concurrency[Kind.BUILD])
        # Build workers only wait for a builder slot; the pool is what
        # bounds how many builds run at once.
        self.concurrency[Kind.BUILD] = self.builders.slots

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
            row = self.units.row(uid)
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
                    deps: dict[str, dict | None],
                    nonce: bool = True) -> str:
        """The cache key. Without the nonce, it is what a gate approval
        is bound to: loops and retries stay within one approval."""
        blob = {
            "stage_version": spec.stage.version,
            "options": spec.options,
            "agent": _non_default(spec.agent),
            "unit": self._unit_data(spec, uid),
            "deps": {
                name: None if d is None else _stable_hash(
                    self._dep_key(name, d))
                for name, d in deps.items()
            },
            "nonce": self._nonce(spec, uid) if nonce else None,
            "stage_inputs": spec.stage.inputs(ctx, uid),
        }
        return _stable_hash(blob)

    def _dep_key(self, name: str, d: dict) -> dict:
        """What of an upstream result goes into the inputs hash: all of
        it but its id and the producing stage's volatile fields."""
        drop = {"id"}
        if self.recipe >= 2:
            drop.update(self.pipeline[name].stage.volatile)
        return {k: v for k, v in d.items() if k not in drop}

    def hash_with(self, spec: StageSpec, uid: str,
                  deps: dict[str, dict | None], nonce: bool = True) -> str:
        """The inputs hash this unit would have with these upstream
        results instead of the latest ones (for rehash)."""
        ctx = self._context(spec, {uid: 0})
        return self.inputs_hash(spec, uid, ctx, deps, nonce)

    def hash_now(self, spec: StageSpec, uid: str,
                 nonce: bool = True) -> str:
        """The inputs hash this unit would have now, whatever its
        dependencies' status, `when` or gate say (for relocate)."""
        ctx = self._context(spec, {uid: 0})
        deps = self._dep_results(spec, uid, spec.after)
        return self.inputs_hash(spec, uid, ctx, deps, nonce)

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
                env["item"] = self.units.row(uid)
            if not evaluate(spec.when, env):
                return Decision(False, f"when is false: {spec.when}")
        reason = spec.stage.eligible(ctx, uid)
        if reason:
            return Decision(False, f"not eligible: {reason}")
        adopt = None
        if spec.gate == "manual":
            gate = self.db.one(
                "SELECT decision, inputs FROM gate WHERE unit_id=? AND"
                " stage=?", (uid, spec.name),
            )
            if gate is None or gate["decision"] == "pending":
                return Decision(False, WAITING)
            if gate["decision"] == "rejected":
                return Decision(False, "rejected at gate")
            # An approval is for the inputs it was given on: new
            # upstream results (a re-run triage, diagnose or reproduce)
            # send the unit back to a person, not to a paid re-run.
            key = self.inputs_hash(spec, uid, ctx, deps, nonce=False)
            if gate["inputs"] is None:
                adopt = key
            elif gate["inputs"] != key:
                return Decision(False, WAITING, gate_inputs=key)
        d = self._decide_cached(spec, uid, ctx, deps)
        d.gate_inputs = adopt
        return d

    def _decide_cached(self, spec: StageSpec, uid: str, ctx: Context,
                       deps: dict[str, dict | None]) -> Decision:
        ih = self.inputs_hash(spec, uid, ctx, deps)
        prev = self.db.query(
            "SELECT status, run_id FROM stage_result WHERE unit_id=? AND"
            " stage=? AND inputs_hash=? ORDER BY id DESC",
            (uid, spec.name, ih),
        )
        if prev and prev[0]["status"] in FINAL:
            return Decision(False, f"cached ({prev[0]['status']})", ih)
        if (prev and prev[0]["status"] == Status.PENDING
                and self.run_id is not None
                and prev[0]["run_id"] == self.run_id):
            return Decision(False, "pending (already polled this run)", ih)
        errors = sum(1 for r in prev if r["status"] == Status.ERROR)
        # Expensive stages (agents) can lower this with `max_errors`.
        if errors >= spec.options.get("max_errors", MAX_ERRORS_PER_INPUTS):
            return Decision(False, f"gave up after {errors} errors", ih)
        return Decision(True, "ready", ih)

    def _context(self, spec: StageSpec, attempts: dict[str, int]) -> Context:
        return Context(db=self.db, run_id=self.run_id, spec=spec,
                       units=self.units, backends=self.backends,
                       paths=self.paths, attempts=attempts,
                       unit_types={s.name: s.stage.unit
                                   for s in self.pipeline},
                       builders=self.builders, identity=self.identity)

    def outdated(self, uid_for: dict[str, str], stage: str
                 ) -> list[tuple[str, str]]:
        """Why the latest result of `stage` for this item is no longer
        current, as (stage, reason); empty when it is. It is current
        while a run would keep it and the results it was made from:
        `stage` and the stages it runs after are cached for their
        inputs now. New upstream results change those inputs, reopen
        a gate bound to the old ones or block it; a retry or a loop
        makes the stage run again."""
        names = [*self.pipeline[stage].after, stage]
        return [(n, reason) for n, reason in self.explain(uid_for, names)
                if plan_outcome(reason) != "done"]

    def explain(self, uid_for: dict[str, str],
                names: list[str] | None = None) -> list[tuple[str, str]]:
        """Decision per stage (or per stage in `names`) for one item
        (and its package/cluster)."""
        out = []
        for spec in self.pipeline:
            if names is not None and spec.name not in names:
                continue
            uid = uid_for.get(spec.stage.unit)
            if uid is None:
                out.append((spec.name, f"no {spec.stage.unit} unit"))
                continue
            ctx = self._context(spec, {uid: 0})
            out.append((spec.name, self.decide(spec, uid, ctx).reason))
        return out

    def plan(self, names: list[str] | None = None
             ) -> dict[str, dict[str, list[str]]]:
        """What a run would do now, without running anything: per stage,
        the units in each outcome of plan_outcome(). Stages only see
        results that exist already, so units downstream of ready work
        show as blocked until that work has run."""
        out: dict[str, dict[str, list[str]]] = {}
        for spec in self.pipeline:
            if names is not None and spec.name not in names:
                continue
            uids = self.units.of(spec.stage.unit)
            ctx = self._context(spec, dict.fromkeys(uids, 0))
            per = out.setdefault(spec.name, {})
            for uid in uids:
                reason = self.decide(spec, uid, ctx).reason
                per.setdefault(plan_outcome(reason), []).append(uid)
        return out

    # -- execution --------------------------------------------------------

    def stage_names(self, only: list[str] | None = None,
                    until: str | None = None) -> list[str]:
        names = list(self.pipeline.specs)
        if until:
            names = [*self.pipeline.ancestors(until), until]
        if only:
            names = [n for n in names if n in only]
        return names

    def run(self, only: list[str] | None = None,
            until: str | None = None) -> dict[str, dict[str, int]]:
        """Run passes until quiescent. Returns counts per stage/status.

        Gated stages left out by `only` or `until` still queue their
        pending gates, so the gate queue shows everything that waits
        for a human, not only what this run reached."""
        names = self.stage_names(only, until)
        totals: dict[str, dict[str, int]] = {}
        for _ in range(MAX_PASSES):
            did_work = False
            for name in names:
                spec = self.pipeline[name]
                try:
                    counts = self._run_stage(spec)
                except Cancelled as e:
                    _add(totals, name, e.counts)
                    e.totals = totals
                    raise
                _add(totals, name, counts)
                # Pending polls are not progress; looping on them would
                # just hammer the external service.
                did_work |= any(k != Status.PENDING for k in counts)
            if not did_work:
                break
        skipped = [n for n in self.pipeline.specs if n not in names]
        for stage, per in self.plan(
                [n for n in skipped if self.pipeline[n].gate]).items():
            spec = self.pipeline[stage]
            ctx = self._context(spec, dict.fromkeys(per.get("gate", []), 0))
            for uid in per.get("gate", []):
                self._gate_upkeep(spec, uid, self.decide(spec, uid, ctx))
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
            self._gate_upkeep(spec, uid, d)
            if d.run:
                ready.append((uid, d.inputs_hash))
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
        cancelled = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._run_batch, ctx, batch, attempts)
                       for batch in batches]
            for fut in as_completed(futures):
                results = fut.result()
                if results is None:
                    cancelled = True
                    continue
                for res in results:
                    self._record(spec, ctx, res, hashes[res.unit_id],
                                 attempts[res.unit_id])
                    counts[res.status] = counts.get(res.status, 0) + 1
        ctx.event("stage_end", counts=counts,
                  **({"cancelled": True} if cancelled else {}))
        if cancelled:
            raise Cancelled(counts)
        return counts

    def _run_batch(self, ctx: Context, batch: list[str],
                   attempts: dict[str, int]) -> list | None:
        """Results for one batch, or None when the run was cancelled
        before it started. Pause and cancel are checked here, when a
        worker picks the batch up, because a stage queues all of its
        batches at once: units already running finish and are
        recorded, queued ones never start."""
        try:
            self._check_control()  # blocks while paused
        except Cancelled:
            return None
        for u in batch:
            ctx.event("unit_start", unit=u, attempt=attempts[u])
        try:
            results = ctx.stage.run(ctx, batch)
            by_unit = {r.unit_id: r for r in results}
            missing = [u for u in batch if u not in by_unit]
            if missing:
                raise RuntimeError(f"stage returned no result for {missing}")
            return [by_unit[u] for u in batch]
        except NoBuilder as e:
            # Every host that can build this is down: wait for the next
            # run like an external job, without counting an error.
            return [StageResult(u, Status.PENDING,
                                {"phase": "waiting-for-builder",
                                 "reason": str(e)})
                    for u in batch]
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
        prev = latest_result(self.db, res.unit_id, spec.name)
        if (prev is not None and prev["status"] == Status.PENDING
                and prev["inputs_hash"] == inputs_hash):
            # Same external job, new poll: update in place instead of
            # piling up one row per poll.
            self.db.execute(
                "UPDATE stage_result SET run_id=?, status=?, data=?,"
                " artifacts=?, ts=? WHERE id=?",
                (self.run_id, res.status, json.dumps(res.data),
                 json.dumps(res.artifacts), now(), prev["id"]))
            ctx.event("unit_end", unit=res.unit_id, status=res.status,
                      result_id=prev["id"], poll=True)
            return
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
                " = ? AND id > ?", (uid, target, spec.name,
                                    approval_event(self.db, uid, target))
            )["n"]
            if n >= spec.on_fail.max_loops:
                ctx.event("loop_exhausted", unit=uid, target=target,
                          loops=n)
                continue
            self.db.event("loop_back", run_id=self.run_id, unit=uid,
                          stage=target, **{"from": spec.name,
                                           "loop": n + 1,
                                           "result_id": result_id,
                                           "data": res.data})

    def _gate_upkeep(self, spec: StageSpec, uid: str,
                     d: Decision) -> None:
        """The gate writes a decision implies: queue a pending gate, bind
        a fresh approval to the current inputs, or reopen an approval
        whose inputs changed."""
        if d.reason != WAITING:
            if d.gate_inputs is not None:
                self.db.execute(
                    "UPDATE gate SET inputs=? WHERE unit_id=? AND stage=?"
                    " AND decision='approved' AND inputs IS NULL",
                    (d.gate_inputs, uid, spec.name))
        elif d.gate_inputs is not None:
            self.db.execute(
                "UPDATE gate SET decision='pending', inputs=NULL, by=NULL,"
                " note=?, ts=? WHERE unit_id=? AND stage=?",
                ("inputs changed since approval", now(), uid, spec.name))
            self.db.event("gate_reopened", run_id=self.run_id, unit=uid,
                          stage=spec.name)
        else:
            self._ensure_pending_gate(spec, uid)

    def _ensure_pending_gate(self, spec: StageSpec, uid: str) -> None:
        with self.db.transaction():
            exists = self.db.one(
                "SELECT 1 FROM gate WHERE unit_id=? AND stage=?",
                (uid, spec.name),
            )
            if exists:
                return
            self.db.execute(
                "INSERT INTO gate (unit_id, stage, decision, ts)"
                " VALUES (?, ?, 'pending', ?)",
                (uid, spec.name, now()),
            )
        self.db.event("gate_wait", run_id=self.run_id, unit=uid,
                      stage=spec.name)


WAITING = "waiting for manual approval"


def plan_outcome(reason: str) -> str:
    """Bucket a Decision reason: ready, gate, done, pending, blocked
    (an upstream stage has no ok result), skipped (not applicable or
    rejected) or gave-up."""
    if reason == "ready":
        return "ready"
    if reason == WAITING:
        return "gate"
    if reason.startswith("cached"):
        return "done"
    if reason.startswith("pending"):
        return "pending"
    if reason.startswith("gave up"):
        return "gave-up"
    if reason.startswith(("when is false", "not eligible", "rejected",
                          "no ")) or reason.endswith(" is skip"):
        return "skipped"
    return "blocked"


def _add(totals: dict[str, dict[str, int]], stage: str,
         counts: dict[str, int]) -> None:
    for status, n in counts.items():
        per = totals.setdefault(stage, {})
        per[status] = per.get(status, 0) + n


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

