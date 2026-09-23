# terra-scrub workflow

There are two ways to run terra-scrub. The single-bucket path runs every step by hand,
which is the best way to learn the tool. The estate path uses drivers to cover every
bucket in one or more Terra namespaces. Both paths end the same way: a person approves
each plan and runs its wrapper, then verifies the result.

Read [SAFETY.md](SAFETY.md) first. It explains why each step below is in the order it
is.

## Run layout

Keep each capture in its own root and never reuse an old one. The estate drivers
create this layout, and `verify` uses it to find the "before" snapshot.

```
<root>/
  scope.json                                   workspaces in scope (estate scan)
  inv/<key>.jsonl                              snapshot (object listing)
  inv/<key>.terra.json                         scan-time context
  inv/<key>.plan-time.terra.json               plan-time context
  cleanup/<key>/<bucket>.cleanup.tsv           delete list
  cleanup/<key>/<bucket>.cleanup.jsonl         delete list, full evidence
  cleanup/<key>/<bucket>.cleanup.protected.tsv LAST-COPY review list
  cleanup/<key>/<bucket>.cleanup.tsv.plan.json      plan summary
  cleanup/<key>/<bucket>.cleanup.tsv.plan.uris.txt  gcloud storage rm -I input (executable plans only)
  cleanup/<key>/<bucket>.cleanup.tsv.plan.sh        wrapper (CONFIRM gate; executable plans only)
  logs/                                        per-bucket logs (estate drivers)
  scan-status.json                             per-bucket outcome (estate scan)
```

`<key>` is the bucket name with `fc-secure-` shortened to `fc-`, cut to 22
characters (for example `fc-1234abcd-5678-90ef-`).

---

## A. One bucket

```bash
B=fc-1234abcd-5678-90ef-aaaa-bbbbccccdddd
K=${B:0:22}
R=runs/run-$(date -u +%F)
mkdir -p $R/inv $R/cleanup/$K
```

**0. Find the workspace** (if all you have is the bucket):

```bash
terra-scrub lookup $B                 # -> namespace / workspace name
NS=my-namespace WS=my-workspace
```

**1. Snapshot.** This must come first. It lists the bucket once, stores metadata only
and writes the file atomically with a `__done__` marker:

```bash
terra-scrub snapshot $B --out $R/inv/$K.jsonl
```

**2. Context.** This must come after the snapshot. It captures workspace and entity
attributes, every referenced `gs://` URI, and the submissions:

```bash
terra-scrub context $NS $WS --out $R/inv/$K.terra.json
```

**3. Look before you cut** (offline, optional but recommended):

```bash
terra-scrub report $R/inv/$K.jsonl --depth 3 --top 25          # where the bytes are
terra-scrub stale  $R/inv/$K.jsonl --terra $R/inv/$K.terra.json --days 180
terra-scrub dupes  $R/inv/$K.jsonl                              # md5 groups, cacheCopy
```

**4. Candidates.** This step is offline. It applies guards G1–G9 and writes the
delete list and the review list:

```bash
terra-scrub candidates --snapshot $R/inv/$K.jsonl --terra $R/inv/$K.terra.json \
    --out-dir $R/cleanup/$K --max-snapshot-age 1 \
    --reference-list delivered/sample_map.tsv          # repeatable; G8/G9
```

Read the PASS line and the summary. Open `$B.cleanup.tsv` and
`$B.cleanup.protected.tsv` and look through them.

**5. Plan.** Capture a **fresh** context now, after the list's `generated=` stamp,
then re-validate every row against live GCS:

```bash
terra-scrub context $NS $WS --out $R/inv/$K.plan-time.terra.json
terra-scrub plan --manifest $R/cleanup/$K/$B.cleanup.tsv \
    --terra $R/inv/$K.plan-time.terra.json --workers 16
```

Read the `objects_by_status` tally. `executable: true` together with
`PLAN_DELETE == rows` means every row held up against live GCS.

**6. Approve.** This step is for a person, not an assistant:

```bash
terra-scrub approve --root $R                # list pending plans
terra-scrub approve --root $R <plan_id>      # re-checks, then writes CONFIRM=<plan_id>
```

**7. Run the wrapper** (a person, or an assistant a person has explicitly told to):

```bash
bash $R/cleanup/$K/$B.cleanup.tsv.plan.sh
```

**8. Verify** right away:

```bash
terra-scrub verify --plan $R/cleanup/$K/$B.cleanup.tsv.plan.json
```

It exits 1 if any of checks 1–5 fails. Keep its output with the plan.

---

## B. The estate (many buckets)

**1. Scan.** For each workspace bucket in the namespaces: snapshot, then context, then
candidates. It is read-only and resumable.

```bash
cat > reference-lists.txt <<'EOF'
# one path per line; relative paths resolve against this file's directory
delivered/callset_v1/sample_map.tsv
delivered/callset_v1/gvcf_list.txt
EOF

terra-scrub estate scan --namespace my-ns --namespace my-ns-analysis \
    --reference-lists reference-lists.txt \
    --root runs/run-$(date -u +%F) --workers 4 > scan.log 2>&1
```

- The workspace list is re-queried live on every run and written to `scope.json`.
- It refuses to start without `--reference-lists`, or if any listed file is missing.
  `--no-reference-lists` runs without G8/G9 and prints a WARNING.
- `--aborted-last-copy-deletable` passes the owner's policy through to every
  candidates run. Use it only if the data owner has decided that.
- If the scan is interrupted, re-run the same command. Finished buckets are skipped
  and nothing gets overwritten. `fc-secure-` buckets are always skipped.
- It ends with `DONE N workspaces ... | not-clean: M` and lists the buckets that did
  not finish cleanly. Their logs are in `logs/scan-<key>.log`.

**2. Review.** Read each `cleanup/<key>/*.cleanup.tsv` and `*.protected.tsv`. Review
lists never get a wrapper. If a list looks wrong, fix the cause (a missing reference
list, the wrong prefix) and re-scan into a **new** root.

**3. Plan.** Do this within 24 h of the scan:

```bash
terra-scrub estate plan --root runs/run-2026-09-23 --workers 32 \
    [--only fc-1234,fc-5678] [--exclude fc-9abc] [--min-rows 1]
```

For each bucket with a non-empty delete list, it captures a fresh context
(`inv/<key>.plan-time.terra.json`) and then runs `plan`. It runs the cheapest buckets
first. Each bucket gets one line: `ok`, `CHECK` (the plan exists but not every row is
`PLAN_DELETE`), `REFUSED`, `CTX-RC` or `NO-WS`. At the end it prints a `plan_id` table
and exits 1 if any bucket failed to produce a plan. Logs go to `logs/plan-<key>.log`.

**4. Approve each plan** (a person, one plan at a time):

```bash
terra-scrub approve --root runs/run-2026-09-23 <plan_id>
```

**5. Run each wrapper, then verify it:**

```bash
bash runs/run-2026-09-23/cleanup/<key>/<bucket>.cleanup.tsv.plan.sh
terra-scrub verify --plan runs/run-2026-09-23/cleanup/<key>/<bucket>.cleanup.tsv.plan.json
```

---

## When a gate refuses

A refusal is the tool doing its job. Fix the cause. Do not work around the gate.

| refusal (where) | what it means | what to do |
|---|---|---|
| snapshot has no `__done__` marker / counts mismatch (`read_snapshot`) | truncated or partial listing | re-run `snapshot` to a new file |
| snapshot older than N days (`candidates --max-snapshot-age`) | the listing is too old to base a delete list on | re-capture snapshot, then context |
| context predates the snapshot (`candidates` capture-ordering guard) | the Terra read came before the listing, so a pointer written in between would look unreferenced | re-capture the **context** (after the listing) and regenerate |
| context names a different bucket / has no bucket key (`candidates`, `stale`, `plan`) | wrong workspace for this bucket | find the right workspace with `lookup` |
| outputs already exist (`candidates`) | refuses to overwrite | write to a new `--out-dir` / new root (`--force` only if you mean it) |
| `[EMPTY: ...]` on the PASS line (`candidates`) | nothing matched; often a mistyped `--prefix` | check the prefix and submission statuses |
| reference list file / listed path missing (`estate scan`) | G8/G9 would be off without anyone noticing | fix the path, or pass `--no-reference-lists` and say so |
| manifest older than 24 h (`plan`) | the bucket may have changed | re-scan (snapshot → context → candidates) and re-plan |
| header has no md5 column (`plan`) | an older generator; size-only checks are not proof | regenerate the list with the current `candidates` |
| context captured before the manifest (`plan`) | it would certify rather than check | capture the context again **now**, then plan |
| pointer check OFF / not executable (`plan`; no URI list or wrapper is written, and earlier ones are left untouched) | no `--terra` passed, or `--limit` / `--allow-stale-manifest` used | re-run with a fresh `--terra` and without those flags |
| many `SKIP_SIZE_CHANGED` / `SKIP_MD5_CHANGED` (`plan`) | the bucket changed since the listing | re-scan; do not plan from this list |
| `REFUSE_REFERENCED_NOW` / `REFUSE_SUBMISSION_*` (`plan`) | a new pointer, or a submission running again | leave those rows; re-scan once the workspace is quiet |
| no wrapper written (`plan`) | a review (LAST-COPY) list | by design; it is for a person to read. Change the owner policy (`--aborted-last-copy-deletable`) and regenerate if appropriate |
| manifest older than 24 h (`approve`) | the bucket may have changed since the listing | re-scan (snapshot → context → candidates), then re-plan; re-planning alone is refused by `plan`'s own 24 h gate |
| plan older than 24 h, manifest still fresh (`approve`) | the live re-check behind the plan has expired | re-run `estate plan` / `plan` |
| URI list sha256 changed (`approve`, or the armed wrapper) | the delete set changed after planning | re-plan; never hand-edit the URI list |
| already armed (`approve`) | the token is already written | run the wrapper, or re-plan to get a new plan_id |
| `REFUSED` / `CTX-RC` / `NO-WS` (`estate plan`) | see `logs/plan-<key>.log` | handle the underlying refusal per the rows above |
| verify check fails | something other than the planned set changed, or a keeper is missing | stop. Check soft-deleted copies (check 5) while still inside the retention window |
