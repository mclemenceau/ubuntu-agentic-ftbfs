# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Debian bugs related to build failures, from the public UDD mirror.

One query per batch of source packages. FTBFS bugs are recognized by
title or by the QA `ftbfs` usertag (Debian has no plain `ftbfs` tag).
"""

from __future__ import annotations

import psycopg

DSN = ("host=udd-mirror.debian.net dbname=udd user=udd-mirror"
       " password=udd-mirror")

QUERY = """
SELECT b.id, b.source, b.title, b.severity, b.status, b.done <> '' AS done,
       b.forwarded, b.last_modified,
       EXISTS (SELECT 1 FROM bugs_tags t
               WHERE t.id = b.id AND t.tag = 'patch') AS patch,
       ARRAY(SELECT f.version FROM bugs_fixed_in f WHERE f.id = b.id)
         AS fixed_in
FROM bugs b
WHERE b.source = ANY(%(sources)s)
  AND (b.title ILIKE '%%ftbfs%%'
       OR b.title ILIKE '%%fails to build%%'
       OR b.title ILIKE '%%build failure%%'
       OR b.id IN (SELECT u.id FROM bugs_usertags u WHERE u.tag = 'ftbfs'))
ORDER BY b.source, b.id DESC
"""


def _text(v):
    # The mirror's encoding is SQL_ASCII: text columns come back as bytes.
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, list):
        return [_text(x) for x in v]
    return v


def ftbfs_bugs(sources: list[str], dsn: str = DSN,
               timeout: int = 30) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {s: [] for s in sources}
    with psycopg.connect(dsn, connect_timeout=timeout) as conn:
        cur = conn.execute(QUERY, {"sources": sources})
        cols = [d.name for d in cur.description]
        for row in cur:
            bug = {c: _text(v) for c, v in zip(cols, row, strict=True)}
            bug["last_modified"] = str(bug["last_modified"])
            bug["url"] = f"https://bugs.debian.org/{bug['id']}"
            out.setdefault(bug.pop("source"), []).append(bug)
    return out
