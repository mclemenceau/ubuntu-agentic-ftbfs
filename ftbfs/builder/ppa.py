"""Launchpad PPA builds for architectures we cannot build locally.

Reproduction copies the exact source from the Ubuntu primary archive into
the user's PPA (no signing, no upload), which triggers real Launchpad
builds. Builds are asynchronous: callers poll status() on later runs.

One-time setup (interactive): `ftbfs lp-login`, and in the PPA's
"Edit PPA dependencies" page select the Proposed pocket so builds resolve
dependencies like the primary archive does.
"""

from __future__ import annotations

import gzip
import urllib.request
from dataclasses import dataclass
from pathlib import Path

APP = "ftbfs-review"

# Launchpad buildstate -> our coarse phase
DONE_OK = {"Successfully built"}
DONE_FAIL = {"Failed to build"}
RUNNING = {"Needs building", "Currently building", "Uploading build",
           "Gathering build output"}
# Anything else ("Dependency wait", "Chroot problem", "Failed to upload",
# "Cancelled build", "Build for superseded Source") is not a verdict on
# the package itself.


class NotConfigured(RuntimeError):
    pass


@dataclass
class PPABuild:
    state: str
    arch: str
    web_link: str
    log_url: str | None


def login(credentials: Path):
    from launchpadlib.launchpad import Launchpad

    return Launchpad.login_with(
        APP, "production", version="devel",
        credentials_file=str(credentials),
    )


class PPA:
    def __init__(self, credentials: Path, ref: str, series: str):
        """ref is 'owner/name', e.g. 'someone/ftbfs-repro'."""
        if not credentials.exists():
            raise NotConfigured(
                "no Launchpad credentials; run `ftbfs lp-login` once")
        if "/" not in ref:
            raise NotConfigured(f"ppa must be 'owner/name', got {ref!r}")
        self.lp = login(credentials)
        owner, name = ref.split("/", 1)
        self.archive = self.lp.people[owner].getPPAByName(name=name)
        self.ubuntu = self.lp.distributions["ubuntu"]
        self.series = series
        self.ref = ref

    def processors(self) -> set[str]:
        return {p.name for p in self.archive.processors}

    def copy(self, source: str, version: str) -> None:
        """Copy the source (not binaries) from the primary archive. A
        version already present is fine (idempotent retries)."""
        if self._published(source, version):
            return
        self.archive.copyPackage(
            source_name=source, version=version,
            from_archive=self.ubuntu.main_archive,
            to_series=self.series, to_pocket="Release",
            include_binaries=False,
        )

    def _published(self, source: str, version: str):
        pubs = self.archive.getPublishedSources(
            source_name=source, version=version, exact_match=True)
        for pub in pubs:
            return pub
        return None

    def status(self, source: str, version: str, arch: str) -> PPABuild | None:
        pub = self._published(source, version)
        if pub is None:
            return None
        for b in pub.getBuilds():
            if b.arch_tag == arch:
                return PPABuild(b.buildstate, arch, b.web_link,
                                b.build_log_url)
        return None


def fetch_build_log(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": APP})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    dest.write_bytes(data)
    return dest
