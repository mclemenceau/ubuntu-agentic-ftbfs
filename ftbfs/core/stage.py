# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Stage plugin contract and registry.

A stage is a unit of work in the pipeline. It declares what it works on
(item, package or cluster), what kind of work it is (which decides
concurrency limits and whether a human gate is forced), and a version
that invalidates cached results when bumped.

Stages are found in three places:
  - built-ins: modules in ftbfs/stages/
  - installed plugins: the `ftbfs.stages` entry point group
  - local plugins: *.py files in the configured plugins directory
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .context import Context


class Kind(StrEnum):
    DETERMINISTIC = "deterministic"  # pure code, no tokens
    AGENT = "agent"  # calls an agent backend
    BUILD = "build"  # sbuild / PPA
    OUTWARD = "outward"  # touches LP, Debian, a forge: always gated


class UnitType(StrEnum):
    ITEM = "item"  # source/version/arch
    PACKAGE = "package"  # source
    CLUSTER = "cluster"  # failure cluster id


class Status(StrEnum):
    OK = "ok"  # done, downstream may proceed
    FAIL = "fail"  # a legitimate negative outcome (build failed, ...)
    SKIP = "skip"  # stage decided it does not apply
    NEEDS_HUMAN = "needs_human"  # cannot proceed without a person
    ERROR = "error"  # crash / transient problem: retried next run
    PENDING = "pending"  # waiting on something external (e.g. a PPA
    #                      build): polled once per run, not an error


# Statuses that are final for a given set of inputs (cache hits).
FINAL = {Status.OK, Status.FAIL, Status.SKIP, Status.NEEDS_HUMAN}


@dataclass
class StageResult:
    """`status` and `id` are reserved: downstream stages and `when`
    expressions see a result as {**data, "status": ..., "id": ...}."""

    unit_id: str
    status: Status
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    backend: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    cost: float | None = None


class Stage(ABC):
    name: ClassVar[str]
    kind: ClassVar[Kind]
    unit: ClassVar[UnitType] = UnitType.ITEM
    version: ClassVar[str] = "1"
    # Units handed to one run() call; >1 lets agent stages pack items
    # into a single prompt.
    batch_size: ClassVar[int] = 1
    description: ClassVar[str] = ""

    def inputs(self, ctx: Context, unit_id: str) -> Any:
        """Extra stage-specific inputs folded into the cache key."""
        return None

    def eligible(self, ctx: Context, unit_id: str) -> str | None:
        """Return a reason when this unit should not run, else None."""
        return None

    @abstractmethod
    def run(self, ctx: Context, unit_ids: list[str]) -> list[StageResult]:
        """Process a batch; return exactly one result per unit."""


STAGES: dict[str, type[Stage]] = {}


def register(cls: type[Stage]) -> type[Stage]:
    if not getattr(cls, "name", None):
        raise TypeError(f"{cls.__name__} has no name")
    other = STAGES.get(cls.name)
    if other is not None and other is not cls:
        raise TypeError(
            f"stage name {cls.name!r} already used by {other.__module__}"
        )
    STAGES[cls.name] = cls
    return cls


def discover(plugins_dir: Path | None = None) -> dict[str, type[Stage]]:
    from .. import stages as builtin

    for mod in pkgutil.iter_modules(builtin.__path__):
        importlib.import_module(f"{builtin.__name__}.{mod.name}")
    for ep in importlib.metadata.entry_points(group="ftbfs.stages"):
        register(ep.load())
    if plugins_dir and plugins_dir.is_dir():
        for path in sorted(plugins_dir.glob("*.py")):
            spec = importlib.util.spec_from_file_location(
                f"ftbfs_plugin_{path.stem}", path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    return STAGES
