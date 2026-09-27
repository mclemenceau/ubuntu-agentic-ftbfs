"""verify: build the dev stage's new source package (0 tokens).

ok = builds. fail = still fails; the new excerpt is fed back to `dev`
through the pipeline's on_fail loop.
"""

from __future__ import annotations

from pathlib import Path

from ..builder import local, outcome
from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..logs import extract
from ..stages.reproduce import _series, extra_repositories

MAX_FEEDBACK = 6000


@register
class VerifyStage(Stage):
    name = "verify"
    kind = Kind.BUILD
    unit = UnitType.ITEM
    version = "1"
    description = "Rebuild the patched source package with sbuild"

    def eligible(self, ctx, unit_id):
        arch = ctx.item(unit_id)["arch"]
        if arch not in ctx.options.get("local_arches", ["amd64"]):
            return f"no local builder for {arch} (PPA verify not yet" \
                   " supported)"
        return None

    def run(self, ctx, unit_ids):
        out = []
        for uid in unit_ids:
            item = ctx.item(uid)
            dev = ctx.result(uid, "dev") or {}
            adir = ctx.attempt_dir(uid)
            series = _series(ctx)
            ctx.event("build_start", unit=uid, where="local",
                      dsc=dev["new_dsc"])
            b = local.build(
                Path(dev["new_dsc"]), item["arch"], f"{series}-proposed",
                adir / "build", extra_repositories(item["component"],
                                                   series),
                parallel=ctx.options.get("parallel", 8),
                timeout=ctx.options.get("timeout", 4 * 3600))
            ctx.event("build_end", unit=uid, ok=b.ok,
                      duration_s=round(b.duration_s))
            base = {"version": dev.get("version"),
                    "duration_s": round(b.duration_s),
                    "log": str(b.log) if b.log else None}
            if b.ok:
                out.append(StageResult(uid, Status.OK, {
                    **base, "outcome": outcome.BUILT}))
                continue
            ex = extract(b.log.read_text(errors="replace")) if b.log \
                else None
            if ex is None or ex.fail_stage not in ("build", "install-deps"):
                out.append(StageResult(uid, Status.ERROR, {
                    **base, "error": "builder failed before the build"}))
                continue
            original = ctx.result(uid, "excerpt") or {}
            same = ex.signature == original.get("signature")
            out.append(StageResult(uid, Status.FAIL, {
                **base,
                "outcome": "same-failure" if same else "new-failure",
                "key_lines": ex.key_lines[:3],
                "excerpt": ex.text[:MAX_FEEDBACK],
            }, [str(b.log)]))
        return out
