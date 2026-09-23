"""Run layout shared by the workspace-addressed commands (scan / status / clean).

A "run" is one capture of one workspace bucket. Everything the low-level tools
produce for it lives under one directory, in the same layout the estate driver
uses, so `verify.guess_before` and the low-level commands keep working on it:

    <home>/runs/<namespace>/<workspace>/<UTC stamp>/
        inv/<key>.jsonl                      snapshot (must carry __done__)
        inv/<key>.terra.json                 context captured AFTER the snapshot
        inv/<key>.plan-time.terra.json       fresh context captured AFTER the manifest
        cleanup/<key>/<bucket>.cleanup.tsv   delete manifest
        cleanup/<key>/<bucket>.cleanup.protected.tsv
        cleanup/<key>/<bucket>.cleanup.tsv.plan.json / .plan.uris.txt / .plan.sh
        logs/<step>.log                      stdout+stderr of each low-level step
        run.json                             what this run is (target, bucket, stamps, outcome)

<home> is $TERRA_SCRUB_HOME or ~/.terra-scrub. Nothing here touches the network.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass


def home() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("TERRA_SCRUB_HOME", "~/.terra-scrub")))


def parse_target(s: str) -> tuple[str, str]:
    """'namespace/workspace' -> (namespace, workspace). Refuses anything else."""
    parts = s.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise SystemExit(f"target must be <namespace>/<workspace>, got {s!r}")
    return parts[0], parts[1]


def key_of(bucket: str) -> str:
    """Same short key the estate driver uses for per-bucket file names."""
    return bucket.replace("fc-secure-", "fc-")[:22]


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


@dataclass(frozen=True)
class Run:
    namespace: str
    workspace: str
    root: str            # <home>/runs/<ns>/<ws>/<stamp>
    bucket: str | None   # None until the workspace has been resolved

    @property
    def stamp(self) -> str:
        return os.path.basename(self.root)

    @property
    def key(self) -> str:
        assert self.bucket, "bucket not resolved"
        return key_of(self.bucket)

    # --- paths (all derived; nothing is created here) ---
    @property
    def inv_dir(self): return os.path.join(self.root, "inv")
    @property
    def cleanup_dir(self): return os.path.join(self.root, "cleanup", self.key)
    @property
    def logs_dir(self): return os.path.join(self.root, "logs")
    @property
    def snapshot(self): return os.path.join(self.inv_dir, f"{self.key}.jsonl")
    @property
    def context(self): return os.path.join(self.inv_dir, f"{self.key}.terra.json")
    @property
    def plan_context(self): return os.path.join(self.inv_dir, f"{self.key}.plan-time.terra.json")
    @property
    def manifest(self): return os.path.join(self.cleanup_dir, f"{self.bucket}.cleanup.tsv")
    @property
    def protected(self): return os.path.join(self.cleanup_dir, f"{self.bucket}.cleanup.protected.tsv")
    @property
    def plan_json(self): return self.manifest + ".plan.json"
    @property
    def plan_uris(self): return self.manifest + ".plan.uris.txt"
    @property
    def wrapper(self): return self.manifest + ".plan.sh"
    @property
    def run_json(self): return os.path.join(self.root, "run.json")

    def log(self, step: str) -> str:
        return os.path.join(self.logs_dir, f"{step}.log")

    def with_bucket(self, bucket: str) -> "Run":
        return Run(self.namespace, self.workspace, self.root, bucket)

    # --- run.json ---
    def read_meta(self) -> dict:
        if not os.path.exists(self.run_json):
            return {}
        with open(self.run_json) as f:
            return json.load(f)

    def write_meta(self, **updates) -> dict:
        meta = self.read_meta()
        meta.update(namespace=self.namespace, workspace=self.workspace,
                    bucket=self.bucket, stamp=self.stamp, **updates)
        os.makedirs(self.root, exist_ok=True)
        with open(self.run_json, "w") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
        return meta


def workspace_dir(ns: str, ws: str, home_dir: str | None = None) -> str:
    return os.path.join(home_dir or home(), "runs", ns, ws)


def new_run(ns: str, ws: str, home_dir: str | None = None) -> Run:
    return Run(ns, ws, os.path.join(workspace_dir(ns, ws, home_dir), utc_stamp()), None)


def list_runs(ns: str, ws: str, home_dir: str | None = None) -> list[Run]:
    """All runs for a workspace, newest first. Bucket comes from run.json when present."""
    d = workspace_dir(ns, ws, home_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for stamp in sorted(os.listdir(d), reverse=True):
        root = os.path.join(d, stamp)
        if not os.path.isdir(root):
            continue
        r = Run(ns, ws, root, None)
        b = r.read_meta().get("bucket")
        out.append(r.with_bucket(b) if b else r)
    return out


def latest_run(ns: str, ws: str, home_dir: str | None = None) -> Run | None:
    runs = list_runs(ns, ws, home_dir)
    return runs[0] if runs else None
