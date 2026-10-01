# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Source package work for the dev agent, done deterministically.

The agent only edits files in an unpacked tree. Everything
Debian-specific is done here, so packaging is always well formed and no
tokens are spent on quilt/dch mechanics:

  - unpack the source (patches applied) and snapshot it in git, with
    the repository next to the tree, not in it: the tree is untrusted
    (package sources, agent edits), and a git config or hook written
    into it would run commands here
  - turn upstream-file edits into debian/patches/<name>.patch with a
    DEP-3 header, registered in the series and applied with quilt
  - add an Ubuntu changelog entry with the next Ubuntu version and set
    the Ubuntu maintainer on first delta
  - build the new source package and a debdiff against the original
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_ENV = {"GIT_AUTHOR_NAME": "ftbfs", "GIT_AUTHOR_EMAIL": "ftbfs@localhost",
           "GIT_COMMITTER_NAME": "ftbfs",
           "GIT_COMMITTER_EMAIL": "ftbfs@localhost"}
# Only the repository's own config counts (not the operator's: a
# global filter driver such as git-lfs would rewrite content), and
# nothing in it may run a command.
GIT_ISOLATION = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


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


def git_dir(tree: Path) -> Path:
    return tree.parent / f"{tree.name}.git"


def _git(tree: Path, *args: str, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", f"--git-dir={git_dir(tree)}",
                 f"--work-tree={tree}", *GIT_SAFE, *args], cwd=tree,
                env={**GIT_ISOLATION, **(env or {})}, check=check)


def git_state(tree: Path) -> str:
    """Digest of everything in the repository that can make git run a
    command: config, hooks, info/. It must not change while the agent
    runs (see check_git_state)."""
    h = hashlib.sha256()
    gd = git_dir(tree)
    for p in sorted([gd / "config", *(gd / "hooks").rglob("*"),
                     *(gd / "info").rglob("*")]):
        h.update(str(p.relative_to(gd)).encode() + b"\0")
        if p.is_file() or p.is_symlink():
            h.update(os.readlink(p).encode() if p.is_symlink()
                     else p.read_bytes())
    return h.hexdigest()


def check_git_state(tree: Path, before: str) -> None:
    if git_state(tree) != before:
        raise SourceError(f"{git_dir(tree)} changed during the agent run;"
                          " refusing to run git on it")


def escaping_links(tree: Path) -> dict[str, str]:
    """Symlinks in the tree that lead outside it, {path: target}."""
    root = tree.resolve()
    out = {}
    for d, dirs, files in os.walk(tree):
        for name in dirs + files:
            p = Path(d) / name
            if not p.is_symlink():
                continue
            try:
                inside = p.resolve().is_relative_to(root)
            except (OSError, RuntimeError):  # a loop
                inside = False
            if not inside:
                out[str(p.relative_to(tree))] = os.readlink(p)
    return out


def hide_links(tree: Path) -> dict[str, str]:
    """Remove the links that lead out of the tree for the agent's run:
    its tools then cannot read or write outside the tree through them,
    whatever the backend's own checks. Refuse a tree whose packaging
    (debian/, .pc/) has one: the steps after the agent write there.
    Returns what restore_links() needs."""
    links = escaping_links(tree)
    bad = sorted(p for p in links
                 if p.split("/")[0] in ("debian", ".pc"))
    if bad:
        raise SourceError(f"{bad[0]} -> {links[bad[0]]} points outside"
                          " the source tree")
    for path in links:
        (tree / path).unlink()
    return links


def restore_links(tree: Path, links: dict[str, str]) -> None:
    for path, target in links.items():
        p = tree / path
        if p.exists() or p.is_symlink():
            raise SourceError(f"{path} was a link out of the tree and the"
                              " agent wrote a file in its place")
        p.symlink_to(target)


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
    for old in (tree, git_dir(tree)):
        if old.exists():
            shutil.rmtree(old)
    _run(["dpkg-source", "--no-check", "-x", str(dsc), str(tree)],
         cwd=tree.parent)
    _git(tree, "init", "-q")
    _git(tree, "add", "-A", "-f")
    _git(tree, "commit", "-q", "-m", "baseline", "--no-gpg-sign",
         env=GIT_ENV)
    return tree


def changed_paths(tree: Path) -> list[str]:
    _git(tree, "add", "-A", "-f")
    out = _git(tree, "diff", "--cached", "--no-ext-diff",
               "--name-only").stdout
    return [p for p in out.splitlines() if p and not p.startswith(".pc/")]


def diff_text(tree: Path, paths: list[str]) -> str:
    _git(tree, "add", "-A", "-f")
    return _git(tree, "diff", "--cached", "--no-ext-diff", "--no-textconv",
                "--src-prefix=a/", "--dst-prefix=b/", "--", *paths).stdout


def edits(tree: Path) -> str:
    """Everything changed since the baseline, as one diff: the agent's
    raw edits, before they are turned into a patch and changelog."""
    paths = changed_paths(tree)
    if not paths:
        return ""
    _git(tree, "add", "-A", "-f")
    return _git(tree, "diff", "--cached", "--no-ext-diff", "--no-textconv",
                "--binary", "--", *paths).stdout


def apply_edits(tree: Path, diff: str) -> None:
    """Replay earlier edits on a fresh tree, uncommitted, so they stay
    part of this attempt's change."""
    patch = tree.parent / "previous-edits.diff"
    patch.write_text(diff)
    _git(tree, "apply", "--whitespace=nowarn", str(patch))


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
    _git(tree, "reset", "-q")
    _git(tree, "checkout", "HEAD", "--",
         *[p for p in upstream if _in_head(tree, p)])
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
    return _git(tree, "cat-file", "-e", f"HEAD:{path}",
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
