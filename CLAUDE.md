# terra-scrub — repo conventions

Read-only-by-construction toolkit for inventorying Terra workspace buckets and building
auditable delete PLANS. No module in `src/terra_scrub/` calls a delete API. The one
exception to "delete-free" is `clean.py`: it runs a plan's `*.plan.sh` wrapper after a
person types the plan id. See `docs/SAFETY.md` for the property and `docs/DESIGN.md`
for the module map.

## Rules for the agent

- **Never arm or run a delete plan.** `terra-scrub approve`, `terra-scrub clean` (with or
  without `--confirm`/`--dry-run`) and `*.plan.sh` are human-only steps;
  `.claude/settings.json` denies them. `terra-scrub scan` and `terra-scrub status` are
  fine. If a plan needs cleaning, tell the user the plan_id and the
  `terra-scrub clean <ns>/<ws>` command, and stop.
- **`clean.py` stays a thin runner.** It may import `subprocess` for its one
  `["bash", <wrapper>]` call, but must never name `gsutil`/`gcloud` or delete anything
  itself; `tests/test_clean.py` checks that.
- **Keep the GET-only modules GET-only.** `tests/test_readonly_lint.py` fails on any mutating
  HTTP verb, `subprocess`/`shutil` import, `--execute`-style flag, or `gsutil`/`gcloud` string
  outside the wrapper template in `plan.py`. Do not weaken the lint to make a change pass.
- **Guards G1–G10, plan gates and the ordering invariant are behaviour, not documentation.**
  Changing one means changing its test and its `docs/SAFETY.md` section in the same change.
- Hand-computed test expectations in `tests/conftest.py` are the contract. Do not re-derive
  them with the code under test.

## Dev loop

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q          # offline; no network, no GCS, no Terra
.venv/bin/ruff check src tests
```

Run artifacts from the low-level commands go under `runs/` (gitignored). `scan` /
`status` / `clean` keep theirs under `~/.terra-scrub/runs/<ns>/<ws>/<stamp>/`
(`$TERRA_SCRUB_HOME`; layout in `src/terra_scrub/runs.py`). Tests must set
`TERRA_SCRUB_HOME` or `--home` to a tmp dir and must never let a real wrapper run.
