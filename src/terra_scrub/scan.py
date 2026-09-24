"""scan -- from a workspace name to a reviewed delete PLAN in one command (read-only).

    terra-scrub scan <namespace>/<workspace> [<namespace>/<workspace> ...]

For each target, scan runs the whole read-only half of the workflow, in this order:

  1. resolve   look up the workspace's bucket (Terra GET)
  2. snapshot  list every object in the bucket              -> inv/<key>.jsonl
  3. context   capture Terra attributes, references and submissions, AFTER the
               listing                                      -> inv/<key>.terra.json
  4. candidates apply guards G1-G10 offline and write the delete list and the
               last-copy review list                        -> cleanup/<key>/<bucket>.cleanup*.tsv
  5. plan-time context: a FRESH context captured after the delete list's
               generated= stamp                             -> inv/<key>.plan-time.terra.json
  6. plan      re-check every delete row against live GCS and the fresh context
                                                            -> <bucket>.cleanup.tsv.plan.json/.uris.txt/.sh

The order is the ordering invariant of docs/SAFETY.md §2, enforced here by
construction: the listing precedes the Terra read, and the plan's context
postdates the manifest. Nothing is ever deleted and nothing is armed: the output
is a plan for a person to review and, if they agree, run with
`terra-scrub clean <namespace>/<workspace>`.

Everything for one run lives under one directory (see `terra_scrub.runs`):

    $TERRA_SCRUB_HOME/runs/<namespace>/<workspace>/<UTC stamp>/     (default ~/.terra-scrub)

The low-level tools' own output goes to `logs/<step>.log` in that directory;
`run.json` records what the run was and how it ended. The terminal gets one
summary block per target: bucket, snapshot size, delete candidates, review
list, plan id, whether the plan is executable, and what to do next.

Outcomes (recorded as run.json `outcome`):
  planned            an executable plan exists: next is `terra-scrub clean`
  not_executable     a plan was written but plan.json says it cannot be run
  review_only        nothing is deletable, but the last-copy review list has rows
  nothing_to_delete  both lists are empty
  candidates_only    --no-plan: stopped after the candidate lists
  refused            a guard refused (the message is in run.json `refusal`)
  failed             an unexpected error (run.json `error`, and the step logs)

Candidate options (--reference-list, --aborted-last-copy-deletable, --prefix,
--include-provenance, --include-zero-byte, --allow-index-split) are passed through
to `terra-scrub candidates` unchanged; see `terra-scrub candidates --help`.

Exit status: 0 when every target ended normally (planned, not_executable,
review_only, nothing_to_delete, candidates_only); 1 if any target was refused or failed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime

from terra_scrub import candidates, http, plan, runs, snapshot, terra
from terra_scrub.util import human

OK_OUTCOMES = {"planned", "not_executable", "review_only", "nothing_to_delete",
               "candidates_only"}


def _now():
    return datetime.now(UTC).isoformat()


class _Refused(Exception):
    """A step refused (SystemExit with a message). Carries the step and message."""

    def __init__(self, step, msg):
        super().__init__(msg)
        self.step, self.msg = step, msg


def _refusal_text(code, step, log):
    if isinstance(code, str):
        return code
    return f"{step} exited with status {code} (see {log})"


def _captured(step, log_path, fn, *a, **kw):
    """Run fn with stdout+stderr appended to log_path. A SystemExit with a non-zero
    code becomes _Refused(step, message); the message is also written to the log."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as lf:
        try:
            with contextlib.redirect_stdout(lf), contextlib.redirect_stderr(lf):
                return fn(*a, **kw)
        except SystemExit as e:
            if e.code in (None, 0):
                return 0
            msg = _refusal_text(e.code, step, log_path)
            lf.write(f"\n{msg}\n")
            raise _Refused(step, msg) from None


def resolve_bucket(ns, ws, session=None):
    """The workspace's bucketName (Terra GET). Refuses if the workspace has none."""
    try:
        r = http.api_get(terra._ws_url(ns, ws), params={"fields": "workspace.bucketName"},
                         session=session)
        bucket = (r.json().get("workspace") or {}).get("bucketName")
    except Exception as e:  # noqa: BLE001 -- HTTPError (403/404), JSON, transport
        raise _Refused("resolve", f"REFUSING: could not look up workspace {ns}/{ws} "
                                  f"({type(e).__name__}: {e}) -- check the name and that "
                                  f"you can read it (`terra-scrub workspaces --namespace {ns}`)") from None
    if not bucket:
        raise _Refused("resolve", f"REFUSING: workspace {ns}/{ws} has no bucketName -- "
                                  f"nothing to scan")
    return bucket


def plan_prefix(prefixes):
    """The single --prefix to hand `plan` for the candidate prefixes (plan refuses
    anything outside it). None -> plan's own default (submissions/)."""
    if not prefixes:
        return None
    norm = [p if (not p or p.endswith("/")) else p + "/" for p in prefixes]
    if len(norm) == 1:
        return norm[0]
    common = os.path.commonprefix(norm)
    return common[:common.rfind("/") + 1]


def candidate_argv(r, args):
    argv = ["--snapshot", r.snapshot, "--terra", r.context, "--out-dir", r.cleanup_dir,
            "--max-snapshot-age", "1"]
    for f in args.reference_list or []:
        argv += ["--reference-list", f]
    for p in args.prefix or []:
        argv += ["--prefix", p]
    for flag in ("aborted_last_copy_deletable", "include_provenance", "include_zero_byte",
                 "allow_index_split", "include_logs", "include_done_logs"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    if args.logs_older_than:
        argv += ["--logs-older-than", f"{args.logs_older_than:g}"]
    return argv


def _tally(path):
    """(rows, bytes) of a candidates TSV via plan.read_manifest; (0, 0) if absent."""
    if not os.path.exists(path):
        return 0, 0
    _meta, rows = plan.read_manifest(path)
    return len(rows), sum(int(x.get("size_bytes") or 0) for x in rows)


def scan_one(ns, ws, args, say):
    """Run every step for one workspace. Returns the final run.json dict."""
    home_dir = args.home
    r = runs.new_run(ns, ws, home_dir)
    meta = {}
    step = "resolve"
    try:
        sess = http._authed_session()
        say(f"{ns}/{ws}: resolving bucket")
        bucket = resolve_bucket(ns, ws, session=sess)
        r = r.with_bucket(bucket)
        for d in (r.inv_dir, r.cleanup_dir, r.logs_dir):
            os.makedirs(d, exist_ok=True)
        r.write_meta(started_utc=_now(), step="snapshot", target=f"{ns}/{ws}")

        # 1. listing BEFORE the Terra read (docs/SAFETY.md §2)
        step = "snapshot"
        say(f"{ns}/{ws}: snapshot gs://{bucket} (progress: {r.log('snapshot')})")
        n_obj, n_bytes = _captured(step, r.log(step), snapshot.snapshot_bucket, bucket,
                                   r.snapshot, page_size=args.page_size, session=sess)
        r.write_meta(step="context", snapshot_objects=n_obj, snapshot_bytes=n_bytes)

        # 2. context AFTER the listing
        step = "context"
        say(f"{ns}/{ws}: capturing Terra context")
        _captured(step, r.log(step), terra.capture_context, ns, ws, r.context, session=sess)
        r.write_meta(step="candidates")

        # 3. candidates (offline, guards G1-G10)
        step = "candidates"
        say(f"{ns}/{ws}: building candidate lists")
        _captured(step, r.log(step), candidates.main, candidate_argv(r, args))
        del_n, del_b = _tally(r.manifest)
        prot_n, prot_b = _tally(r.protected)
        meta = r.write_meta(candidate_objects=del_n, candidate_bytes=del_b,
                            protected_rows=prot_n, protected_bytes=prot_b)

        if args.no_plan:
            outcome = "candidates_only"
        elif del_n == 0:
            outcome = "review_only" if prot_n else "nothing_to_delete"
        else:
            # 4. fresh context AFTER the manifest's generated= stamp, then plan
            step = "plan-context"
            r.write_meta(step=step)
            say(f"{ns}/{ws}: capturing plan-time Terra context")
            _captured(step, r.log(step), terra.capture_context, ns, ws, r.plan_context,
                      session=sess)
            step = "plan"
            r.write_meta(step=step)
            say(f"{ns}/{ws}: live re-check of {del_n:,} rows ({args.workers} workers)")
            pargv = ["--manifest", r.manifest, "--terra", r.plan_context,
                     "--workers", str(args.workers)]
            pp = plan_prefix(args.prefix)
            if pp is not None:
                pargv += ["--prefix", pp]
            _captured(step, r.log(step), plan.main, pargv)
            with open(r.plan_json) as fh:
                p = json.load(fh)
            outcome = "planned" if p.get("executable") else "not_executable"
            r.write_meta(plan_id=p.get("plan_id"), plan_objects=p.get("plan_objects"),
                         plan_bytes=p.get("plan_bytes"), executable=bool(p.get("executable")),
                         not_executable_reason=p.get("not_executable_reason") or [],
                         plan_rows=p.get("rows"))
        meta = r.write_meta(finished_utc=_now(), step="done", outcome=outcome)
    except _Refused as e:
        meta = _record_failure(r, outcome="refused", step=e.step, refusal=e.msg)
    except Exception as e:  # noqa: BLE001 -- recorded in run.json; next target still runs
        meta = _record_failure(r, outcome="failed", step=step,
                               error=f"{type(e).__name__}: {e}")
    meta["_run"] = r
    return meta


def _record_failure(r, **kw):
    kw.setdefault("finished_utc", _now())
    if r.bucket:
        return r.write_meta(**kw)
    # nothing was created for an unresolved workspace: report without writing
    return {"namespace": r.namespace, "workspace": r.workspace, "bucket": None,
            "stamp": r.stamp, **kw}


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def _home_flag(args):
    return f" --home {args.home}" if args.home else ""


def next_step(m, args):
    t = f"{m['namespace']}/{m['workspace']}"
    o = m.get("outcome")
    r = m["_run"]
    if o == "planned":
        return f"terra-scrub clean {t}{_home_flag(args)}"
    if o == "nothing_to_delete":
        return "nothing to delete"
    if o == "review_only":
        if args.aborted_last_copy_deletable:
            return f"review {r.protected} (last copies; kept for a human)"
        return (f"review {r.protected}; re-run scan with --aborted-last-copy-deletable "
                f"to include last copies")
    if o == "candidates_only":
        return (f"review {r.manifest}, then re-run `terra-scrub scan {t}` without "
                f"--no-plan")
    if o == "not_executable":
        return (f"plan is not executable ({'; '.join(m.get('not_executable_reason') or [])}); "
                f"fix that and re-run `terra-scrub scan {t}`")
    if o == "refused":
        return f"{m.get('step')} refused -- see above; fix and re-run `terra-scrub scan {t}`"
    return f"{m.get('step')} failed -- see {r.log(m.get('step') or 'scan')}"


def _objs(n, b):
    return f"{n:,} objects, {human(b)}"


def summary_block(m, args):
    r = m["_run"]
    rows = [("target", f"{m['namespace']}/{m['workspace']}"),
            ("bucket", f"gs://{m['bucket']}" if m.get("bucket") else "(unresolved)")]
    if "snapshot_objects" in m:
        rows.append(("snapshot", _objs(m["snapshot_objects"], m["snapshot_bytes"])))
    if "candidate_objects" in m:
        rows.append(("delete candidates", _objs(m["candidate_objects"], m["candidate_bytes"])))
        rows.append(("review list", f"{m['protected_rows']:,} rows, "
                                    f"{human(m['protected_bytes'])}"
                                    + (f"  {r.protected}" if m["protected_rows"] else "")))
    if m.get("plan_id"):
        ex = "yes" if m.get("executable") else "NO"
        rows.append(("plan", (f"{m['plan_id']}  {_objs(m['plan_objects'], m['plan_bytes'])}  "
                              f"executable: {ex}")))
        blocked = (m.get("plan_rows") or 0) - (m.get("plan_objects") or 0)
        if blocked:
            rows.append(("blocked at plan", (f"{blocked:,} rows did not re-validate "
                                             f"(see {r.log('plan')})")))
        if not m.get("executable"):
            rows.append(("not executable", "; ".join(m.get("not_executable_reason") or [])))
    if m.get("outcome") == "refused":
        rows.append(("REFUSED", str(m.get("refusal", "")).splitlines()[0][:300]))
    if m.get("outcome") == "failed":
        rows.append(("FAILED", str(m.get("error", ""))[:300]))
    rows.append(("outcome", m.get("outcome", "?")))
    if r.bucket:
        rows.append(("run dir", r.root))
    rows.append(("next", next_step(m, args)))
    w = max(len(k) for k, _ in rows) + 1
    return "\n".join(f"  {k + ':':<{w}} {v}" for k, v in rows)


def summary_table(metas):
    lines = [f"  {'target':<40} {'outcome':<18} {'plan':<12} {'objects':>9} {'bytes':>12}"]
    for m in metas:
        pid = m.get("plan_id") or "-"
        n = m.get("plan_objects", m.get("candidate_objects"))
        b = m.get("plan_bytes", m.get("candidate_bytes"))
        lines.append(f"  {m['namespace'] + '/' + m['workspace']:<40} "
                     f"{m.get('outcome', '?'):<18} {pid:<12} "
                     f"{'-' if n is None else format(n, ','):>9} "
                     f"{'-' if b is None else human(b):>12}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target", nargs="+", metavar="namespace/workspace",
                    help="workspace(s) to scan (repeatable)")
    ap.add_argument("--home", default=None,
                    help="state directory (default $TERRA_SCRUB_HOME or ~/.terra-scrub)")
    g = ap.add_argument_group("candidate options (passed to `terra-scrub candidates`)")
    g.add_argument("--reference-list", action="append", default=[], metavar="FILE",
                   help="file naming protected objects/samples (G8/G9); repeatable")
    g.add_argument("--aborted-last-copy-deletable", action="store_true",
                   help="owner policy: last copies under Aborted/Failed submissions "
                        "become deletable (G9 carve-outs still apply)")
    g.add_argument("--prefix", action="append", default=None,
                   help="restrict candidates to this prefix (repeatable; default submissions/)")
    g.add_argument("--include-provenance", action="store_true",
                   help="list Cromwell logs/rc/stdout/stderr too (G5 off)")
    g.add_argument("--include-zero-byte", action="store_true",
                   help="list zero-byte objects too (G6 off)")
    g.add_argument("--allow-index-split", action="store_true",
                   help="allow deleting a sidecar whose data survives (G7 off)")
    g.add_argument("--include-logs", action="store_true",
                   help="delete Cromwell logs (stdout/stderr/*.log) under Aborted/Failed "
                        "submissions, last copies included (G10); rc files stay")
    g.add_argument("--include-done-logs", action="store_true",
                   help="with --include-logs: logs under Done submissions too")
    g.add_argument("--logs-older-than", type=candidates.log_age_days, default=0.0,
                   metavar="DAYS",
                   help="with --include-logs: only logs older than DAYS")
    ap.add_argument("--workers", type=int, default=10,
                    help="parallel live stats during planning (default 10)")
    ap.add_argument("--page-size", type=int, default=1000,
                    help="objects per listing page for the snapshot (default 1000)")
    ap.add_argument("--no-plan", action="store_true",
                    help="stop after the candidate lists (review-only pass)")
    ap.add_argument("--quiet", action="store_true", help="no progress or summary output")


def run(args: argparse.Namespace) -> int:
    targets = [runs.parse_target(t) for t in args.target]
    if args.prefix and len(args.prefix) > 1 and not plan_prefix(args.prefix):
        raise SystemExit("REFUSING: the --prefix values share no common directory, so "
                         "`plan` would have to accept any object name -- run one scan per "
                         "prefix instead")
    if args.home:
        args.home = os.path.abspath(os.path.expanduser(args.home))

    def say(msg):
        if not args.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    metas = []
    for ns, ws in targets:
        m = scan_one(ns, ws, args, say)
        metas.append(m)
        if not args.quiet:
            print(f"\n== scan {ns}/{ws} ==")
            print(summary_block(m, args))
    if len(metas) > 1 and not args.quiet:
        print("\n== summary ==")
        print(summary_table(metas))
    bad = [m for m in metas if m.get("outcome") not in OK_OUTCOMES]
    if bad and args.quiet:
        for m in bad:
            print(f"{m['namespace']}/{m['workspace']}: {m.get('outcome')}: "
                  f"{(m.get('refusal') or m.get('error') or '').splitlines()[0][:300]}",
                  file=sys.stderr)
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub scan",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
