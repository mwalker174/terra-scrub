# terra-scrub safety argument

This document explains why terra-scrub can be pointed at a production Terra bucket
without putting that bucket at risk, and which steps a person still has to take before
anything is deleted. Each rule comes with the failure that motivated it. If you need to
relax a rule, read that rule's reason first.

Most Terra workspace buckets are **not versioned**, so a deleted object stays
recoverable only for the bucket's soft-delete window (see
[Verify](#verify-checks-15-and-the-soft-delete-window)). Every design choice below
follows from treating a delete as irreversible.

---

## 1. Read-only by construction

"Read-only" here is a property the code has, checked by a test. It is not a promise in
the README.

**Precisely:** every module except `clean` is delete-free. None of them calls a delete
API or runs the delete wrapper. `clean` is the one module that runs a delete, and it
does so only by running the plan's own wrapper (`bash <manifest>.plan.sh`). It runs
the wrapper only after it has repeated every re-check `approve` performs and a person
has typed the plan id at its prompt (or passed it as `--confirm`). The deny rules in
§7 stop AI agents from running `clean` at all.

**GET-only modules:** `http`, `util`, `snapshot`, `terra`, `analyze`, `candidates`,
`plan`, `verify`, `runs`, `scan`, `status`.

- **GET only.** Every GCS JSON-API and Terra API call in those modules is a `GET`:
  through `http.api_get` (bucket listing, workspaces, attributes, paged entities,
  submissions), or through `session.get` directly where a 404 is an expected answer
  rather than an error: `plan.live_stat` (object stat) and `verify`'s object and
  soft-deleted stats. Every one of them passes a timeout.
- **The lint.** `tests/test_readonly_lint.py` parses the AST of every GET-only module
  and fails the build on any of:
  - a `.post` / `.put` / `.patch` / `.delete` call, or `requests.<mutating verb>`
  - `os.remove` / `unlink` / `rmdir`, or an import of `shutil` or `subprocess`
  - an argparse flag matching `--execute | --apply | --delete | --rm | --force-delete`
  - any `gsutil` / `gcloud` string, except the one wrapper template in `plan.py`,
    which must name exactly one `gcloud storage rm` and no `gsutil`
- **"No delete verb."** `plan` does not have a dry-run mode that some flag could
  switch off. The file has no code path that deletes. Its outputs are a JSON summary, a
  URI list and a shell wrapper, and the wrapper holds the only deleter invocation in
  the package: `exec gcloud storage rm -I < <uris>`, the same stdin URI-list format
  that `gsutil rm -I` / FISS `mop` used.
  Only `clean` runs that wrapper, on a person's typed confirmation. No other module
  runs it.
- **Exempt modules, and why:**
  - `clean` runs one subprocess, `["bash", <run>/.../<manifest>.plan.sh]`, the
    wrapper that `plan` wrote. It arms the wrapper through `approve.arm` and verifies
    in-process through `verify`. The source names no cloud CLI (a test in
    `tests/test_clean.py` checks there is no `gsutil`/`gcloud` string and exactly one
    subprocess call, whose argv starts with `bash`). The wrapper does the delete.
  - `approve` writes a single token (`CONFIRM=<plan_id>`) into a local wrapper file.
    It makes no network calls.
  - `estate` runs terra-scrub's own commands as subprocesses
    (`[sys.executable, "-m", "terra_scrub", ...]`) so each bucket gets process
    isolation and its own log. It never calls gsutil or gcloud. Everything it runs is a
    GET-only command.
  - `verify` re-lists the bucket **in-process** through `snapshot`, which keeps it
    inside the lint.

---

## 2. The ordering invariant

A snapshot (the object listing) and a context (the Terra side: attributes, referenced
URIs, submissions) come from two separate reads, and **the order of those reads
matters**.

1. **Take the listing before the context.** Suppose the context is read first. A
   workspace or entity attribute written between the two reads is then invisible to
   the context, while the object it points to is in the listing. That object looks
   unreferenced, and an unreferenced object under a terminal submission is exactly
   what becomes a delete candidate. `candidates` enforces the order with its
   **capture-ordering guard**: it refuses any context whose `captured_utc` is earlier
   than the snapshot's `snapshot_utc`. `--max-context-skew H` tolerates up to H hours
   of inversion, and `--allow-stale-context` downgrades the refusal to a warning. That
   flag exists for reproducing old runs and must never be used for a list you will
   execute.
2. **Capture a fresh context at plan time.** The context a candidate list was built
   from is always older than the list. A pointer created after the list was generated
   is the one class of bug a stale list cannot see. `plan` refuses any context whose
   `captured_utc` is earlier than the manifest's `generated=` stamp, because a context
   that old would certify the list instead of checking it.

`estate scan` runs snapshot → context → candidates per bucket, in that order.
`estate plan` captures a new context for each bucket immediately before planning it.
Both drivers exist because keeping this ordering correct by hand across dozens of
buckets is the most likely way for a gate to be worked around.

---

## 3. Candidate rules and keep-rank

`candidates` works offline from a snapshot and one or more contexts. It makes no
network calls. A file can match both rules, in which case the reasons are combined.

**ABORTED_SUBMISSION.** Every object under `submissions/<id>/` whose submission is
`Aborted` or `Failed`. Each row is marked `superseded=True` when its md5 also appears
outside that submission, and `superseded=False` when it does not. A `False` row is the
only copy of that content in the bucket: usually regenerable, but flagged for review.

**EXACT_DUPLICATE.** Every object under the candidate prefix whose md5 appears more
than once, except one canonical kept copy per md5 group. The kept copy is the one with
the lowest rank. Ties go to the newest `updated` time, then to the name.

| rank | where the copy lives |
|---|---|
| 0 | outside the candidate prefix (deliverables) |
| 1 | under a Done submission, non-cacheCopy |
| 2 | under a Done submission, cacheCopy |
| 3 | under an in-flight or unknown submission |
| 4 | under a dead (Aborted/Failed) submission, non-cacheCopy |
| 5 | under a dead submission, cacheCopy |

So a deliverable outside `submissions/` always wins, and a copy from a successful run
beats a call-cache copy or a copy from a dead run.

---

## 4. Guards G1–G9

All guards are checked before any output is written. If any check fails, the run
aborts.

**G1: prefix and terminal status only.** Candidates must sit under `--prefix` (default
`submissions/`), and only under submissions in a terminal state: Done for the duplicate
rule, Aborted/Failed for the aborted rule. In-flight submissions are never touched. The
prefix is normalised to end in `/`, so `submissions/abc` cannot also sweep in
`submissions/abcX/`.
*Why:* a running or retrying workflow may still read its own intermediates, and a
prefix typo must not widen the scope.

**G2: every md5 group keeps at least one copy.** If a list would delete every copy of
some content (including a unique-md5 object under an aborted submission), the canonical
copy is taken off the delete list and moved to `.protected.tsv`, the review list. That
review list is the only thing a human may override. Objects with no `md5Hash` under a
dead submission go to the same review list (`NO_MD5_PROTECTED`), because nobody can
tell whether they are unique.
*Why:* deduplication must never turn into deletion of the last copy.

**G3: nothing referenced by Terra.** No candidate may be referenced by any workspace or
entity attribute in any of the `--terra` contexts, taken together.
*Why:* an attribute is an issued pointer. Somebody's data table or deliverable depends
on it.

**G4: no md5, no duplicate.** An object without an md5 is never listed as
EXACT_DUPLICATE.
*Why:* "duplicate" has to mean identical bytes. Matching names or sizes do not prove
that.

**G5: Cromwell provenance stays.** `stdout`, `stderr`, `output`, `rc`,
`memory_retry_rc`, `exec.sh`, `script`, `gcs_{localization,delocalization,transfer}.sh`
and anything ending in `.log` or `-rc.txt` are kept off both lists. They are a tiny
share of a typical bucket, and they are the only record of what each shard actually
ran. The per-call return codes are how you diagnose a failed pipeline weeks later. FISS
`mop`'s `can_delete()` has always kept these files too. Opt out with
`--include-provenance`.

**G6: zero-byte objects stay.** Deleting them frees nothing. They also all share the
empty-file md5, which breaks the duplicate rule: `x.gc_bias.pdf` would get deleted
because `x.bamout.bam` is also empty, and that is not deduplication. An empty named
output also tells you that a task ran and produced nothing. Delete it and that case
looks the same as a task that never ran. Opt out with `--include-zero-byte`.

**G7: sidecars stay with their data.** A sidecar (`.bai .crai .csi .tbi .idx .md5 .sbi
.fai`) is never deleted while its data file is still in the bucket, so a kept copy
never loses its own index or checksum.
*Why:* two attempts of the same shard have byte-identical `.md5` sidecars. Without G7
the duplicate rule deletes one of them and can leave the surviving attempt's BAM with no
checksum. Opt out with `--allow-index-split`.

**G8: reference lists protect URIs.** Any object whose URI appears inside a file passed
with `--reference-list` is kept off both lists. Typical reference lists are a
joint-calling sample map, a gVCF list or a delivery manifest.
*Why:* G3 only sees attributes. It cannot see a file whose contents point at objects. A
delivered callset's input map can name outputs that live inside an aborted submission,
and without G8 those outputs survive only by luck (for example, because G2 happened to
pick them as md5-group keepers).

**G9: deliverable-grade products are never promoted.** A deliverable-grade file
(`.g.vcf.gz`, `.cram`, `.vcf.gz` and their indexes; `.bam` is not included) is never
moved off the review list by `--aborted-last-copy-deletable`. This holds whether or not
a reference list names its sample. Column-1 sample names in reference lists protect
those samples' final products as well.
*Why:* name matching alone misses things. A callset name is not a sample name, so
last-copy shards of a delivered callset can slip past a sample-name check.

`estate scan` requires `--reference-lists FILE`, a file listing reference-list paths.
It refuses to start if that file or any path in it is missing: a reference list that
silently resolves to nothing would switch G8/G9 off across the whole estate. To run
without them, pass `--no-reference-lists`. It prints a WARNING, and you should state it
in your write-up.

`--aborted-last-copy-deletable` is an **owner policy** you opt into per run. It moves
LAST_COPY rows under Aborted/Failed submissions to the delete list with reason
`ABORTED_LAST_COPY`. Rows without an md5 are never promoted, and G8/G9 still apply.

---

## 5. Operator protections in `candidates`

- It refuses to overwrite existing outputs unless you pass `--force`. (`estate scan`
  never passes `--force`. It skips finished buckets instead.)
- `--max-snapshot-age N` refuses a snapshot older than N days. `estate scan` uses 1.
- `read_snapshot` requires the snapshot's `__done__` integrity marker and checks
  `n_objects` / `total_bytes` against the file's contents. A truncated or hand-edited
  snapshot is refused rather than analysed.
- A candidate set and protected set that are both empty get flagged on the PASS line
  (`[EMPTY: ...]`), so a mistyped prefix cannot pass for a healthy run.
- Every run prints the snapshot and context capture times and the snapshot age.

Outputs: `<bucket>.cleanup.tsv` (one row per candidate, name first),
`<bucket>.cleanup.jsonl` (the full evidence for each object) and
`<bucket>.cleanup.protected.tsv` (LAST-COPY rows held back for human review).

---

## 6. Plan gates and verdicts

A manifest is a claim about a non-versioned bucket at one point in time, so none of it
can be trusted at delete time. `plan` re-checks every row against **live GCS**, and
against **live pointers** when you give it a fresh context. It then writes a plan for a
person to read.

**Whole-manifest gates (refusals):**

| gate | why |
|---|---|
| **stale manifest**: older than `--max-manifest-age-hours` (24 h) | the bucket may have changed since. Re-capture and regenerate. `--allow-stale-manifest` gives a review-only plan that is never executable |
| **no md5 column** in the header (an older generator) | re-validating on size alone cannot tell "the audited bytes" from "other bytes of the same length". No opt-out |
| **foreign context**: a `--terra` context names a different bucket | mixing buckets is never correct |
| **stale context**: a context captured before the manifest's `generated=` stamp | it cannot see pointers written in between, so it would certify the list rather than check it |
| **no parseable `captured_utc`** on a context | freshness cannot be established |

**Per-row verdicts:**

| verdict | meaning / why |
|---|---|
| `PLAN_DELETE` | live object matches the manifest on size and md5, is unreferenced, and its submission is terminal |
| `SKIP_ALREADY_ABSENT` | **live 404**. The object is already gone, and deleting it again would do nothing |
| `SKIP_SIZE_CHANGED` | **live size differs**. The bytes behind that name are not the bytes that were audited. Manifests are keyed by name (the generation must never be the delete key), so they need a content tie-break |
| `SKIP_MD5_CHANGED` | **live md5 differs**. The same problem, caught through content instead of length |
| `SKIP_NO_DIGEST` / `SKIP_MD5_UNVERIFIABLE` | **no digest, no plan**. A row whose md5 is `-`, or one that cannot be compared, is never planned. A matching length is not evidence that the bytes are the same |
| `REFUSE_REFERENCED_NOW` | **referenced now**. A pointer created after the capture, which only a fresh context can see |
| `REFUSE_SUBMISSION_NOT_TERMINAL` / `REFUSE_SUBMISSION_UNKNOWN` | **in flight**. Resubmission and retry happen. A submission that is running now, or that the fresh context does not know about, stops the row |
| `SKIP_FOREIGN_BUCKET` / `SKIP_BAD_NAME` / `SKIP_BAD_SIZE_FIELD` | **foreign bucket** or a malformed row. A hand-merged or sed-mangled TSV must not become executable |
| `SKIP_OUTSIDE_PREFIX` | **outside `--prefix`**. The audited scope was `submissions/`, so anything else in a hand-edited file is a red flag, not a target |

**Plan-level outcomes:**

- A **review list** (`*.protected.tsv`, kind LAST-COPY) produces a plan only: **no URI
  list and no wrapper**. A review queue must not come with a way to delete, and a
  one-URI-per-line file is already the deleter's stdin.
- No `--terra` context means "pointer check OFF", and the plan is marked **not
  executable**. So is a `--limit` (partial) plan or one produced with
  `--allow-stale-manifest`. A plan that is not executable writes **neither** the URI
  list nor the wrapper, and it never overwrites the ones an earlier executable plan of
  the same manifest left behind. `plan_id` is the manifest's hash, so without this a
  non-executable re-plan could swap an unchecked or partial list in under a wrapper
  that was already armed. `plan.json` records what was written (`uris_written`,
  `commands_written`) and which earlier files were left untouched (`left_untouched`).
- `plan.json` records `plan_id`, `executable`, `not_executable_reason`,
  `objects_by_status`, `plan_bytes`, `plan_objects`, the manifest's `generated=`, and
  the sha256 of the URI list (computed even when the list is withheld).

Outputs next to the manifest: `<manifest>.plan.json` always; for an executable plan
only, `<manifest>.plan.uris.txt` (one `gs://` URI per line, the `gcloud storage rm -I` stdin format) and `<manifest>.plan.sh`
(the wrapper, with `CONFIRM=""`).

---

## 7. The two-man rule

Deleting is a deliberate act that the owner approves. There are two ways to get from
a plan to a delete. Both go through the same re-checks and the same wrapper.

**Path A: `terra-scrub clean <ns>/<ws>` (a person at a terminal).** `scan` builds the
plan and `clean` finishes it:

1. It takes the latest run for the workspace (`~/.terra-scrub/runs/<ns>/<ws>/<stamp>/`)
   and refuses unless `run.json` says `outcome: planned` and the run has a `plan.json`.
2. It runs `approve.recheck` on that plan, the same function `approve` uses (the list
   is below). Any failure is a refusal that tells you to re-run `terra-scrub scan`. A
   wrapper that is already armed with this plan's id (someone ran `approve` by hand)
   is accepted and not re-armed. A wrapper armed with any other token is refused.
3. It prints the bucket, the plan id, the object count and bytes, the manifest and plan
   ages, the soft-delete caveat and the path to the manifest TSV for review.
4. The person types the plan id at the prompt `Type the plan id to delete N objects
   (X GiB) from gs://<bucket>, or anything else to abort:`, or passes
   `--confirm <plan_id>`. Anything else aborts with exit 1 and arms nothing. With no
   TTY and no `--confirm`, it refuses. `--dry-run` stops before arming and writes
   nothing.
5. It writes `CONFIRM=<plan_id>` through `approve.arm`, runs `bash <wrapper>` (the
   wrapper repeats its own checks, step 3 below), and then runs `verify` in-process.
   Output goes to `logs/clean.log` and `logs/verify.log`. `run.json` records
   `armed_utc`, `clean_rc`, `cleaned_utc`, `verify_rc`, `verified_utc` and
   `outcome` (`cleaned` or `cleaned-verify-failed`). If the wrapper's own self-check
   refuses, `clean` exits 1 without verifying. Any other non-zero exit still goes to
   `verify`, because verify is what shows what a partial run removed.

Typing the plan id plays the part of `approve`. It is done by a person and is denied to
AI agents (the rules below).

**Path B: `plan` / `approve` / wrapper (split roles, or an agent does the prep).** Three
separate actions:

1. **plan** (anyone, including an AI assistant): produces the wrapper. The wrapper
   refuses to run while `CONFIRM` is empty.
2. **approve** (a person): `terra-scrub approve <plan_id>`. Typing the 12-character
   `plan_id` is the approval. It has to match exactly one plan on disk, so nobody can
   approve "whatever plan happens to be newest". Before writing the token, `approve`
   (and `clean`, through the same function) checks each of the following and refuses
   if any fails:
   - the plan is `manifest_kind=delete` and `executable=true`
   - the manifest is under 24 h old **and** the plan is under 24 h old, both computed
     *now* from the stamps. `plan.json`'s recorded `manifest_age_hours` is frozen at
     generation time, and trusting it would arm a stale plan
   - the URI list's current sha256 equals the one recorded in `plan.json`, and its line
     count equals `plan_objects`, so the token certifies one specific delete set, byte
     for byte
   - the wrapper exists and its `CONFIRM` is still empty. `approve` never re-arms an
     armed plan
3. **run** (a person, or an assistant a person has told to run it): `bash <...>.plan.sh`.
   Behind the `CONFIRM` gate, the wrapper repeats the URI-list check itself before it
   deletes: the list's sha256 (via `sha256sum`, or `shasum -a 256`; it refuses if
   neither is on `PATH`) must equal the value recorded at plan time, and its line
   count must equal `plan_objects`. A list changed after arming is refused, not
   deleted. Check the bucket's soft-delete policy first (§8): the window may be 0.
   The wrapper runs `gcloud storage rm -I` without `--continue-on-error`, so it stops
   at the first object that fails; `verify` (§8) then shows exactly what a partial run
   removed, and you re-plan (inside the 24 h window) rather than retry blindly.

`terra-scrub approve` with no plan id lists pending plans with their current age,
executable flag and armed state.

### Recommended deny rules for AI coding assistants

If an AI assistant works in the same checkout, stop it from arming or running a wrapper
itself, including through `clean`. This repository's `.claude/settings.json` ships
these rules; for another checkout, add them to its `.claude/settings.json` or to
`~/.claude/settings.json`:

```json
{
  "permissions": {
    "deny": [
      "Edit(**/*.plan.sh)",
      "Write(**/*.plan.sh)",
      "Bash(bash **/*.plan.sh*)",
      "Bash(sh **/*.plan.sh*)",
      "Bash(terra-scrub approve *)",
      "Bash(* terra-scrub approve *)",
      "Bash(* -m terra_scrub approve *)",
      "Bash(terra-scrub clean *)",
      "Bash(* terra-scrub clean *)",
      "Bash(* -m terra_scrub clean *)",
      "Bash(gsutil rm *)",
      "Bash(gsutil -m rm *)",
      "Bash(gcloud storage rm *)",
      "Bash(gcloud storage objects delete *)"
    ]
  }
}
```

With these rules the assistant can run `scan` and `status`, and build and explain a
plan, but it cannot approve or clean. A plan gets armed only when a person types its
id, either to `approve` or at `clean`'s prompt.

---

## 8. Verify checks 1–5 and the soft-delete window

Run `terra-scrub verify --plan <...>.plan.json` right after a wrapper returns. It is
GET-only and exits 1 if any check fails.

The planned set is the plan's URI list (what the wrapper fed the deleter) when
`plan.json` records one, falling back to the manifest rows; keepers and md5s come from
the manifest. The after-listing is written to `--after-snapshot`, by default the before
snapshot's path with its extension replaced by `.after.jsonl`. `verify` refuses
(before any GCS call) when the after path resolves to the before snapshot, because
re-listing there would destroy the only record of what the bucket held before and
make check 3 compare the bucket with itself.

1. **Is every planned object gone?**
2. **Does every keeper still exist, with the md5 the manifest recorded for the row it
   was keeping?** "A copy survives" only holds if that copy still matches.
3. **Did the bucket lose exactly the planned set and nothing more?** This is the
   check that carries the most weight. It needs a full before/after re-listing (the
   before snapshot is found from the run layout `<root>/inv/<key>.jsonl`), because a
   spot check cannot see collateral loss.
4. **Is every Terra-referenced object that was live before still live?** A reference
   that was already dangling before the delete is written to a sidecar
   (`<manifest>.dangling_refs.txt`). It is not counted as a failure and is never
   changed.
5. **Can the deletion still be undone?** For each deleted object: is there a
   soft-deleted copy with a matching md5, and until when?

**Caveat about soft delete.** GCS soft delete keeps deleted objects for the bucket's
retention window (7 days by default, and configurable, including to zero). Treat it as
a durability net, not a free undo. Restoring takes deliberate effort per object,
soft-deleted bytes are still billed during the window, and once the window closes the
objects are gone for good. Check 5 tells you how long the net lasts. Do not rely on it
to fix a plan you did not read.

---

## 9. What terra-scrub deliberately does not do

- **It never deletes on its own.** No module calls a delete API. The one
  `gcloud storage rm` is inside a wrapper, and the wrapper runs only when a person
  runs it by hand after `approve`, or when `clean` runs it after the person types the
  plan id.
- It never moves, renames, re-copies or "tidies" objects. A path written into a Terra
  attribute or a deposit is an issued pointer that someone already holds.
- It never writes to Terra: no attribute edits and no entity or submission changes.
- It never keys a delete on the object generation. Manifests are keyed by name and
  checked against content.
- It never plans from a stale manifest, a manifest without md5, or a context older than
  the manifest. Nothing reuses an earlier list. You regenerate.
- It never gives a review (LAST-COPY) list a wrapper.
- It never touches `fc-secure-` buckets in `estate scan`. They are skipped.
- It has no `--execute` flag, and v0.1 has no deleter option other than the `gcloud storage rm`
  wrapper. `clean --confirm <plan_id>` is not an execute flag: its value has to be the
  plan id, which you only get by reading the plan.
