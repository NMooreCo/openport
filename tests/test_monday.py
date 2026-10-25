"""The Monday runbook (scripts/monday.py), checked without spending any credit: its commands are real, it keeps
what it overwrites, it totals what it spends, and no recording with a local path or a key reaches the image."""

import importlib.util
import json
import time
from pathlib import Path

import click
import typer

from openport import cli

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("monday", ROOT / "scripts" / "monday.py")
monday = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monday)


def test_every_step_is_a_command_the_cli_takes():
    group = typer.main.get_command(cli.app)
    for step in monday.STEPS:
        if step.command is None:
            assert step.record[0] in ("/api/runs", "/api/upgrades")
            continue
        command = group.get_command(click.Context(group), step.command[0])
        assert command is not None, step.name
        try:
            command.make_context(step.command[0], list(step.command[1:]))
        except click.BadParameter as error:  # only a path a step before it writes may not exist yet
            assert "does not exist" in str(error), (step.name, error)
    paths = [arg for step in monday.STEPS for arg in step.command or [] if arg.startswith("examples/")]
    assert paths and all((ROOT / path).exists() for path in paths)
    assert [s.name for s in monday.STEPS].index("tune-supportdesk") < [s.name for s in monday.STEPS].index("port-supportdesk")


def test_it_keeps_what_it_overwrites_and_totals_what_it_spends(tmp_path, monkeypatch):
    monkeypatch.setattr(monday, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(monday, "ROOT", tmp_path)
    (tmp_path / "runs" / "ports" / "supportdesk").mkdir(parents=True)
    (tmp_path / "runs" / "plan-supportdesk.json").write_text('{"old": true}')
    kept = monday.set_aside(("plan-supportdesk.json", "ports/supportdesk", "pr-missing.md"))
    assert len(kept) == 2 and (tmp_path / "runs" / "plan-supportdesk.json").exists()  # a file is copied, a folder moved
    assert not (tmp_path / "runs" / "ports" / "supportdesk").exists() and all((tmp_path / k).exists() for k in kept)

    started = time.time() - 1
    (tmp_path / "runs" / "tune-x.json").write_text(json.dumps({"kind": "tune", "helper_cost_usd": 0.02, "results": [
        {"attempts": [{"cost_usd": 0.10}, {"cost_usd": 0.05}]}]}))
    (tmp_path / "runs" / "baseline-x.json").write_text(json.dumps({"kind": "baseline", "results": [{"cost_usd": 0.30}]}))
    (tmp_path / "runs" / "dashboard").mkdir()
    (tmp_path / "runs" / "dashboard" / "r.json").write_text(json.dumps({"events": [{"type": "cost", "evals_usd": 0.5, "helper_usd": 0.03}]}))
    assert round(monday.spent_since(started), 2) == 1.0


def test_no_recording_with_a_local_path_or_a_key_ships():
    for private in ('"output": "C:\\\\Users\\\\nick\\\\app"', "/home/runner/work", "sk-abcdefghijklmnopqrst", "tvly-abcdefghij12"):
        assert monday.PRIVATE.search(private), private
    assert not monday.PRIVATE.search(json.dumps({"events": [{"type": "tests", "summary": "5 passed", "output": "ok 1 - a token round-trips"}]}))
    clean = (ROOT / "examples" / "recorded" / "upgrade-authkit-node.json").read_text(encoding="utf-8")
    assert not monday.PRIVATE.search(clean)  # the recording that ships passes
