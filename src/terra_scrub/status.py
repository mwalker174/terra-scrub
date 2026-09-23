"""status -- where each workspace stands: latest scan, its plan, and what to do next.

    terra-scrub status                          # one line per scanned workspace
    terra-scrub status <namespace>/<workspace>  # full detail for one (or several)
    terra-scrub status --all                    # the table, explicitly

status reads only local files under $TERRA_SCRUB_HOME (default ~/.terra-scrub):
each run's run.json and plan.json and the plan's wrapper script. It makes no
network call and writes nothing.

For a workspace it shows the LATEST run (see `terra_scrub.runs` for the layout):
  * when it ran and how long ago, its bucket, and the snapshot's size
  * the delete candidates and the last-copy review list
  * the plan: id, objects/bytes, executable or not
  * the manifest's and the plan's age, computed NOW from their stamps (plan.json's
    own `manifest_age_hours` is frozen at generation time). Either older than 24 h
    is STALE: `approve` and `clean` will refuse it, so re-run `terra-scrub scan`
  * the wrapper: not armed / ARMED / missing / none
  * after `terra-scrub clean`: when it ran, how many objects it deleted, and
    whether verify passed
  * a `next:` line with the one command to run next

Exit status is 0 unless a run.json or plan.json cannot be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from terra_scrub import runs
from terra_scrub.approve import MAX_AGE_H, _age_h, _confirm_lines, _is_empty_token
from terra_scrub.util import human


class _Unreadable(Exception):
    pass


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise _Unreadable(f"{path}: {type(e).__name__}: {e}") from None


def _fmt_age(h):
    if h is None:
        return "??"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} d"


def wrapper_state(p):
    """'not armed' / 'ARMED' / 'missing' / 'none' for a plan.json dict (or None)."""
    if not p:
        return "none"
    sh = p.get("commands_out")
    if not sh:
        return "none"
    if not os.path.exists(sh):
        return "missing"
    tok = _confirm_lines(sh)
    return "ARMED" if tok and not _is_empty_token(tok[0]) else "not armed"


def describe(r: runs.Run, home_flag="", now=None):
    """Everything status knows about one run, as a dict (reads files, writes none)."""
    now = now or datetime.now(UTC)
    meta = _load_json(r.run_json) if os.path.exists(r.run_json) else {}
    t = f"{r.namespace}/{r.workspace}"
    d = {"target": t, "run": r, "meta": meta,
         "run_age_h": _age_h(meta.get("started_utc"), now), "plan": None}
    if r.bucket and os.path.exists(r.plan_json):
        p = _load_json(r.plan_json)
        if "plan_id" in p:
            d["plan"] = p
    p = d["plan"]
    if p:
        d["manifest_age_h"] = _age_h(p.get("manifest_generated"), now)
        d["plan_age_h"] = _age_h(p.get("generated_utc"), now)
        ages = (d["manifest_age_h"], d["plan_age_h"])
        d["stale"] = any(a is None or a > MAX_AGE_H for a in ages)
    else:
        d["stale"] = d["run_age_h"] is None or d["run_age_h"] > MAX_AGE_H
    d["wrapper"] = wrapper_state(p)
    d["next"] = next_step(d, home_flag)
    return d


def next_step(d, home_flag=""):
    m, p, r = d["meta"], d["plan"], d["run"]
    t = d["target"]
    scan = f"terra-scrub scan {t}{home_flag}"
    if m.get("cleaned_utc"):
        rc = m.get("verify_rc")
        v = "not verified" if rc is None else ("verified OK" if rc == 0 else "verified FAILED")
        return f"cleaned {m['cleaned_utc']}; {v}"
    o = m.get("outcome")
    if o is None:
        return f"run incomplete (stopped at {m.get('step', '?')}); {scan}"
    if o in ("refused", "failed"):
        return f"last scan {o} at {m.get('step', '?')}; fix and re-run: {scan}"
    if o == "nothing_to_delete":
        return "nothing to delete"
    if o == "review_only":
        return f"review {r.protected} (nothing deletable without owner review)"
    if o == "candidates_only":
        return f"review {r.manifest}, then {scan}"
    if not p:
        return scan
    if d["stale"]:
        return f"STALE: re-run {scan}"
    if not p.get("executable"):
        return f"plan not executable; {scan}"
    if d["wrapper"] == "missing":
        return f"wrapper missing; {scan}"
    return f"terra-scrub clean {t}{home_flag}"


def _objs(n, b):
    return f"{n:,} objects, {human(b)}"


def detail_block(d):
    m, p, r = d["meta"], d["plan"], d["run"]
    rows = [("target", d["target"]),
            ("latest run", f"{r.stamp}  ({_fmt_age(d['run_age_h'])} ago)"),
            ("outcome", m.get("outcome") or f"(incomplete: {m.get('step', '?')})"),
            ("bucket", f"gs://{m['bucket']}" if m.get("bucket") else "(unresolved)")]
    if "snapshot_objects" in m:
        rows.append(("snapshot", _objs(m["snapshot_objects"], m["snapshot_bytes"])))
    if "candidate_objects" in m:
        rows.append(("delete candidates", _objs(m["candidate_objects"], m["candidate_bytes"])))
    if "protected_rows" in m:
        rows.append(("review list",
                     f"{m['protected_rows']:,} rows, {human(m.get('protected_bytes', 0))}"))
    if m.get("refusal") or m.get("error"):
        rows.append(("reason", str(m.get("refusal") or m.get("error")).splitlines()[0][:300]))
    if p:
        rows.append(("plan", (f"{p['plan_id']}  {p['plan_objects']:,} objects, "
                              f"{human(p['plan_bytes'])}  executable: "
                              f"{'yes' if p.get('executable') else 'NO'}")))
        if not p.get("executable"):
            rows.append(("not executable", "; ".join(p.get("not_executable_reason") or [])))
        rows.append(("age", (f"manifest {_fmt_age(d['manifest_age_h'])}, plan "
                             f"{_fmt_age(d['plan_age_h'])} -> "
                             f"{'STALE (>24 h): re-run scan' if d['stale'] else 'fresh'}")))
    else:
        rows.append(("plan", "none"))
    rows.append(("wrapper", d["wrapper"]))
    if m.get("cleaned_utc"):
        rows.append(("cleaned", m["cleaned_utc"]
                     + (f"  ({m['deleted_objects']:,} objects deleted)"
                        if isinstance(m.get("deleted_objects"), int) else "")))
        rc = m.get("verify_rc")
        rows.append(("verify", "not run" if rc is None else
                     ("OK" if rc == 0 else f"FAILED (rc={rc})")))
    rows.append(("run dir", r.root))
    rows.append(("next", d["next"]))
    w = max(len(k) for k, _ in rows) + 1
    return "\n".join(f"  {k + ':':<{w}} {v}" for k, v in rows)


def table(ds):
    head = (f"  {'workspace':<40} {'latest run':<17} {'age':>7} {'outcome':<18} "
            f"{'plan':<12} {'objects':>9} {'bytes':>12}  {'wrapper':<10} next")
    lines = [head]
    for d in ds:
        m, p, r = d["meta"], d["plan"], d["run"]
        lines.append(
            f"  {d['target']:<40} {r.stamp:<17} {_fmt_age(d['run_age_h']):>7} "
            f"{(m.get('outcome') or 'incomplete'):<18} "
            f"{(p['plan_id'] if p else '-'):<12} "
            f"{(format(p['plan_objects'], ',') if p else '-'):>9} "
            f"{(human(p['plan_bytes']) if p else '-'):>12}  "
            f"{d['wrapper']:<10} {d['next']}")
    return "\n".join(lines)


def _latest(ns, ws, home_dir):
    # runs.list_runs reads every run.json to learn its bucket; a corrupt one raises
    try:
        return runs.latest_run(ns, ws, home_dir)
    except (OSError, ValueError) as e:
        raise _Unreadable(f"{runs.workspace_dir(ns, ws, home_dir)}: {type(e).__name__}: {e}") \
            from None


def all_workspaces(home_dir):
    """(ns, ws) for every <home>/runs/<ns>/<ws> directory, sorted."""
    root = os.path.join(home_dir, "runs")
    if not os.path.isdir(root):
        return []
    out = []
    for ns in sorted(os.listdir(root)):
        nsd = os.path.join(root, ns)
        if not os.path.isdir(nsd):
            continue
        for ws in sorted(os.listdir(nsd)):
            if os.path.isdir(os.path.join(nsd, ws)):
                out.append((ns, ws))
    return out


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target", nargs="*", metavar="namespace/workspace",
                    help="workspace(s) to show in detail; omit for a one-line-per-workspace table")
    ap.add_argument("--home", default=None,
                    help="state directory (default $TERRA_SCRUB_HOME or ~/.terra-scrub)")
    ap.add_argument("--all", action="store_true",
                    help="table of every scanned workspace (the default with no target)")


def run(args: argparse.Namespace) -> int:
    home_dir = os.path.abspath(os.path.expanduser(args.home)) if args.home else runs.home()
    home_flag = f" --home {home_dir}" if args.home else ""
    rc = 0
    if args.target and not args.all:
        targets = [runs.parse_target(t) for t in args.target]
        for i, (ns, ws) in enumerate(targets):
            if i:
                print()
            print(f"== status {ns}/{ws} ==")
            try:
                r = _latest(ns, ws, home_dir)
                if r is None:
                    print(f"  no runs for {ns}/{ws} under {home_dir}")
                    print(f"  next: terra-scrub scan {ns}/{ws}{home_flag}")
                    continue
                print(detail_block(describe(r, home_flag)))
            except _Unreadable as e:
                print(f"  UNREADABLE: {e}", file=sys.stderr)
                rc = 1
        return rc

    pairs = all_workspaces(home_dir)
    if args.target:     # --all with targets: table restricted to them
        pairs = [runs.parse_target(t) for t in args.target]
    ds = []
    for ns, ws in pairs:
        try:
            r = _latest(ns, ws, home_dir)
            if r is None:
                continue
            ds.append(describe(r, home_flag))
        except _Unreadable as e:
            print(f"  UNREADABLE: {e}", file=sys.stderr)
            rc = 1
    if not ds:
        print(f"no runs under {home_dir}")
        print("next: terra-scrub scan <namespace>/<workspace>")
        return rc
    print(f"runs under {home_dir}:")
    print(table(ds))
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub status",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
