# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cluster helpers shared by cluster-level stages: pick a representative
item and build the compact context pack handed to LLM prompts.

The pack is the token budget: an excerpt (~3 KB), a few key lines from
other members, and a handful of facts. Never a raw log.
"""

from __future__ import annotations

from .core.stage import UnitType

# Real hardware first; amd64 is also what the local builder reproduces.
ARCH_PREFERENCE = ["amd64", "arm64", "amd64v3", "ppc64el", "s390x",
                   "armhf", "riscv64", "i386"]
MAX_PACKAGES = 8
MAX_OTHER_KEYS = 3


def members(ctx, cluster_id: str) -> list[str]:
    return ctx.units.children(UnitType.CLUSTER, cluster_id)


def representative(ctx, cluster_id: str) -> str:
    def key(iid):
        arch = ctx.item(iid)["arch"]
        rank = (ARCH_PREFERENCE.index(arch) if arch in ARCH_PREFERENCE
                else len(ARCH_PREFERENCE))
        return rank, iid

    return min(members(ctx, cluster_id), key=key)


def package_facts(ctx, source: str) -> dict:
    """The few facts that matter to a reviewer, not the whole record."""
    f = ctx.result(source, "facts")
    if not f or f.get("status") != "ok":
        return {}
    bugs = f["debian_bugs"]["open"] + f["debian_bugs"]["fixed_newer"]
    return {
        "signals": f["signals"],
        "ubuntu_version": f["ubuntu"]["newest_failing"],
        "ubuntu_delta": f["ubuntu"]["delta"],
        "debian_unstable": f["debian"]["unstable"],
        "debian_experimental": f["debian"]["experimental"],
        "debian_testing_build": {
            a: i["status"]
            for a, i in f["debian"]["testing_repro"]["arches"].items()
        },
        "debian_bugs": [
            {"id": b["id"], "title": b["title"], "done": b["done"],
             "patch": b["patch"], "fixed_in": b.get("fixed_in")}
            for b in bugs[:4]
        ],
        "upstream": f["upstream"]["repo"] or f["upstream"]["homepage"],
    }


def context_pack(ctx, cluster_id: str, excerpt: bool = True) -> dict:
    mem = members(ctx, cluster_id)
    rep = representative(ctx, cluster_id)
    item = ctx.item(rep)
    cls = ctx.result(rep, "classify") or {}
    ex = ctx.result(rep, "excerpt") or {}
    packages = sorted({ctx.item(m)["source"] for m in mem})
    arches = sorted({ctx.item(m)["arch"] for m in mem})
    rep_keys = set(ex.get("key_lines", [])[:1])
    others = []
    for m in mem:
        if m == rep or len(others) >= MAX_OTHER_KEYS:
            continue
        mex = ctx.result(m, "excerpt") or {}
        first = (mex.get("key_lines") or [None])[0]
        if first and first not in rep_keys:
            rep_keys.add(first)
            others.append({"item": m, "key_line": first})
    pack = {
        "cluster": cluster_id,
        "class": cls.get("class"),
        "family": cls.get("family"),
        "rule_hint": cls.get("hint") or None,
        "items": len(mem),
        "packages": packages[:MAX_PACKAGES],
        "package_count": len(packages),
        "arches": arches,
        "representative": {
            "item": rep,
            "source": item["source"],
            "version": item["version"],
            "arch": item["arch"],
            "failed_step": ex.get("step"),
            "fail_stage": ex.get("fail_stage"),
            "key_lines": ex.get("key_lines", [])[:3],
            "facts": package_facts(ctx, item["source"]),
        },
        "other_members": others,
    }
    if excerpt:
        pack["representative"]["excerpt"] = ex.get("text", "")
    return pack


def cluster_signals(ctx, cluster_id: str) -> list[set[str]]:
    """Facts signals of every package in the cluster."""
    out = []
    for source in sorted({ctx.item(m)["source"]
                          for m in members(ctx, cluster_id)}):
        f = ctx.result(source, "facts") or {}
        out.append(set(f.get("signals", [])))
    return out
