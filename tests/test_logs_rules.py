"""Excerpt extraction and classification against real Launchpad logs."""

from pathlib import Path

import pytest

from ftbfs.logs import extract, normalize, read_log, sections
from ftbfs.rules import RuleSet

LOGS = Path(__file__).parent / "fixtures" / "logs"
ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def rules():
    return RuleSet.load(ROOT / "rules.toml")


def ex(build_id: int):
    return extract(read_log(LOGS / f"{build_id}.txt.gz"))


# build id -> (Fail-Stage, failed step, text expected in first key line,
#              class, cluster id prefix)
CASES = {
    # compile error in a header, no rule: clustered by signature
    32793398: ("build", "dh_auto_build", "expected identifier",
               "unknown", "sig:"),
    # real error carries [-Werror]: must not be treated as noise
    32891842: ("build", "dh_auto_build", "'_FORTIFY_SOURCE' redefined",
               "fortify-source-redefined", "fortify-source-redefined"),
    # no error text at all: the weak hint nearest the failure wins
    32794135: ("build", "dh_auto_install", "install-sh: Permission denied",
               "permission-denied", "permission-denied:sig:"),
    32846618: ("build", "dpkg-gensymbols", "symbols or patterns disappeared",
               "symbols-file-mismatch", "symbols-file-mismatch:pkg:"),
    # keyed on the uninstallable dependency, not the generic apt message
    33505747: ("install-deps", None, "none of the choices are installable",
               "dependency-unsatisfiable",
               "dependency-unsatisfiable:libamdhip64-7"),
    # ninja "FAILED: x.o" is noise; the compiler error follows it
    33462878: ("build", "dh_auto_build", "discards",
               "c23-const-qualifier", "c23-const-qualifier"),
    # automake summary "# ERROR: 2" is too generic to cluster across pkgs
    33527361: ("build", "dpkg-buildpackage", "# ERROR: 2",
               "test-failure", "test-failure:sig:"),
    33005518: ("build", "dh_auto_test", "error[E0425]",
               "rust-compile-error", "rust-compile-error:sig:"),
    # bare crash message says nothing about the cause: never cross-package
    33435719: ("build", "dh_auto_build", "Aborted (core dumped)",
               "unknown", "sig:"),
    # GCC's other wording of the same -fcf-protection error
    33494108: ("build", "dpkg-buildpackage", "is not compatible with this",
               "cf-protection-unsupported",
               "cf-protection-unsupported:pkg:somepkg"),
    33607715: ("build", "dh_auto_build", "ocamlopt",
               "ocaml-no-native-compiler", "ocaml-no-native-compiler"),
    # udeb-only source built with -Pnoudeb: nothing to package
    32793637: ("build", "dpkg-genbuildinfo", "no binary artifacts",
               "no-binary-artifacts", "no-binary-artifacts:pkg:somepkg"),
}


@pytest.mark.parametrize("build_id", CASES)
def test_excerpt_and_class(build_id, rules):
    stage, step, key, cls, cid = CASES[build_id]
    e = ex(build_id)
    assert e.fail_stage == stage
    assert e.step == step
    assert key in e.key_lines[0]
    assert 5 < e.lines <= 150
    assert len(e.text) < 20_000  # ~5k tokens worst case
    assert e.log_bytes > 10 * len(e.text)
    c = rules.classify(e.to_dict(), "somepkg")
    assert c.cls == cls
    assert c.cluster_id.startswith(cid)


def test_generic_signature_is_per_package(rules):
    e = ex(33527361).to_dict()
    a = rules.classify(e, "pkg-a").cluster_id
    b = rules.classify(e, "pkg-b").cluster_id
    assert a != b and a.endswith(":pkg:pkg-a")


def test_crash_message_is_per_package(rules):
    e = ex(33435719)
    assert e.generic
    assert rules.classify(e.to_dict(), "td").cluster_id.endswith(":pkg:td")


def test_signature_stable_across_machine_names():
    # drawterm on arm64 and armhf: "posix-aarch64" vs "posix-armv7l"
    a, b = ex(32793910), ex(32794110)
    assert "aarch64" in a.key_lines[0] and "armv7l" in b.key_lines[0]
    assert a.signature == b.signature
    assert normalize("posix-ppc64le x86_64 i686") == "posix-ARCH ARCH ARCH"


def test_generic_key_line_never_groups_packages(rules):
    e = {"signature": "abc", "signature_text": "collect2: error: ld "
         "returned N exit status", "generic": True,
         "key_lines": ["collect2: error: ld returned 1 exit status"]}
    assert rules.classify(e, "p1").cluster_id == "sig:abc:pkg:p1"


def test_sections_and_summary():
    secs = sections(read_log(LOGS / "32793398.txt.gz"))
    assert {"Build", "Summary", "Cleanup"} <= set(secs)


def test_repeated_lines_collapsed():
    assert "previous line repeated" in ex(32891842).text


def test_signature_stable_across_paths_versions_arches():
    a = ("/<<PKGBUILDDIR>>/src/x/foo.c:12:3: error: 'bool' cannot be "
         "defined via 'typedef' in x86_64-linux-gnu 1.2.3-1build1")
    b = ("/build/pkg-9/lib/bar.c:99:1: error: ‘bool’ cannot be defined via "
         "‘typedef’ in aarch64-linux-gnu 4.5-2ubuntu1")
    assert normalize(a) == normalize(b)


def test_rules_file_validates():
    with pytest.raises(ValueError, match="bad family"):
        RuleSet.parse('[[rule]]\nid="x"\nclass="c"\nfamily="nope"\n'
                      "match='a'")
    one = '[[rule]]\nid="x"\nclass="c"\nfamily="test"\nmatch=\'a\'\n'
    with pytest.raises(ValueError, match="duplicate rule id x"):
        RuleSet.parse(one * 2)
