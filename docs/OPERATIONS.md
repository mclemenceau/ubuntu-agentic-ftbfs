# Operating the ftbfs pipeline

How to run the system day to day, what to review and what to watch. All
commands are `uv run ftbfs ...` from the project root (or `--root DIR`).
See `README.md` for the installation, `DESIGN.md` for why it works
this way and `STATUS.md` for the roadmap.

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

## Site configuration

`config.toml` holds the project defaults. This machine's own settings go
in `config.local.toml` (git-ignored), merged over it: tables merge key
by key, other values replace. Start from the example:

```sh
cp config.local.toml.example config.local.toml
```

It holds:
- `[identity]`: name and email for the changelog entries and patches
  the dev stage writes. Without it, dev uses `git config user.name` and
  `user.email`, and errors if neither is set.
- `[concurrency]`: how many units of each kind run at once here.
- `[backend.opencode] api_keys`: the key files (see "Dedicated
  OpenRouter key").
- `[builders.*]`: the build hosts (see "Build hosts").
- `[web]`, `[web.auth]`, `[web.roles]`: who can reach the web UI and
  what they can do (see "Web access").
- `[agents] default_backend`: see "Choosing the agent backend".

## Commands

`ftbfs --help` lists them all, and `ftbfs <command> --help` their
options.

**Selecting items.** `list`, `run`, `export`, `clusters`, `signals`,
`verdicts` and `report` work on a selection. Its defaults come from
`[filter]` in `config.toml`; flags override them: `--component`,
`--state F`, `--arch`, `--pocket`, `--packageset`, `--team`,
`--source 'python-*'`, `--include-bugged`, `--limit`, `--profile NAME`
(a named set from `[filter.profiles]`), and `--sample-clusters N --seed
S` for random whole clusters.

**Running.**
- `ingest`: fetch and parse the FTBFS page, save a snapshot in
  `state/snapshots/` and diff it with the previous one
- `run`: one pass of the pipeline over the selection; `--ingest`
  fetches first, `--until STAGE` stops after a stage, `--stage STAGE`
  runs only that one
- `report`: write `work/<src>/<ver>/investigation.md` (no tokens)
- `export --out ftbfs.json`: the selection as JSON, with build and log
  URLs
- `serve`: the web UI, on <http://127.0.0.1:8047> by default

**Seeing what happens.**

| Command | Shows |
|---|---|
| `status` | runs, latest result per stage, gates, cost |
| `why <source or item>` | filter exclusions, then the decision per stage (waiting, `when` false, gated, cached, ...) |
| `show <source>` | event timeline and stage results |
| `tail -f [--unit X] [--stage Y]` | live event stream |
| `clusters` | failure clusters and the rule hit rate |
| `signals [-v] [--signal S]` | packages per Debian facts signal |
| `verdicts` | triage and diagnosis per cluster |
| `stages` | registered stages and the pipeline as configured |
| `builders` | build hosts, their image and workers |

**Deciding.**
- `approve <stage> <unit...> [--reject --note "reason"]`
- `retry <stage> <unit...>`: re-run a stage on the next `run`
- `control pause|resume|cancel`: the run in progress

A unit is a source name (all its items), an item
(`source/version/arch`) or a cluster id, depending on the stage.

Artifacts live under `work/<source>/<version>/<arch>/<stage>/`. For
agent stages that includes `attempt-N/` with `prompt.md`,
`transcript.jsonl`, `result.json` and `usage.json`.

## The web UI

`ftbfs serve` is a separate process over the same database, so it can
be restarted at any time without disturbing a run. Its controls go
through the same code as the CLI and are recorded as events. Pages:
- **Next steps** (`/`): verified fixes to review, accepted fixes to
  upload, units that need a human, gates to approve grouped by
  diagnosis, syncs and merges from Debian, and a preview of what the
  next run would do, with a button to start it. A disposition on a
  source version (accepted, uploaded, rejected as "won't fix", handled)
  takes it out of the inbox and out of later runs.
- **Pipeline** (`/overview`): the latest snapshot and its delta, the
  DAG with counts per stage, the runs
- **Run**: live counters, agents and builds in flight with the host
  they run on, running cost, event stream, pause, resume and cancel
- **Console**: an agent's transcript as it streams, and a kill button
- **Package**: the investigation report, pipeline dots per arch, `why`
  per stage with approve and retry, every attempt's artifacts (prompt,
  transcript, debdiff, build log) and the timeline
- **Gates** and **Attention** (errors, needs-human, exhausted loops)
- **Items**, **Clusters**, **Signals**, **Costs** (by stage, model, run
  and package) and **Snapshots** (new, regressed, gone between any two)

### Web access

Without `[web.auth]`, the UI is for the person at the machine: it
serves loopback only (`serve --host` anything else is refused) and
needs no login. To open it to others, configure a Launchpad login in
`config.local.toml` (the example has every key):

1. Create the session secret, which signs the login cookies:
   ```sh
   install -m 600 /dev/null ~/.config/ftbfs/session.key
   head -c 32 /dev/urandom | base64 > ~/.config/ftbfs/session.key
   ```
2. Set `[web] public_url` (the address people use; Launchpad sends them
   back to `<public_url>/auth/callback`), add its host name to `[web]
   allowed_hosts`, set `session_secret_file`, and `[web.auth] provider
   = "launchpad"`.
3. Grant roles by Launchpad name or team (public membership only):
   ```toml
   [web.roles]
   viewer = ["~ubuntu-dev"]        # sees costs
   reviewer = ["~my-review-team"]  # gates, dispositions, retries
   operator = ["me"]               # runs, pause/cancel, kill agents
   ```
   Restart `ftbfs serve` after a change. Removing someone takes effect
   at the restart; a team's members are read when they log in.
4. Serve behind a TLS proxy (the UI itself speaks plain HTTP), with
   `ftbfs serve` on loopback.

Anyone can read every page except costs. People log in with "log in
with Launchpad" at the top right; Launchpad asks them to allow "Read
non private Data", and each login adds one entry to their "Authorized
applications" on Launchpad, which they can revoke there. Actions are
recorded as `web:<login>` on the timeline and the gates. `[web]
daily_cost_cap` (USD per UTC day, default 10) stops the UI from
starting runs once web-started runs have spent that much; the CLI is not
capped.

### Reports

`investigation.md` is stitched from one Jinja partial per stage
(`ftbfs/templates/stages/<stage>.md.j2`) in DAG order; the summary and
the recommended next action are derived from the results.

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

- **Tokens:** about $0.003 per cluster
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

- **Tokens:** about $0.03 per cluster
- **Re-runs:** when `prompts/diagnose.md` or triage changes

### reproduce (item, build)

Runs when triage's action is patch, investigate or report-upstream. It
is not gated, since a build on your own hosts is free: it rebuilds the
failing version with sbuild against `<series>-proposed` on one of the
builders (see "Build hosts"), then compares the result with the
Launchpad failure:

| Outcome | Meaning |
|---|---|
| `reproduced` | same error signature |
| `reproduced-similar` | same cluster, different text |
| `different-failure` | fails, but for another reason |
| `built` | builds now: flaky, or fixed by newer dependencies |
| `timeout` | the build ran out of time |
| `infra-error` | the builder failed (chroot, fetch); retried |

- **Tokens:** none. About 20 s to a few minutes per build.
- **Arches:** the arches your builders support (amd64). Other arches
  need the PPA path, which is gated because it uploads to Launchpad.

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
- **Tokens:** about $0.10 per attempt on the medium tier, $0.27 on
  the large one, capped at $2
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

Planned stages (an adversarial review of verified debdiffs, filing
bugs, rule proposals) are listed in `STATUS.md`, "Roadmap".

## When to run

| Cadence | Command | Notes |
|---|---|---|
| Daily | `ftbfs run --ingest` | New snapshot, then excerpt, classify, facts, triage and diagnose on new or changed items. Tokens are spent only on new clusters. |
| After approving gates | `ftbfs run` | Runs the approved dev units and their verify. |
| After editing `rules.toml` or `prompts/*.md` | `ftbfs run --sample-clusters 30 --seed 1`, then a full `run` | An edit re-runs only the affected stage and what follows it. |
| Start of a review cycle | `ftbfs report` | Regenerates `work/<src>/<ver>/investigation.md` (no tokens). |

### The first full run

On a new database, the first run over the whole default selection
pays for every cluster at once: about 1000 items and 400 clusters,
which cost $9 for triage and diagnose on 2026-09-27 (opencode). Try a
sample first (see "Choosing the agent backend"), then do it once,
deliberately:

```sh
uv run ftbfs run --ingest --until diagnose
uv run ftbfs status
```

After that, daily runs are incremental: tokens are spent only on new
or changed clusters.

### Choosing the agent backend

`[agents] default_backend` picks the backend for every agent stage:
`opencode` (the default in `config.toml`) or `claude`. Set it in
`config.local.toml` to change it for your site, or set one stage's
`agent = { backend = "...", ... }` in `pipeline.toml`. Stages ask for
a tier (`small`, `medium`, `large`), which `[backend.<name>.tiers]`
maps to a model.

- **opencode** runs `opencode run --format json` with models as
  `provider/model` ids (`openrouter/anthropic/claude-sonnet-5`). Its key
  comes from a dedicated file (see below); providers without an entry
  in `api_keys` use opencode's own login. It runs with a private config
  home under `state/opencode/`, and its sessions are kept in opencode's
  database, titled `ftbfs <unit>/<stage>/attempt-N`.
- **claude** runs `claude -p` with your Claude Code login or key.

Both run isolated from your own setup (DESIGN, "Isolated agents").
Per stage, `agent = {...}` can also set:
- `effort` (low to max): thinking tokens dominate output cost. claude
  passes it as `--effort`; opencode maps it to the model's `--variant`,
  which is not calibrated the same way (Sonnet at `medium` often does
  not think, hence diagnose at `high`).
- `max_budget_usd`: the agent is stopped beyond it.
- `timeout`, and `max_turns` (unset: the backend's own limit).

The agent spec is part of the cache key, so changing the backend
re-runs the agent stages on the next run. Compare on a sample before
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
`config.local.toml`), not from your interactive opencode login:

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
   - **Next steps:** the inbox. It lists verified fixes to review,
     gates to approve and Debian syncs, and has a preview of the next
     run.
   - **Pipeline:** the snapshot delta (new, regressed, gone). New
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
- Typical costs (opencode, 2026-09-30): triage about $0.003 per
  cluster, diagnose about $0.03 per cluster, dev about $0.10 to $0.27
  per attempt, or $0.36 per verified fix counting retries.
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

### Build hosts

Reproduce and verify run sbuild (unshare mode, `<series>-proposed`) on
the builders in `config.local.toml`. Without a `[builders.*]` table,
builds use your own sbuild on this machine, `[concurrency] build` at a
time. That needs `sbuild`, `mmdebstrap` and `uidmap`, unshare mode
(`$chroot_mode = 'unshare';` in `~/.config/sbuild/config.pl`) and the
chroot tarball `~/.cache/sbuild/<series>-proposed-amd64.tar` (README,
"Quick start", has the command). sbuild upgrades the chroot at the
start of every build, so the tarball only needs remaking when the
series changes. Package code runs in sbuild's chroot as your user, so
prefer LXD builders on a machine that holds anything of value
(`SECURITY.md`, "Builds").

With LXD, each host is one table, and each slot is a worker container
on it:

```toml
[builders.local]
kind = "lxd"
remote = "local"      # an `lxc remote` name; local = this machine
slots = 2
parallel = 6          # DEB_BUILD_OPTIONS parallel per build

[builders.buildhost]
kind = "lxd"
remote = "buildhost"
slots = 2
parallel = 4
```

- A build goes to the builder with the most free slots for its arch.
  Results record it (`builder` in the reproduce and verify data; the
  run page shows it under "Where").
- Every host builds from the same image, so a signature mismatch is
  never a host difference. `ftbfs builders image` builds it on the
  first LXD builder's host (sbuild, mmdebstrap, a builder user, the
  chroot tarball) and copies it to the others. Workers switch to a new
  image at their next build.
- Rebuild the image when the series changes, or when "reproduced"
  rates suddenly drop or builds fail in chroot setup (a stale or broken
  chroot). sbuild upgrades the chroot at the start of every build, so
  an image a few weeks old is fine otherwise.
- `ftbfs builders` shows each host: reachable, image, and each worker.
- The image sets sbuild's AppArmor profile to complain mode: enforced,
  it blocks apt's network access in the unshare chroot inside a
  container. The container is the isolation boundary.
- A worker is force-restarted the first time a run uses it and after
  a killed build, so nothing from a crashed or killed build survives.
- Adding a host: `lxc remote add <name> <address>`, then a table.

### Disk

- `cache/logs`, `cache/sources` and `work/` grow steadily.
- From time to time, prune entries for packages that are gone from the
  FTBFS list.

### Moving an instance

Results, events and snapshots record absolute paths, and those results
are part of the cache key of the stages after them. After moving the
project directory, or copying `state/`, `work/` and `cache/` to
another machine (docs/DEPLOYMENT.md), run from the new directory:

```sh
uv run ftbfs relocate --dry-run /old/project/dir   # counts only
uv run ftbfs relocate /old/project/dir
```

It rewrites the old directory in the database and in the symlinks
under `work/` and `cache/`, and moves each cached result onto the
inputs hash its rewritten upstream data gives, so the next run plans
the same work as before the move: compare the Next steps preview
before and after. Run it between runs, never during one.

## Emergency controls

| Situation | Action |
|---|---|
| Stop a run cleanly | `ftbfs control cancel` (or the Run page). Units already running finish; queued ones never start. |
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

What is planned to lift these is in `STATUS.md`, "Roadmap".
