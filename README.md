# terra-scrub

A toolkit for finding reclaimable storage in Terra workspace GCS buckets and writing
auditable delete plans for it. It is read-only by construction: nothing in the package
deletes an object. It lists the bucket, reads the Terra workspace (attributes, entity
references, submissions), builds delete and review lists under explicit guards,
re-checks each row against live GCS, and writes a plan. The last step is a wrapper
script that stays inert until a human arms it.

## Install

```bash
pip install -e .          # or: uv pip install -e .
gcloud auth application-default login
```

Python 3.11 or newer. Every GCS and Terra call uses your application-default
credentials. Set `TERRA_API_URL` to point at a Terra API other than
`https://api.firecloud.org`.

## Quickstart (one bucket)

```bash
R=runs/manual && mkdir -p $R/inv $R/cleanup/mybucket
terra-scrub lookup fc-1234abcd-...                             # -> namespace / workspace
terra-scrub snapshot fc-1234abcd-... --out $R/inv/mybucket.jsonl      # listing FIRST
terra-scrub context my-ns my-ws --out $R/inv/mybucket.terra.json      # then Terra
terra-scrub report $R/inv/mybucket.jsonl                               # where the bytes are
terra-scrub candidates --snapshot $R/inv/mybucket.jsonl --terra $R/inv/mybucket.terra.json \
    --out-dir $R/cleanup/mybucket --max-snapshot-age 1
terra-scrub context my-ns my-ws --out $R/inv/mybucket.plan-time.terra.json   # fresh
terra-scrub plan --manifest $R/cleanup/mybucket/fc-1234abcd-....cleanup.tsv \
    --terra $R/inv/mybucket.plan-time.terra.json
terra-scrub approve --root $R <plan_id>        # HUMAN step, then: bash <...>.plan.sh
terra-scrub verify --plan $R/cleanup/mybucket/fc-1234abcd-....cleanup.tsv.plan.json
```

For many buckets, use `terra-scrub estate scan --namespace NS ...` followed by
`terra-scrub estate plan --root ...`. [docs/WORKFLOW.md](docs/WORKFLOW.md) walks
through both paths.

## Commands

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

- **Read-only by construction.** The analysis and planning modules issue HTTP GET
  only, through a single network primitive. An AST lint in the test suite fails the
  build if any of them gains a mutating verb, a `subprocess` import or an
  `--execute`-style flag.
- **Ordering is enforced.** The bucket listing has to be captured before the Terra
  context. A plan needs a context captured after its manifest was generated. Both
  rules are refusals in the code.
- **Guards G1–G9** keep deliverables, last copies, Cromwell provenance, zero-byte
  markers, index/data pairs and anything named in a reference list off the delete
  list.
- **Two-man rule.** `plan` writes a wrapper that refuses to run. `approve`, run by a
  person, writes the plan's id into it. The recommended agent deny-rules stop an AI
  assistant from arming or running a wrapper on its own.
- **Verify afterwards.** `verify` re-lists the bucket and proves that exactly the
  planned set disappeared.

[docs/SAFETY.md](docs/SAFETY.md) gives the reasoning behind each rule.

## Development

```bash
pip install -e .[dev] && pytest
```

The tests run offline: no network, GCS, Terra, gcloud or gsutil.

## Provenance

Extracted from the CLARUM project's bucket-cleanup tooling (Broad Institute, Talkowski
lab, 2026). Inspired by FISS `mop`. BSD-3-Clause; see [LICENSE](LICENSE).
