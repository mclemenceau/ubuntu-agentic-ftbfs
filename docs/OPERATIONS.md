# Operating the ftbfs pipeline

How to run the system day to day, what to review and what to watch. All
commands are `uv run ftbfs ...` from the project root. See `README.md`
for what each command does and `docs/HANDOFF.md` for the project status.

## Mental model

`ftbfs run` is not a service. Each call makes one pass over the pipeline
DAG and exits. Anything that cannot move forward yet (waiting at a gate,
waiting on an upstream stage, already cached) is left alone and picked
up by the next `run`. Administering it is a loop:

```
ingest -> run -> review (gates, attention) -> approve/retry -> run -> ...
```

- State lives in `state/ftbfs.db` (SQLite, WAL).
- Stages re-run only when their inputs change, so running again is cheap
  and safe.
- `approve` and `retry` only record a decision. The work happens on the
  next `run`.
- Nothing outward-facing happens automatically. Uploads, syncs and bug
  reports stay with you.

## When to run

| Cadence | Command | Notes |
|---|---|---|
| Daily | `ftbfs run --ingest` | New snapshot, then excerpt, classify, facts, triage and diagnose on new or changed items. Tokens are spent only on new clusters. |
| After approving gates | `ftbfs run` | Runs the approved reproduce, dev and verify units. |
| After editing `rules.toml` or `prompts/*.md` | `ftbfs run --sample-clusters 30 --seed 1`, then a full `run` | An edit re-runs only the affected stage and what follows it. |
| Start of a review cycle | `ftbfs report` | Regenerates `work/<src>/<ver>/investigation.md` (no tokens). |

### First full run

Until the first full run, triage and diagnose have covered only a sample
of clusters. The whole default selection (about 1058 items, 411
clusters) is projected at about $15. Do it once, deliberately:

```sh
uv run ftbfs run --ingest --until diagnose
uv run ftbfs status
```

After that, daily runs are incremental.

## Daily routine

1. `ftbfs run --ingest`
2. `ftbfs serve` and open <http://127.0.0.1:8047>, or use the CLI
   equivalents:
   - **Overview:** the snapshot delta (new, regressed, gone). New
     regressions are the most useful signal.
   - **Gates:** reproduce and dev wait for you. Approve the ones whose
     triage and diagnosis look right:
     `ftbfs approve reproduce <src>`, then after the next run
     `ftbfs approve dev <src>`. Reject with
     `--reject --note "reason"`.
   - **Attention:** errors, needs-human results and exhausted verify
     loops. `ftbfs why <src>` explains why a unit is stuck.
3. `ftbfs run` to execute what you approved. Follow it on the UI's Run
   page or with `ftbfs tail -f`.
4. Review the verified debdiffs under
   `work/<src>/<ver>/amd64/dev/attempt-N/fix.debdiff`, together with the
   agent's notes and `investigation.md`. Sponsoring or uploading is up to
   you.

## What to watch

### Cost

- Check `ftbfs status` (total) or the Costs page (by stage, model, run
  and package).
- Typical costs: triage about $0.007 per cluster, diagnose about $0.05
  per cluster, dev about $0.07 to $0.25 per attempt.
- A dev attempt close to its `max_budget_usd` (2.0) means the agent is
  thrashing.
- If a running agent looks stuck, the Console page has a kill button.

### API limits

- A "429 session limit" shows up as an error on Attention.
- Wait for the limit to reset, then `ftbfs retry <stage> <unit>`.
  `max_errors = 1` on dev keeps it from retrying on its own.

### Classification quality

- `ftbfs clusters` shows the rule hit rate (66% at the first full
  ingest).
- If unclassified or mixed clusters grow, add rules to `rules.toml`.
  That is the cheapest improvement you can make, since it cuts both
  tokens and noise.

### Verdict sanity

- Look at `ftbfs verdicts` now and then.
- Spot-check that facts-decided clusters (`sync-candidate`,
  `fixed-in-debian`; see `ftbfs signals`) are handled as syncs or
  merges, not patches.

### Verify outcomes

- A verify fail with exhausted loops needs a human. One more attempt:
  `ftbfs retry dev <item>`.
- A "verified" fix can still carry unneeded changes. Review every
  debdiff until an adversarial review stage exists.

### Local builds

- Reproduce and verify use your local sbuild, in unshare mode against
  the `<series>-proposed` chroot.
- If "reproduced" rates suddenly drop, or builds fail in chroot setup,
  the chroot tarball is probably stale or broken. Rebuild it.

### Disk

- `cache/logs`, `cache/sources` and `work/` grow steadily.
- From time to time, prune entries for packages that are gone from the
  FTBFS list.

## Emergency controls

| Situation | Action |
|---|---|
| Stop a run cleanly | `ftbfs control cancel` (or the Run page) |
| Hold a run while you look | `ftbfs control pause`, then `resume` |
| Runaway agent | Kill button on the Console page |
| Bad result to redo | `ftbfs retry <stage> <unit>`, then `ftbfs run` |

The UI is a separate process over the same database, so it can be
restarted at any time without disturbing a run.

## Current limits

- **amd64 only:** reproduce and verify run on amd64 only. Other arches
  need the PPA path (`ftbfs lp-login` and `ppa =` in `pipeline.toml`),
  which is not set up yet.
- **Dev per arch:** dev runs once per failing arch, not once per source.
  Approve one arch per package for now.
- **Local only:** the UI has no authentication and binds to loopback.
  Do not expose it.
