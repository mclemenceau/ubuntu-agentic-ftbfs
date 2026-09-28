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
  - `opencode`: `opencode run --format json`, isolated the same way
    (private config home in `state/opencode/`, inline agent with an
    allowlist of permissions, nothing outside cwd). Tiers point at the
    same Claude models through OpenRouter, billed to a dedicated key
    (`~/.config/ftbfs/openrouter.key`, `api_keys` in config.toml).
  - `fake`: for tests.
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
| 6 | Dev agent + verify loop | done | 94a2368, 727c17a |
| 7 | Web app + investigation reports | done | ee9bddc |
| 8 | Extensibility proof: adversarial review, opencode, lp_file_bug | opencode backend done | uncommitted |

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
- **Dev + verify (runs 17 and 18):**
  - 4 of 5 fixed and verified: hexcurse (C23 const), freehsm-c (FORTIFY),
    libshairport (signal handler prototype), xfaces (K&R prototypes;
    see open issue 1). About $0.07 to $0.23 of agent time each.
  - libevhtp: the cumulative retry fixed both CMake 4 errors, then the
    build hit a third, unrelated failure (OpenSSL: incomplete
    `ASN1_OCTET_STRING`). The loop cap (2) stopped it: needs a human or
    another `ftbfs retry dev`.

## Milestone 6: dev + verify

- `ftbfs/builder/srcpkg.py`: the agent only edits files; code does the
  Debian mechanics (DEP-3 quilt patch, `dch`, `update-maintainer`,
  `dpkg-source -b`, `debdiff`).
- `ftbfs/stages/dev.py` + `prompts/dev.md`: Read/Grep/Glob/Edit/Write
  only; gated; `max_errors = 1`.
- `ftbfs/stages/verify.py`: sbuild of the new `.dsc`; failures loop back
  to dev (at most 2 loops, the last on the large tier).
- Retries are cumulative (was open issue 1): each attempt saves its raw
  edits as `attempt-N/edits.diff`; a retry replays the latest successful
  attempt's edits on a fresh tree, the agent adds to them, and the
  tooling regenerates one combined patch. `edits.diff` was backfilled by
  hand for the xfaces and libevhtp attempts made before this.
- The claude backend now reports API failures (e.g. "API 429: You've
  hit your session limit") instead of the CLI's misleading subtype
  "success". That was why run 17's escalated third attempts errored.

## Milestone 8 (part): opencode backend

- `ftbfs/agents/opencode.py`:
  - Isolation:
    - `XDG_CONFIG_HOME=state/opencode`
    - `OPENCODE_DISABLE_PROJECT_CONFIG` and `OPENCODE_DISABLE_CLAUDE_CODE`
    - `--pure`
    - one inline `ftbfs` agent via `OPENCODE_CONFIG_CONTENT`
  - Permissions: `"*": "deny"` plus the policy's tools, and
    `external_directory` denied.
  - The prompt goes on stdin. A `--title` skips the title LLM call.
  - The answer is the text of the last step. Usage and cost are summed
    from `step_finish` events.
  - `max_budget_usd` kills the agent when summed cost exceeds it.
    `effort` maps to the agent `variant`, `max_turns` to `steps`.
  - The transcript is bookended by `ftbfs_start` and `ftbfs_end`. The
    web console renders opencode events.
- `max_turns` is now unset unless configured. Before, the context
  defaulted it to 1, which claude ignored but opencode would have
  enforced as `steps`. claude now gets `--max-turns` when it is set.
- E2E (scratch copy of the state, OpenRouter key):
  - triage and diagnose on the 5 test clusters: 5 of 5 verdicts match
    claude's, $0.018 for triage and $0.02 to $0.03 per diagnosis.
  - dev and verify on hexcurse: the same fix as claude's, verified by
    sbuild, $0.053, 7 steps.
  - Sonnet's `medium` variant did not think where claude's `--effort
    medium` did (1k vs 4k output tokens).
- A/B on the 30-cluster sample (10a65e8): triage matches claude about
  as well as claude matches itself, at 40% of the cost. Diagnose at the
  `high` variant matches claude's depth at about half its cost, so
  diagnose now runs at `effort = "high"`.

## Milestone 7: web app and reports

- `ftbfs serve` (FastAPI + Jinja + htmx + SSE, `ftbfs/web/`):
  - overview (snapshot delta, DAG with counts, runs)
  - live run: counters, agents/builds in flight, cost, event stream,
    pause/resume/cancel
  - agent console: streams `transcript.jsonl`, kill button (the pid is
    checked against the process start time, so a recycled pid is never
    signalled)
  - package page: rendered report, per-arch dots, `why` with
    approve/retry, attempts and artifacts, timeline
  - gate queue with context, attention, items, clusters, cost ledger,
    snapshot deltas
  - `queries.py` holds all the SQL; `transcript.py` renders claude
    stream-json (unknown events are shown raw)
  - security: loopback bind, Host check, POSTs need `HX-Request`, file
    serving limited to `work/`, `cache/` and snapshots
- Next steps inbox at `/` (`ftbfs/web/nextsteps.py`; the old overview
  is `/overview`): verified fixes to review, accepted fixes to upload,
  needs a human, gate approvals in buckets by diagnosis (decided ahead
  of the gate), sync/merge verdicts, and a dry-run preview of the next
  run (`Scheduler.plan`) with a start button (`App.start_run`, a
  detached `python -m ftbfs run --trigger web`). A disposition per
  source version (accepted, uploaded, rejected, handled) takes it out
  of the inbox and out of runs (`App.workable`). Runs now queue the
  pending gates of stages beyond `--until`.
- `ftbfs report` / `ftbfs/report.py`: `investigation.md` per source
  version, stitched from `ftbfs/templates/stages/<stage>.md.j2` in DAG
  order; plugins override with `plugins/templates/`. Summary and next
  action are deterministic.
- Approve/retry/control/kill moved into `App`, shared by CLI and UI.
- Checked end to end: the real server in headless Chrome over the real DB
  (overview, package, run, console, gates), and SSE events from a
  separate `ftbfs run` process reaching an open stream.
- 121 tests pass, lint clean.

## Open issues (fix these next, in order)

1. **xfaces' verified fix carries a probably unneeded fallback.** Its
   retry got build feedback from the old non-cumulative attempt 2, so
   the agent also added `-std=gnu17` in `debian/rules` in case the
   header edit was missing (see its notes). The regexp.h prototypes
   alone should fix it: drop the rules change and rebuild before using
   this debdiff. The M8 adversarial review should catch cases like this.
2. **libevhtp needs a human** (OpenSSL 3 opaque struct in sslutils.c),
   or one more `ftbfs retry dev libevhtp/1.2.18-2.1build6/amd64`.
3. **The dev unit is per item (arch).** It should be per source+version:
   the same fix is currently made separately for each failing arch. That
   matters once several arches of one package are approved.
4. **Verify is amd64-only.** Foreign-arch verify needs the PPA path: a
   signed upload with a `~ftbfsN` version, which requires your GPG key.
5. **PPA reproduce is deferred.** It needs `ftbfs lp-login` (interactive)
   and a PPA with the Proposed dependency, set as `ppa = "owner/name"` in
   `[stage.reproduce]`.
6. **The `confirm` stage and the rules learning loop** (LLM-proposed
   regexes going to a review queue) are deferred, and with them the UI's
   rule review queue. Add them only if the reports show mixed clusters
   or low rule coverage.
7. **UI gaps against the plan:** retry with another backend or tier
   from the UI (retry exists, with the configured agent only); no
   authentication (loopback only, by design).

## Plan after the first full run (run 22, 2026-09-27)

Run 22 took the whole selection through diagnose on opencode:
- triage: 410 clusters, $1.18
- diagnose: 283 clusters, $7.80 (the estimate was $15 for both)
- triage verdicts: 158 patch/yes (115 obvious), 35 sync, 6 merge,
  32 wait-dependency/no
- ready for reproduce on amd64: 108 packages with a patch verdict (69
  low risk, 39 medium), plus 27 investigate and 9 report-upstream

Steps, in order:
1. **Fix the diagnose schema repair, then retry the affected clusters.**
   51 diagnoses (18%) failed validation. Haiku rewrote them from a
   truncated copy, and they were recorded as Haiku's. The causes are
   mechanical: trailing commas or raw newlines in the JSON (13), fields
   slightly over their length caps (about 35), and one legitimate
   4-character evidence line. Parse leniently, give the caps headroom,
   and repair on the same tier.
   **Done (run 23):** all 51 saved answers now pass without a repair;
   the 45 affected clusters were retried, all ok on Sonnet, 0 repairs,
   $1.41.
2. **Spot-check diagnosis quality** before building. The A/B covered
   30 clusters; this is the first run at scale. Read about 15
   diagnoses from the weak buckets
   (`disable-or-skip-test` at 0.49 to 0.58, `unknown` at 0.36,
   medium-risk `code-patch` at 0.64). If they are shallow, re-run those
   buckets on the claude backend and compare.
3. **Zero-token wins:** list the 35 sync and 6 merge clusters, already
   decided by the Debian facts.
4. **Reproduce the 69 low-risk patch packages** (local sbuild, free).
   **Done:** the manual gate on local reproduce is dropped (a gate
   comes back with `ppa`, which uploads to Launchpad).
5. **Dev + verify on a batch of 15 to 20.** Measure the fix rate and
   cost per verified fix before going wider. Build the M8
   `adversarial_review` stage before trusting dozens of debdiffs
   (see open issue 1).

## Next milestone

- **M8:** extensibility proof:
  - `adversarial_review` stage (on_fail goes to dev), config only
  - opencode backend: done, including the triage/diagnose A/B
    against claude on the 30-cluster sample
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
uv run ftbfs serve                   # UI on http://127.0.0.1:8047
uv run ftbfs report $S               # work/<src>/<ver>/investigation.md
```

Verified debdiffs:
`work/{hexcurse,freehsm-c,libshairport}/*/amd64/dev/attempt-1/fix.debdiff`
and `work/xfaces/3.3-30.3/amd64/dev/attempt-4/fix.debdiff` (see issue 1).

## Conventions and preferences (from the user)

- No em dashes. Stay within 80 columns where possible. Lint and tests
  must be clean, and pre-existing failures get fixed too.
- Quality and simplicity over dev cost. Start with the direct path.
- Never push. Commit only when asked. No co-author trailer.
- Explain the tradeoffs and ask before large subagent swarms.
- Anything outward-facing (Launchpad, Debian, forges) stays behind a
  manual gate; nothing is filed automatically.
