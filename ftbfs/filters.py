# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Selection of the items a run works on.

A filter never hides why an item was left out: explain() returns the
reasons, which `ftbfs why` and the UI show.
"""

from __future__ import annotations

import fnmatch
import json
import random
from dataclasses import asdict, dataclass, field, fields

from .db import DB

STATE_CODES = {
    "F": "FAILEDTOBUILD",
    "M": "MANUALDEPWAIT",
    "X": "CANCELLED",
    "U": "UPLOADFAIL",
    "C": "CHROOTWAIT",
}


@dataclass
class Filter:
    components: list[str] = field(default_factory=lambda: ["universe"])
    states: list[str] = field(default_factory=lambda: ["FAILEDTOBUILD"])
    arches: list[str] = field(default_factory=list)  # empty = all
    pockets: list[str] = field(default_factory=list)  # release, proposed
    packagesets: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # glob patterns
    skip_lp_bug: bool = True
    include_gone: bool = False
    limit: int | None = None
    # Random whole clusters (all their selected items); for evaluating
    # cluster-level stages on a representative sample.
    sample_clusters: int | None = None
    seed: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> Filter:
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown filter keys: {sorted(unknown)}")
        f = cls(**d)
        f.states = [STATE_CODES.get(s, s) for s in f.states]
        return f

    def to_dict(self) -> dict:
        return asdict(self)

    def explain(self, row) -> list[str]:
        """Reasons this item is excluded; empty means selected."""
        why = []
        if not self.include_gone and row["lifecycle"] == "gone":
            why.append("no longer listed as failing (gone)")
        checks = (
            ("component", row["component"], self.components),
            ("state", row["state"], self.states),
            ("arch", row["arch"], self.arches),
            ("pocket", row["pocket"], self.pockets),
        )
        for label, value, allowed in checks:
            if allowed and value not in allowed:
                why.append(f"{label} {value} not in {allowed}")
        if self.packagesets and not set(
            json.loads(row["packagesets"])
        ) & set(self.packagesets):
            why.append(f"not in packagesets {self.packagesets}")
        if self.teams and not set(json.loads(row["teams"])) & set(
            self.teams
        ):
            why.append(f"not in teams {self.teams}")
        if self.sources and not any(
            fnmatch.fnmatch(row["source"], pat) for pat in self.sources
        ):
            why.append(f"source not matching {self.sources}")
        bugs = json.loads(row["lp_bugs"])
        if self.skip_lp_bug and bugs:
            ids = ", ".join(f"LP: #{b['id']}" for b in bugs)
            why.append(f"already has a Launchpad bug ({ids})")
        return why


ITEM_QUERY = """
    SELECT i.*, p.component, p.packagesets, p.teams, p.lp_bugs, p.pts, p.bts
    FROM item i JOIN package p ON p.source = i.source
"""


def all_items(db: DB, where: str = "", params=()) -> list:
    return db.query(f"{ITEM_QUERY} {where} ORDER BY i.id", params)


def select(db: DB, flt: Filter) -> list:
    rows = [r for r in all_items(db) if not flt.explain(r)]
    if flt.sample_clusters:
        clusters = sorted({r["cluster_id"] for r in rows if r["cluster_id"]})
        n = min(flt.sample_clusters, len(clusters))
        keep = set(random.Random(flt.seed).sample(clusters, n))
        rows = [r for r in rows if r["cluster_id"] in keep]
    return rows[: flt.limit] if flt.limit else rows
