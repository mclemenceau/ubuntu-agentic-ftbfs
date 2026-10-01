# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

import random
import shutil
import subprocess

import pytest

from ftbfs.facts.version import compare, has_ubuntu_delta, newest

PAIRS = [
    ("1.0-1", "1.0-1", 0),
    ("1.0-1build1", "1.0-1", 1),
    ("1.0-1ubuntu1", "1.0-2", -1),
    ("1.0~rc1-1", "1.0-1", -1),
    ("1:0.1-1", "9.9-1", 1),
    ("2.0.1-2build1", "2.0.1-3", -1),
    ("1.6.15+ds-1", "1.6.15-1", 1),
    ("1.0", "1.0-0", 0),
    ("7.9.3.1+~cs7.9.3.1-2build1", "7.9.3.1+~cs7.9.3.1-2", 1),
    ("26.09.0~git20260819.c60a7af-1", "26.09.0-1", -1),
    ("1.0a", "1.0", 1),
    ("1.0~~", "1.0~", -1),
]


def sign(x):
    return (x > 0) - (x < 0)


@pytest.mark.parametrize("a, b, expected", PAIRS)
def test_compare(a, b, expected):
    assert sign(compare(a, b)) == expected
    assert sign(compare(b, a)) == -expected


@pytest.mark.skipif(not shutil.which("dpkg"), reason="needs dpkg")
def test_matches_dpkg_on_random_versions():
    rnd = random.Random(1)
    atoms = ["0", "1", "2", "10", "~", "+", ".", "a", "b", "~rc", "build",
             "ubuntu", "+ds", "dfsg"]

    def gen():
        up = "1" + "".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 4)))
        rev = str(rnd.randint(0, 3)) + "".join(
            rnd.choice(atoms) for _ in range(rnd.randint(0, 2)))
        ep = f"{rnd.randint(1, 2)}:" if rnd.random() < 0.1 else ""
        return f"{ep}{up}-{rev}"

    for _ in range(300):
        a, b = gen(), gen()
        lt = subprocess.run(["dpkg", "--compare-versions", a, "lt", b])
        eq = subprocess.run(["dpkg", "--compare-versions", a, "eq", b])
        dpkg = -1 if lt.returncode == 0 else (0 if eq.returncode == 0 else 1)
        assert sign(compare(a, b)) == dpkg, (a, b)


def test_newest_and_delta():
    assert newest(["1.0-1", "1.0-1build2", None, "1.0-1build10"]) \
        == "1.0-1build10"
    assert newest([]) is None
    assert has_ubuntu_delta("1.0-1ubuntu2")
    assert has_ubuntu_delta("20.2.1-0ubuntu3")
    assert not has_ubuntu_delta("1.0-1build1")
