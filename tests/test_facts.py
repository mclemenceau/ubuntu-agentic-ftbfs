from ftbfs.facts.derive import derive, forge
from ftbfs.facts.sources import iter_deb822, parse_sources, reduce_repro


def bug(id_, done=False, patch=False, fixed_in=()):
    return {"id": id_, "title": "pkg: FTBFS", "severity": "serious",
            "status": "done" if done else "pending", "done": done,
            "forwarded": "", "patch": patch, "fixed_in": list(fixed_in),
            "url": f"https://bugs.debian.org/{id_}"}


def repro(status, arch="amd64", version="1.0-2"):
    return {"suite": "forky", "version": version, "status": status,
            "arches": {arch: {"status": status, "version": version,
                              "date": "2026-09-01"}}}


def run(failing=None, ubuntu=None, unstable=None, experimental=None,
        repro_=None, bugs=()):
    return derive("pkg", failing or {"1.0-1build1": ["amd64"]},
                  ubuntu, unstable, experimental, repro_, list(bugs))


def test_sync_vs_merge_candidate():
    assert "sync-candidate" in run(unstable={"version": "1.0-2"})["signals"]
    merge = run(failing={"1.0-1ubuntu1": ["amd64"]},
                unstable={"version": "1.0-2"})
    assert "merge-candidate" in merge["signals"]
    same = run(unstable={"version": "1.0-1"})  # 1.0-1build1 is newer
    assert not {"sync-candidate", "merge-candidate"} & set(same["signals"])


def test_experimental_only_is_not_a_sync_candidate():
    f = run(unstable={"version": "1.0-1"}, experimental={"version": "2.0-1"})
    assert f["debian"]["newest"] == "2.0-1"
    assert "newer-in-experimental" in f["signals"]
    assert "sync-candidate" not in f["signals"]


def test_not_in_debian():
    assert run()["signals"] == ["not-in-debian"]


def test_debian_bugs_signals():
    f = run(unstable={"version": "1.0-1"}, bugs=[
        bug(1, patch=True), bug(2, done=True, fixed_in=["pkg/1.0-3"]),
        bug(3, done=True, fixed_in=["0.9-1"])])
    assert {"debian-ftbfs-open", "debian-patch", "fixed-in-debian"} \
        <= set(f["signals"])
    assert [b["id"] for b in f["debian_bugs"]["fixed_newer"]] == [2]
    assert f["debian_bugs"]["open_count"] == 1


def test_repro_status_maps_arches():
    f = run(failing={"1.0-1": ["amd64v3"]}, unstable={"version": "1.0-1"},
            repro_=repro("FTBFS"))
    assert "ftbfs-in-debian-testing" in f["signals"]
    assert "amd64v3" in f["debian"]["testing_repro"]["arches"]
    ok = run(unstable={"version": "1.0-1"}, repro_=repro("reproducible"))
    assert "builds-in-debian-testing" in ok["signals"]
    other = run(failing={"1.0-1": ["s390x"]}, unstable={"version": "1.0-1"},
                repro_=repro("FTBFS"))
    assert other["debian"]["testing_repro"]["arches"] == {}


def test_forge_detection():
    assert forge("https://github.com/rbsec/sslscan/releases") == (
        "github", "https://github.com/rbsec/sslscan")
    assert forge("https://salsa.debian.org/debian/x.git") == (None, None)
    assert forge("https://gitlab.gnome.org/GNOME/gtk.git")[1] == \
        "https://gitlab.gnome.org/GNOME/gtk"
    f = run(ubuntu={"version": "1.0-1build1",
                    "homepage": "https://example.org",
                    "vcs_git": "https://github.com/up/proj.git"})
    assert f["upstream"]["repo"] == "https://github.com/up/proj"


def test_parse_sources_keeps_newest():
    text = ("Package: a\nVersion: 1.0-1\nHomepage: https://h\n"
            "Binary: a,\n b\n\n"
            "Package: a\nVersion: 1.0-2\n\n"
            "Package: b\nVersion: 2:0.1-1\n")
    idx: dict = {}
    parse_sources(text, idx)
    assert idx["a"] == {"version": "1.0-2"}
    assert idx["b"]["version"] == "2:0.1-1"
    paras = list(iter_deb822(text))
    assert paras[0]["Binary"] == "a,\nb"


def test_reduce_repro():
    r = reduce_repro([{"package": "p", "suite": "forky", "version": "1",
                       "status": "FTBFS", "architecture_details": [
                           {"architecture": "arm64", "status": "FTBFS",
                            "version": "1", "build_date": "d"}]}])
    assert r["p"]["arches"]["arm64"]["status"] == "FTBFS"
