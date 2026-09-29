"""reproduce: rebuild the failing version and compare with Launchpad.

sbuild on our own builders (config.toml `[builders.*]`) for
`local_arches` (default amd64) that a builder supports; other arches go
to a PPA (option `ppa = "owner/name"`) when configured. PPA builds are
asynchronous: the stage returns `pending` and is polled on later runs.

Outcomes: reproduced, reproduced-similar, different-failure, built
(no longer fails: flaky or fixed by newer dependencies), timeout,
infra-error. Local infrastructure errors are retried (status error).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ..builder import local, outcome
from ..builder.ppa import (
    DONE_FAIL,
    DONE_OK,
    PPA,
    RUNNING,
    NotConfigured,
    fetch_build_log,
)
from ..core.stage import Kind, Stage, StageResult, Status, UnitType, register
from ..logs import extract
from ..rules import RuleSet

ARCHIVE = "http://archive.ubuntu.com/ubuntu"
KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
COPY_TIMEOUT_S = 6 * 3600


def _series(ctx) -> str:
    return ctx.db.one(
        "SELECT series FROM snapshot ORDER BY id DESC LIMIT 1")["series"]


def extra_repositories(component: str, series: str) -> list[str]:
    """The sbuild chroots only carry main+universe."""
    if component in ("main", "universe"):
        return []
    return [f"deb [signed-by={KEYRING}] {ARCHIVE} {suite} {component}"
            for suite in (series, f"{series}-proposed")]


@register
class ReproduceStage(Stage):
    name = "reproduce"
    kind = Kind.BUILD
    unit = UnitType.ITEM
    version = "1"
    description = "Rebuild the failing version (sbuild or PPA) and " \
                  "compare with the Launchpad failure"

    def _local(self, ctx, arch: str) -> bool:
        return (arch in ctx.options.get("local_arches", ["amd64"])
                and ctx.can_build(arch))

    def eligible(self, ctx, unit_id):
        arch = ctx.item(unit_id)["arch"]
        if self._local(ctx, arch) or ctx.options.get("ppa"):
            return None
        return f"no local builder for {arch} and no ppa configured"

    def run(self, ctx, unit_ids):
        rules = RuleSet.load(ctx.paths.root / ctx.options.get(
            "rules", "rules.toml"))
        out = []
        ppa = None
        for uid in unit_ids:
            item = ctx.item(uid)
            original = ctx.result(uid, "excerpt") or {}
            if self._local(ctx, item["arch"]):
                out.append(self._sbuild(ctx, uid, item, original, rules))
                continue
            try:
                ppa = ppa or PPA(ctx.paths.root / ctx.options.get(
                    "lp_credentials", "state/lp-credentials"),
                    ctx.options["ppa"], _series(ctx))
            except NotConfigured as e:
                out.append(StageResult(uid, Status.SKIP,
                                       {"reason": str(e)}))
                continue
            out.append(self._ppa(ctx, ppa, uid, item, original, rules))
        return out

    # -- local --------------------------------------------------------------

    def _sbuild(self, ctx, uid, item, original, rules) -> StageResult:
        series = _series(ctx)
        dsc = local.fetch_source(item["source"], item["version"],
                                 ctx.paths.cache / "sources")
        adir = ctx.attempt_dir(uid)
        b = ctx.build(uid, dsc, item["arch"], f"{series}-proposed",
                      adir / "build",
                      extra_repositories(item["component"], series),
                      dir=str(adir))
        base = {"where": "local", "builder": b.builder,
                "arch": item["arch"],
                "duration_s": round(b.duration_s),
                "log": str(b.log) if b.log else None}
        if b.timed_out:
            return StageResult(uid, Status.OK,
                               {**base, "outcome": "timeout",
                                "stalled": b.stalled})
        ex = extract(b.log.read_text(errors="replace")) if b.log else None
        verdict, details = outcome.judge(b.ok, ex, original, rules,
                                         item["source"])
        artifacts = [str(b.log)] if b.log else []
        data = {**base, "outcome": verdict, **details}
        if ex is not None:
            path = adir / "excerpt.txt"
            path.write_text(ex.text)
            artifacts.append(str(path))
            data["excerpt_path"] = str(path)
        if verdict == outcome.INFRA:
            return StageResult(uid, Status.ERROR,
                               {**data, "error": details.get("reason")},
                               artifacts)
        return StageResult(uid, Status.OK, data, artifacts)

    # -- ppa ----------------------------------------------------------------

    def _ppa(self, ctx, ppa, uid, item, original, rules) -> StageResult:
        arch = item["arch"]
        base = {"where": "ppa", "ppa": ppa.ref, "arch": arch}
        if arch not in ppa.processors():
            return StageResult(uid, Status.SKIP, {
                **base, "reason": f"PPA {ppa.ref} does not build {arch}"})
        prev = ctx.result(uid, self.name) or {}
        requested = prev.get("requested_at") if prev.get(
            "status") == "pending" else None
        st = ppa.status(item["source"], item["version"], arch)
        if st is None:
            if requested is None:
                ppa.copy(item["source"], item["version"])
                ctx.event("build_start", unit=uid, where="ppa", arch=arch)
                requested = time.time()
            elif time.time() - requested > COPY_TIMEOUT_S:
                return StageResult(uid, Status.ERROR, {
                    **base, "error": "copied source never got a build"})
            return StageResult(uid, Status.PENDING, {
                **base, "phase": "copy-requested",
                "requested_at": requested,
                "polled": datetime.now(UTC).isoformat(timespec="seconds")})
        base.update(web_link=st.web_link, state=st.state)
        if st.state in RUNNING:
            return StageResult(uid, Status.PENDING, {
                **base, "phase": "building",
                "requested_at": requested or time.time()})
        ctx.event("build_end", unit=uid, where="ppa", state=st.state)
        if st.state in DONE_OK:
            return StageResult(uid, Status.OK, {**base, "outcome":
                                                outcome.BUILT})
        if st.state in DONE_FAIL and st.log_url:
            adir = ctx.attempt_dir(uid)
            log = fetch_build_log(st.log_url, adir / "build.log")
            ex = extract(log.read_text(errors="replace"))
            (adir / "excerpt.txt").write_text(ex.text)
            verdict, details = outcome.judge(False, ex, original, rules,
                                             item["source"])
            return StageResult(uid, Status.OK, {
                **base, "outcome": verdict, "log": str(log), **details,
                "excerpt_path": str(adir / "excerpt.txt")},
                [str(log), str(adir / "excerpt.txt")])
        # Dependency wait, chroot problem, cancelled...: final for this
        # build, but says nothing about the package itself.
        return StageResult(uid, Status.OK, {
            **base, "outcome": outcome.INFRA,
            "reason": f"PPA build state: {st.state}"})
