# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

from collections import Counter

import pytest

from ftbfs.ingest import ParseError, Snapshot, parse


def builds(snap):
    return [b for p in snap.packages for v in p.versions for b in v.builds]


def pkg(snap, name):
    return next(p for p in snap.packages if p.source == name)


def test_series_and_arches(snapshot):
    assert snapshot.series == "stonking"
    assert snapshot.arches == ["amd64", "arm64", "armhf", "ppc64el",
                               "s390x", "riscv64", "i386", "amd64v3"]


def test_component_counts_match_page_header(snapshot):
    # "Jump to: main (12) restricted (0) universe (1082) multiverse (34)"
    assert Counter(p.component for p in snapshot.packages) == {
        "main": 12, "universe": 1082, "multiverse": 34,
    }


def test_state_totals_match_legend(snapshot):
    # parse() already cross-checks per state/arch; spot check totals
    assert Counter(b.state for b in builds(snapshot)) == {
        "FAILEDTOBUILD": 1293, "MANUALDEPWAIT": 1390, "CANCELLED": 74,
        "UPLOADFAIL": 13, "CHROOTWAIT": 1,
    }


def test_failed_build_fields(snapshot):
    p = pkg(snapshot, "4ti2")
    assert p.component == "universe"
    assert p.pts == "https://tracker.debian.org/pkg/4ti2"
    assert p.bts == "http://bugs.debian.org/src:4ti2"
    (v,) = p.versions
    assert v.version == "1.6.15+ds-1"
    assert v.pocket == "proposed"
    assert v.changed_by == "Debian Math Team (team+math)"
    (b,) = v.builds
    assert b.arch == "ppc64el"
    assert b.state == "FAILEDTOBUILD"
    assert b.build_id == 32844029
    assert b.finished_at == "2026-09-24 09:56:50"
    assert b.log_url.endswith(
        "buildlog_ubuntu-stonking-ppc64el.4ti2_1.6.15+ds-1_BUILDING.txt.gz")
    assert b.note is None


def test_multi_version_package_with_bug_and_sets(snapshot):
    p = pkg(snapshot, "ceph")
    assert [v.version for v in p.versions] == [
        "20.2.1-0ubuntu1", "20.2.1-0ubuntu3"]
    assert [v.pocket for v in p.versions] == ["release", "proposed"]
    assert p.lp_bugs == [{"id": 2099865,
                          "title": "FTBFS on armhf in the release pocket"}]
    assert "ubuntu-server" in p.packagesets
    assert "ubuntu-openstack" in p.teams
    assert p.versions[0].builds[0].note == "waits on architecture-is-64-bit"


def test_roundtrip(snapshot):
    again = Snapshot.from_dict(snapshot.to_dict())
    assert again == snapshot


def test_miscount_fails_loudly(page):
    # Drop one failed build cell in a package table: the legend no longer
    # matches.
    cut = page.index(b'<h2 id="universe"')
    broken = page[:cut] + page[cut:].replace(
        b'<td class="FAILEDTOBUILD"', b"<td", 1)
    with pytest.raises(ParseError, match="FAILEDTOBUILD"):
        parse(broken)


def test_missing_legend_row_fails_loudly(page):
    broken = page.replace(b'<td class="FAILEDTOBUILD"', b"<td", 1)
    with pytest.raises(ParseError, match="missing from legend"):
        parse(broken)
