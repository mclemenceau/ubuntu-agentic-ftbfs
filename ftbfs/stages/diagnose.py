"""diagnose: root cause and fix strategy per cluster (medium tier).

Runs once per cluster that triage considers fixable by a change in
Ubuntu or unclear; the diagnosis is shared by every member.
"""

from __future__ import annotations

import json

from ..clusters import context_pack
from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..prompts import load

FIX_KINDS = ["code-patch", "packaging-change", "sync-from-debian",
             "merge-from-debian", "backport-upstream-fix",
             "disable-or-skip-test", "restrict-arch", "fix-elsewhere",
             "retry", "unknown"]


def _text(lo: int, hi: int) -> dict:
    return {"type": "string", "minLength": lo, "maxLength": hi}


def _list(n: int, lo: int, hi: int) -> dict:
    return {"type": "array", "minItems": 1, "maxItems": n,
            "items": _text(lo, hi)}


# Short enum/boolean fields first: a verbose answer must not crowd them
# out. Length bounds keep output tokens (the main cost) in check and
# reject placeholder answers.
SCHEMA = {
    "type": "object",
    "required": ["fix_kind", "risk", "confidence", "needs_source",
                 "applies_to_all_members", "root_cause", "evidence",
                 "fix_strategy", "patch_outline", "upstream"],
    "properties": {
        "fix_kind": {"type": "string", "enum": FIX_KINDS},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "number"},
        "needs_source": {"type": "boolean"},
        "applies_to_all_members": {"type": "boolean"},
        "root_cause": _text(40, 700),
        "evidence": _list(4, 5, 300),
        "fix_strategy": _text(30, 700),
        "patch_outline": _list(6, 10, 200),
        "upstream": _text(5, 300),
    },
    "additionalProperties": False,
}


@register
class DiagnoseStage(Stage):
    name = "diagnose"
    kind = Kind.AGENT
    unit = UnitType.CLUSTER
    version = "2"
    description = "Root cause, evidence and fix strategy per cluster"

    def inputs(self, ctx, unit_id):
        return load(ctx.paths.root, "diagnose").digest

    def run(self, ctx, unit_ids):
        prompt = load(ctx.paths.root, "diagnose")
        out = []
        for cid in unit_ids:
            pack = context_pack(ctx, cid)
            triage = ctx.result(cid, "triage") or {}
            pack["triage"] = {k: triage.get(k) for k in (
                "category", "summary", "root_cause_guess", "fixable",
                "action", "confidence")}
            res = ctx.run_agent(cid, json.dumps(pack, indent=1),
                                output_schema=SCHEMA, system=prompt.text)
            if not res.ok or res.data is None:
                out.append(StageResult(cid, Status.ERROR,
                                       {"error": res.error or "no data"}))
                continue
            out.append(StageResult(cid, Status.OK, res.data))
        return out
