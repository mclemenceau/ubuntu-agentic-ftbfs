# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Next steps inbox: what needs a human now.

Everything here is derived from the pipeline's results plus a dry run
of the scheduler (App.plan), so it cannot drift from what a run would
actually do. The only human state is a disposition per source version
(accepted, uploaded, rejected, handled), which takes it out of the
inbox; accepted fixes stay listed until they are uploaded.
"""

from __future__ import annotations

import json

from ..app import App
from ..core.scheduler import plan_outcome
from ..filters import Filter
from . import queries as q

RISKS = {"low": 0, "medium": 1, "high": 2}
# What dev runs on (pipeline.toml, stage.dev `when`).
REPRODUCED = ("reproduced", "reproduced-similar")
# How many units a preview lists by name per stage.
PREVIEW_NAMES = 12


def _data(row) -> dict:
    return json.loads(row["data"]) if row else {}


def _context(core: App, items: dict[str, dict]) -> dict[str, dict]:
    """Per item: the cluster's triage and diagnosis and its latest
    reproduce, for grouping and showing gate candidates."""
    clusters = sorted({i["cluster_id"] for i in items.values()
                       if i["cluster_id"]})
    latest = q.latest_by_unit(core.db, clusters + sorted(items))
    out = {}
    for iid, it in items.items():
        c = latest.get(it["cluster_id"], {})
        mine = latest.get(iid, {})
        out[iid] = {
            "triage": _data(c.get("triage")),
            "diagnose": _data(c.get("diagnose")),
            "reproduce": _data(mine.get("reproduce")),
        }
    return out


def _outdated_why(arch: str, why: list[tuple[str, str]],
                  reproduce: dict) -> list[str]:
    """Why a verified fix is not current, for a person."""
    out = []
    for stage, reason in why:
        match plan_outcome(reason):
            case "gate":
                text = (f"{stage} is back at its gate: its inputs changed"
                        " after the fix (approve it to redo the fix)")
            case "ready":
                text = (f"{stage} would run again: its inputs changed"
                        " after the fix, or it was retried")
            case _:
                text = f"{stage} is held: {reason}"
        out.append(f"{arch}: {text}")
    rep = reproduce.get("outcome")
    if rep and rep not in REPRODUCED:
        out.append(f"{arch}: the latest reproduce says {rep}, not the"
                   " failure the fix is for")
    return out


def preview(core: App, plan: dict[str, dict[str, list[str]]]) -> list[dict]:
    """Per stage in the plan: what a run would start now, and roughly
    what it costs. Units blocked behind ready work run too, so this is
    a lower bound for everything downstream of the first ready stage."""
    avg = q.avg_cost(core.db)
    rows = []
    for name, per in plan.items():
        spec = core.pipeline[name]
        ready = per.get("ready", [])
        rows.append({
            "stage": name, "unit": str(spec.stage.unit),
            "agent": spec.agent is not None, "ready": ready,
            "names": ready[:PREVIEW_NAMES],
            "gate": len(per.get("gate", [])),
            "blocked": len(per.get("blocked", [])),
            "cost": len(ready) * avg.get(name, 0.0) if spec.agent else 0,
        })
    return rows


def build(core: App, flt: Filter) -> dict:
    db = core.db
    plan = core.plan(flt)
    disp = q.dispositions(db)
    ready = {s: set(per.get("ready", [])) for s, per in plan.items()}

    def disposed(it: dict | None) -> bool:
        return it is not None and (it["source"], it["version"]) in disp

    # Verified fixes: to review, or accepted and waiting for upload. A
    # fix whose unit has new upstream results since (dev back at its
    # gate, or blocked) is outdated: listed apart, with why.
    verified = q.latest_with_status(db, "verify", "ok")
    vitems = q.items_by_id(db, [r["unit_id"] for r in verified])
    vlatest = q.latest_by_unit(db, sorted(vitems))
    review: dict[tuple, dict] = {}
    upload: dict[tuple, dict] = {}
    entries = []
    for r in verified:
        it = vitems.get(r["unit_id"])
        if it is None or it["lifecycle"] == "gone":
            continue
        key = (it["source"], it["version"])
        d = disp.get(key)
        target = (review if d is None else upload
                  if d["status"] == "accepted" else None)
        if target is None:
            continue
        entry = target.setdefault(key, {
            "source": key[0], "version": key[1], "arches": [],
            "unit": it["id"],
            "dev": _data(vlatest.get(it["id"], {}).get("dev")),
            "disposition": d, "outdated": []})
        entry["arches"].append(it["arch"])
        entries.append((entry, it))
    outdated = core.outdated(sorted(it["id"] for _, it in entries))
    for entry, it in entries:
        why = outdated.get(it["id"])
        if why:
            rep = _data(vlatest.get(it["id"], {}).get("reproduce"))
            entry["outdated"] += _outdated_why(it["arch"], why, rep)

    # Needs a human: fixes that do not build once the loop is spent,
    # and errors the next run will not retry by itself.
    fails = q.latest_with_status(db, "verify", "fail")
    problems = q.attention(db)["problems"]
    built = [r for r in q.latest_with_status(db, "reproduce", "ok")
             if _data(r).get("outcome") == "built"]
    others = q.items_by_id(db, [r["unit_id"]
                                for r in (*fails, *problems, *built)])
    human = []
    for r in fails:
        it = others.get(r["unit_id"])
        if it is None or it["lifecycle"] == "gone" or disposed(it) or \
                r["unit_id"] in ready.get("dev", ()) or \
                (it["source"], it["version"]) in review:
            continue
        human.append({"stage": "verify", "unit": r["unit_id"],
                      "retry": "dev", "why": "the automated fix does not"
                      " build: " + (_data(r).get("key_lines") or ["?"])[0]})
    for r in problems:
        if r["unit_id"] in ready.get(r["stage"], ()) or \
                disposed(others.get(r["unit_id"])):
            continue
        data = _data(r)
        why = str(data.get("error") or data.get("reason") or "")
        human.append({"stage": r["stage"], "unit": r["unit_id"],
                      "retry": r["stage"],
                      "why": f"{r['status']}: {why[:200]}"})

    # Builds that pass when rebuilt: retry on Launchpad.
    lp_retry = [it for r in built if (it := others.get(r["unit_id"]))
                and not disposed(it) and it["lifecycle"] != "gone"]

    # Approvals: every unit a run would hold at a gate, grouped so a
    # whole bucket can be approved at once.
    approvals = []
    for name, per in plan.items():
        waiting = per.get("gate", [])
        if not waiting:
            continue
        items = q.items_by_id(db, waiting)
        ctx = _context(core, items)
        groups: dict[tuple, dict] = {}
        for uid in waiting:
            it = items.get(uid)
            if disposed(it):
                continue
            c = ctx.get(uid, {})
            diag, tri = c.get("diagnose", {}), c.get("triage", {})
            rep = c.get("reproduce", {}).get("outcome") \
                if name != "reproduce" else None
            key = (RISKS.get(diag.get("risk"), 9), diag.get("risk"),
                   diag.get("fix_kind") or tri.get("action"), rep)
            g = groups.setdefault(key, {
                "risk": diag.get("risk"),
                "kind": diag.get("fix_kind") or tri.get("action")
                or "undiagnosed", "reproduce": rep, "units": []})
            g["units"].append({"id": uid, "item": it, **c})
        buckets = [groups[k] for k in sorted(
            groups, key=lambda k: (k[0], -len(groups[k]["units"]),
                                   str(k[2])))]
        approvals.append({"stage": name, "buckets": buckets,
                          "total": sum(len(b["units"]) for b in buckets)})

    # Sync or merge: triage settled it from the Debian facts.
    selected = core.select(flt)
    clusters = sorted({r["cluster_id"] for r in selected
                       if r["cluster_id"]})
    verdicts = {u: _data(per.get("triage")).get("action") for u, per in
                q.latest_by_unit(db, clusters).items()}
    fixing = {k[0] for k in (*review, *upload)}
    wanted: dict[str, dict] = {}
    for r in selected:
        action = verdicts.get(r["cluster_id"])
        if action not in ("sync", "merge") or r["source"] in fixing or \
                (r["source"], r["version"]) in disp:
            continue
        d = wanted.setdefault(r["source"], {
            "source": r["source"], "action": action, "versions": set()})
        d["versions"].add(r["version"])
    facts = q.facts(db, sorted(wanted))
    debian = []
    for src, d in sorted(wanted.items()):
        f = facts.get(src, {})
        debian.append({**d, "versions": sorted(d["versions"]),
                       "debian": (f.get("debian") or {}).get("newest"),
                       "signals": [s for s in f.get("signals", [])
                                   if s.endswith("-candidate")]})

    snaps = q.snapshots(db)
    return {
        "review": [e for e in review.values() if not e["outdated"]],
        "outdated": [e for e in review.values() if e["outdated"]],
        "upload": list(upload.values()),
        "human": human,
        "lp_retry": lp_retry,
        "approvals": approvals,
        "debian": debian,
        "preview": preview(core, plan),
        "snapshot": snaps[0] if snaps else None,
        "snapshot_age": q.age_s(snaps[0]["fetched_at"]) if snaps else None,
        "selected": len(selected),
    }
