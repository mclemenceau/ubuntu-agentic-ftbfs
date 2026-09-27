"""Regex failure classifier driven by rules.toml (zero tokens)."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

FAMILIES = {"compile", "link", "test", "packaging", "deps", "toolchain",
            "arch", "infra"}


@dataclass
class Rule:
    id: str
    cls: str
    family: str
    pattern: re.Pattern
    key: int | None = None
    cluster_by: str = "signature"
    scope: str = "key_lines"
    step: str | None = None
    fail_stage: str | None = None
    hint: str = ""


@dataclass
class Classification:
    cls: str
    family: str | None
    rule: str | None
    key: str | None
    cluster_id: str
    matched: str | None
    hint: str = ""

    def to_dict(self) -> dict:
        return {"class": self.cls, "family": self.family, "rule": self.rule,
                "key": self.key, "cluster_id": self.cluster_id,
                "matched": self.matched, "hint": self.hint}


class RuleSet:
    def __init__(self, rules: list[Rule], digest: str):
        self.rules = rules
        self.digest = digest

    @classmethod
    def load(cls, path: Path) -> RuleSet:
        raw = path.read_bytes()
        return cls.parse(raw.decode(), hashlib.sha256(raw).hexdigest()[:12])

    @classmethod
    def parse(cls, text: str, digest: str = "") -> RuleSet:
        rules = []
        seen = set()
        for r in tomllib.loads(text).get("rule", []):
            if r["id"] in seen:
                raise ValueError(f"duplicate rule id {r['id']}")
            seen.add(r["id"])
            if r["family"] not in FAMILIES:
                raise ValueError(f"{r['id']}: bad family {r['family']}")
            key = r.get("key")
            cluster_by = r.get("cluster_by",
                               "key" if key is not None else "signature")
            if cluster_by not in ("class", "key", "signature", "package"):
                raise ValueError(f"{r['id']}: bad cluster_by {cluster_by}")
            if cluster_by == "key" and key is None:
                raise ValueError(f"{r['id']}: cluster_by=key needs key")
            try:
                pattern = re.compile(r["match"], re.M)
            except re.error as e:
                raise ValueError(f"{r['id']}: bad regex: {e}") from e
            rules.append(Rule(
                id=r["id"], cls=r["class"], family=r["family"],
                pattern=pattern, key=key, cluster_by=cluster_by,
                scope=r.get("scope", "key_lines"), step=r.get("step"),
                fail_stage=r.get("fail_stage"), hint=r.get("hint", ""),
            ))
        return cls(rules, digest)

    def _applies(self, rule: Rule, ex: dict) -> bool:
        if rule.step and rule.step != ex.get("step"):
            return False
        return not (rule.fail_stage
                    and rule.fail_stage != ex.get("fail_stage"))

    def classify(self, ex: dict, source: str) -> Classification:
        """ex is an excerpt dict (see logs.Excerpt)."""
        sig = ex["signature"]
        # A signature with little text ("# ERROR: N") says nothing about
        # the cause: never group different packages on it.
        first = (ex.get("signature_text") or "").split("\n")[0]
        if ex.get("generic") or len(re.findall(r"[A-Za-z]{3,}", first)) < 3:
            sig = f"{sig}:pkg:{source}"
        for line in ex.get("key_lines", [])[:3]:
            for rule in self.rules:
                if rule.scope != "key_lines" or not self._applies(rule, ex):
                    continue
                m = rule.pattern.search(line)
                if m:
                    return self._result(rule, m, sig, source)
        for rule in self.rules:
            if rule.scope == "excerpt" and self._applies(rule, ex):
                m = rule.pattern.search(ex.get("text", ""))
                if m:
                    return self._result(rule, m, sig, source)
        return Classification("unknown", None, None, None, f"sig:{sig}",
                               None)

    @staticmethod
    def _result(rule: Rule, m: re.Match, sig: str,
                source: str) -> Classification:
        key = None
        if rule.key is not None:
            key = m.group(rule.key) or next(
                (g for g in m.groups() if g), None)
        if rule.cluster_by == "class":
            cid = rule.cls
        elif rule.cluster_by == "key" and key:
            cid = f"{rule.cls}:{key}"
        elif rule.cluster_by == "package":
            cid = f"{rule.cls}:pkg:{source}"
        else:
            cid = f"{rule.cls}:sig:{sig}"
        return Classification(rule.cls, rule.family, rule.id, key, cid,
                              m.group(0)[:200], rule.hint)
