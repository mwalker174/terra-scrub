"""clean -- arm and run the latest plan for a workspace, then verify it.

    terra-scrub clean <namespace>/<workspace> [--confirm PLAN_ID] [--dry-run]
                      [--skip-verify] [--workers N] [--home DIR]

This is the ONE module in the package that runs a delete, and it is a HUMAN step.
It does not delete anything itself either: it runs the delete wrapper that `plan`
wrote (`bash <manifest>.plan.sh`), and that wrapper holds the package's only
deleter invocation. This file names no cloud CLI and has no delete verb of its
own; its only subprocess is `bash <wrapper>`.

What it does, in order, refusing (and arming nothing) at the first failure:

  1. finds the latest run for the workspace under <home>/runs/<ns>/<ws>/ (see
     terra_scrub.runs) and refuses unless run.json says it is `planned` and the
     run has a plan.json;
  2. re-checks the plan exactly as `terra-scrub approve` does (approve.recheck:
     kind=delete, executable, manifest AND plan under 24 h computed now, URI list
     sha256 + line count, wrapper present, CONFIRM empty). A wrapper that is
     already armed with this plan's id (someone ran `approve` by hand) is accepted
     and not re-armed; any other refusal says to re-run `terra-scrub scan`;
  3. prints what will be deleted;
  4. requires the plan id typed at the prompt, or passed as --confirm. Without a
     TTY and without --confirm it refuses. A mismatch aborts with exit 1;
  5. arms the wrapper (approve.arm), runs it, and records the result in run.json;
  6. runs `verify` in-process (unless --skip-verify) and records its verdict.

Everything each step prints also goes to <run>/logs/clean.log and verify.log.

Denied to AI coding agents by .claude/settings.json: typing the plan id is the
approval, and it has to come from a person (docs/SAFETY.md §7).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime

from terra_scrub import approve, runs
from terra_scrub.util import human

PLANNED_OUTCOMES = {"planned"}
SOFT_DELETE_NOTE = ("recovery afterwards is a soft-delete restore, and only inside the "
                    "bucket's soft-delete window (GCS default 7 days; it can be 0) -- "
                    "docs/SAFETY.md §8")


def _now():
    return datetime.now(UTC).isoformat()


def _interactive():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class _Tee(io.TextIOBase):
    """Write-through to several text streams (the terminal and a log file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def _find_run(ns, ws, home_dir):
    rescan = f"run `terra-scrub scan {ns}/{ws}`"
    r = runs.latest_run(ns, ws, home_dir)
    if r is None:
        sys.exit(f"REFUSING: no scan for {ns}/{ws} -- {rescan}")
    meta = r.read_meta()
    outcome = meta.get("outcome")
    if outcome not in PLANNED_OUTCOMES:
        sys.exit(f"REFUSING: latest run {r.stamp} for {ns}/{ws} has outcome={outcome!r}, "
                 f"not a plan ready to clean -- `terra-scrub status {ns}/{ws}` shows why; "
                 f"re-run `terra-scrub scan {ns}/{ws}` for a fresh plan")
    if not r.bucket:
        sys.exit(f"REFUSING: run.json in {r.root} names no bucket -- {rescan}")
    if not os.path.exists(r.plan_json):
        sys.exit(f"REFUSING: no plan.json in the latest run ({r.plan_json}) -- {rescan}")
    return r, meta


def _recheck(r, ns, ws):
    """approve.recheck, verbatim. Returns (plan, already_armed).

    recheck's LAST check is "CONFIRM is still empty", so a refusal that says
    "already armed" means every other check passed. Only a wrapper armed with this
    plan's own id is accepted; anything else is a refusal."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            p = approve.recheck(r.plan_json)
        armed = False
    except SystemExit as e:
        msg = str(e.code)
        if "already armed" not in msg:
            sys.exit(f"{msg}\n  -> re-run `terra-scrub scan {ns}/{ws}` for a fresh plan")
        with open(r.plan_json) as fh:
            p = json.load(fh)
        tok = approve._confirm_lines(p["commands_out"])[0].strip()
        if tok != f"CONFIRM={p['plan_id']}":
            sys.exit(f"REFUSING: wrapper is armed with {tok!r}, not this plan's id "
                     f"{p['plan_id']} -- re-run `terra-scrub scan {ns}/{ws}`")
        armed = True
    if os.path.realpath(p["commands_out"]) != os.path.realpath(r.wrapper):
        sys.exit(f"REFUSING: plan.json's wrapper {p['commands_out']} is not this run's "
                 f"wrapper {r.wrapper} -- re-run `terra-scrub scan {ns}/{ws}`")
    return p, armed


def _summary(r, p, armed):
    m_age = approve._age_h(p.get("manifest_generated"))
    p_age = approve._age_h(p.get("generated_utc"))
    print(f"== terra-scrub clean {r.namespace}/{r.workspace} ==")
    print(f"   bucket    : gs://{p['bucket']}")
    print(f"   plan id   : {p['plan_id']}")
    print(f"   delete    : {p['plan_objects']:,} objects, {human(p['plan_bytes'])} "
          f"({p['plan_bytes']:,} bytes)")
    print(f"   age       : manifest {m_age:.1f} h, plan {p_age:.1f} h (limit "
          f"{approve.MAX_AGE_H} h)")
    print(f"   re-checks : URI list sha256 + line count verified; pointer check "
          f"{p.get('pointer_check')}")
    print(f"   wrapper   : {r.wrapper}" + ("   (ALREADY ARMED -- will not re-arm)"
                                          if armed else ""))
    print(f"   review    : {r.manifest}")
    print(f"   NOTE      : {SOFT_DELETE_NOTE}")


def _confirm(args, p):
    """True when the human supplied the plan id; exits otherwise."""
    pid = p["plan_id"]
    if args.confirm is not None:
        if args.confirm.strip() != pid:
            sys.exit(f"ABORTED: --confirm {args.confirm!r} does not match plan id {pid}; "
                     f"nothing was armed")
        return True
    if args.dry_run:
        return False
    if not _interactive():
        sys.exit("REFUSING: not interactive; pass --confirm <plan_id>")
    try:
        got = input(f"Type the plan id to delete {p['plan_objects']:,} objects "
                    f"({human(p['plan_bytes'])}) from gs://{p['bucket']}, or anything "
                    f"else to abort: ")
    except (EOFError, KeyboardInterrupt):
        got = ""
    if got.strip() != pid:
        sys.exit("ABORTED: plan id not typed; nothing was armed")
    return True


def _run_wrapper(r):
    """bash <wrapper>, output captured, then printed and written to logs/clean.log."""
    proc = subprocess.run(["bash", r.wrapper], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, check=False)
    out = proc.stdout or ""
    with open(r.log("clean"), "w") as fh:
        fh.write(out)
    sys.stdout.write(out)
    sys.stdout.flush()
    return proc.returncode, out


def _run_verify(r, workers):
    from terra_scrub import verify
    with open(r.log("verify"), "w") as fh:
        tee = _Tee(sys.stdout, fh)
        try:
            with redirect_stdout(tee):
                rc = int(verify.main(["--plan", r.plan_json, "--workers", str(workers)]) or 0)
        except SystemExit as e:
            code = e.code
            rc = code if isinstance(code, int) else (0 if code is None else 1)
            if not isinstance(code, int) and code is not None:
                tee.write(f"{code}\n")
        except Exception as e:  # noqa: BLE001 -- verify is the record; never crash here
            tee.write(f"verify crashed: {type(e).__name__}: {e}\n")
            rc = 1
    with open(r.log("verify")) as fh:
        text = fh.read()
    return rc, text


def _window(verify_text):
    for line in verify_text.splitlines():
        if "reversible until:" in line:
            return line.split("reversible until:", 1)[1].split("(", 1)[0].strip()
    return None


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("target", help="<namespace>/<workspace>")
    ap.add_argument("--home", default=None,
                    help="terra-scrub home (default $TERRA_SCRUB_HOME or ~/.terra-scrub)")
    ap.add_argument("--confirm", metavar="PLAN_ID", default=None,
                    help="the plan id, given up front instead of typed at the prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="re-check and summarise only; arm and run nothing, write nothing")
    ap.add_argument("--skip-verify", action="store_true",
                    help="do not run verify afterwards (run `terra-scrub verify` yourself)")
    ap.add_argument("--workers", type=int, default=24,
                    help="parallel GETs for verify (default 24)")


def run(args: argparse.Namespace) -> int:
    ns, ws = runs.parse_target(args.target)
    r, _meta = _find_run(ns, ws, args.home)
    p, armed = _recheck(r, ns, ws)
    _summary(r, p, armed)
    confirmed = _confirm(args, p)

    if args.dry_run:
        print(f"\nDRY RUN: would {'run' if armed else 'arm'} {r.wrapper}"
              f"{'' if armed else ' and run it'}")
        return 0
    assert confirmed

    os.makedirs(r.logs_dir, exist_ok=True)
    if armed:
        print(f"\nwrapper already armed with CONFIRM={p['plan_id']}; not re-arming")
    else:
        approve.arm(r.wrapper, p["plan_id"])
    r.write_meta(armed_utc=_now())

    print(f"\n-- running {r.wrapper} (log: {r.log('clean')})", flush=True)
    rc, out = _run_wrapper(r)
    refusals = ([ln for ln in out.splitlines() if ln.lower().startswith("refusing")]
                if rc != 0 else [])
    # cleaned_utc means "the wrapper got past its own self-check" (status reads it as
    # "cleaned"); a self-refusal deleted nothing, so it is not recorded as a clean.
    if refusals:
        r.write_meta(clean_rc=rc, clean_refused_utc=_now())
    else:
        r.write_meta(clean_rc=rc, cleaned_utc=_now())
    if rc != 0:
        if refusals:
            sys.exit(f"REFUSED by the wrapper's own self-check (rc={rc}); nothing deleted:\n  "
                     + "\n  ".join(refusals) + f"\n  log: {r.log('clean')}")
        tail = "\n".join(out.splitlines()[-20:])
        print(f"\nWARNING: wrapper exited rc={rc}; last lines of {r.log('clean')}:\n{tail}\n"
              f"verify will show what was actually deleted.", file=sys.stderr)

    if args.skip_verify:
        r.write_meta(outcome="cleaned-unverified")
        print(f"\nCLEANED {ns}/{ws} (UNVERIFIED, wrapper rc={rc}): run "
              f"`terra-scrub verify --plan {r.plan_json}` now")
        return 0 if rc == 0 else 1

    print(f"\n-- verifying (log: {r.log('verify')})", flush=True)
    vrc, vtext = _run_verify(r, args.workers)
    ok = vrc == 0
    r.write_meta(verify_rc=vrc, verified_utc=_now(),
                 deleted_objects=p["plan_objects"] if ok else None,
                 outcome="cleaned" if ok else "cleaned-verify-failed")
    if ok:
        win = _window(vtext)
        print(f"\nCLEANED {ns}/{ws}: {p['plan_objects']:,} objects, "
              f"{human(p['plan_bytes'])} freed; verify OK -- soft-delete restore window "
              f"until {win or 'unknown (see verify check 5)'}")
        return 0
    print(f"\nFAILED {ns}/{ws}: verify rc={vrc} (wrapper rc={rc}) -- read "
          f"{r.log('verify')} before doing anything else", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub clean",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
