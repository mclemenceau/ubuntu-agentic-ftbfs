# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Every source file carries the copyright and license notice."""

from pathlib import Path

from ftbfs.prompts import load

ROOT = Path(__file__).resolve().parent.parent
NOTICE = ("Copyright (C) 2026 Matthieu Clemenceau",
          "SPDX-License-Identifier: GPL-3.0-or-later")
# Run data, build output and test data. The vendored htmx files are .js,
# which SOURCES leaves out: their notices are in THIRD_PARTY.md.
SKIP_DIRS = {"state", "cache", "work", "build", "dist", "fixtures"}
SOURCES = ("*.py", "*.html", "*.j2", "*.css", "prompts/*.md")


def _sources():
    for pattern in SOURCES:
        for p in ROOT.rglob(pattern):
            parts = p.relative_to(ROOT).parts
            if any(d.startswith(".") or d in SKIP_DIRS for d in parts[:-1]):
                continue
            yield p


def test_sources_carry_the_notice():
    found = list(_sources())
    assert any(p.name == "cli.py" for p in found)
    missing = [str(p.relative_to(ROOT)) for p in found
               if not all(n in "".join(p.read_text().splitlines(True)[:3])
                          for n in NOTICE)]
    assert missing == []


def test_prompt_notice_stays_out_of_the_model_text():
    # The notice sits in the front matter, which load() strips: it neither
    # reaches the model nor changes the digest in the stage's cache key.
    for p in (ROOT / "prompts").glob("*.md"):
        prompt = load(ROOT, p.stem)
        assert prompt.version != "0"
        assert "SPDX" not in prompt.text
        assert "Copyright" not in prompt.text
