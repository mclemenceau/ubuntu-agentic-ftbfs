# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Moving stored inputs hashes onto the current hash recipe
(`ftbfs rehash`).

Recipe 2 (November 2026) leaves a stage's volatile result fields out of
the inputs of the stages after it: for reproduce, the build's duration,
its log and excerpt paths and the builder. A free reproduce re-run that
finds the same failure no longer changes dev's inputs, so it no longer
reopens dev's gate. The recipe change itself would change every stored
dev hash, though: each bound approval would reopen and each cached dev
result would look new.

So rehash finds, for each stage whose inputs include a stage with
volatile fields, the upstream results its latest result and its bound
approval were made from: earlier results of each dependency whose
recipe-1 hash gives exactly the stored one. It stores the recipe-2 hash
of those same results. What was current stays current, and what was
stale stays stale, unless only volatile fields made it stale.

Then it restores the approvals that recipe 1 reopened for volatile
fields only: a pending gate whose last decision was an approval,
reopened since, where the stage's latest result is ok and cached for
the inputs the unit has now. The approval is bound to those inputs, by
"migration", with a gate_restored event. A unit the restored approval
would send to a paid run stays with a person.

It is idempotent: a moved hash no longer matches recipe 1, and a
restored gate is no longer pending. Everything happens in one
transaction; a dry run rolls it back.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .core.context import Units
from .core.scheduler import latest_result, plan_outcome
from .db import now
from .filters import all_items

if TYPE_CHECKING:
    from .app import App
    from .core.pipeline import StageSpec
    from .core.scheduler import Scheduler

# Earlier results tried per dependency when looking for the ones a
# stored hash was made from.
CANDIDATES = 12
RESTORED = "restored: reopened only by volatile upstream fields"


@dataclass
class Rehash:
    results: list[str] = field(default_factory=list)  # stage unit
    bindings: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    kept: dict[str, list[str]] = field(default_factory=dict)  # why
    dry_run: bool = False


class _DryRun(Exception):
    pass


def rehash(app: App, dry_run: bool = False) -> Rehash:
    app.reap_stale_runs()
    if app.db.one("SELECT 1 FROM run WHERE status='running'"):
        raise ValueError("a run is in progress: rehash between runs")
    out = Rehash(dry_run=dry_run)
    units = Units(all_items(app.db), app.db)
    old, new = app.scheduler(units), app.scheduler(units)
    old.recipe = 1
    specs = [s for s in app.pipeline
             if any(app.pipeline[d].stage.volatile for d in s.after)]
    try:
        with app.db.transaction():
            for spec in specs:
                for uid in units.of(spec.stage.unit):
                    _move(app, old, new, spec, uid, out)
            for spec in specs:
                if spec.gate != "manual" or spec.stage.unit != "item":
                    continue
                for uid in units.of(spec.stage.unit):
                    _restore(app, new, spec, uid, out)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return out


def _move(app: App, old: Scheduler, new: Scheduler, spec: StageSpec,
          uid: str, out: Rehash) -> None:
    """Move the latest result's hash and the bound approval's."""
    res = latest_result(app.db, uid, spec.name)
    if res is not None:
        h = _translate(app, old, new, spec, uid, res["inputs_hash"],
                       before=res["id"], nonce=True)
        if h is not None and h != res["inputs_hash"]:
            app.db.execute("UPDATE stage_result SET inputs_hash=? WHERE"
                           " id=?", (h, res["id"]))
            out.results.append(f"{spec.name} {uid}")
    gate = app.db.one("SELECT inputs FROM gate WHERE unit_id=? AND stage=?"
                      " AND decision='approved' AND inputs IS NOT NULL",
                      (uid, spec.name))
    if gate is not None:
        h = _translate(app, old, new, spec, uid, gate["inputs"],
                       before=None, nonce=False)
        if h is not None and h != gate["inputs"]:
            app.db.execute("UPDATE gate SET inputs=? WHERE unit_id=? AND"
                           " stage=?", (h, uid, spec.name))
            out.bindings.append(f"{spec.name} {uid}")


def _translate(app: App, old: Scheduler, new: Scheduler, spec: StageSpec,
               uid: str, stored: str, before: int | None,
               nonce: bool) -> str | None:
    """The recipe-2 hash of the upstream results whose recipe-1 hash is
    `stored`, among the latest results of each dependency (earlier than
    the result `before`); None if none gives it."""
    per_dep = []
    for name in spec.after:
        targets = new.units.related(spec.stage.unit, uid,
                                    app.pipeline[name].stage.unit)
        if len(targets) != 1:
            return None  # a per-unit mapping: no volatile fields there
        rows = app.db.query(
            "SELECT * FROM stage_result WHERE unit_id=? AND stage=?"
            + (" AND id<?" if before else "") + " ORDER BY id DESC"
            f" LIMIT {CANDIDATES}",
            (targets[0], name, *([before] if before else [])))
        per_dep.append([{**json.loads(r["data"]), "status": r["status"],
                         "id": r["id"]} for r in rows] or [None])
    for combo in itertools.product(*per_dep):
        deps = dict(zip(spec.after, combo, strict=True))
        if old.hash_with(spec, uid, deps, nonce) == stored:
            return new.hash_with(spec, uid, deps, nonce)
    return None


def _restore(app: App, new: Scheduler, spec: StageSpec, uid: str,
             out: Rehash) -> None:
    gate = app.db.one("SELECT * FROM gate WHERE unit_id=? AND stage=?",
                      (uid, spec.name))
    if gate is None or gate["decision"] != "pending":
        return
    ev = app.db.one(
        "SELECT MAX(CASE WHEN type='gate_approved' THEN id END) AS a,"
        " MAX(CASE WHEN type='gate_reopened' THEN id END) AS o,"
        " MAX(CASE WHEN type='gate_rejected' THEN id END) AS r"
        " FROM event WHERE unit=? AND stage=?", (uid, spec.name))
    if not (ev["a"] and ev["o"] and ev["o"] > ev["a"]) or \
            (ev["r"] and ev["r"] > ev["a"]):
        return  # not an approval reopened since, nor decided again
    res = latest_result(app.db, uid, spec.name)
    if res is None or res["status"] != "ok":
        out.kept.setdefault("its latest result is not ok", []).append(uid)
        return
    if res["inputs_hash"] != new.hash_now(spec, uid):
        out.kept.setdefault("its inputs changed", []).append(uid)
        return
    app.db.execute(
        "UPDATE gate SET decision='approved', inputs=?, by='migration',"
        " note=?, ts=? WHERE unit_id=? AND stage=?",
        (new.hash_now(spec, uid, nonce=False), RESTORED, now(), uid,
         spec.name))
    # The restored approval must leave the result cached: nothing paid.
    reason = new.explain(_uid_for(new, uid), [spec.name])[0][1]
    if plan_outcome(reason) != "done":
        app.db.execute(
            "UPDATE gate SET decision=?, inputs=?, by=?, note=?, ts=?"
            " WHERE unit_id=? AND stage=?",
            (gate["decision"], gate["inputs"], gate["by"], gate["note"],
             gate["ts"], uid, spec.name))
        out.kept.setdefault(f"it would run: {reason}", []).append(uid)
        return
    app.db.event("gate_restored", unit=uid, stage=spec.name,
                 by="migration", note=RESTORED)
    out.restored.append(f"{spec.name} {uid}")


def _uid_for(sched: Scheduler, uid: str) -> dict[str, str]:
    item = sched.units.row(uid)
    out = {"item": uid, "package": item["source"]}
    if item.get("cluster_id"):
        out["cluster"] = item["cluster_id"]
    return out
