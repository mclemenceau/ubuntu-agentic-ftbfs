# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bulk data sources for package facts, cached once per UTC day.

- Ubuntu and Debian Sources indexes: newest version, Homepage, Vcs-*
- reproducible-builds tracker: Debian testing build status per arch

Each source is downloaded once a day and reduced to a compact JSON index
in cache/facts/, so a facts run over hundreds of packages costs a few
bulk downloads, not hundreds of requests.
"""

from __future__ import annotations

import json
import lzma
import threading
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from .version import compare

UBUNTU_MIRROR = "http://archive.ubuntu.com/ubuntu"
DEBIAN_MIRROR = "https://deb.debian.org/debian"
REPRO_URL = ("https://tests.reproducible-builds.org/debian/"
             "reproducible-tracker.json")

UBUNTU_COMPONENTS = ("main", "restricted", "universe", "multiverse")
DEBIAN_COMPONENTS = ("main", "contrib", "non-free", "non-free-firmware")
KEEP_FIELDS = {
    "Version": "version",
    "Homepage": "homepage",
    "Vcs-Git": "vcs_git",
    "Vcs-Browser": "vcs_browser",
    "Maintainer": "maintainer",
    "Testsuite": "testsuite",
}

_locks: dict[str, threading.Lock] = {}
_memo: dict[Path, dict] = {}
_guard = threading.Lock()


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _download(url: str, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ftbfs-review"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def daily_index(cache_dir: Path, name: str,
                build: Callable[[], dict]) -> dict:
    """Return today's index `name`, building (downloading) it at most once
    per day and process. Old days are removed."""
    path = cache_dir / f"{name}-{today()}.json"
    with _guard:
        lock = _locks.setdefault(name, threading.Lock())
    with lock:
        if path in _memo:
            return _memo[path]
        if not path.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            data = build()
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(data))
            tmp.rename(path)
            for old in cache_dir.glob(f"{name}-*.json"):
                if old != path:
                    old.unlink()
        _memo[path] = json.loads(path.read_text())
        return _memo[path]


def iter_deb822(text: str) -> Iterator[dict[str, str]]:
    para: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            if para:
                yield para
            para, key = {}, None
        elif line[0] in " \t":
            if key:
                para[key] += "\n" + line.strip()
        else:
            key, _, value = line.partition(":")
            para[key] = value.strip()
    if para:
        yield para


def parse_sources(text: str, into: dict[str, dict]) -> None:
    """Merge a Sources index into `into`, keeping the newest version."""
    for p in iter_deb822(text):
        name = p.get("Package")
        if not name or "Version" not in p:
            continue
        old = into.get(name)
        if old and compare(old["version"], p["Version"]) >= 0:
            continue
        into[name] = {v: p[k] for k, v in KEEP_FIELDS.items() if k in p}


def _sources(mirror: str, suites: list[str], components: tuple) -> dict:
    index: dict[str, dict] = {}
    for suite in suites:
        for comp in components:
            url = f"{mirror}/dists/{suite}/{comp}/source/Sources.xz"
            try:
                raw = _download(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:  # e.g. component absent in a suite
                    continue
                raise
            parse_sources(lzma.decompress(raw).decode("utf-8", "replace"),
                          index)
    return index


def ubuntu_sources(cache_dir: Path, series: str) -> dict:
    return daily_index(
        cache_dir, f"ubuntu-{series}",
        lambda: _sources(UBUNTU_MIRROR, [series, f"{series}-proposed"],
                         UBUNTU_COMPONENTS),
    )


def debian_sources(cache_dir: Path, suite: str) -> dict:
    return daily_index(
        cache_dir, f"debian-{suite}",
        lambda: _sources(DEBIAN_MIRROR, [suite], DEBIAN_COMPONENTS),
    )


def reduce_repro(entries: list[dict]) -> dict:
    out = {}
    for e in entries:
        out[e["package"]] = {
            "suite": e.get("suite"),
            "version": e.get("version"),
            "status": e.get("status"),
            "arches": {
                a["architecture"]: {"status": a.get("status"),
                                    "version": a.get("version"),
                                    "date": a.get("build_date")}
                for a in e.get("architecture_details", [])
            },
        }
    return out


def repro_status(cache_dir: Path) -> dict:
    return daily_index(
        cache_dir, "repro",
        lambda: reduce_repro(json.loads(_download(REPRO_URL))),
    )
