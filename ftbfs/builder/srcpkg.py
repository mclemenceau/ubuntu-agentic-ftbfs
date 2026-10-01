# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Source package work for the dev agent, done deterministically.

The agent only edits files in an unpacked tree. Everything
Debian-specific is done here, so packaging is always well formed and no
tokens are spent on quilt/dch mechanics:

  - unpack the source (patches applied) and snapshot it in git
  - turn upstream-file edits into debian/patches/<name>.patch with a
    DEP-3 header, registered in the series and applied with quilt
  - add an Ubuntu changelog entry with the next Ubuntu version and set
    the Ubuntu maintainer on first delta
  - build the new source package and a debdiff against the original
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_ENV = {"GIT_AUTHOR_NAME": "ftbfs", "GIT_AUTHOR_EMAIL": "ftbfs@localhost",
           "GIT_COMMITTER_NAME": "ftbfs",
           "GIT_COMMITTER_EMAIL": "ftbfs@localhost"}


class SourceError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=900)
    if check and proc.returncode != 0:
        raise SourceError(f"{' '.join(cmd)} failed ({proc.returncode}): "
                          f"{(proc.stderr or proc.stdout).strip()[-800:]}")
    return proc


def next_ubuntu_version(version: str) -> str:
    """1.0-2 -> 1.0-2ubuntu1; 1.0-2build3 -> 1.0-2ubuntu1;
    1.0-2ubuntu4 -> 1.0-2ubuntu5."""
    m = re.match(r"^(.*ubuntu)(\d+)$", version)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}"
    m = re.match(r"^(.*)build\d+$", version)
    if m:
        return f"{m.group(1)}ubuntu1"
    return f"{version}ubuntu1"


def source_format(tree: Path) -> str:
    fmt = tree / "debian" / "source" / "format"
    return fmt.read_text().strip() if fmt.exists() else "1.0"


def unpack(dsc: Path, tree: Path) -> Path:
    """Unpack with patches applied and commit a git baseline."""
    if tree.exists():
        shutil.rmtree(tree)
    _run(["dpkg-source", "--no-check", "-x", str(dsc), str(tree)],
         cwd=tree.parent)
    _run(["git", "init", "-q"], cwd=tree)
    _run(["git", "add", "-A", "-f"], cwd=tree)
    _run(["git", "commit", "-q", "-m", "baseline", "--no-gpg-sign"],
         cwd=tree, env=GIT_ENV)
    return tree


def changed_paths(tree: Path) -> list[str]:
    _run(["git", "add", "-A", "-f"], cwd=tree)
    out = _run(["git", "diff", "--cached", "--name-only"], cwd=tree).stdout
    return [p for p in out.splitlines() if p and not p.startswith(".pc/")]


def diff_text(tree: Path, paths: list[str]) -> str:
    _run(["git", "add", "-A", "-f"], cwd=tree)
    return _run(["git", "diff", "--cached", "--src-prefix=a/",
                 "--dst-prefix=b/", "--", *paths], cwd=tree).stdout


def edits(tree: Path) -> str:
    """Everything changed since the baseline, as one diff: the agent's
    raw edits, before they are turned into a patch and changelog."""
    paths = changed_paths(tree)
    if not paths:
        return ""
    _run(["git", "add", "-A", "-f"], cwd=tree)
    return _run(["git", "diff", "--cached", "--binary", "--", *paths],
                cwd=tree).stdout


def apply_edits(tree: Path, diff: str) -> None:
    """Replay earlier edits on a fresh tree, uncommitted, so they stay
    part of this attempt's change."""
    patch = tree.parent / "previous-edits.diff"
    patch.write_text(diff)
    _run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=tree)


@dataclass
class PatchMeta:
    name: str  # slug, without .patch
    description: str
    forwarded: str = "no"
    bug_debian: str | None = None
    origin: str | None = None
    author: str | None = None


def dep3_header(meta: PatchMeta) -> str:
    lines = [f"Description: {_one_line(meta.description)}"]
    if meta.author:
        lines.append(f"Author: {meta.author}")
    if meta.origin:
        lines.append(f"Origin: {meta.origin}")
    if meta.bug_debian:
        lines.append(f"Bug-Debian: {meta.bug_debian}")
    lines.append(f"Forwarded: {meta.forwarded or 'no'}")
    lines.append("Last-Update: " + _today())
    return "\n".join(lines) + "\n---\n"


def _one_line(text: str) -> str:
    # DEP-3 continuation lines must start with a space.
    first, *rest = text.strip().splitlines() or [""]
    return first + "".join(f"\n {r.strip() or '.'}" for r in rest)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "fix-ftbfs")[:60].rstrip("-")


def record_upstream_changes(tree: Path, meta: PatchMeta) -> str | None:
    """Move edits outside debian/ into a new quilt patch. Returns the
    patch file name, or None when only debian/ changed."""
    upstream = [p for p in changed_paths(tree)
                if not p.startswith("debian/")]
    if not upstream:
        return None
    if not source_format(tree).startswith("3.0 (quilt)"):
        return None  # 1.0 / native: edits stay in the diff directly
    body = diff_text(tree, upstream)
    patches = tree / "debian" / "patches"
    patches.mkdir(exist_ok=True)
    name = slug(meta.name)
    patch_file = patches / f"{name}.patch"
    n = 2
    while patch_file.exists():
        patch_file = patches / f"{name}-{n}.patch"
        n += 1
    patch_file.write_text(dep3_header(meta) + body)
    # Revert the upstream edits, then let quilt apply and record them.
    _run(["git", "reset", "-q"], cwd=tree)
    _run(["git", "checkout", "HEAD", "--", *[
        p for p in upstream if _in_head(tree, p)]], cwd=tree)
    for p in upstream:
        if not _in_head(tree, p):
            (tree / p).unlink(missing_ok=True)
    series = patches / "series"
    existing = series.read_text() if series.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    series.write_text(existing + patch_file.name + "\n")
    env = {"QUILT_PATCHES": "debian/patches", "QUILT_PC": ".pc"}
    _run(["quilt", "push", "-q", patch_file.name], cwd=tree, env=env)
    return patch_file.name


def _in_head(tree: Path, path: str) -> bool:
    return _run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=tree,
                check=False).returncode == 0


def add_changelog(tree: Path, version: str, series: str,
                  lines: list[str], name: str, email: str,
                  first_delta: bool) -> None:
    env = {"DEBFULLNAME": name, "DEBEMAIL": email}
    first, *rest = lines
    _run(["dch", "-v", version, "-D", series, "--force-distribution",
          "--force-bad-version", first], cwd=tree, env=env)
    for line in rest:
        _run(["dch", "-a", line], cwd=tree, env=env)
    if first_delta:
        _run(["update-maintainer"], cwd=tree)


def build_source(tree: Path, orig_dir: Path) -> Path:
    """dpkg-source -b next to the tree; returns the new .dsc."""
    out_dir = tree.parent
    for orig in orig_dir.glob("*.orig*"):
        link = out_dir / orig.name
        if not link.exists():
            link.symlink_to(orig.resolve())
    before = set(out_dir.glob("*.dsc"))
    _run(["dpkg-source", "--no-check", "-b", tree.name], cwd=out_dir)
    new = sorted(set(out_dir.glob("*.dsc")) - before)
    if not new:
        raise SourceError("dpkg-source -b produced no .dsc")
    return new[-1]


def debdiff(old_dsc: Path, new_dsc: Path, out: Path) -> str:
    proc = _run(["debdiff", str(old_dsc), str(new_dsc)], cwd=out.parent,
                check=False)
    if proc.returncode not in (0, 1):  # 1 = differences found
        raise SourceError(f"debdiff failed: {proc.stderr.strip()[-500:]}")
    out.write_text(proc.stdout)
    return proc.stdout
