"""Judge a rebuild against the original Launchpad failure."""

from __future__ import annotations

from ..logs import Excerpt
from ..rules import RuleSet

REPRODUCED = "reproduced"  # same normalized error signature
SIMILAR = "reproduced-similar"  # same failure cluster, different text
DIFFERENT = "different-failure"  # fails, but for another reason
BUILT = "built"  # builds fine now: flaky, or fixed by newer deps
INFRA = "infra-error"  # the builder itself failed (no verdict)


def judge(ok: bool, excerpt: Excerpt | None, original: dict,
          rules: RuleSet, source: str) -> tuple[str, dict]:
    """Return (outcome, details). `original` is the excerpt stage data
    of the Launchpad build."""
    if ok:
        return BUILT, {}
    if excerpt is None or excerpt.fail_stage in (None, "", "?"):
        return INFRA, {"reason": "no usable build log"}
    ex = excerpt.to_dict()
    new = rules.classify(ex, source)
    old = rules.classify(original, source)
    details = {
        "signature": excerpt.signature,
        "original_signature": original.get("signature"),
        "key_lines": excerpt.key_lines[:3],
        "fail_stage": excerpt.fail_stage,
        "step": excerpt.step,
        "cluster_id": new.cluster_id,
        "original_cluster_id": old.cluster_id,
    }
    if excerpt.fail_stage not in ("build", "install-deps"):
        # e.g. fetch-src, unpack, chroot setup: not the package's fault
        return INFRA, {**details, "reason": f"failed at {excerpt.fail_stage}"}
    if excerpt.signature == original.get("signature"):
        return REPRODUCED, details
    if new.cluster_id == old.cluster_id or (
            new.cls == old.cls and new.cls != "unknown"):
        return SIMILAR, details
    return DIFFERENT, details
