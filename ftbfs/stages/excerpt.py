"""excerpt: download the build log, cut the failure excerpt (0 tokens)."""

from __future__ import annotations

from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..logs import extract, fetch_log, read_log


@register
class ExcerptStage(Stage):
    name = "excerpt"
    kind = Kind.DETERMINISTIC
    unit = UnitType.ITEM
    version = "5"  # bump when logs.extract() changes
    description = "Fetch the build log and extract the failure excerpt"

    def run(self, ctx, unit_ids):
        out = []
        for uid in unit_ids:
            item = ctx.item(uid)
            if not item["log_url"]:
                out.append(StageResult(uid, Status.SKIP,
                                       {"reason": "no build log"}))
                continue
            path = fetch_log(item["log_url"], ctx.paths.cache / "logs",
                             item["build_id"])
            ex = extract(read_log(path))
            dest = ctx.workdir(uid) / "excerpt.txt"
            dest.write_text(ex.text)
            data = ex.to_dict()
            data["log_path"] = str(path)
            out.append(StageResult(uid, Status.OK, data, [str(dest)]))
        return out
