# Status

Last updated: 2026-10-03.

ftbfs runs end to end on the Ubuntu development series: ingest,
excerpts, classification, Debian facts, triage and diagnosis on every
cluster, local reproduction, and dev + verify on the units a person
approves. The output is a verified debdiff and an investigation report
per package, reviewed on the web UI. Measured results are in
`DESIGN.md`, "Evidence".

## History

| # | What | Commit |
|---|---|---|
| 1 | Core framework, ingest, filters, JSON export | 058ff3a |
| 2 | Log excerpts, rules classifier, clusters | de0d228 |
| 3 | Debian and upstream facts (Sources, UDD, reproducible builds) | 97b86e4 |
| 4 | claude backend, triage, diagnose | e187584 |
| 5 | reproduce with local sbuild (PPA path written, not set up) | df758ee |
| 6 | dev agent and verify loop, cumulative retries | edd72d9, c439373 |
| 7 | Web UI and investigation reports | d39cb53 |
| 8 | opencode backend, A/B against claude | d484a64, 10a65e8 |
| 9 | Next steps inbox as the landing page | 40257ab |
| 10 | Build hosts: a pool of LXD workers | ffc1581 |
| 11 | Publication: site config split, GPLv3, review, CI, docs | fe7e456 and later |
| 12 | Web login with Launchpad, roles, public reading without costs | 2b3590a |

The first full run to diagnose was run 22 (2026-09-27). Batches of dev
and verify followed (runs 27 to 31, up to 2026-09-30).

## Roadmap

Planned, in rough order. This is the one place that lists them.

- **`adversarial_review` stage.** A second agent reviews each verified
  debdiff for unneeded or risky changes and loops back to dev on
  failure. "Verified" only means it builds: xfaces' fix still carries a
  `-std=gnu17` fallback in `debian/rules` that its header fix makes
  unneeded.
- **dev per source version.** The dev unit is an item (one arch), so a
  package failing on several arches gets the same fix made once per
  approved arch. Approve one arch per package until then.
- **Foreign arches through a PPA.** reproduce and verify build amd64
  only. The PPA path (`ftbfs lp-login`, `ppa = "owner/name"` in
  `[stage.reproduce]`, a signed upload with a `~ftbfsN` version) is
  written but not set up; it uploads to Launchpad, so it comes back
  with a manual gate.
- **`lp_file_bug` stage** (outward, always gated), in dry-run first.
- **`confirm` stage and a rules learning loop.** An LLM checks mixed
  clusters and proposes new `rules.toml` entries to a review queue.
  Only if reports show mixed clusters or rule coverage drops.
- **Retry with another backend or tier** from the UI (retry exists,
  with the configured agent only).

## Open issues

Known defects and hardening work are tracked as
[GitHub issues](https://github.com/mclemenceau/ubuntu-agentic-ftbfs/issues).
