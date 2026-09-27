"""Build log handling: fetch/cache, sbuild structure, failure excerpt,
normalized signature.

This is the main token saver. A build log is ~140 KB (median); what an
LLM needs is the 50-150 lines around the first real error. Extraction is
purely deterministic:

  1. split the sbuild log into its boxed sections and read the Summary
     (Fail-Stage, Status, ...)
  2. find the terminal line: the first make "***" / dh_*/dpkg-* error /
     ninja stop in the Build section
  3. look back from it for the first error-ish line (compiler error,
     traceback, test failure summary, ...)
  4. excerpt = context around that first error + the lines just before
     the terminal line, merged, capped
  5. key lines = the error-ish lines, normalized into a signature that
     is stable across packages, paths, versions and arches
"""

from __future__ import annotations

import gzip
import hashlib
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_EXCERPT_LINES = 150
MAX_LINE = 300
LOOKBACK = 400
CONTEXT_BEFORE = 12
CONTEXT_AFTER = 25
TAIL = 30

_SECTION_RE = re.compile(r"^\| (\S.*?)\s{2,}\S.*\|$")
_SUMMARY_FIELD_RE = re.compile(r"^([A-Z][\w-]+): (.*)$")

TERMINAL = re.compile(
    r"^(make(\[\d+\])?: \*\*\* "
    r"|dh_\w+: error"
    r"|dpkg-\w+: error"
    r"|ninja: build stopped"
    r"|E: Build killed"
    r"|Build killed with signal)"
)
# The failure chain unwinding: useful in the excerpt, useless as a key.
CHAIN = re.compile(
    r"^(make(\[\d+\])?: (\*\*\* \[|Leaving|Entering)"
    r"|dh_\w+: error: .*returned exit code"
    r"|dpkg-buildpackage: error: debian/rules"
    r"|E: Build failure|ninja: build stopped)"
)
ERRORISH = re.compile(
    r"(\berror\b(\[\w+\])?:"
    r"|\bError:"
    r"|^E\s{3}"
    r"|^(FAILED|FAIL|ERROR)\b"
    r"|\bFAILED\b"
    r"|Traceback \(most recent call last\)"
    r"|^\s*\w+(Error|Exception): "
    r"|undefined reference to"
    r"|CMake Error"
    r"|fatal error"
    r"|Segmentation fault"
    r"|^# (FAIL|ERROR):\s+[1-9]"
    r"|\bfailed:\s*$|test\(s\) failed"
    r"|The following tests FAILED"
    r"|tests? failed|failures:$"
    r"|test result: FAILED"
    r"|\btimed out\b|\bTimeout\b"
    r"|dpkg-gensymbols: error"
    r"|unsat-dependency|Unable to satisfy|not installable"
    r"|No rule to make target"
    r"|unrecognized (command[- ]line )?option|multiple definition of"
    r"|cannot find -l\S+"
    r"|none of the choices are installable|is not selected for install"
    r"|^\s+\d+ - \S+ \((Failed|SEGFAULT|Timeout|Subprocess aborted"
    r"|Exception|Child aborted|ILLEGAL|BUS error)\))",
)
# Weaker hints: only used when no strong error exists, and then the one
# nearest to the failure wins (early ones are usually configure noise).
WEAK = re.compile(
    r"(Permission denied|No such file or directory|command not found"
    r"|unknown option|not recognized|\bAborted\b|core dumped)"
)
# Lines that look error-ish but are not failures.
NOT_ERROR = re.compile(
    r"(^checking |error\.[ch]\b|_error\b|\berror\.o\b"
    r"|^\s*(gcc|g\+\+|cc|c\+\+|clang|libtool|/usr/bin/\S+-gcc)\s"
    r"|^# (ERROR|FAIL):\s+0\b|^# XFAIL|expected error"
    r"|^FAILED: (\[code=\d+\] )?\S+\.(o|obj|so|a)\b"
    r"|0 failed|failed: 0\b)"
)


# Error-ish but uninformative on their own: kept as key lines only after
# the specific ones, so they never decide the signature when a better line
# exists.
GENERIC_KEY = re.compile(
    r"(^Traceback \(most recent call last\)"
    r"|collect2: error: ld returned \d+ exit status"
    r"|\d+% tests passed, \d+ tests failed out of"
    r"|The following tests FAILED:"
    r"|dh_missing: error: missing files"
    r"|^E: (Package installation failed|Unable to satisfy dependencies)"
    r"|apt-get failed|Some test\(s\) failed|^# (FAIL|ERROR):)"
)
# CMakeCache.txt dumped by dh_auto_configure after a failed configure.
_CMAKE_CACHE = re.compile(
    r"^(//.*|#.*|[\w.+-]+(-ADVANCED)?:(INTERNAL|STRING|BOOL|PATH|FILEPATH"
    r"|STATIC|UNINITIALIZED)=.*|)$"
)
DUMP_MIN = 20

# Compiler/libtool invocations: long and rarely informative; truncated.
COMMAND = re.compile(
    r"^\s*(/usr/bin/\S*(gcc|g\+\+|cc|c\+\+|clang\S*)"
    r"|(\S+-)?(gcc|g\+\+|cc|c\+\+)(-\d+)? "
    r"|libtool: (compile|link): |/bin/bash \S*libtool )"
)
COMMAND_MAX = 100


# -- fetching ---------------------------------------------------------------


def fetch_log(url: str, cache_dir: Path, build_id: int,
              timeout: int = 120) -> Path:
    """Download (once) and return the cached gzipped log."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{build_id}.txt.gz"
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "ftbfs-review"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data.startswith(b"\x1f\x8b"):
        data = gzip.compress(data)
    tmp.write_bytes(data)
    tmp.rename(path)
    return path


def read_log(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        return f.read()


# -- structure --------------------------------------------------------------


def sections(text: str) -> dict[str, list[str]]:
    """Split an sbuild log into its boxed sections.

    +-----------------------------------+
    | Build          Thu, 24 Sep 2026 ...|
    +-----------------------------------+
    """
    lines = text.splitlines()
    out: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    i = 0
    while i < len(lines):
        line = lines[i]
        if (line.startswith("+---") and i + 2 < len(lines)
                and lines[i + 2].startswith("+---")):
            m = _SECTION_RE.match(lines[i + 1])
            if m:
                current = m.group(1).strip()
                # Repeated names (rare) keep the last one.
                out[current] = []
                i += 3
                continue
        out[current].append(line)
        i += 1
    return out


def summary(secs: dict[str, list[str]]) -> dict[str, str]:
    fields = {}
    for line in secs.get("Summary", []):
        m = _SUMMARY_FIELD_RE.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


# -- excerpt ----------------------------------------------------------------


@dataclass
class Excerpt:
    fail_stage: str | None
    sbuild_status: str | None
    step: str | None  # e.g. dh_auto_test
    step_line: str | None
    key_lines: list[str] = field(default_factory=list)
    signature: str = ""
    signature_text: str = ""
    # True when even the best key line is a generic summary; such
    # signatures must not group different packages.
    generic: bool = False
    text: str = ""
    lines: int = 0
    log_bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def is_errorish(line: str) -> bool:
    return bool(ERRORISH.search(line)) and not NOT_ERROR.search(line)


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _shorten(line: str) -> str:
    limit = COMMAND_MAX if COMMAND.match(line) else MAX_LINE
    return line if len(line) <= limit else line[:limit] + " [...]"


def _render(lines: list[str], ranges: list[tuple[int, int]]) -> list[str]:
    """Selected ranges with omission markers; runs of lines that only
    differ by numbers/paths are collapsed."""
    out: list[str] = []
    prev = None
    for a, b in ranges:
        if prev is not None and a > prev + 1:
            out.append(f"[... {a - prev - 1} lines omitted ...]")
        last_norm, repeats = None, 0
        for line in lines[a:b + 1]:
            norm = normalize(line)
            if norm and norm == last_norm:
                repeats += 1
                continue
            if repeats:
                out.append(f"[... previous line repeated {repeats}x ...]")
                repeats = 0
            out.append(_shorten(line))
            last_norm = norm
        if repeats:
            out.append(f"[... previous line repeated {repeats}x ...]")
        prev = b
    return out


def _failure_lines(secs: dict[str, list[str]],
                   fail_stage: str | None) -> list[str]:
    if fail_stage and fail_stage != "build":
        # install-deps, fetch-src, unpack...: the resolver output lives in
        # the dependency installation section.
        for name, body in secs.items():
            if "Install" in name and "dependencies" in name.lower():
                return body
    return secs.get("Build", []) or [
        line for body in secs.values() for line in body
    ]


def _collapse_dumps(lines: list[str]) -> list[str]:
    """Replace long CMakeCache.txt dumps with a one-line marker."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _CMAKE_CACHE.match(lines[j]):
            j += 1
        dump = lines[i:j]
        if len(dump) >= DUMP_MIN and any(":INTERNAL=" in x for x in dump):
            out.append(f"[... CMakeCache.txt dump, {len(dump)} lines"
                       " omitted ...]")
            i = j
        elif j > i:
            out.extend(dump)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def extract(text: str) -> Excerpt:
    secs = sections(text)
    summ = summary(secs)
    fail_stage = summ.get("Fail-Stage")
    lines = _collapse_dumps(_failure_lines(secs, fail_stage))

    terminal = next(
        (i for i, line in enumerate(lines) if TERMINAL.match(line)), None
    )
    step = step_line = None
    for line in lines:
        m = re.match(r"^(dh_\w+|dpkg-\w+): error", line)
        if m:
            step, step_line = m.group(1), line[:MAX_LINE]
            break
    if terminal is None:
        terminal = len(lines) - 1

    start = max(0, terminal - LOOKBACK)
    window = range(start, min(len(lines), terminal + 1))
    errs = [i for i in window
            if is_errorish(lines[i]) and not CHAIN.match(lines[i])]
    if not errs:
        weak = [i for i in window if WEAK.search(lines[i])
                and not NOT_ERROR.search(lines[i])]
        errs = weak[-1:]
    ranges = [(max(0, terminal - TAIL), min(len(lines) - 1, terminal + 3))]
    if errs:
        first = errs[0]
        ranges.append((max(0, first - CONTEXT_BEFORE),
                       min(len(lines) - 1, first + CONTEXT_AFTER)))
    if step and step.startswith("dpkg-gensymbols"):
        # the symbols diff comes after the error line
        ranges.append((terminal, min(len(lines) - 1, terminal + 60)))
    rendered = _render(lines, _merge(ranges))
    if len(rendered) > MAX_EXCERPT_LINES:
        half = MAX_EXCERPT_LINES // 2
        cut = len(rendered) - MAX_EXCERPT_LINES
        rendered = (rendered[:half]
                    + [f"[... {cut} lines omitted ...]"]
                    + rendered[-half:])

    key_lines: list[str] = []
    seen: set[str] = set()
    ordered = ([i for i in errs if not GENERIC_KEY.search(lines[i])]
               + [i for i in errs if GENERIC_KEY.search(lines[i])])
    for i in ordered:
        norm = normalize(lines[i])
        if norm and norm not in seen:
            seen.add(norm)
            key_lines.append(lines[i].strip()[:MAX_LINE])
        if len(key_lines) == 5:
            break
    if not key_lines:
        # No recognizable error text: the last output line before the
        # failure chain is usually the failing command's message.
        before = next(
            (lines[i].strip() for i in range(terminal - 1, start - 1, -1)
             if lines[i].strip() and not CHAIN.match(lines[i])
             and not COMMAND.match(lines[i])),
            None,
        )
        key_lines = [(before or step_line or "")[:MAX_LINE]]
    sig_text = "\n".join(normalize(k) for k in key_lines[:3])
    header = [
        f"Fail-Stage: {fail_stage or '?'}   Status: "
        f"{summ.get('Status', '?')}   Failed step: {step or '?'}",
        "-" * 60,
    ]
    return Excerpt(
        fail_stage=fail_stage,
        sbuild_status=summ.get("Status"),
        step=step,
        step_line=step_line,
        key_lines=key_lines,
        signature=hashlib.sha256(sig_text.encode()).hexdigest()[:12],
        signature_text=sig_text,
        generic=bool(key_lines and GENERIC_KEY.search(key_lines[0])),
        text="\n".join(header + rendered) + "\n",
        lines=len(rendered),
        log_bytes=len(text.encode()),
    )


# -- normalization ----------------------------------------------------------

_NORMALIZERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),  # ANSI colours
    (re.compile(r"[‘’`´]"), "'"),
    # compiler diagnostics: drop the file:line:col prefix
    (re.compile(r"^\S+?:\d+(:\d+)?:\s*"), ""),
    (re.compile(r"/<<(PKG)?BUILDDIR>>\S*"), "PATH"),
    (re.compile(r"(?<![\w.])/(?:[\w.+@-]+/)+[\w.+@-]*"), "PATH"),
    (re.compile(r"\b(x86_64|aarch64|arm|powerpc64le|s390x|riscv64|i686|"
                r"i386)-linux-gnu\w*\b"), "TRIPLET"),
    (re.compile(r"\b(amd64v3|amd64|arm64|armhf|ppc64el|s390x|riscv64|"
                r"i386)\b"), "ARCH"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "HEX"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "HEX"),
    (re.compile(r"\b\d+(\.\d+)+[\w.~+-]*"), "VER"),
    (re.compile(r"(?<![A-Za-z])\d+"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalize(line: str) -> str:
    out = line.strip()
    for pattern, repl in _NORMALIZERS:
        out = pattern.sub(repl, out)
    return out.strip()
