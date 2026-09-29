"""What every builder shares: the result of a build, and the error
that says a builder cannot build right now."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class BuildResult:
    ok: bool  # dpkg-buildpackage succeeded
    exit_code: int
    log: Path | None
    duration_s: float
    timed_out: bool = False
    stalled: bool = False  # killed because its output stopped growing
    builder: str | None = None  # name of the builder that ran it


class BuilderUnavailable(Exception):
    """The builder failed before sbuild started (host unreachable, no
    image, worker would not start): a host problem, not a build result.
    The pool takes the builder out for a while and uses another."""
