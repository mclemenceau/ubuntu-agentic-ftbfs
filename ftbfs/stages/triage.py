# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""triage: first verdict per failure cluster (small tier, packed).

Clusters whose answer is already known from facts are decided without an
LLM; the rest are packed ~12 per call.
"""

from __future__ import annotations

import json

from ..clusters import cluster_signals, context_pack
from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..prompts import load

CATEGORIES = ["compile", "link", "test", "packaging", "deps", "toolchain",
              "arch", "infra"]
# sync/merge are per-package facts, decided deterministically (all
# packages fixed/newer in Debian); the LLM judges the failure pattern.
LLM_ACTIONS = ["patch", "retry", "wait-dependency", "restrict-arch",
               "report-upstream", "investigate"]
ACTIONS = ["sync", "merge", *LLM_ACTIONS]
VERDICT = {
    "type": "object",
    "required": ["id", "category", "summary", "root_cause_guess", "obvious",
                 "fixable", "action", "confidence"],
    "properties": {
        "id": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "summary": {"type": "string"},
        "root_cause_guess": {"type": ["string", "null"]},
        "obvious": {"type": "boolean"},
        "fixable": {"type": "string", "enum": ["yes", "no", "maybe"]},
        "action": {"type": "string", "enum": LLM_ACTIONS},
        "confidence": {"type": "number"},
    },
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "required": ["clusters"],
    "properties": {"clusters": {"type": "array", "items": VERDICT}},
    "additionalProperties": False,
}


def deterministic(ctx, cluster_id: str, pack: dict) -> dict | None:
    """A verdict that needs no LLM, or None."""
    if pack["class"] == "dependency-unsatisfiable":
        blocker = cluster_id.split(":", 1)[1] if ":" in cluster_id else ""
        what = (f"build-dependency {blocker} not installable"
                if blocker and not blocker.startswith(("pkg:", "sig:"))
                else "build-dependencies not installable")
        return {"category": "deps", "summary": what.capitalize(),
                "root_cause_guess": "dependency not installable in the"
                " series (transition or missing build)",
                "obvious": True, "fixable": "no",
                "action": "wait-dependency", "confidence": 0.9}
    sigs = cluster_signals(ctx, cluster_id)
    newer = {"sync-candidate", "merge-candidate"}
    if sigs and all("fixed-in-debian" in s or (
            s & newer and "builds-in-debian-testing" in s) for s in sigs):
        merge = any("merge-candidate" in s for s in sigs)
        facts = pack["representative"]["facts"]
        return {"category": pack["family"] or "packaging",
                "summary": "Newer Debian version"
                f" {facts.get('debian_unstable')} is fixed or builds",
                "root_cause_guess": "fixed in Debian",
                "obvious": True, "fixable": "yes",
                "action": "merge" if merge else "sync",
                "confidence": 0.8}
    return None


@register
class TriageStage(Stage):
    name = "triage"
    kind = Kind.AGENT
    unit = UnitType.CLUSTER
    version = "2"
    batch_size = 12
    description = "First verdict per cluster: category, fixable, action"

    def inputs(self, ctx, unit_id):
        return load(ctx.paths.root, "triage").digest

    def run(self, ctx, unit_ids):
        prompt = load(ctx.paths.root, "triage")
        out: dict[str, StageResult] = {}
        packs = {}
        for cid in unit_ids:
            pack = context_pack(ctx, cid)
            verdict = deterministic(ctx, cid, pack)
            if verdict:
                out[cid] = StageResult(cid, Status.OK, {
                    "id": cid, **verdict, "decided_by": "rules"})
            else:
                pack["id"] = cid
                packs[cid] = pack
        if packs:
            res = ctx.run_agent(
                list(packs), json.dumps(list(packs.values()), indent=1),
                output_schema=SCHEMA, system=prompt.text,
            )
            got = {v["id"]: v for v in (res.data or {}).get("clusters", [])}
            for cid in packs:
                if not res.ok:
                    out[cid] = StageResult(cid, Status.ERROR,
                                           {"error": res.error})
                elif cid in got:
                    out[cid] = StageResult(cid, Status.OK, {
                        **got[cid], "decided_by": "llm"})
                else:
                    out[cid] = StageResult(cid, Status.ERROR, {
                        "error": "no verdict returned for cluster"})
        return [out[c] for c in unit_ids]
