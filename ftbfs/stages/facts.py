"""facts: Debian and upstream status per source package (0 tokens).

Answers "is this open in Debian / fixed there / would a sync fix it?"
from bulk indexes (cached daily) and one UDD query per batch.
"""

from __future__ import annotations

import json

from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..facts import sources as src
from ..facts.derive import derive
from ..facts.udd import ftbfs_bugs


def _series(ctx) -> str:
    row = ctx.db.one("SELECT series FROM snapshot ORDER BY id DESC LIMIT 1")
    return row["series"]


@register
class FactsStage(Stage):
    name = "facts"
    kind = Kind.DETERMINISTIC
    unit = UnitType.PACKAGE
    version = "2"
    batch_size = 200  # one UDD query per batch
    description = "Debian versions, Debian FTBFS bugs, testing build " \
                  "status, upstream forge"

    def _failing(self, ctx, source: str) -> dict[str, list[str]]:
        failing: dict[str, list[str]] = {}
        for iid in ctx.units.children(UnitType.PACKAGE, source):
            item = ctx.item(iid)
            failing.setdefault(item["version"], []).append(item["arch"])
        return {v: sorted(a) for v, a in failing.items()}

    def inputs(self, ctx, unit_id):
        # Facts refresh daily, or when the failing versions/arches change.
        return {"day": src.today(), "failing": self._failing(ctx, unit_id)}

    def run(self, ctx, unit_ids):
        cache = ctx.paths.cache / "facts"
        ubuntu = src.ubuntu_sources(cache, _series(ctx))
        unstable = src.debian_sources(cache, "unstable")
        experimental = src.debian_sources(cache, "experimental")
        repro = src.repro_status(cache)
        bugs = ftbfs_bugs(unit_ids)
        out = []
        for source in unit_ids:
            facts = derive(
                source, self._failing(ctx, source), ubuntu.get(source),
                unstable.get(source), experimental.get(source),
                repro.get(source), bugs.get(source, []),
            )
            path = ctx.workdir(source) / "facts.json"
            path.write_text(json.dumps(facts, indent=1))
            out.append(StageResult(source, Status.OK, facts, [str(path)]))
        return out
