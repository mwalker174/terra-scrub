"""cli: the root parser wires every command and every command's --help works."""
import importlib.util

import pytest
from conftest import run_cmd

from terra_scrub import cli

HAS_ESTATE = importlib.util.find_spec("terra_scrub.estate") is not None


def test_root_help_lists_every_command():
    r = run_cmd(cli, ["--help"])
    assert r.returncode == 0, r.stderr
    for name, _mod, _help in cli.COMMANDS:
        assert name in r.stdout, f"{name} missing from terra-scrub --help"


@pytest.mark.parametrize("name", [c[0] for c in cli.COMMANDS])
def test_command_help(name):
    if name == "estate" and not HAS_ESTATE:
        pytest.skip("terra_scrub/estate.py is absent")
    r = run_cmd(cli, [name, "--help"])
    assert r.returncode == 0, f"{name} --help failed: {r.stderr[-400:]}"
    assert "usage:" in r.stdout
