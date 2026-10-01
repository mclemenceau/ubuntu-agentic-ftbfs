# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic backend for tests and dry runs: zero tokens.

Responses come from a responder callable (prompt -> data), or default to
an empty object. Writes the same prompt/transcript files as real backends
so the observability path is exercised too.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from .base import AgentBackend, AgentRequest, AgentResult, Usage


class FakeBackend(AgentBackend):
    name = "fake"

    def __init__(self, tiers=None,
                 responder: Callable[[AgentRequest], Any] | None = None,
                 **options):
        super().__init__(
            tiers or {"small": "fake-s", "medium": "fake-m",
                      "large": "fake-l"},
            **options,
        )
        self.responder = responder or (lambda req: {})
        self.calls: list[AgentRequest] = []

    def run(self, req: AgentRequest) -> AgentResult:
        start = time.monotonic()
        self.calls.append(req)
        req.attempt_dir.mkdir(parents=True, exist_ok=True)
        (req.attempt_dir / "prompt.md").write_text(req.prompt)
        data = self.responder(req)
        text = json.dumps(data)
        transcript = req.attempt_dir / "transcript.jsonl"
        with transcript.open("w") as f:
            f.write(json.dumps({"type": "assistant", "text": text}) + "\n")
        return AgentResult(
            ok=True,
            text=text,
            data=data,
            usage=Usage(input_tokens=len(req.prompt) // 4,
                        output_tokens=len(text) // 4),
            cost=0.0,
            model=self.model_for(req.tier),
            backend=self.name,
            duration_s=time.monotonic() - start,
            transcript_path=transcript,
        )
