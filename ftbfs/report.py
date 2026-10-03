# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""investigation.md: one report per failing source version.

The report is stitched from per-stage Jinja partials in pipeline (DAG)
order, rendered from the DB with no tokens. A stage ships its partial as
`templates/stages/<stage>.md.j2`, either here or in the plugins
directory (which wins); a stage without one gets a generic section, so a
new stage shows up in the report and web UI for free.

The summary and the recommended next action are derived
deterministically from the stage results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    TemplateNotFound,
)

from .core.pipeline import Pipeline
from .core.stage import UnitType
from .db import DB
from .filters import all_items

BUILTIN_TEMPLATES = Path(__file__).parent / "templates"
MAX_EMBED = 200_000  # bytes of an artifact (debdiff, ...) put in a report


@dataclass
class Result:
    unit: str
    status: str
    data: dict
    attempt: int
    ts: str
    cost: float | None
    model: str | None
    artifacts: list[str]

    @property
    def arch(self) -> str | None:
        parts = self.unit.split("/")
        return parts[2] if len(parts) == 3 else None


@dataclass
class Section:
    stage: str
    unit_type: str
    description: str
    results: list[Result]
    body: str = ""


@dataclass
class Investigation:
    source: str
    version: str
    items: list[dict]
    package: dict | None
    sections: list[Section] = field(default_factory=list)
    gates: list[dict] = field(default_factory=list)
    next_actions: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""
    cost: float = 0.0

    @property
    def arches(self) -> list[str]:
        return [i["arch"] for i in self.items]

    def section(self, stage: str) -> Section | None:
        return next((s for s in self.sections if s.stage == stage), None)

    def results(self, stage: str) -> list[Result]:
        s = self.section(stage)
        return s.results if s else []

    def first(self, stage: str, status: str | None = None) -> dict | None:
        for r in self.results(stage):
            if status is None or r.status == status:
                return r.data
        return None


def _fence(text: str | None, lang: str = "") -> str:
    """A code fence that cannot be closed early by the text itself."""
    text = (text or "").rstrip("\n")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _oneline(text) -> str:
    return " ".join(str(text or "").split())


def _cell(text) -> str:
    """Safe inside a Markdown table cell."""
    return _oneline(text).replace("|", "\\|")


def _money(v) -> str:
    return f"${v:.3f}" if v else "-"


def _read_file(path) -> str | None:
    try:
        return Path(path).read_text(errors="replace")[:MAX_EMBED]
    except (OSError, TypeError):
        return None


def environment(plugins_dir: Path | None = None) -> Environment:
    loaders = []
    if plugins_dir and (plugins_dir / "templates").is_dir():
        loaders.append(FileSystemLoader(plugins_dir / "templates"))
    loaders.append(FileSystemLoader(BUILTIN_TEMPLATES))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=False,
                      trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True)
    env.filters.update(fence=_fence, oneline=_oneline, cell=_cell,
                       money=_money, read_file=_read_file,
                       tojson_pretty=lambda v: json.dumps(v, indent=1))
    return env


class Reporter:
    def __init__(self, db: DB, pipeline: Pipeline,
                 plugins_dir: Path | None = None):
        self.db = db
        self.pipeline = pipeline
        self.env = environment(plugins_dir)

    def versions(self, source: str) -> list[str]:
        return [r["version"] for r in self.db.query(
            "SELECT DISTINCT version FROM item WHERE source=?"
            " ORDER BY last_seen DESC, version DESC", (source,))]

    def gather(self, source: str, version: str) -> Investigation:
        items = [dict(r) for r in all_items(
            self.db, "WHERE i.source=? AND i.version=?", (source, version))]
        if not items:
            raise LookupError(f"no items for {source} {version}")
        pkg = self.db.one("SELECT * FROM package WHERE source=?", (source,))
        inv = Investigation(source, version, items,
                            dict(pkg) if pkg else None)
        units = {
            UnitType.ITEM: [i["id"] for i in items],
            UnitType.PACKAGE: [source],
            UnitType.CLUSTER: sorted({i["cluster_id"] for i in items
                                      if i["cluster_id"]}),
        }
        for spec in self.pipeline:
            results = []
            for uid in units[spec.stage.unit]:
                row = self.db.one(
                    "SELECT * FROM stage_result WHERE unit_id=? AND stage=?"
                    " ORDER BY id DESC LIMIT 1", (uid, spec.name))
                if row is not None:
                    results.append(Result(
                        uid, row["status"], json.loads(row["data"]),
                        row["attempt"], row["ts"], row["cost"],
                        row["model"], json.loads(row["artifacts"])))
            if results:
                inv.sections.append(Section(
                    spec.name, spec.stage.unit, spec.stage.description,
                    results))
        ids = units[UnitType.ITEM] + [source] + units[UnitType.CLUSTER]
        marks = ",".join("?" * len(ids))
        inv.gates = [dict(r) for r in self.db.query(
            f"SELECT * FROM gate WHERE unit_id IN ({marks}) ORDER BY stage",
            ids)]
        inv.cost = self.db.one(
            f"SELECT COALESCE(SUM(cost), 0) AS c FROM stage_result WHERE"
            f" unit_id IN ({marks})", ids)["c"]
        inv.summary = summarize(inv)
        inv.next_actions = recommend(inv)
        return inv

    def render(self, inv: Investigation, costs: bool = True) -> str:
        """The report; without `costs`, the LLM cost line is left out
        (the public web UI)."""
        for section in inv.sections:
            try:
                tpl = self.env.get_template(f"stages/{section.stage}.md.j2")
            except TemplateNotFound:
                tpl = self.env.get_template("stages/_default.md.j2")
            section.body = tpl.render(inv=inv, s=section).strip()
        return self.env.get_template("investigation.md.j2").render(
            inv=inv, costs=costs)

    def report(self, source: str, version: str,
               costs: bool = True) -> str:
        return self.render(self.gather(source, version), costs)

    def write(self, work_dir: Path, source: str, version: str) -> Path:
        path = work_dir / source / version / "investigation.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.report(source, version))
        return path


def summarize(inv: Investigation) -> str:
    triage = inv.first("triage") or {}
    diag = inv.first("diagnose", "ok") or {}
    parts = []
    if triage.get("summary"):
        parts.append(_oneline(triage["summary"]).rstrip(".") + ".")
    if diag.get("fix_kind"):
        parts.append(f"Diagnosis: {diag['fix_kind']}, risk"
                     f" {diag.get('risk')}, confidence"
                     f" {diag.get('confidence')}.")
    return " ".join(parts) or "Not triaged yet."


def recommend(inv: Investigation) -> list[tuple[str, str]]:
    """(action, why) pairs, most useful first."""
    out: list[tuple[str, str]] = []
    verified = [r for r in inv.results("verify") if r.status == "ok"]
    if verified:
        dev = next((r.data for r in inv.results("dev")
                    if r.unit == verified[0].unit), {})
        out.append(("Upload the verified fix",
                    f"{dev.get('version', '?')} builds on"
                    f" {', '.join(r.arch or r.unit for r in verified)};"
                    f" debdiff: {dev.get('debdiff', '?')}"))
    facts = inv.first("facts", "ok") or {}
    signals = facts.get("signals", [])
    debian = (facts.get("debian") or {}).get("newest")
    if "sync-candidate" in signals:
        out.append(("Sync from Debian",
                    f"Debian has {debian}, and Ubuntu has no delta"))
    elif "merge-candidate" in signals:
        out.append(("Merge from Debian",
                    f"Debian has {debian}, and Ubuntu carries a delta"))
    failed = [r for r in inv.results("verify") if r.status == "fail"]
    if failed and not verified:
        out.append(("Needs a human: the automated fix does not build",
                    failed[0].data.get("key_lines", ["?"])[0]))
    for r in inv.results("reproduce"):
        if r.data.get("outcome") == "built":
            out.append(("Retry the build on Launchpad",
                        f"{r.arch} builds when rebuilt locally"
                        " (flaky, or fixed by newer dependencies)"))
            break
    pending = [g for g in inv.gates if g["decision"] == "pending"]
    for g in pending:
        out.append((f"Review the {g['stage']} gate",
                    f"{g['unit_id']} waits for approval"))
    triage = inv.first("triage") or {}
    action = triage.get("action")
    hints = {
        "wait-dependency": "Wait for the dependency",
        "retry": "Retry the build on Launchpad",
        "restrict-arch": "Consider restricting the architecture list",
        "report-upstream": "Report upstream",
        "investigate": "Investigate by hand",
    }
    if action in hints and not verified:
        out.append((hints[action],
                    f"triage: {_oneline(triage.get('summary'))}"))
    if not out:
        out.append(("Keep running the pipeline",
                    "no stage has reached a verdict yet"))
    return out
