# ftbfs pipeline: status and handoff

Last updated: 2026-09-27. Read this, then `README.md`, then the approved
plan (`~/.claude/plans/so-i-would-like-twinkling-metcalfe.md`).

## Goal

Review the Ubuntu FTBFS list at <http://qa.ubuntuwire.com/ftbfs/> with a
pipeline that is:
- deterministic wherever possible
- LLM-driven only where judgement is needed, with the cheapest model that
  can do it

For each failure it should end with a diagnosis, a verified debdiff and
an investigation report, shown on a dashboard.

## Architecture in one screen

- **Pipeline:** `pipeline.toml` is a DAG of plugin stages
  (`ftbfs/stages/*.py`, `plugins/`, or entry points). New steps (e.g.
  adversarial review, filing an LP bug) are one Stage class plus a config
  block.
- **Scheduler** (`ftbfs/core/scheduler.py`): runs stages in DAG order and
  supports:
  - `when` predicates over upstream results (restricted evaluator)
  - manual gates (always forced for `outward` stages)
  - caching by inputs hash
  - bounded `on_fail` loops
  - `pending` polling for async jobs
  - error retry caps (`max_errors`)
- **Work units:** item = source/version/arch; package; cluster = failures
  with the same normalized error. Cluster membership is global (all active
  classified items), so cached results don't depend on the run's filter.
- **Agent backends** (`ftbfs/agents/`): stages ask for a tier
  (small/medium/large) plus effort; `config.toml` maps tiers to models.
  - `claude`: `claude -p`, isolated (no CLAUDE.md, MCP, skills or
    sessions), with its own system prompt. About 400 tokens of fixed
    overhead instead of about 23k.
  - `fake`: for tests.
  - opencode: designed for, not written.
- **Observability:**
  - `event` table
  - per-attempt `prompt.md` / `transcript.jsonl` / `usage.json` under
    `work/`
  - CLI: `status`, `tail -f`, `show`, `why`, `verdicts`, `clusters`,
    `signals`
- **State:** `state/ftbfs.db` (SQLite, WAL) and `state/snapshots/*.json`.
  Caches are in `cache/`, artifacts in `work/`. All git-ignored.

## Milestones

| # | What | Status | Commit |
|---|---|---|---|
| 1 | Core framework, ingest, filters, JSON export | done | 61f7015 |
| 2 | Log excerpts, rules classifier, clusters | done | a504f55 |
| 3 | Debian/upstream facts (Sources, UDD, repro builds) | done | 801ec9b |
| 4 | Claude backend, triage, diagnose | done | 820c3be |
| 5 | Builder: reproduce (local sbuild; PPA path written) | local done, PPA deferred | 161fa34 |
| 6 | Dev agent + verify loop | **in progress, uncommitted** | - |
| 7 | Web app (live run view, gate queue, dashboard) | not started | - |
| 8 | Extensibility proof: adversarial review, opencode, lp_file_bug | not started | - |

Commits after the first two are **unsigned**, at your request, because
gpg-agent kept timing out.

## Key numbers (universe, FAILEDTOBUILD, no LP bug: 1058 items / 395 pkgs)

- **Excerpts:** 308x smaller than logs, median ~3 KB. 66% of failures are
  classified by `rules.toml`, and they form 411 clusters.
- **Facts:**
  - 70 sync candidates, 9 merge candidates
  - 34 packages with a Debian FTBFS bug fixed in a newer version
  - 116 packages with an open Debian FTBFS bug, 15 of them with a patch
- **Costs, measured on a 30-cluster sample:**
  - triage: $0.0067 per cluster (Haiku, 12 clusters per call)
  - diagnose: $0.048 per cluster (Sonnet, medium effort, bounded fields)
  - projected for the whole selection: about $15 (not run yet)
- **Reproduce:** 5 of 5 amd64 failures reproduced locally with the
  identical signature, about 22 s each.
- **Dev + verify (M6 first live run, run 17):**
  - 3 of 5 fixed and verified: hexcurse (C23 const), freehsm-c (FORTIFY),
    libshairport (signal handler prototype). About $0.07 to $0.09 of
    agent time each.
  - libevhtp and xfaces not fixed yet; see open issues 1 and 2.

## Milestone 6: what exists (uncommitted)

- **`ftbfs/builder/srcpkg.py`:** the agent only edits files; code does
  the Debian mechanics:
  - git baseline, and turning edits into a `debian/patches/*.patch` with
    a DEP-3 header, applied with quilt
  - `dch` entry with the next Ubuntu version, and `update-maintainer` on
    first delta
  - `dpkg-source -b` and `debdiff`
- **`ftbfs/stages/dev.py` + `prompts/dev.md`:**
  - tools are Read/Grep/Glob/Edit/Write only (no shell, no network)
  - context: diagnosis, excerpt, facts, a reference fix from a verified
    cluster member, and retry feedback
  - gated; `max_errors = 1`
- **`ftbfs/stages/verify.py`:** sbuild of the new `.dsc`. A failure loops
  back to dev (at most 2 loops); the last loop escalates to the large
  tier.
- **Also changed:**
  - `scheduler.py`: the `max_errors` option
  - `reproduce.py`: records `excerpt_path`
  - `pipeline.toml`: dev and verify blocks
  - `tests/test_dev_verify.py`: an offline loop test with a real tiny
    source package
- All 111 tests pass and lint is clean (checked 2026-09-27).

## Open issues (fix these next, in order)

1. **Retries lose the previous fix.** Each dev attempt starts from a
   fresh tree and only sees the previous debdiff as text. xfaces'
   attempt 2 fixed the new error (StartTimer prototype) but dropped the
   first fix (regexp.h), so verify got the original error back. libevhtp
   likewise regressed to `same-failure`. Fix: start a retry from the
   previous attempt's tree, which makes changes cumulative. The agent
   then only adds the new fix, and the tooling regenerates one combined
   patch.
2. **libevhtp and xfaces need a re-run.** Their third, escalated dev
   attempt did not complete. After fixing issue 1, run
   `ftbfs retry dev libevhtp/1.2.18-2.1build6/amd64` (and the same for
   the xfaces item), then `ftbfs run` with the same selection.
3. **The dev unit is per item (arch).** It should be per source+version:
   the same fix is currently made separately for each failing arch. That
   matters once several arches of one package are approved.
4. **Verify is amd64-only.** Foreign-arch verify needs the PPA path: a
   signed upload with a `~ftbfsN` version, which requires your GPG key.
5. **PPA reproduce is deferred.** It needs `ftbfs lp-login` (interactive)
   and a PPA with the Proposed dependency, set as `ppa = "owner/name"` in
   `[stage.reproduce]`.
6. **The `confirm` stage and the rules learning loop** (LLM-proposed
   regexes going to a review queue) are deferred. Add them only if the
   reports show mixed clusters or low rule coverage.

## Next milestones after M6

- **M7:** web app (FastAPI + htmx + SSE) over the same DB:
  - live run view and agent console (tail `transcript.jsonl`)
  - item timeline
  - generic gate queue
  - cost ledger
  - snapshot deltas
  - rendered `investigation.md`, stitched from per-stage template partials
    (per-item reports are not written yet)
- **M8:** extensibility proof:
  - `adversarial_review` stage (on_fail goes to dev), config only
  - opencode backend, with triage A/B against claude
  - `lp_file_bug` in dry-run

## How to resume

```sh
cd ~/Work/agentic-ftbfs
uv sync && uv run pytest -q && uv run ruff check .
uv run ftbfs status                  # runs, stage counts, gates, cost
uv run ftbfs stages                  # pipeline as configured
S="--source hexcurse --source freehsm-c --source xfaces \
   --source libevhtp --source libshairport --arch amd64"
uv run ftbfs verdicts $S             # triage + diagnosis
uv run ftbfs show xfaces             # event timeline + results
ls work/xfaces/*/amd64/dev/attempt-*/  # agent transcripts, debdiffs
```

Verified debdiffs:
`work/{hexcurse,freehsm-c,libshairport}/*/amd64/dev/attempt-1/fix.debdiff`

## Conventions and preferences (from the user)

- No em dashes. Stay within 80 columns where possible. Lint and tests
  must be clean, and pre-existing failures get fixed too.
- Quality and simplicity over dev cost. Start with the direct path.
- Never push. Commit only when asked. No co-author trailer.
- Explain the tradeoffs and ask before large subagent swarms.
- Anything outward-facing (Launchpad, Debian, forges) stays behind a
  manual gate; nothing is filed automatically.
