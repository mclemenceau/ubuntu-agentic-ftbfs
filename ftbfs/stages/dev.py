"""dev: an agent edits the unpacked source to fix the failure; the
tooling turns the edits into a DEP-3 patch, changelog entry, new source
package and debdiff. Human-gated; feedback from `verify` loops back here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..agents.base import ToolPolicy
from ..builder import local, srcpkg
from ..clusters import package_facts
from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..prompts import load

MAX_REFERENCE = 4000
MAX_EXCERPT = 6000
EDITS = "edits.diff"

SCHEMA = {
    "type": "object",
    "required": ["summary", "patch_name", "patch_description", "forwarded",
                 "bug_debian", "files_changed", "confidence", "notes"],
    "properties": {
        "summary": {"type": "string", "minLength": 10, "maxLength": 120},
        "patch_name": {"type": "string", "minLength": 3, "maxLength": 60},
        "patch_description": {"type": "string", "minLength": 20,
                              "maxLength": 600},
        "forwarded": {"type": "string", "minLength": 2, "maxLength": 200},
        "bug_debian": {"type": ["string", "null"]},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "notes": {"type": "string", "maxLength": 600},
    },
    "additionalProperties": False,
}


def _identity(ctx) -> tuple[str, str]:
    name = ctx.options.get("name")
    email = ctx.options.get("email")
    if name and email:
        return name, email

    def git(key):
        return subprocess.run(["git", "config", key], cwd=ctx.paths.root,
                              capture_output=True, text=True).stdout.strip()

    return name or git("user.name"), email or git("user.email")


def _read(path, limit: int) -> str | None:
    try:
        return Path(path).read_text(errors="replace")[:limit]
    except (OSError, TypeError):
        return None


def _series(ctx) -> str:
    return ctx.db.one(
        "SELECT series FROM snapshot ORDER BY id DESC LIMIT 1")["series"]


def reference_fix(ctx, uid: str) -> dict | None:
    """A verified debdiff from another member of the same cluster."""
    item = ctx.item(uid)
    cid = item.get("cluster_id")
    if not cid:
        return None
    rows = ctx.db.query(
        "SELECT v.unit_id FROM stage_result v JOIN item i ON i.id=v.unit_id"
        " WHERE v.stage='verify' AND v.status='ok' AND i.cluster_id=? AND"
        " i.source != ? ORDER BY v.id DESC LIMIT 1", (cid, item["source"]))
    for r in rows:
        dev = ctx.result(r["unit_id"], "dev") or {}
        text = _read(dev.get("patch_file"), MAX_REFERENCE)
        if text:
            return {"item": r["unit_id"], "patch": text}
    return None


def previous_edits(ctx, uid: str) -> str | None:
    """The raw edits of the latest successful dev attempt. They sit next
    to its debdiff (attempts made before edits were saved have none)."""
    row = ctx.db.one(
        "SELECT data FROM stage_result WHERE unit_id=? AND stage='dev'"
        " AND status='ok' ORDER BY id DESC LIMIT 1", (uid,))
    if row is None:
        return None
    debdiff = json.loads(row["data"]).get("debdiff")
    return _read(Path(debdiff).with_name(EDITS), 10**7) if debdiff \
        else None


@register
class DevStage(Stage):
    name = "dev"
    kind = Kind.AGENT
    unit = UnitType.ITEM
    version = "1"
    description = "Agent fixes the source; tooling builds patch, " \
                  "changelog, source package and debdiff"

    def inputs(self, ctx, unit_id):
        return load(ctx.paths.root, "dev").digest

    def run(self, ctx, unit_ids):
        return [self._one(ctx, uid) for uid in unit_ids]

    def _context(self, ctx, uid: str) -> dict:
        item = ctx.item(uid)
        repro = ctx.result(uid, "reproduce") or {}
        excerpt = ctx.result(uid, "excerpt") or {}
        diag = ctx.result(item.get("cluster_id"), "diagnose") or {} \
            if item.get("cluster_id") else {}
        # Prefer the reproduction's excerpt: same toolchain as verify.
        text = (_read(repro.get("excerpt_path"), MAX_EXCERPT)
                or excerpt.get("text", ""))
        pack = {
            "package": {"source": item["source"],
                        "version": item["version"], "arch": item["arch"]},
            "key_lines": excerpt.get("key_lines", [])[:3],
            "excerpt": text[:MAX_EXCERPT],
            "diagnosis": {k: diag.get(k) for k in (
                "root_cause", "fix_kind", "fix_strategy", "patch_outline",
                "evidence", "upstream")},
            "facts": package_facts(ctx, item["source"]),
        }
        ref = reference_fix(ctx, uid)
        if ref:
            pack["reference_fix_for_same_failure"] = ref
        feedback = ctx.feedback(uid)
        if feedback:
            last = feedback[-1]["data"]
            prev = previous_edits(ctx, uid)
            pack["retry"] = {
                "attempt": len(feedback) + 1,
                "note": "The source tree already contains your previous"
                        " change (previous_change). Keep what is still"
                        " needed and fix the new build failure on top"
                        " of it.",
                "previous_change": (prev or "")[:MAX_REFERENCE] or None,
                "build_result": last.get("outcome"),
                "new_key_lines": last.get("key_lines"),
                "new_excerpt": (last.get("excerpt") or "")[:MAX_EXCERPT],
            }
        return pack

    def _one(self, ctx, uid: str) -> StageResult:
        item = ctx.item(uid)
        prompt = load(ctx.paths.root, "dev")
        adir = ctx.attempt_dir(uid)
        dsc = local.fetch_source(item["source"], item["version"],
                                 ctx.paths.cache / "sources")
        tree = srcpkg.unpack(dsc, adir / "tree")
        # A retry builds on the previous attempt, so fixes accumulate
        # instead of each attempt starting over from the original.
        prev = previous_edits(ctx, uid) if ctx.feedback(uid) else None
        if prev:
            srcpkg.apply_edits(tree, prev)
        res = ctx.run_agent(
            uid, json.dumps(self._context(ctx, uid), indent=1),
            output_schema=SCHEMA, system=prompt.text, cwd=tree,
            tool_policy=ToolPolicy(read=True, edit=True),
            attempt_dir=adir / "agent",
        )
        if not res.ok or res.data is None:
            return StageResult(uid, Status.ERROR,
                               {"error": res.error or "no result"})
        meta = res.data
        changed = srcpkg.changed_paths(tree)
        if not changed:
            return StageResult(uid, Status.NEEDS_HUMAN, {
                **meta, "reason": "agent made no changes"})
        edits = adir / EDITS
        edits.write_text(srcpkg.edits(tree))
        name, email = _identity(ctx)
        patch = srcpkg.record_upstream_changes(tree, srcpkg.PatchMeta(
            name=meta["patch_name"],
            description=meta["patch_description"],
            forwarded=meta["forwarded"],
            bug_debian=meta.get("bug_debian"),
            author=f"{name} <{email}>",
        ))
        version = srcpkg.next_ubuntu_version(item["version"])
        lines = [meta["summary"]]
        if patch:
            lines.append(f"d/p/{patch}: {_first_sentence(meta)}")
        packaging = [p for p in changed if p.startswith("debian/")]
        if packaging:
            lines.append(f"{', '.join(packaging)}: see patch description.")
        srcpkg.add_changelog(tree, version, _series(ctx), lines, name,
                             email, first_delta="ubuntu" not in
                             item["version"])
        new_dsc = srcpkg.build_source(tree, dsc.parent)
        debdiff = adir / "fix.debdiff"
        srcpkg.debdiff(dsc, new_dsc, debdiff)
        return StageResult(uid, Status.OK, {
            **meta,
            "version": version,
            "files_changed": changed,
            "patch_file": str(tree / "debian" / "patches" / patch)
            if patch else None,
            "new_dsc": str(new_dsc),
            "debdiff": str(debdiff),
            "debdiff_lines": len(debdiff.read_text().splitlines()),
        }, [str(debdiff), str(new_dsc), str(edits)])


def _first_sentence(meta: dict) -> str:
    text = meta["patch_description"].strip().split(". ")[0]
    return text.rstrip(".") + "."
