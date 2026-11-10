"""snapshot: enumerate a GCS bucket once -> JSONL snapshot (metadata only, GET only).

The snapshot never downloads object content. It is written atomically
(temp file in the destination directory + rename) so an interrupted run never
leaves a half-written snapshot at the target path, and it ends with a
``__done__`` integrity marker that :func:`read_snapshot` requires.

File format (one JSON object per line, keys sorted)::

    {"__bucket__": {...bucket metadata...}, "snapshot_utc": "...", "source": "gcs json api v1"}
    {"name": ..., "size": ..., "updated": ..., "md5Hash": ..., "generation": ..., ...}
    ...
    {"__done__": true, "n_objects": N, "total_bytes": B}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime

from terra_scrub import http, util

# Some buckets reject ANY `fields` parameter on object list (even
# `fields=nextToken` -> 400 "Invalid field selection"). Use plain
# `projection=noAcl` and keep only the keys we need below.
OBJ_KEEP = ("name", "size", "updated", "md5Hash", "generation", "storageClass", "contentType")
FIELDS_BUCKET = "id,name,location,versioning,storageClass,timeCreated"


class SnapObj:
    """One object line of a snapshot, held as compactly as Python allows.

    A json.loads() dict of a snapshot line costs ~1.2 KB; a bucket of 7.7M objects
    loaded that way needed ~9 GiB before `candidates` built anything on top of it,
    and swapped a 24 GB laptop to death. Slots instead of a per-object dict, only
    the keys a reader uses (storageClass/contentType stay in the file, unread) cut
    that to roughly a third; what is left is mostly the object names themselves.

    Read-only mapping interface (``o["name"]``, ``o.get("md5Hash")``, ``k in o``) so
    callers written against the old dicts keep working unchanged.
    """
    __slots__ = ("generation", "md5Hash", "name", "size", "updated")

    def __init__(self, name, size, updated, md5Hash, generation):
        self.name = name
        self.size = size
        self.updated = updated
        self.md5Hash = md5Hash
        self.generation = generation

    # A key the source line lacked is stored as None and reads as absent, exactly
    # as it did when records were the json.loads() dicts.
    def __getitem__(self, key):
        v = getattr(self, key, None) if key in SnapObj.__slots__ else None
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key, default=None):
        v = getattr(self, key, None) if key in SnapObj.__slots__ else None
        return default if v is None else v

    def __contains__(self, key):
        return key in SnapObj.__slots__ and getattr(self, key) is not None

    def __repr__(self):
        return f"SnapObj({self.name!r}, size={self.size})"


# Preflight against loading a snapshot the machine cannot hold. Peak RSS of
# `candidates` per byte of snapshot file, measured on samples of a real 7.7M-object
# bucket (3.3 GB file): 2.6-4x, the high end on small files where the fixed cost of
# the interpreter and terra context dominates. Refuse when the estimate exceeds
# MEM_FRACTION of physical RAM: past that, macOS swaps the whole desktop into the
# ground before Python ever raises MemoryError.
MEM_PER_FILE_BYTE = 2.6
MEM_FRACTION = 0.6
ALLOW_LARGE_ENV = "TERRA_SCRUB_ALLOW_LARGE_SNAPSHOT"


def _physical_memory():
    """Bytes of physical RAM, or None where the platform will not say."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None


def check_memory(path):
    """SystemExit if loading ``path`` would likely exhaust this machine's RAM."""
    if os.environ.get(ALLOW_LARGE_ENV) == "1":
        return
    phys = _physical_memory()
    if not phys:
        return
    need = os.path.getsize(path) * MEM_PER_FILE_BYTE
    if need > phys * MEM_FRACTION:
        raise SystemExit(
            f"REFUSING: {path} is {util.human(os.path.getsize(path))}; analyzing it needs "
            f"roughly {util.human(int(need))} of RAM and this machine has "
            f"{util.human(phys)} (limit {MEM_FRACTION:.0%}). Run on a machine with more "
            f"memory, or set {ALLOW_LARGE_ENV}=1 to try anyway.")


def read_snapshot(path, require_done=True):
    """Parse a snapshot JSONL into (bucket_meta, [SnapObj]).

    When require_done (default), the `__done__` marker the snapshot writer
    appends must be present AND consistent with the file contents
    (n_objects / total_bytes). This catches truncated, interrupted-copy, or
    hand-chopped snapshots and refuses to analyze them — downstream outputs
    (including the delete-list pipeline) must not run on a partial inventory.
    See docs/SAFETY.md.
    """
    check_memory(path)
    meta, objs, done = None, [], None
    total_bytes = 0
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{lineno}: invalid JSON ({e.msg} at column "
                                  f"{e.colno}) — snapshot is corrupt or truncated")
            if "__bucket__" in rec:
                meta = rec
            elif "__done__" in rec:
                done = rec
            else:
                size = int(rec.get("size") or 0)
                objs.append(SnapObj(rec["name"], size, rec.get("updated"), rec.get("md5Hash"),
                                    rec.get("generation")))
                total_bytes += size
    if meta is None:
        raise SystemExit(f"no bucket header in {path} — is this a snapshot file?")
    if require_done:
        if done is None:
            raise SystemExit(f"{path}: missing __done__ marker after {len(objs):,} object "
                              f"lines — snapshot is truncated or incomplete. Re-run the "
                              f"snapshot before analyzing (or deleting anything).")
        if done.get("n_objects") != len(objs):
            raise SystemExit(f"{path}: __done__ says n_objects={done.get('n_objects')} but "
                              f"the file contains {len(objs):,} — snapshot is truncated or "
                              f"corrupt.")
        if done.get("total_bytes") != total_bytes:
            raise SystemExit(f"{path}: __done__ says total_bytes={done.get('total_bytes')} "
                              f"but the file sums to {total_bytes} — snapshot is corrupt.")
    return meta, objs


def snapshot_bucket(bucket, out_path, page_size=1000, session=None):
    """Enumerate ``bucket`` into a JSONL snapshot at ``out_path`` (GET only).

    Callable in-process (``verify`` uses it for the after-listing so it stays
    subprocess-free). Returns ``(n_objects, total_bytes)``. Progress goes to
    stderr. On any failure the temp file is removed and the target path is
    left untouched.
    """
    b = bucket
    sess = session or http._authed_session()
    rb = http.api_get(f"{http.GCS_API}/b/{b}", params={"fields": FIELDS_BUCKET}, session=sess)
    bmeta = rb.json()
    print(f"bucket: {bmeta.get('name')} | location={bmeta.get('location')} "
          f"| versioning={bmeta.get('versioning')} | class={bmeta.get('storageClass')}",
          file=sys.stderr)

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".inv_", suffix=".jsonl")
    n = 0
    total = 0
    token = None
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"__bucket__": bmeta,
                                "snapshot_utc": datetime.now(UTC).isoformat(),
                                "source": "gcs json api v1"}, sort_keys=True) + "\n")
            while True:
                params = {"maxResults": page_size, "projection": "noAcl"}
                if token:
                    params["pageToken"] = token
                r = http.api_get(f"{http.GCS_API}/b/{b}/o", params=params, session=sess)
                body = r.json()
                items = body.get("items") or []
                for o in items:
                    slim = {k: o[k] for k in OBJ_KEEP if k in o}
                    f.write(json.dumps(slim, sort_keys=True) + "\n")
                    n += 1
                    total += int(o.get("size") or 0)
                token = body.get("nextPageToken") or body.get("nextToken")
                print(f"  {n:,} objects so far | {util.human(total)}", file=sys.stderr)
                if not token:
                    break
            f.write(json.dumps({"__done__": True, "n_objects": n,
                                "total_bytes": total}, sort_keys=True) + "\n")
        # local temp file -> local target; atomic on the same filesystem
        os.replace(tmp, out_path)
    except BaseException:
        # clean up OUR local temp file only (never a remote object)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"done: {n:,} objects, {util.human(total)} -> {out_path}", file=sys.stderr)
    return n, total


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bucket")
    parser.add_argument("--out", required=True)
    parser.add_argument("--page-size", type=int, default=1000)


def run(args: argparse.Namespace) -> int:
    snapshot_bucket(args.bucket, args.out, page_size=args.page_size)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terra-scrub snapshot",
                                 description="enumerate a bucket -> JSONL snapshot (GET only)")
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
