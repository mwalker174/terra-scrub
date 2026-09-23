"""approve -- write the CONFIRM token into a plan's delete wrapper.

This is the HUMAN half of the two-person rule for GCS deletion (docs/SAFETY.md
§7 (the two-man rule)). An automated operator (e.g. a coding agent) should be denied both
write access to any *.plan.sh and permission to run this command, so a plan can
only be armed by a person. It deletes nothing itself: it writes one token into one
local file, then tells you what to run.

    terra-scrub approve              # list pending plans under --root
    terra-scrub approve <plan_id>    # arm that plan

Typing the 12-char plan_id IS the approval: it has to match the plan on disk,
so an approval cannot be given to "whatever plan happens to be newest".

Before writing the token it re-checks, and refuses on any failure:
  * the plan is kind=delete and plan.json says executable=true
  * the manifest is younger than 24 h (the planner's own freshness gate)
  * the plan itself is younger than 24 h (its live pointer/status re-check expires)
  * the URI list's live sha256 equals the one plan.json recorded, and its line
    count equals plan_objects (so the token certifies a specific byte-for-byte
    delete set)
  * the wrapper exists and its CONFIRM is still empty

The armed wrapper repeats the sha256 + line-count check itself right before it
deletes, so a URI list changed AFTER arming is refused too.

Ages are always computed NOW from the plan's stamps. plan.json's own
`manifest_age_hours` is the age AT GENERATION TIME and never moves; reading it as
current age would report a day-old plan as fresh.

This module is exempt from the read-only lint because it writes that one local
file. It has no network access and nothing else that mutates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime

MAX_AGE_H = 24


def _age_h(stamp, now=None):
    """Hours since an ISO stamp, computed NOW; None if absent or unparseable."""
    if not stamp:
        return None
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - d).total_seconds() / 3600.0


def _confirm_lines(sh):
    with open(sh) as f:
        return [l for l in f if l.startswith("CONFIRM=")]


def _is_empty_token(line):
    return line.strip() in ('CONFIRM=""', "CONFIRM=''")


def list_plans(root):
    """One printable block per *.plan.json under root, youngest plan first."""
    now = datetime.now(UTC)
    rows = []
    for dp, _dn, fn in os.walk(root):
        for n in sorted(fn):
            if not n.endswith(".plan.json"):
                continue
            path = os.path.join(dp, n)
            try:
                with open(path) as fh:
                    p = json.load(fh)
            except Exception as e:
                rows.append((None, f"  {'?':<12}  UNREADABLE  {path}  ({e.__class__.__name__})"))
                continue
            if "plan_id" not in p:            # a different artifact that shares the suffix
                continue
            sh = p.get("commands_out")
            if not sh:
                # A review list never gets a wrapper; a delete plan gets one only when
                # it is executable (no URI list either, otherwise).
                armed = ("no wrapper (review list)" if p.get("manifest_kind") != "delete"
                         else "no wrapper (not executable)")
            elif not os.path.exists(sh):
                armed = "wrapper missing"
            else:
                tok = _confirm_lines(sh)
                armed = "ARMED" if tok and not _is_empty_token(tok[0]) else "not armed"
            mh = _age_h(p.get("manifest_generated"), now)
            ph = _age_h(p.get("generated_utc"), now)
            stale = "STALE" if (mh is None or mh > MAX_AGE_H) else "fresh"
            rows.append((ph if ph is not None else 1e9,
                         f"  {p['plan_id']}  {p['plan_objects']:>7,} objs  "
                         f"{p['plan_bytes']/2**30:9.3f} GiB  manifest "
                         f"{'??' if mh is None else format(mh, '5.1f')} h -> {stale:5s}  "
                         f"exec={p['executable']!s:5s}  {armed}\n"
                         f"      bucket {p['bucket']}\n      {os.path.relpath(path, root)}"))
    for _, line in sorted(rows, key=lambda r: (r[0] is None, r[0] or 0.0)):
        print(line)


def find_plan(root, want):
    """Path of the single plan.json whose plan_id == want; refuse on 0 or >1."""
    hits = []
    for dp, _dn, fn in os.walk(root):
        for n in fn:
            if n.endswith(".plan.json"):
                p = os.path.join(dp, n)
                try:
                    with open(p) as fh:
                        if json.load(fh).get("plan_id") == want:
                            hits.append(p)
                except Exception:
                    pass
    if len(hits) != 1:
        sys.exit(f"expected exactly 1 plan with plan_id {want}, found {len(hits)}")
    return hits[0]


def recheck(plan_path):
    """Every pre-arming check; returns the plan dict. Refuses with REFUSING: ..."""
    with open(plan_path) as fh:
        p = json.load(fh)

    def die(m):
        sys.exit(f"REFUSING: {m}")

    def age(stamp, what):
        if not stamp:
            die(f"no {what} stamp in plan.json -- cannot judge freshness")
        h = _age_h(stamp)
        if h is None:
            die(f"unparseable {what} stamp {stamp!r}")
        return h

    if p.get("manifest_kind") != "delete":
        die(f"manifest_kind={p.get('manifest_kind')!r} -- only a delete manifest is armable")
    if not p.get("executable"):
        die(f"plan.json says not executable: {p.get('not_executable_reason')}")
    # Freshness is computed NOW from the stamps; manifest_age_hours is frozen at
    # generation time and trusting it would arm a stale delete set.
    m_age = age(p.get("manifest_generated"), "manifest_generated")
    p_age = age(p.get("generated_utc"), "generated_utc")
    if m_age > MAX_AGE_H:
        die(f"manifest is {m_age:.1f} h old (>{MAX_AGE_H} h) -- re-capture and re-plan")
    if p_age > MAX_AGE_H:
        die(f"plan itself is {p_age:.1f} h old (>{MAX_AGE_H} h) -- the live pointer/status "
            f"re-check behind it has expired; re-run `terra-scrub plan`")
    uris, want = p.get("uris_out"), p.get("uri_list_sha256")
    if not uris:
        die("plan.json records no URI list (uris_out is null) -- nothing to arm; re-plan")
    if not os.path.exists(uris):
        die(f"URI list missing: {uris}")
    with open(uris, "rb") as fh:
        got = hashlib.sha256(fh.read()).hexdigest()
    if got != want:
        die(f"URI list sha256 changed since planning\n  plan.json {want}\n  on disk   {got}")
    with open(uris) as fh:
        n = sum(1 for l in fh if l.strip())
    if n != p["plan_objects"]:
        die(f"URI list has {n} lines but plan says {p['plan_objects']} objects")
    sh = p.get("commands_out")
    if not sh or not os.path.exists(sh):
        die(f"wrapper missing: {sh}")
    tok = _confirm_lines(sh)
    if not tok:
        die("wrapper has no CONFIRM line")
    if not _is_empty_token(tok[0]):
        die(f"wrapper is already armed ({tok[0].strip()}) -- refusing to re-arm")
    print(f"plan      : {p['plan_id']}")
    print(f"bucket    : gs://{p['bucket']}")
    print(f"deleting  : {p['plan_objects']:,} objects / {p['plan_bytes']:,} B "
          f"({p['plan_bytes']/2**30:.3f} GiB)")
    print(f"manifest  : {m_age:.1f} h old (plan {p_age:.1f} h), pointer check {p['pointer_check']}")
    print(f"URI sha256: {got}  ({n} lines, verified)")
    print(f"wrapper   : {sh}")
    return p


def arm(sh, token):
    """Write CONFIRM=<token> over the single empty CONFIRM line. The only write."""
    with open(sh) as fh:
        text = fh.read()
    new, n = re.subn(r'^CONFIRM=""$', f"CONFIRM={token}", text, count=1, flags=re.MULTILINE)
    if n != 1:
        sys.exit(f"REFUSING: could not find an empty CONFIRM line in {sh}")
    with open(sh, "w") as fh:
        fh.write(new)
    print(f"\nARMED: CONFIRM={token} written into {sh}")


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("plan_id", nargs="?", default=None,
                    help="the 12-char plan_id to arm; omit to list pending plans")
    ap.add_argument("--root", default="./runs",
                    help="directory searched (recursively) for *.plan.json (default ./runs)")


def run(args: argparse.Namespace) -> int:
    root = args.root
    if not os.path.isdir(root):
        sys.exit(f"REFUSING: --root {root!r} is not a directory")
    if not args.plan_id:
        print(f"pending plans under {root}:")
        list_plans(root)
        print()
        print("arm one with:  terra-scrub approve <plan_id>"
              + ("" if root == "./runs" else f" --root {root}"))
        return 0

    want = args.plan_id
    plan_path = find_plan(root, want)
    p = recheck(plan_path)
    sh = p["commands_out"]
    # one token, written by a person, into a file automation must not touch
    arm(sh, want)
    print()
    print(f"Run it with:   bash '{sh}'")
    print("It is armed now. Arming is the human step; running the armed wrapper may be "
          "delegated.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub approve",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
