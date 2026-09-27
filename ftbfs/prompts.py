"""Versioned, backend-neutral prompt files in prompts/*.md.

Front matter carries a version; the prompt digest joins the stage's cache
key, so editing a prompt re-runs exactly that stage (and downstream).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:12]


def load(root: Path, name: str) -> Prompt:
    raw = (root / "prompts" / f"{name}.md").read_text()
    version = "0"
    if raw.startswith("---\n"):
        head, _, raw = raw[4:].partition("\n---\n")
        for line in head.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = value.strip()
    return Prompt(name, version, raw.strip() + "\n")
