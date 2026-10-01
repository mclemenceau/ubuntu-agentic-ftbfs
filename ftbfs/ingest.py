# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turn the qa.ubuntuwire.com/ftbfs HTML page into structured data.

The page has one table per component (h2 sections), then one table per
packageset and per team (h3 sections) which repeat rows from the component
tables. We take package data from component tables and only membership from
the others.

The page comes over plain HTTP (the site has no HTTPS), so it is
untrusted: names and versions become paths and command arguments, and
links are fetched and shown. Every such field must have the shape
Launchpad gives it, or the whole page is rejected.
"""

from __future__ import annotations

import gzip
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lxml import html

FTBFS_URL = "http://qa.ubuntuwire.com/ftbfs/"
COMPONENTS = ("main", "restricted", "universe", "multiverse")
STATES = {
    "FAILEDTOBUILD": "F",
    "MANUALDEPWAIT": "M",
    "CANCELLED": "X",
    "UPLOADFAIL": "U",
    "CHROOTWAIT": "C",
}

_TIP_RE = re.compile(r"Tip\('(.*?)'\)", re.S)
_FINISHED_RE = re.compile(r"Build finished on (\S+ \S+) UTC")
_BUILD_ID_RE = re.compile(r"/\+build/(\d+)")
_TITLE_RE = re.compile(r"Build status for Ubuntu (\S+) in", re.I)

# Debian policy 5.6.1 and 5.6.12; arch and series as Launchpad names them.
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+~:-]*")
_ARCH_RE = re.compile(r"[a-z0-9]+")
_SERIES_RE = re.compile(r"[a-z]+")
LAUNCHPAD = "https://launchpad.net/"
WEB = ("https://", "http://")


def _checked(value: str, pattern: re.Pattern, what: str) -> str:
    if not pattern.fullmatch(value):
        raise ParseError(f"bad {what} {value[:80]!r}")
    return value


def _link(url: str | None, prefixes: tuple[str, ...] | str,
          what: str) -> str | None:
    # Empty links occur (a build without a log) and are kept as they are.
    if url and not url.startswith(prefixes):
        raise ParseError(f"bad {what} {url[:80]!r}")
    return url


@dataclass
class Build:
    arch: str
    state: str
    build_id: int
    build_url: str
    log_url: str | None
    finished_at: str | None
    note: str | None


@dataclass
class Version:
    version: str
    pocket: str
    changed_by: str | None
    builds: list[Build] = field(default_factory=list)


@dataclass
class Package:
    source: str
    component: str
    lp_bugs: list[dict] = field(default_factory=list)
    packagesets: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    pts: str | None = None
    bts: str | None = None
    versions: list[Version] = field(default_factory=list)


@dataclass
class Snapshot:
    series: str
    fetched_at: str
    source_url: str
    arches: list[str]
    packages: list[Package]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        pkgs = []
        for p in d["packages"]:
            versions = [
                Version(
                    **{k: v for k, v in ver.items() if k != "builds"},
                    builds=[Build(**b) for b in ver["builds"]],
                )
                for ver in p["versions"]
            ]
            fields = {k: v for k, v in p.items() if k != "versions"}
            pkgs.append(Package(**fields, versions=versions))
        return cls(
            series=d["series"],
            fetched_at=d["fetched_at"],
            source_url=d["source_url"],
            arches=d["arches"],
            packages=pkgs,
        )


def fetch(url: str = FTBFS_URL, timeout: int = 300) -> bytes:
    """Download the page. It is ~3 MB and the server is slow."""
    req = urllib.request.Request(
        url, headers={"Accept-Encoding": "gzip", "User-Agent": "ftbfs-review"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data


def read_html(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def _tip(el) -> str | None:
    m = _TIP_RE.search(el.get("onmouseover", ""))
    return html.fromstring(f"<p>{m.group(1)}</p>").text_content() if m else None


def _section_tables(doc, tag: str):
    """Yield (section id, table element) for each h2/h3 section."""
    for head in doc.iter(tag):
        sid = head.get("id")
        if not sid:
            continue
        table = head.getnext()
        while table is not None and table.tag != "table":
            table = table.getnext()
        if table is not None:
            yield sid, table


def _arches(table) -> list[str]:
    rows = table.findall("./thead/tr")
    return [_checked(th.text_content().strip(), _ARCH_RE, "arch")
            for th in rows[1].findall("th")]


def _parse_version_cell(td) -> tuple[str, str, str | None]:
    text = td.text_content().strip()
    pocket = "proposed" if text.endswith("(Proposed)") else "release"
    version = _checked(text.removesuffix("(Proposed)").strip(),
                       _VERSION_RE, "version")
    changed_by = _tip(td)
    if changed_by:
        changed_by = changed_by.removeprefix("Changed-By:").strip()
    return version, pocket, changed_by


def _parse_build_cell(td, arch: str) -> Build | None:
    state = td.get("class")
    if state not in STATES:
        return None
    links = td.findall(".//a")
    build_url = _link(links[0].get("href"), LAUNCHPAD, "build link")
    link_arch = links[0].text_content().split()[0]
    if link_arch != arch:
        raise ValueError(f"arch column mismatch: {link_arch} != {arch}")
    log_url = _link(links[1].get("href") if len(links) > 1 else None,
                    LAUNCHPAD, "log link")
    tip = _tip(td)
    finished = None
    note = tip
    if tip and (m := _FINISHED_RE.search(tip)):
        finished = m.group(1)
        note = None
    return Build(
        arch=arch,
        state=state,
        build_id=int(_BUILD_ID_RE.search(build_url).group(1)),
        build_url=build_url,
        log_url=log_url,
        finished_at=finished,
        note=note,
    )


def _parse_component(table, component: str, arches: list[str]):
    pkg: Package | None = None
    for tr in table.findall("./tbody/tr"):
        tds = tr.findall("td")
        first = tds[0].find("a")
        href = first.get("href", "") if first is not None else ""
        is_pkg_cell = (
            first is not None
            and "/+source/" in href
            and href.rstrip("/").count("/") == 5
        )
        if is_pkg_cell:
            if pkg is not None:
                yield pkg
            pkg = Package(source=_checked(first.text_content().strip(),
                                          _SOURCE_RE, "source name"),
                          component=component)
            tds = tds[1:]
        if pkg is None:
            raise ValueError("continuation row without package")
        version, pocket, changed_by = _parse_version_cell(tds[0])
        rest = tds[1:]
        if is_pkg_cell:
            rest = rest[1:]  # packageset marker cell
        ver = Version(version=version, pocket=pocket, changed_by=changed_by)
        for arch, td in zip(arches, rest[: len(arches)], strict=True):
            if (b := _parse_build_cell(td, arch)) is not None:
                ver.builds.append(b)
        pkg.versions.append(ver)
        if is_pkg_cell:
            bugs_td, links_td = rest[len(arches)], rest[len(arches) + 1]
            for a in bugs_td.findall(".//a"):
                bug_id = int(a.get("href").rstrip("/").rsplit("/", 1)[1])
                pkg.lp_bugs.append(
                    {"id": bug_id, "title": a.get("title") or ""}
                )
            for a in links_td.findall(".//a"):
                label = a.text_content().strip()
                if label == "PTS":
                    pkg.pts = _link(a.get("href"), WEB, "PTS link")
                elif label == "BTS":
                    pkg.bts = _link(a.get("href"), WEB, "BTS link")
    if pkg is not None:
        yield pkg


def _membership(table) -> set[str]:
    names = set()
    for tr in table.findall("./tbody/tr"):
        a = tr.find("td/a")
        if a is not None and a.get("href", "").count("/") == 5:
            names.add(a.text_content().strip())
    return names


def parse(data: bytes, fetched_at: str | None = None,
          source_url: str = FTBFS_URL) -> Snapshot:
    doc = html.fromstring(data)
    title = doc.findtext(".//title") or ""
    m = _TITLE_RE.search(title)
    series = _checked(m.group(1).lower(), _SERIES_RE, "series") \
        if m else "unknown"

    packages: dict[str, Package] = {}
    arches: list[str] = []
    for sid, table in _section_tables(doc, "h2"):
        if sid not in COMPONENTS:
            continue
        arches = _arches(table)
        for pkg in _parse_component(table, sid, arches):
            packages[pkg.source] = pkg

    for sid, table in _section_tables(doc, "h3"):
        for suffix, attr in (("-pkgset", "packagesets"), ("-team", "teams")):
            if sid.endswith(suffix):
                name = sid.removesuffix(suffix)
                for src in _membership(table):
                    if src in packages:
                        getattr(packages[src], attr).append(name)

    snap = Snapshot(
        series=series,
        fetched_at=fetched_at
        or datetime.now(UTC).isoformat(timespec="seconds"),
        source_url=source_url,
        arches=arches,
        packages=sorted(packages.values(), key=lambda p: p.source),
    )
    _check_against_legend(doc, snap)
    return snap


class ParseError(Exception):
    pass


def legend_totals(doc) -> dict[str, dict[str, int]]:
    """Per state, per arch counts from the page's own legend table."""
    for table in doc.iter("table"):
        head = table.getprevious()
        if head is None or not head.text_content().startswith("Legend"):
            continue
        arches = _arches(table)
        totals = {}
        for tr in table.findall("./tbody/tr"):
            tds = tr.findall("td")
            state = tds[0].get("class")
            if state in STATES:
                counts = [int(td.text_content().strip() or 0)
                          for td in tds[2:]]
                totals[state] = dict(zip(arches, counts, strict=True))
        return totals
    raise ParseError("legend table not found")


def _check_against_legend(doc, snap: Snapshot) -> None:
    """Fail loudly if the page format changed and we miscounted."""
    expected = legend_totals(doc)
    got: dict[str, dict[str, int]] = {}
    for p in snap.packages:
        for v in p.versions:
            for b in v.builds:
                per_arch = got.setdefault(b.state, {})
                per_arch[b.arch] = per_arch.get(b.arch, 0) + 1
    if set(got) - set(expected):
        raise ParseError(
            f"states missing from legend: {sorted(set(got) - set(expected))}"
        )
    for state, per_arch in expected.items():
        for arch, n in per_arch.items():
            if got.get(state, {}).get(arch, 0) != n:
                raise ParseError(
                    f"{state}/{arch}: parsed "
                    f"{got.get(state, {}).get(arch, 0)}, legend says {n}"
                )
