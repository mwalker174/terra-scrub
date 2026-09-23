"""Read-only by construction: a SOURCE lint over every GET-only module.

A claim in a docstring is not a property. This test parses each GET-only module's AST
and fails on anything that could mutate GCS, shell out, or advertise a delete mode.
Add a `session.post`, an `os.remove`, a `subprocess` import or an `--execute` flag to
any of these modules and this file fails first.

GET-only: http, util, snapshot, terra, analyze, candidates, plan, verify.
Exempt (not linted): approve (writes one CONFIRM token into a local file) and estate
(shells out to terra-scrub itself). approve still gets a narrow check below.
"""
import ast
import os
import re

import pytest

import terra_scrub

PKG = os.path.dirname(terra_scrub.__file__)
GET_ONLY = ["http", "util", "snapshot", "terra", "analyze", "candidates", "plan", "verify",
            "runs", "scan", "status"]

MUTATING_HTTP = {"post", "put", "patch", "delete"}
# Imports that can shell out, touch the network outside api_get, or mutate the
# filesystem wholesale. Matched on the FULL dotted module name (or a dotted
# prefix of it), so `from terra_scrub import http` -- our own module -- is not
# confused with the stdlib `http` package.
BANNED_MODULES = {"subprocess", "shutil", "socket", "ftplib", "smtplib", "http", "urllib.request"}
BANNED_FLAG = re.compile(r"--execute|--apply|--delete|--rm\b|--force-delete")
# os.<fn> calls that mutate the local filesystem or spawn processes. Only
# os/shutil/subprocess/pathlib receivers count: `replace` is also a str method.
DANGEROUS_FS = {"system", "popen", "execv", "execve", "execl", "execvp", "spawn", "spawnl",
                "spawnv", "remove", "unlink", "rmdir", "removedirs", "rename", "replace",
                "truncate", "rmtree", "mknod", "kill"}
FS_MODULES = {"os", "shutil", "subprocess", "pathlib"}
# Method names that only ever mean "delete a path", whatever the receiver (Path.unlink ...)
PATH_DELETERS = {"unlink", "rmdir", "rmtree"}
# snapshot writes its local temp file atomically: os.replace(tmp, out) and, on
# failure, os.unlink(tmp). Allowed there and ONLY inside snapshot_bucket.
SNAPSHOT_ALLOWED = {"unlink", "replace"}


def _src(mod):
    path = os.path.join(PKG, f"{mod}.py")
    with open(path) as f:
        return path, f.read()


def _tree(mod):
    path, src = _src(mod)
    return ast.parse(src, path), src


def _parents(tree):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    return par


def _enclosing_func(node, par):
    while node in par:
        node = par[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _docstring_nodes(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.body and isinstance(n.body[0], ast.Expr) \
                    and isinstance(n.body[0].value, ast.Constant) \
                    and isinstance(n.body[0].value.value, str):
                out.add(n.body[0].value)
    return out


def _imported_modules(tree):
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module)
            # `from os import remove` style: record os.remove so it is still seen
            mods |= {f"{n.module}.{a.name}" for a in n.names}
    return mods


def _banned_imports(tree):
    hits = []
    for m in _imported_modules(tree):
        for b in BANNED_MODULES:
            if m == b or m.startswith(b + "."):
                hits.append(m)
    # `from os import remove/unlink/...` would hide the receiver from the call check
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in FS_MODULES:
            hits += [f"{n.module}.{a.name}" for a in n.names if a.name in DANGEROUS_FS]
    return sorted(set(hits))


def _attr_calls(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]


def _flags(tree):
    flags = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "add_argument":
            flags += [a.value for a in n.args if isinstance(a, ast.Constant)
                      and isinstance(a.value, str)]
    return flags


def _tool_strings(tree):
    """Non-docstring string constants naming gsutil/gcloud, as (node, text)."""
    docs = _docstring_nodes(tree)
    return [(n, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n not in docs
            and re.search(r"gsutil|gcloud", n.value)]


# ---------------------------------------------------------------------------
# the lint rules, as pure functions of source (so the negative tests below can
# prove each one actually bites)
# ---------------------------------------------------------------------------

def lint(mod_name, src):
    tree = ast.parse(src)
    par = _parents(tree)
    problems = []

    bad = _banned_imports(tree)
    if bad:
        problems.append(f"banned import(s): {bad}")

    for c in _attr_calls(tree):
        if c.func.attr in MUTATING_HTTP:
            problems.append(f"line {c.lineno}: mutating HTTP verb .{c.func.attr}()")
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in MUTATING_HTTP \
                and isinstance(n.value, ast.Name) and n.value.id == "requests":
            problems.append(f"line {n.lineno}: requests.{n.attr}")

    for c in _attr_calls(tree):
        f = c.func
        if isinstance(f.value, ast.Name) and f.value.id in FS_MODULES and f.attr in DANGEROUS_FS:
            if mod_name == "snapshot" and f.value.id == "os" and f.attr in SNAPSHOT_ALLOWED \
                    and _enclosing_func(c, par) == "snapshot_bucket":
                continue
            problems.append(f"line {c.lineno}: {f.value.id}.{f.attr}()")
        elif f.attr in PATH_DELETERS and not (isinstance(f.value, ast.Name)
                                              and f.value.id in FS_MODULES):
            problems.append(f"line {c.lineno}: .{f.attr}() on a path-like receiver")

    for fl in _flags(tree):
        if BANNED_FLAG.search(fl):
            problems.append(f"banned flag {fl!r}")

    tool = _tool_strings(tree)
    if mod_name == "plan":
        # exactly the wrapper template: the f-string assigned to `sh`
        tmpl = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "sh" for t in n.targets)
                and isinstance(n.value, ast.JoinedStr)]
        inside = set()
        for a in tmpl:
            inside |= set(ast.walk(a.value))
        tool = [(n, s) for n, s in tool if n not in inside]
        # ...and inside the template only the one `gcloud storage rm` deleter is allowed
        for a in tmpl:
            txt = "".join(v.value for v in a.value.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if "gsutil" in txt:
                problems.append(f"line {a.lineno}: gsutil in the wrapper template")
            if txt.count("gcloud storage rm") != 1 or txt.count("gcloud") != 1:
                problems.append(f"line {a.lineno}: wrapper template must name exactly one "
                                "`gcloud storage rm` and no other gcloud command")
    for n, s in tool:
        problems.append(f"line {n.lineno}: gsutil/gcloud string {s[:60]!r}")
    return problems


@pytest.mark.parametrize("mod", GET_ONLY)
def test_get_only_module_is_readonly(mod):
    path, src = _src(mod)
    assert os.path.exists(path)
    problems = lint(mod, src)
    assert not problems, f"{mod}.py is not read-only by construction:\n  " + "\n  ".join(problems)


def test_snapshot_local_writes_confined():
    """os.unlink/os.replace are allowed in snapshot.py only for the atomic temp-file
    write, i.e. only inside snapshot_bucket -- and they are actually there."""
    tree, _ = _tree("snapshot")
    par = _parents(tree)
    uses = [(c.func.attr, _enclosing_func(c, par)) for c in _attr_calls(tree)
            if isinstance(c.func.value, ast.Name) and c.func.value.id == "os"
            and c.func.attr in SNAPSHOT_ALLOWED]
    assert uses, "the lint is policing something real (os.replace/os.unlink present)"
    assert all(fn == "snapshot_bucket" for _, fn in uses), f"outside snapshot_bucket: {uses}"
    assert {a for a, _ in uses} == SNAPSHOT_ALLOWED


def test_api_get_is_the_only_network_primitive():
    tree, _ = _tree("http")
    gets = [c for c in _attr_calls(tree) if c.func.attr == "get"]
    assert len(gets) >= 1, "the lint actually sees the GET primitive it is policing"


def test_apply_readonly_lint():
    """The original plan-gate lint (gcs_cleanup_apply.py), ported to plan.py."""
    tree, src = _tree("plan")
    calls = _attr_calls(tree)
    verbs = sorted({c.func.attr for c in calls if c.func.attr in
                    {"post", "put", "patch", "delete", "options", "request", "upload", "download"}})
    assert not verbs, f"no mutating HTTP verb anywhere in the AST (hits {verbs})"
    gets = [c for c in calls if c.func.attr == "get"]
    assert len(gets) >= 1, f"the lint sees the GET primitive it is policing ({len(gets)} .get calls)"

    chmods = [c for c in calls if isinstance(c.func.value, ast.Name)
              and c.func.value.id == "os" and c.func.attr == "chmod"]
    assert len(chmods) == 1 and any(isinstance(a, ast.Constant) and a.value == 0o644
                                    for a in chmods[0].args), \
        "the only os.chmod sets 0o644 -- the emitted wrapper is NOT executable by default"

    flags = _flags(tree)
    banned = [f for f in flags if re.search(r"execute|do.?delete|^--delete$|--yes|--force|apply", f)]
    assert not banned, f"no flag that deletes/executes exists to discover (hits {banned})"
    assert not [f for f in flags if "md5" in f], \
        "the md5 gate has no --allow-* opt-out: 'no digest, no plan' is not a default"
    for want in ("--manifest", "--terra", "--prefix", "--max-manifest-age-hours",
                 "--allow-stale-manifest", "--limit"):
        assert want in flags, f"gate flag {want} is still there"

    assert 'CONFIRM=""' in src and "exit 1" in src, \
        "the wrapper template ships an empty CONFIRM token and a non-zero refusal path"
    assert src.count("gcloud storage rm") == 1, \
        "the template names the delete command exactly once (and nowhere else in plan.py)"
    assert "gsutil" not in src, "the deprecated gsutil deleter is gone from plan.py"


def test_approve_narrow_check():
    """approve is exempt from the full lint (it writes one local file), but it must
    still never shell out or name a cloud CLI."""
    tree, src = _tree("approve")
    assert "gsutil" not in src and "gcloud" not in src
    mods = _imported_modules(tree)
    assert not [m for m in mods if m == "subprocess" or m.startswith("subprocess.")]
    assert not [m for m in mods if m == "shutil" or m.startswith("shutil.")]
    assert not [c for c in _attr_calls(tree) if c.func.attr in MUTATING_HTTP]


# ---------------------------------------------------------------------------
# the lint must bite: each synthetic violation is caught
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("snippet", [
    "import subprocess\n",
    "import shutil\n",
    "from subprocess import run\n",
    "import http.client\n",
    "from os import remove\n",
    "def f(s):\n    s.post('u')\n",
    "def f(s):\n    s.delete('u')\n",
    "import requests\nx = requests.put\n",
    "import os\nos.remove('x')\n",
    "import os\nos.unlink('x')\n",
    "import os\nos.rmdir('x')\n",
    "def f(p):\n    p.unlink()\n",
    "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--execute')\n",
    "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--rm')\n",
    "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--force-delete')\n",
    "CMD = 'gsutil rm gs://x'\n",
    "CMD = f'gcloud storage rm {1}'\n",
])
def test_lint_bites(snippet):
    assert lint("candidates", snippet), f"lint missed: {snippet!r}"


@pytest.mark.parametrize("snippet", [
    # a gsutil string outside the template
    "sh = f'exec gcloud storage rm -I < {1}'\nCMD = 'gsutil rm gs://x'\n",
    # a second deleter outside the template
    "sh = f'exec gcloud storage rm -I < {1}'\nCMD = 'gcloud storage rm gs://x'\n",
    # gsutil injected into the template itself
    "sh = f'exec gcloud storage rm -I < {1}\\ngsutil -m rm -I < {1}'\n",
    # a second gcloud storage rm injected into the template itself
    "sh = f'exec gcloud storage rm -I < {1}\\ngcloud storage rm -I < {1}'\n",
])
def test_plan_lint_bites(snippet):
    assert lint("plan", snippet), f"plan lint missed: {snippet!r}"


def test_lint_does_not_flag_own_http_module():
    assert not lint("candidates", "from terra_scrub import http\nfrom terra_scrub import http as _h\n")
    # snapshot's allowance is scoped to snapshot_bucket only
    bad = "import os\ndef other():\n    os.unlink('x')\n"
    assert lint("snapshot", bad)
    ok = "import os\ndef snapshot_bucket():\n    os.replace('a', 'b')\n    os.unlink('a')\n"
    assert not lint("snapshot", ok)
