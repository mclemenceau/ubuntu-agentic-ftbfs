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

## The pipeline, stage by stage

```
ingest
  |
excerpt -> classify --+
                      +--> triage --> diagnose ----------+
facts ----------------+      |                           v
                             +--> reproduce --> [gate] dev
                                                          ^   |
                                                     fail |   v
                                                          +- verify
```

The arrows are the `after` dependencies in `pipeline.toml`. Reproduce
also needs excerpt, to compare against the original failure.

Each stage works on one kind of unit:
- **item:** one failing build, i.e. source/version/arch
- **package:** one source package
- **cluster:** the items that share the same normalized error

Cluster-level results (triage, diagnose) are shared by every member.

### ingest (command, not a stage)

`ftbfs ingest`, or `run --ingest`, fetches the qa.ubuntuwire.com/ftbfs
page and parses it. Package data comes from the per-component tables.
Packagesets and teams only provide membership. The result is saved as a
snapshot in `state/snapshots/` and diffed against the previous one:
new, regressed, gone and state changes. The `[filter]` in `config.toml`
then picks the items the run works on.

- **Tokens:** none
- **Watch:** the Overview delta. A parse error or a page layout change
  shows up here first: the parser checks its totals against the page's
  legend.

### excerpt (item, deterministic)

Downloads the Launchpad build log into `cache/logs` and cuts out the
part that matters. It reads sbuild's summary (Fail-Stage, Status),
finds the terminal error (make `***`, dh_*, ninja), and looks back to
the first real error. The excerpt is at most 150 lines around it,
about 3 KB instead of about 140 KB. The error lines are also turned
into a normalized signature, stable across paths, versions and arches.

- **Output:** `work/<src>/<ver>/<arch>/excerpt/excerpt.txt`, key lines,
  signature, failed step, fail stage
- **Tokens:** none. This is the main token saver: nothing downstream
  ever sees a raw log.
- **Re-runs:** when the build changes, or when `excerpt.version` is
  bumped after a change to `logs.extract()`

### classify (item, deterministic)

Matches the excerpt's key lines against `rules.toml`, first match wins.
It sets a failure class (for example `dependency-unsatisfiable`), a
family (compile, link, test, packaging, deps, toolchain, arch, infra)
and a cluster id. A rule's `cluster_by` controls how items group: by
class, by a captured key (e.g. the missing dependency), by signature or
by package. Items no rule matches are clustered by their signature.
A rule's `hint` is handed to the LLM prompts as known-fix knowledge.

- **Tokens:** none
- **Re-runs:** every item, whenever `rules.toml` changes (cheap)
- **Watch:** the rule hit rate in `ftbfs clusters`

### facts (package, deterministic)

Answers "is this known or fixed in Debian?" before any LLM runs. It
compares Ubuntu and Debian versions (Sources indexes), asks UDD for
Debian FTBFS bugs (one query per batch of 200 packages), and reads
the reproducible-builds status in Debian testing. It emits signals
such as `sync-candidate`, `merge-candidate`, `fixed-in-debian`,
`debian-ftbfs-open`, `debian-patch` and `builds-in-debian-testing`.

- **Tokens:** none. Indexes are cached for a day in `cache/facts/`.
- **Independent** of the log stages. It re-runs once a day, or when
  the set of failing builds of the package changes.

### triage (cluster, LLM, small tier)

The first verdict per cluster:
- category
- one-line summary and root cause guess
- obvious (yes/no)
- fixable (yes, no, maybe)
- action: patch, retry, wait-dependency, restrict-arch,
  report-upstream, investigate, sync or merge

Some clusters are decided by code, with no LLM call
(`decided_by = "rules"`):
- dependency-unsatisfiable clusters get `wait-dependency`
- clusters where every package is fixed in Debian, or is newer there
  and builds in Debian testing, get `sync` or `merge`

The rest go to the model about 12 per call. Each cluster is sent as a
compact context pack: the representative item's excerpt (amd64
preferred), a few key lines from other members, and the package facts.

- **Tokens:** about $0.007 per cluster
- **Re-runs:** when `prompts/triage.md` changes, or the cluster's
  inputs change
- **Watch:** `ftbfs verdicts`

### diagnose (cluster, LLM, medium tier)

Runs only when triage says the cluster may be fixable in Ubuntu
(action patch, investigate, report-upstream or restrict-arch). It gets
the same context pack plus the triage verdict. It returns:
- root cause and evidence
- fix kind (code-patch, packaging-change, sync or merge from Debian,
  backport-upstream-fix, disable-or-skip-test, ...)
- fix strategy and patch outline
- risk, confidence, upstream status

All fields are length-bounded to keep output tokens down.

- **Tokens:** about $0.05 per cluster
- **Re-runs:** when `prompts/diagnose.md` or triage changes

### reproduce (item, build)

Runs when triage's action is patch, investigate or report-upstream. It
is not gated, since a local build is free: it rebuilds the failing version
with your local sbuild against `<series>-proposed`, then compares the
result with the Launchpad failure:

| Outcome | Meaning |
|---|---|
| `reproduced` | same error signature |
| `reproduced-similar` | same cluster, different text |
| `different-failure` | fails, but for another reason |
| `built` | builds now: flaky, or fixed by newer dependencies |
| `timeout` | the build ran out of time |
| `infra-error` | the builder failed (chroot, fetch); retried |

- **Tokens:** none. About 20 s to a few minutes per build.
- **Arches:** amd64 locally. Other arches need the PPA path.
- **Why a gate:** builds are slow and use your machine. Approve only
  what triage and diagnosis make worth fixing.

### dev (item, LLM agent, manual gate)

Offered when reproduce says `reproduced` or `reproduced-similar`. After
`ftbfs approve dev <src>`, the agent works on the unpacked source tree.
It can only Read, Grep, Glob, Edit and Write: no shell, no network. It
gets:
- the excerpt (preferably from the local reproduction)
- the diagnosis and the package facts
- a reference fix, when one already exists for the same cluster

The tooling, not the agent, then does the Debian mechanics:
- turns the edits into a DEP-3 quilt patch
- adds a changelog entry with the next Ubuntu version
- runs update-maintainer
- builds the new source package and the debdiff

When verify fails, dev runs again on top of the previous attempt's
edits, with the new build error. From the second loop it uses the
large tier.

- **Output:** `work/<src>/<ver>/<arch>/dev/attempt-N/`: `fix.debdiff`,
  `edits.diff`, and the agent's prompt, transcript and notes
- **Tokens:** about $0.07 to $0.25 per attempt, capped at $2
- **needs-human:** when the agent makes no change
- **Errors:** `max_errors = 1`, so an API error is not retried on its
  own. Use `ftbfs retry dev <item>`.

### verify (item, build)

Rebuilds dev's new source package with the same sbuild setup:
- builds: `ok`, the debdiff is verified
- still fails (`same-failure` or `new-failure`): `fail`, and the new
  excerpt loops back to dev, at most twice
- the builder broke before the build: `error`

- **Tokens:** none
- **Watch:** Attention for exhausted loops. "Verified" means it builds,
  not that the change is minimal: review every debdiff.

### Planned stages

Not implemented yet (see `docs/HANDOFF.md`):
- `confirm`: check mixed clusters and propose new rules
- `adversarial_review`: review the verified debdiff, looping back to
  dev on failure
- `lp_file_bug`: an outward stage, always gated

## When to run

| Cadence | Command | Notes |
|---|---|---|
| Daily | `ftbfs run --ingest` | New snapshot, then excerpt, classify, facts, triage and diagnose on new or changed items. Tokens are spent only on new clusters. |
| After approving gates | `ftbfs run` | Runs the approved dev units and their verify. |
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

### Choosing the agent backend

`[agents] default_backend` in `config.toml` picks `claude` (`claude -p`,
your Claude subscription or key) or `opencode` (`opencode run`, e.g. an
OpenRouter key). The tier models are under `[backend.<name>.tiers]`.
Changing the backend
re-runs the agent stages on the next run, so compare on a sample before
switching a full run:

```sh
uv run ftbfs run --sample-clusters 30 --seed 1 --until diagnose
uv run ftbfs verdicts --sample-clusters 30 --seed 1
```

The older backend's results stay in the database (`stage_result`), so
both sets of verdicts can be compared there.

### Dedicated OpenRouter key

The opencode backend reads its OpenRouter key from
`~/.config/ftbfs/openrouter.key` (`[backend.opencode] api_keys` in
`config.toml`), not from your interactive opencode login:

1. Create a key at <https://openrouter.ai/settings/keys> with a credit
   limit: the limit is the hard cap on a runaway run, and the key's
   activity page is the pipeline's bill.
2. `install -m 600 /dev/null ~/.config/ftbfs/openrouter.key`, then paste
   the key into it (one line).

If the file is missing or empty, agent calls fail with "missing API key
file" instead of using another key.

## Daily routine

1. `ftbfs run --ingest`
2. `ftbfs serve` and open <http://127.0.0.1:8047>, or use the CLI
   equivalents:
   - **Overview:** the snapshot delta (new, regressed, gone). New
     regressions are the most useful signal.
   - **Gates:** dev waits for you once reproduce has confirmed the
     failure. Approve the ones whose triage, diagnosis and reproduce
     look right: `ftbfs approve dev <src>`. Reject with
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

- A "429 session limit" (claude) or an `APIError` (opencode) shows up as
  an error on Attention.
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
