# terra-scrub — repo conventions

Read-only-by-construction toolkit for inventorying Terra workspace buckets and building
auditable delete PLANS. Nothing in `src/terra_scrub/` deletes a GCS object; see
`docs/SAFETY.md` for the property and `docs/DESIGN.md` for the module map.

## Rules for the agent

- **Never arm or run a delete plan.** `terra-scrub approve` and `*.plan.sh` are human-only
  steps; `.claude/settings.json` denies them. If a plan needs arming, tell the user the
  plan_id and stop.
- **Keep the GET-only modules GET-only.** `tests/test_readonly_lint.py` fails on any mutating
  HTTP verb, `subprocess`/`shutil` import, `--execute`-style flag, or `gsutil`/`gcloud` string
  outside the wrapper template in `plan.py`. Do not weaken the lint to make a change pass.
- **Guards G1–G9, plan gates and the ordering invariant are behaviour, not documentation.**
  Changing one means changing its test and its `docs/SAFETY.md` section in the same change.
- Hand-computed test expectations in `tests/conftest.py` are the contract. Do not re-derive
  them with the code under test.

## Dev loop

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q          # offline; no network, no GCS, no Terra
.venv/bin/ruff check src tests
```

Run artifacts (snapshots, contexts, manifests, plans) go under `runs/` (gitignored).
