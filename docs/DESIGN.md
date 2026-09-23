# terra-scrub — design and porting contract

terra-scrub is the generalised, packaged form of the Terra bucket-cleanup machinery
built in `~/Work/clarum/src/scripts/` (Sept 2026). This document is the contract every
porting lane follows so independently written modules fit together.

## Source → module map (behaviour-preserving port)

| clarum source (`~/Work/clarum/src/scripts/`) | terra-scrub module | commands |
|---|---|---|
| `gcs_bucket_inventory.py` — `_authed_session`, `api_get` | `terra_scrub/http.py` | (library) |
| `gcs_bucket_inventory.py` — `human`, `parse_ts` ; `gcs_cleanup_candidates.py` — `tsv_escape` | `terra_scrub/util.py` | (library) |
| `gcs_bucket_inventory.py` — `cmd_snapshot`, `read_snapshot` | `terra_scrub/snapshot.py` | `snapshot` |
| `gcs_bucket_inventory.py` — `cmd_terra_context`, `_get_entities_paged`, `_unwrap_attr`, `_collect_gs_uris`; `gcs_estate_rescan.py::scope()`; `lookup_terra_bucket.py` | `terra_scrub/terra.py` | `context`, `workspaces`, `lookup` |
| `gcs_bucket_inventory.py` — `cmd_report`, `cmd_stale`, `cmd_dupes`, `_count` | `terra_scrub/analyze.py` | `report`, `stale`, `dupes` |
| `gcs_cleanup_candidates.py` | `terra_scrub/candidates.py` | `candidates` |
| `gcs_cleanup_apply.py` | `terra_scrub/plan.py` | `plan` |
| `approve_plan.sh` | `terra_scrub/approve.py` | `approve` |
| `verify_after_delete.py` | `terra_scrub/verify.py` | `verify` |
| `gcs_estate_rescan.py`, `gcs_estate_plan.py` | `terra_scrub/estate.py` | `estate scan`, `estate plan` |
| `test_gcs_toolkit.py` | `tests/` (pytest) | — |

"Behaviour-preserving" means: same rules, same guards (G1–G9), same gates, same
refusals, same output file formats (TSV/JSONL/plan.json/URI list/wrapper), same
hand-computed test expectations. Rename only what the table above renames.

## Module contract

Every command module exposes:

```python
def add_arguments(parser: argparse.ArgumentParser) -> None: ...
def run(args: argparse.Namespace) -> int: ...           # 0 = ok; raise SystemExit(msg) for refusals
def main(argv: list[str] | None = None) -> int: ...     # standalone: builds its own parser, calls run
```

Modules owning several commands (`terra.py`, `analyze.py`) expose per-command
`add_arguments_<cmd>` / `run_<cmd>` instead (see `cli.py::build_parser`).
`estate.py` exposes `add_arguments`/`run` and manages its own `scan`/`plan` subparsers.

Refusals: `sys.exit("REFUSING: ...")` / `SystemExit` with a message, exactly as the
originals do. Do not convert refusals into return codes without a message.

Keep public function names from the sources (`read_snapshot`, `api_get`, `human`,
`parse_ts`, `is_provenance`, `sidecar_of`, `keep_rank`, `load_reference_lists`,
`read_manifest`, `live_stat`, `manifest_md5`, …) so the ported tests map 1:1.

## Read-only by construction (the property, not the paragraph)

- The GET-only modules issue GET only: through `http.api_get`, or through
  `session.get` directly where a 404 is an expected answer (`plan.live_stat`,
  `verify`'s object and soft-deleted stats). Every call passes a timeout.
- GET-only modules: `http`, `util`, `snapshot`, `terra`, `analyze`, `candidates`,
  `plan`, `verify`, `runs`, `scan`, `status`. `tests/test_readonly_lint.py` parses their AST and fails on:
  any `.post/.put/.patch/.delete` call, `requests.<mutating verb>`, `os.remove/unlink/
  rmdir/rename-of-remote`, `shutil` import, `subprocess` import, any argparse flag
  matching `--execute|--apply|--delete|--rm|--force-delete`, and any `gsutil`/`gcloud`
  string outside the wrapper-template in `plan.py`.
- Exempt (and why): `clean.py` is the ONE module that runs a delete: it arms and executes the
  plan's wrapper only after a human types the plan id (see docs/SAFETY.md §7).
  `approve.py` writes ONE token into a local wrapper file;
  `estate.py` shells out to `terra-scrub` itself (`[sys.executable, "-m", "terra_scrub", ...]`)
  for per-bucket isolation and logs. Neither touches GCS.
- `verify.py` in clarum used `subprocess` to invoke the snapshot script for the
  after-listing. In terra-scrub call `snapshot.run(...)`/a helper IN-PROCESS so verify
  stays subprocess-free and inside the lint.

## Generalisation (what to strip from the clarum sources)

- No project names, namespaces, people, or `scratch/…` paths. `NAMESPACES` becomes
  `--namespace` (repeatable, required) on `estate scan`; `REF_LIST_FILE` becomes
  `--reference-lists FILE` (a file of paths) with the same refuse-if-missing semantics
  and the same `--no-reference-lists` escape hatch that prints a WARNING.
- `PY`/`INV_TOOL`/`APPLY_TOOL` constants go away: estate invokes
  `[sys.executable, "-m", "terra_scrub", <cmd>, ...]`.
- Run layout stays `<root>/inv/<key>.jsonl`, `<root>/inv/<key>.terra.json`,
  `<root>/cleanup/<key>/<bucket>.cleanup.tsv` etc. so `verify.guess_before` still works.
  `approve` takes `--root` (default `./runs`) instead of a hard-coded `scratch/bucket-cleanup`.
- Drop the `firecloud` dependency: every Terra call goes through `http.api_get` against
  `https://api.firecloud.org/api/...` (workspaces, workspace attrs, entities paged,
  submissions). Base URL overridable via `TERRA_API_URL` env var.
- Docstrings: keep the WHY of every guard/gate but replace citations like
  "docs/progress/092 §23" with "see docs/SAFETY.md § <guard id>". SAFETY.md carries the
  rationale (ported from the docstrings themselves — do not read the 158 KB progress log).
- `lookup_terra_bucket.py` contains a hard-coded bearer token. DO NOT copy it. `lookup`
  uses `http.api_get`.
- Wrapper script: keep `exec gcloud storage rm -I < <uris>` exactly (tested; same stdin
  URI-list format that `gsutil rm -I` / `fissfc mop` used; no `--continue-on-error`, so it
  stops at the first failure). No new deleter option in v0.1.

## Estate drivers: the ordering invariant is enforced, not documented

Per bucket, strictly: `snapshot` → `context` → `candidates --max-snapshot-age 1`.
The listing must precede the Terra read (a pointer written in between would look
unreferenced). `estate plan` captures a FRESH context per bucket AFTER the manifest's
`generated=` stamp, then runs `plan`. Resumable: skip buckets with a `__done__` snapshot,
a context and a candidate list. Never overwrite.

## Tests

- pytest, offline: no network, no GCS, no Terra, no gcloud/gsutil. Port every test in
  `test_gcs_toolkit.py` (30 tests) with its hand-computed expectations unchanged.
- The shared fixture (submissions aaaa1111…eeee5555, objects d1a…d13, p1…p9) becomes
  `tests/conftest.py` fixtures. `run_cli(script, *args)` → in-process `module.main([...])`
  with `capsys`/`redirect_stdout`; `apply_with_stub` → monkeypatch `plan.live_stat`.
- `test_readonly_lint` extends to every GET-only module listed above.
