# terra-scrub

Finds reclaimable storage in a Terra workspace bucket, builds an auditable delete plan
for it, and, once a person types the plan's id, runs that plan and proves what it
removed. From a dirty workspace to a clean one takes three commands:

```bash
terra-scrub scan   my-ns/my-ws     # read-only: list, read Terra, build + live-check a plan
terra-scrub status my-ns/my-ws     # what the latest scan found, and whether it can be cleaned
terra-scrub clean  my-ns/my-ws     # YOU type the plan id; it runs the plan, then verifies
```

A session looks like this (abridged):

```text
$ terra-scrub scan my-ns/my-ws --reference-list delivered/callset_v1/sample_map.tsv
== scan my-ns/my-ws ==
  target:            my-ns/my-ws
  bucket:            gs://fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd
  snapshot:          184,212 objects, 41.87 TiB
  delete candidates: 61,904 objects, 12.31 TiB
  review list:       2,118 rows, 402.55 GiB  ~/.terra-scrub/runs/my-ns/my-ws/20260923T141502Z/cleanup/fc-1234abcd-5678-90ef-/fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd.cleanup.protected.tsv
  plan:              3f9c0a7e21bd  61,904 objects, 12.31 TiB  executable: yes
  outcome:           planned
  run dir:           ~/.terra-scrub/runs/my-ns/my-ws/20260923T141502Z
  next:              terra-scrub clean my-ns/my-ws

$ terra-scrub status my-ns/my-ws
== status my-ns/my-ws ==
  latest run:  20260923T141502Z  (0.4 h ago)
  bucket:      gs://fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd
  plan:        3f9c0a7e21bd  61,904 objects, 12.31 TiB  executable: yes
  age:         manifest 0.4 h, plan 0.4 h -> fresh
  wrapper:     not armed
  next:        terra-scrub clean my-ns/my-ws

$ terra-scrub clean my-ns/my-ws
== terra-scrub clean my-ns/my-ws ==
   bucket    : gs://fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd
   plan id   : 3f9c0a7e21bd
   delete    : 61,904 objects, 12.31 TiB (13,534,915,011,584 bytes)
   age       : manifest 0.5 h, plan 0.4 h (limit 24 h)
   re-checks : URI list sha256 + line count verified; pointer check on
   wrapper   : ~/.terra-scrub/runs/my-ns/my-ws/20260923T141502Z/cleanup/fc-1234abcd-5678-90ef-/fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd.cleanup.tsv.plan.sh
   review    : ~/.terra-scrub/runs/my-ns/my-ws/20260923T141502Z/cleanup/fc-1234abcd-5678-90ef-/fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd.cleanup.tsv
   NOTE      : recovery afterwards is a soft-delete restore, and only inside the bucket's soft-delete window (GCS default 7 days; it can be 0) -- docs/SAFETY.md §8
Type the plan id to delete 61,904 objects (12.31 TiB) from gs://fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd, or anything else to abort: 3f9c0a7e21bd

ARMED: CONFIRM=3f9c0a7e21bd written into ...cleanup.tsv.plan.sh
-- running ...cleanup.tsv.plan.sh (log: .../logs/clean.log)
-- verifying (log: .../logs/verify.log)
RESULT: ALL CHECKS PASS

CLEANED my-ns/my-ws: 61,904 objects, 12.31 TiB freed; verify OK -- soft-delete restore window until 2026-09-30T14:40
```

Read the manifest TSV the summary points at before you type the id. Typing it is the
approval. `terra-scrub clean my-ns/my-ws --dry-run` does every check and prints the
summary without arming or running anything.

## Install

```bash
pip install -e .          # or: uv pip install -e .
gcloud auth application-default login
```

Python 3.11 or newer. Every GCS and Terra call uses your application-default
credentials. Set `TERRA_API_URL` to point at a Terra API other than
`https://api.firecloud.org`. `scan`/`status`/`clean` keep their state under
`~/.terra-scrub` (override with `TERRA_SCRUB_HOME` or `--home`).

## Commands

| command | what it does |
|---|---|
| `scan <ns>/<ws>` | snapshot → context → candidates → fresh context → plan, into a new run dir (read-only) |
| `status <ns>/<ws>` | the latest run for a workspace: outcome, plan id, size, freshness, armed state |
| `clean <ns>/<ws>` | re-check the latest plan, take the typed plan id, run the wrapper, verify (a human step) |

### Under the hood (advanced)

`scan` and `clean` drive the low-level commands below. Use them directly to run steps
by hand, to split roles (someone plans, someone else approves), or across a whole
estate. [docs/WORKFLOW.md](docs/WORKFLOW.md) covers both.

| command | what it does |
|---|---|
| `snapshot` | list a bucket into a JSONL snapshot (GET only) |
| `context` | capture Terra workspace attributes, referenced URIs and submissions |
| `workspaces` | list the workspaces visible to you, optionally filtered by namespace |
| `lookup` | find the workspace that owns a bucket |
| `report` | offline space report from a snapshot |
| `stale` | staleness report from a snapshot plus a context |
| `dupes` | duplicate analysis from a snapshot |
| `candidates` | build reviewable delete and protected lists (offline) |
| `plan` | re-validate a candidate list against live GCS and write a PLAN (it has no delete verb) |
| `approve` | arm a plan's wrapper by writing its CONFIRM token (a human step) |
| `verify` | check what a delete wrapper actually did (before/after re-listing) |
| `estate` | run `scan` / `plan` across every bucket in a set of namespaces |

## Safety model

- **Delete-free, except `clean`.** Every module but `clean` issues HTTP GET only,
  through a single network primitive. An AST lint in the test suite fails the build if
  any of them gains a mutating verb, a `subprocess` import or an `--execute`-style
  flag. `clean` deletes only by running the plan's own wrapper, after the same
  re-checks `approve` performs and after a person types the plan id.
- **Ordering is enforced.** The bucket listing has to be captured before the Terra
  context. A plan needs a context captured after its manifest was generated. Both
  rules are refusals in the code, and `scan` runs the steps in that order.
- **Guards G1–G9** keep deliverables, last copies, Cromwell provenance, zero-byte
  markers, index/data pairs and anything named in a reference list off the delete
  list.
- **Two-man rule.** `plan` writes a wrapper that refuses to run. It is armed only when
  a person types the plan id, at `clean`'s prompt or to `approve`. The wrapper
  re-checks its URI list's sha256 before deleting. This repo's
  `.claude/settings.json` stops an AI assistant from running `approve`, `clean` or a
  wrapper.
- **Stale plans are refused.** A manifest or plan older than 24 h cannot be armed. Run
  `scan` again.
- **Verify afterwards.** `clean` runs `verify`, which re-lists the bucket and checks
  that exactly the planned set disappeared, that keepers and Terra-referenced objects
  survive, and that soft-deleted copies exist.

[docs/SAFETY.md](docs/SAFETY.md) gives the reasoning behind each rule.

## Development

```bash
pip install -e .[dev] && pytest
```

The tests run offline: no network, GCS, Terra, gcloud or gsutil, and no wrapper is
ever executed.

## Provenance

Extracted from the CLARUM project's bucket-cleanup tooling (Broad Institute, Talkowski
lab, 2026). Inspired by FISS `mop`. BSD-3-Clause; see [LICENSE](LICENSE).
