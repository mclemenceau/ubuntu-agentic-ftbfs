# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""classify: regex rules -> failure class and cluster (0 tokens).

The cluster id is also written onto the item so cluster-level stages
(diagnose, confirm) can group items.
"""

from __future__ import annotations

from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..rules import RuleSet


@register
class ClassifyStage(Stage):
    name = "classify"
    kind = Kind.DETERMINISTIC
    unit = UnitType.ITEM
    version = "1"
    batch_size = 100
    description = "Classify the failure with rules.toml and assign a cluster"

    def _rules(self, ctx) -> RuleSet:
        path = ctx.paths.root / ctx.options.get("rules", "rules.toml")
        return RuleSet.load(path)

    def inputs(self, ctx, unit_id):
        # Editing rules.toml re-classifies everything (cheap).
        return self._rules(ctx).digest

    def run(self, ctx, unit_ids):
        rules = self._rules(ctx)
        out = []
        for uid in unit_ids:
            ex = ctx.upstream(uid, "excerpt")
            c = rules.classify(ex, ctx.item(uid)["source"])
            ctx.update_item(uid, cluster_id=c.cluster_id, **{"class": c.cls},
                            family=c.family)
            out.append(StageResult(uid, Status.OK, c.to_dict()))
        return out
