"""Build hosts and their slots.

A builder runs one sbuild per slot. Stages never pick a host: they ask
the pool for a slot that can build their arch, and a build goes to the
builder with the most free slots. Builds are stateless (sources go in,
a log comes out), so any builder can take any unit.

A builder that fails before sbuild starts (BuilderUnavailable: host
unreachable, no image, ...) is taken out for DOWN_S and the build goes
to another one. Only when every builder for the arch is down does the
build fail.

The site config (config.local.toml) declares builders under
`[builders.<name>]`, with `kind = "local"` (sbuild on this machine) or
`kind = "lxd"` (worker containers on an LXD remote, see lxd.py).
Without any, the pool is one local builder with `[concurrency] build`
slots, which is how builds ran before there were several hosts.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .base import BuildResult
from .local import LocalBuilder
from .lxd import LxdBuilder

DOWN_S = 600


class Builder(Protocol):
    name: str
    slots: int
    arches: tuple[str, ...]
    parallel: int | None  # DEB_BUILD_OPTIONS parallel; None: the stage's

    def build(self, dsc: Path, arch: str, dist: str, build_dir: Path,
              extra_repos: list[str], parallel: int, timeout: int,
              idle_timeout: int) -> BuildResult: ...


class NoBuilder(Exception):
    """No builder in the pool can build this arch, or all that can are
    down."""


class BuilderPool:
    def __init__(self, builders: list[Builder]):
        names = [b.name for b in builders]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate builder names: {names}")
        self.builders = builders
        self._busy = {b.name: 0 for b in builders}
        self._down: dict[str, tuple[float, str]] = {}  # until, reason
        self._cond = threading.Condition()

    @property
    def slots(self) -> int:
        return sum(b.slots for b in self.builders)

    def arches(self) -> set[str]:
        return {a for b in self.builders for a in b.arches}

    def supports(self, arch: str) -> bool:
        return arch in self.arches()

    def busy(self) -> dict[str, int]:
        with self._cond:
            return dict(self._busy)

    def mark_down(self, name: str, reason: str,
                  for_s: float = DOWN_S) -> None:
        with self._cond:
            self._down[name] = (time.monotonic() + for_s, reason)
            self._cond.notify_all()

    def down(self) -> dict[str, str]:
        """Builders currently taken out, with why."""
        with self._cond:
            return {n: r for n, (until, r) in self._down.items()
                    if until > time.monotonic()}

    @contextmanager
    def slot(self, arch: str) -> Iterator[Builder]:
        """Hold a slot on the least busy builder for `arch`, waiting
        until one frees up."""
        if not self.supports(arch):
            raise NoBuilder(f"no builder for {arch}")
        with self._cond:
            while (b := self._pick(arch)) is None:
                up = [b for b in self.builders
                      if arch in b.arches and not self._is_down(b.name)]
                if not up:
                    raise NoBuilder(
                        f"every builder for {arch} is down: " + "; ".join(
                            f"{n}: {r}" for n, (_, r) in
                            self._down.items()))
                # Wake up now and then: nobody notifies when a down
                # period ends.
                self._cond.wait(timeout=30)
            self._busy[b.name] += 1
        try:
            yield b
        finally:
            with self._cond:
                self._busy[b.name] -= 1
                self._cond.notify_all()

    def _is_down(self, name: str) -> bool:
        down = self._down.get(name)
        return down is not None and down[0] > time.monotonic()

    def _pick(self, arch: str) -> Builder | None:
        free = [(b.slots - self._busy[b.name], b) for b in self.builders
                if arch in b.arches and self._busy[b.name] < b.slots
                and not self._is_down(b.name)]
        if not free:
            return None
        return max(free, key=lambda fb: fb[0])[1]


def make_pool(raw: dict[str, dict], default_slots: int,
              state_dir: Path | None = None) -> BuilderPool:
    """The pool from the config's `[builders.*]` tables. LXD workers
    are locked under state_dir/builders."""
    if not raw:
        return BuilderPool([LocalBuilder("local", default_slots)])
    builders: list[Builder] = []
    for name, conf in raw.items():
        conf = dict(conf)
        kind = conf.pop("kind", "local")
        if kind == "local":
            builders.append(LocalBuilder(name, **_common(name, conf)))
        elif kind == "lxd":
            remote = conf.pop("remote", "local")
            image = conf.pop("image", None)
            b = LxdBuilder(name, remote=remote, **_common(name, conf),
                           lock_dir=state_dir / "builders"
                           if state_dir else None)
            if image:
                b.image = image
            builders.append(b)
        else:
            raise ValueError(f"builder {name}: unknown kind {kind!r}")
    return BuilderPool(builders)


def _common(name: str, conf: dict) -> dict:
    out = {"slots": int(conf.pop("slots", 1))}
    if "arches" in conf:
        out["arches"] = tuple(conf.pop("arches"))
    if "parallel" in conf:
        out["parallel"] = int(conf.pop("parallel"))
    if conf:
        raise ValueError(f"builder {name}: unknown keys {sorted(conf)}")
    return out
