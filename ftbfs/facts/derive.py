"""Turn raw Ubuntu/Debian data for one source package into facts and
signals. Pure function: no I/O, easy to test.

Signals are deterministic answers before any LLM runs; SIGNALS
describes each one.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .version import compare, has_ubuntu_delta, newest

FORGES = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "codeberg.org": "codeberg",
    "sourceforge.net": "sourceforge",
    "bitbucket.org": "bitbucket",
    "gitlab.gnome.org": "gitlab",
    "gitlab.freedesktop.org": "gitlab",
    "invent.kde.org": "gitlab",
    "git.sr.ht": "sourcehut",
    "pagure.io": "pagure",
}
# Arch names differ: Ubuntu amd64v3 builds are checked against amd64.
REPRO_ARCH = {"amd64v3": "amd64"}

SIGNALS = {
    "not-in-debian": "source not in Debian unstable/experimental",
    "sync-candidate": "Debian unstable is newer; no Ubuntu delta",
    "merge-candidate": "Debian unstable is newer; Ubuntu delta",
    "newer-in-experimental": "only Debian experimental is newer",
    "fixed-in-debian": "a Debian FTBFS bug is fixed in a version newer"
                       " than the failing Ubuntu one",
    "debian-ftbfs-open": "open FTBFS bug(s) in Debian",
    "debian-patch": "an open Debian FTBFS bug carries a patch",
    "ftbfs-in-debian-testing": "reproducible-builds sees FTBFS in testing"
                               " on a failing arch",
    "builds-in-debian-testing": "testing builds fine on a failing arch",
}


def forge(url: str | None) -> tuple[str | None, str | None]:
    """(forge kind, repo url) when url points at a known code forge."""
    if not url:
        return None, None
    u = urlparse(url.split()[0])
    host = (u.hostname or "").lower().removeprefix("www.")
    kind = FORGES.get(host)
    if not kind:
        return None, None
    parts = [p for p in u.path.split("/") if p]
    if kind == "sourceforge" or len(parts) < 2:
        return kind, url
    repo = "/".join(parts[:2]).removesuffix(".git")
    return kind, f"https://{host}/{repo}"


def derive(source: str, failing: dict[str, list[str]],
           ubuntu: dict | None, unstable: dict | None,
           experimental: dict | None, repro: dict | None,
           bugs: list[dict]) -> dict:
    """failing: {version: [arches]} for this source's selected items."""
    ubuntu_version = newest(failing)
    delta = has_ubuntu_delta(ubuntu_version)
    deb_versions = [x["version"] for x in (unstable, experimental) if x]
    deb_newest = newest(deb_versions)
    signals: list[str] = []

    if not deb_versions:
        signals.append("not-in-debian")
    elif unstable and compare(unstable["version"], ubuntu_version) > 0:
        signals.append("merge-candidate" if delta else "sync-candidate")
    elif compare(deb_newest, ubuntu_version) > 0:
        signals.append("newer-in-experimental")

    open_bugs = [b for b in bugs if not b["done"]]
    fixed = [b for b in bugs if b["done"] and any(
        compare(v.split("/")[-1], ubuntu_version) > 0
        for v in b.get("fixed_in") or [])]
    if fixed:
        signals.append("fixed-in-debian")
    if open_bugs:
        signals.append("debian-ftbfs-open")
    if any(b["patch"] for b in open_bugs):
        signals.append("debian-patch")

    arches = sorted({a for archs in failing.values() for a in archs})
    repro_arches = {}
    if repro:
        for a in arches:
            info = repro["arches"].get(REPRO_ARCH.get(a, a))
            if info:
                repro_arches[a] = info
        if any(i["status"] == "FTBFS" for i in repro_arches.values()):
            signals.append("ftbfs-in-debian-testing")
        elif any(i["status"] in ("reproducible", "FTBR")
                 for i in repro_arches.values()):
            signals.append("builds-in-debian-testing")

    homepage = (ubuntu or unstable or {}).get("homepage")
    vcs = (ubuntu or unstable or {}).get("vcs_git")
    kind, repo = forge(homepage)
    if not kind:
        # Vcs-Git is usually salsa packaging, but some packages point at
        # the upstream forge directly.
        kind, repo = forge(vcs)

    return {
        "source": source,
        "ubuntu": {
            "failing_versions": failing,
            "newest_failing": ubuntu_version,
            "archive_version": (ubuntu or {}).get("version"),
            "delta": delta,
            "maintainer": (ubuntu or {}).get("maintainer"),
        },
        "debian": {
            "unstable": (unstable or {}).get("version"),
            "experimental": (experimental or {}).get("version"),
            "newest": deb_newest,
            "testing_repro": {
                "version": repro.get("version") if repro else None,
                "status": repro.get("status") if repro else None,
                "arches": repro_arches,
            },
        },
        "debian_bugs": {
            "open": open_bugs[:10],
            "fixed_newer": fixed[:10],
            "open_count": len(open_bugs),
        },
        "upstream": {
            "homepage": homepage,
            "vcs_git": vcs,
            "forge": kind,
            "repo": repo,
        },
        "signals": signals,
    }

