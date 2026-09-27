"""Debian version comparison (dpkg's algorithm, no python-apt needed)."""

from __future__ import annotations

import functools
import re

_VERSION_RE = re.compile(r"^(?:(\d+):)?(.+?)(?:-([^-]*))?$")


def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (
            j < len(b) and not b[j].isdigit()
        ):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def parse(v: str) -> tuple[int, str, str]:
    m = _VERSION_RE.match(v.strip())
    if not m:
        raise ValueError(f"bad version {v!r}")
    epoch, upstream, revision = m.groups()
    return int(epoch or 0), upstream, revision or ""


def compare(a: str, b: str) -> int:
    """<0 if a < b, 0 if equal, >0 if a > b."""
    ea, ua, ra = parse(a)
    eb, ub, rb = parse(b)
    if ea != eb:
        return ea - eb
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


sort_key = functools.cmp_to_key(compare)


def newest(versions) -> str | None:
    versions = [v for v in versions if v]
    return max(versions, key=sort_key) if versions else None


def has_ubuntu_delta(v: str) -> bool:
    """True for 1.0-1ubuntu1, not for no-change rebuilds (1.0-1build1)."""
    return "ubuntu" in parse(v)[2] or "ubuntu" in parse(v)[1]
