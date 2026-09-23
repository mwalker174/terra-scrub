"""Read-only Terra side: workspace context, workspace listing, bucket lookup.

Every call goes through :func:`terra_scrub.http.api_get` (GET only) against the
Terra orchestration API at ``http.TERRA_API`` (``$TERRA_API_URL``, default
``https://api.firecloud.org``). No FISS/firecloud dependency.

Commands:

    context <ns> <ws> --out F   workspace attributes, every gs:// URI referenced
                                by workspace or entity attributes, and the full
                                submission list -> JSON (consumed by stale,
                                candidates, plan, verify).
    workspaces [--namespace NS ...] [--out F]
                                list workspaces visible to you, re-queried live.
    lookup <bucket>             which workspace owns a bucket (exit 1 if none).

Design idea borrowed from FISS ``mop`` / ``validate_file_attrs``: collect every
gs:// URI referenced from Terra workspace + entity attributes so the bucket
listing can be diffed against it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from urllib.parse import quote

from terra_scrub import http
from terra_scrub.analyze import _count

WS_LIST_FIELDS = "workspace.name,workspace.namespace,workspace.bucketName"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ws_url(ns, ws, *rest):
    parts = [quote(ns, safe=""), quote(ws, safe="")] + [quote(p, safe="") for p in rest]
    return f"{http.TERRA_API}/api/workspaces/" + "/".join(parts)


def _unwrap_attr(v):
    """Terra attributes can be plain values or {'itemsType':..., 'items':[...]}."""
    if isinstance(v, dict) and "items" in v:
        items = v["items"]
        return items[0] if len(items) == 1 else (items if items else None)
    return v


def _collect_gs_uris(obj, found):
    """Recursively collect gs:// strings out of unwrapped attribute values."""
    if isinstance(obj, str):
        if obj.startswith("gs://"):
            found.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_gs_uris(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _collect_gs_uris(v, found)


def _get_entities_paged(ns, ws, etype, page_size=500, session=None):
    sess = session or http._authed_session()
    rows = []
    page = 1
    while True:
        params = {"page": page, "pageSize": page_size, "sortDirection": "asc"}
        import requests  # lazy: keep module import free of third-party deps

        try:
            r = http.api_get(_ws_url(ns, ws, "entityQuery", etype), params=params,
                             session=sess)
        except requests.HTTPError as e:
            resp = e.response
            code = resp.status_code if resp is not None else "?"
            text = resp.text[:300] if resp is not None else str(e)
            raise RuntimeError(f"get_entities_query {ns}/{ws}/{etype} page {page}: "
                               f"{code} {text}") from e
        chunk = r.json()
        if isinstance(chunk, dict):
            chunk = chunk.get("results", [])
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return rows


def list_workspaces(fields=WS_LIST_FIELDS, session=None):
    """GET /api/workspaces -> the raw list of {'workspace': {...}, ...} entries."""
    r = http.api_get(f"{http.TERRA_API}/api/workspaces", params={"fields": fields},
                     session=session)
    return r.json()


def scope(namespaces=None, out=None, session=None):
    """Re-query the workspace estate live and return [{namespace,name,bucketName}].

    Never reuse a pinned workspace list: the account's workspace set moves day to
    day (see docs/SAFETY.md). ``namespaces`` (iterable) filters; None = all.
    If ``out`` is given, writes {captured_utc, account_workspaces, workspaces[]}.
    """
    entries = list_workspaces(session=session)
    ns_set = set(namespaces) if namespaces else None
    ws = [{"namespace": w["workspace"]["namespace"], "name": w["workspace"]["name"],
           "bucketName": w["workspace"].get("bucketName")} for w in entries
          if ns_set is None or w["workspace"]["namespace"] in ns_set]
    if out:
        with open(out, "w") as f:
            json.dump({"captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "account_workspaces": len(entries), "workspaces": ws},
                      f, indent=1)
    return ws


def capture_context(ns, ws_name, out, session=None):
    """Write the terra context JSON for workspace ns/ws_name to ``out``.

    Refuses (SystemExit) if the workspace has no bucketName: the ``bucket`` key
    is required downstream to verify a context matches its snapshot.
    """
    sess = session or http._authed_session()
    r = http.api_get(_ws_url(ns, ws_name),
                     params={"fields": "workspace.bucketName,workspace.attributes,"
                                       "workspace.isLocked"},
                     session=sess)
    ws = r.json()["workspace"]

    referenced = set()
    _collect_gs_uris(ws.get("attributes", {}), referenced)

    entities = {}
    r = http.api_get(_ws_url(ns, ws_name, "entities"), session=sess)
    for etype in r.json():
        rows = _get_entities_paged(ns, ws_name, etype, session=sess)
        entities[etype] = len(rows)
        for row in rows:
            _collect_gs_uris(row.get("attributes", {}), referenced)
        print(f"  entities: {etype}: {len(rows):,}", file=sys.stderr)

    r = http.api_get(_ws_url(ns, ws_name, "submissions"), session=sess)
    subs = []
    for s in r.json():
        wf = s.get("workflowStatuses", [])
        subs.append({
            "submissionId": s.get("submissionId"),
            "submissionDate": s.get("submissionDate"),
            "status": s.get("status"),
            "submitter": s.get("submitter"),
            "config": s.get("methodConfigurationName"),
            "entity": s.get("submissionEntity"),
            "n_workflows": len(wf),
            "workflow_status_counts": dict(_count(wf, "status")) if wf else {},
        })

    ws_bucket = ws.get("bucketName")
    if not ws_bucket:
        sys.exit(f"workspace {ns}/{ws_name} came back without a bucketName — "
                 f"cannot write a usable terra context (the bucket key is required "
                 f"downstream); refusing to write it")
    ctx = {
        "namespace": ns,
        "workspace": ws_name,
        "bucket": ws_bucket,
        "isLocked": ws.get("isLocked"),
        "captured_utc": datetime.now(UTC).isoformat(),
        "workspace_attributes": ws.get("attributes", {}),
        "entity_counts": entities,
        "referenced_gs_uris": sorted(referenced),
        "n_referenced": len(referenced),
        "submissions": subs,
    }
    with open(out, "w") as f:
        json.dump(ctx, f, indent=1)
    print(f"done: bucket={ws_bucket} | {len(referenced):,} referenced gs:// URIs | "
          f"{len(subs)} submissions -> {out}", file=sys.stderr)
    return ctx


# ---------------------------------------------------------------------------
# command: context
# ---------------------------------------------------------------------------

def add_arguments_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ns")
    parser.add_argument("ws")
    parser.add_argument("--out", required=True)


def run_context(args: argparse.Namespace) -> int:
    capture_context(args.ns, args.ws, args.out)
    return 0


# ---------------------------------------------------------------------------
# command: workspaces
# ---------------------------------------------------------------------------

def add_arguments_workspaces(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", action="append", default=None,
                        help="only workspaces in this namespace (repeatable; default: all)")
    parser.add_argument("--out", default=None,
                        help="also write {captured_utc, account_workspaces, workspaces[]} JSON")


def run_workspaces(args: argparse.Namespace) -> int:
    ws = scope(args.namespace, out=args.out)
    for w in sorted(ws, key=lambda w: (w["namespace"], w["name"])):
        print(f"{w['namespace']}\t{w['name']}\t{w.get('bucketName') or ''}")
    print(f"{len(ws)} workspace(s)" + (f" -> {args.out}" if args.out else ""),
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# command: lookup
# ---------------------------------------------------------------------------

def add_arguments_lookup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bucket", help="bucket name (with or without gs://)")


def run_lookup(args: argparse.Namespace) -> int:
    target = args.bucket
    target = target.removeprefix("gs://")
    target = target.rstrip("/")
    entries = list_workspaces(fields=WS_LIST_FIELDS + ",workspace.workspaceId")
    matching = None
    for entry in entries:
        info = entry.get("workspace", {})
        if info.get("bucketName") == target:
            matching = info
            break
    if not matching:
        print("Workspace not found, or you do not have permissions to view it.")
        return 1
    print(f"Workspace Name: {matching['name']}")
    print(f"Namespace (Billing Project): {matching['namespace']}")
    print(f"Workspace ID: {matching.get('workspaceId')}")
    print(f"Terra URL: https://app.terra.bio/#workspaces/"
          f"{matching['namespace']}/{matching['name']}")
    return 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terra-scrub (terra)",
                                 description="read-only Terra commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, adder, runner, help_ in (
            ("context", add_arguments_context, run_context,
             "capture Terra workspace attrs, referenced URIs, submissions"),
            ("workspaces", add_arguments_workspaces, run_workspaces,
             "list workspaces visible to you (optionally by namespace)"),
            ("lookup", add_arguments_lookup, run_lookup, "which workspace owns a bucket")):
        p = sub.add_parser(name, help=help_)
        adder(p)
        p.set_defaults(_run=runner)
    args = ap.parse_args(argv)
    return int(args._run(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
