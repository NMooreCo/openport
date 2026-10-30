"""The hosted server and the CLI's fetching: cases from the Oct 29 security review."""

import threading
import time

import pytest
from typer.testing import CliRunner

import openport.server as server
from openport import cli, workspace
from openport.server.pipeline import Run


def test_a_run_that_failed_mid_tune_still_counts_what_it_spent(monkeypatch):
    run = Run(id="f1", target="supportdesk", label="SupportDesk")
    run.status = "error"
    for _ in range(6):
        run.emit("attempt", cost_usd=0.5)
    run.emit("failed", message="RuntimeError: Tavily is out of credit")
    assert run.cost_usd == 3.0
    monkeypatch.setattr(server, "runs", {"f1": run})
    assert server.spent_today() == 3.0  # so repeated failures still reach the day's budget


def test_a_finished_run_counts_its_total_not_its_attempts_twice():
    run = Run(id="f2", target="supportdesk", label="SupportDesk")
    run.emit("attempt", cost_usd=0.5)
    run.emit("cost", evals_usd=0.5, helper_usd=0.25)
    assert run.cost_usd == 0.75


def test_a_repo_whose_size_cant_be_checked_isnt_fetched_by_a_server(monkeypatch, tmp_path):
    from openport import config

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(workspace, "repo_size_mb", lambda owner, name: None)  # rate-limited, or a redirect gone wrong
    cloned = []
    monkeypatch.setattr(workspace.subprocess, "run", lambda *a, **k: cloned.append(a))
    with pytest.raises(ValueError, match="Couldn't check the size"):
        workspace.fetch_repo("someone/huge", max_mb=500)
    assert cloned == []


def test_a_mistyped_local_path_isnt_taken_for_a_github_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apps").mkdir()
    assert not workspace.is_remote("apps/api")  # apps/ is here: a typo, not github.com/apps/api
    assert workspace.is_remote("jsliapark/preflight")
    assert workspace.is_remote("https://github.com/apps/api")  # a URL is always a repo


def test_a_failed_clone_is_reported_not_a_traceback(monkeypatch):
    def fail(target, max_mb=None):
        raise RuntimeError("Couldn't fetch someone/missing from GitHub: check that it exists and is public.")

    monkeypatch.setattr(workspace, "fetch_repo", fail)
    monkeypatch.setattr(workspace, "is_remote", lambda target: True)
    result = CliRunner().invoke(cli.app, ["scan", "someone/missing"])
    assert result.exit_code == 1
    assert "check that it exists and is public" in result.output


def test_two_live_requests_at_once_start_one_run(monkeypatch):
    monkeypatch.setattr(server, "runs", {})
    started = []

    def runner(run, target, local):
        started.append(run.id)
        time.sleep(0.2)

    results = []
    threads = [threading.Thread(target=lambda: results.append(server._launch("port", "receipts", "Receipt reader", None, runner, live=True)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(started) == 1
    assert sorted(r["reused"] for r in results) == [False, True]  # the second watches the first


def test_the_github_token_isnt_inherited_by_what_a_run_starts():
    import subprocess
    import sys

    script = ("import os, subprocess, sys; os.environ['OPENPORT_GITHUB_TOKEN'] = 'ghp_secret'; import openport.server as s; "
              "child = subprocess.run([sys.executable, '-c', 'import os; print(os.getenv(\"OPENPORT_GITHUB_TOKEN\"))'], "
              "capture_output=True, text=True).stdout.strip(); print(child, s.GITHUB_TOKEN == 'ghp_secret')")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.stdout.split() == ["None", "True"], result.stderr[-2000:]


def test_what_a_failed_stage_spent_on_its_own_calls_still_counts(monkeypatch):
    """The engineer, the judge and case generation are paid for as they go. A stage that fails after them reports no
    total, but what they spent still reaches the day's budget."""
    from types import SimpleNamespace

    from openport import tokenfactory
    from openport.server import pipeline

    monkeypatch.setattr(tokenfactory, "model_info", lambda model: SimpleNamespace(cost=lambda p, c: 0.4))

    def spend_then_fail(meter):
        meter.record("nvidia/ultra-test", SimpleNamespace(prompt_tokens=10, completion_tokens=10), 1.0)
        raise RuntimeError("Tavily is out of credit")

    for stage in ("tune", "capture"):
        run = Run(id=f"s-{stage}", target="supportdesk", label="SupportDesk")
        with pytest.raises(RuntimeError):
            with pipeline._metered(run, stage) as meter:
                spend_then_fail(meter)
        assert run.events[-1]["type"] == "spent" and run.events[-1]["stage"] == stage
        assert run.cost_usd == 0.4

    run = Run(id="s-ok", target="supportdesk", label="SupportDesk")
    with pipeline._metered(run, "tune") as meter:  # a stage that finishes reports its own total: nothing extra
        meter.record("nvidia/ultra-test", SimpleNamespace(prompt_tokens=10, completion_tokens=10), 1.0)
    assert run.events == [] and run.cost_usd == 0


def test_tuning_and_capture_spend_on_the_pipelines_meter(monkeypatch, tmp_path):
    from openport.server import pipeline

    meters = {}

    def fake_tune_spec(spec_target, **kwargs):
        meters["tune"] = kwargs["helper_meter"]
        raise RuntimeError("stop here")

    def fake_capture(root, scan, **kwargs):
        meters["capture"] = kwargs["meter"]
        raise RuntimeError("stop here")

    monkeypatch.setattr(pipeline, "tune_spec", fake_tune_spec)
    monkeypatch.setattr(pipeline, "capture_repo", fake_capture)
    for stage, call in (("capture", lambda run: pipeline._capture_spec(run, tmp_path, "x", scan=None)),
                        ("tune", lambda run: pipeline._tune(run, tmp_path, 0.9))):
        run = Run(id=f"m-{stage}", target="x", label="X")
        with pytest.raises(RuntimeError):
            call(run)
        assert meters[stage] is not None and run.events[-1] == {**run.events[-1], "type": "spent", "stage": stage}


def test_a_preset_says_whether_picking_it_replays_a_recording(monkeypatch):
    """The page locks a preset with no recording to judges: picking it would start a live run, not a replay."""
    from openport.server import upgrades

    recorded = Run(id="r1", target="supportdesk", label="SupportDesk", recorded=True)
    live_only = Run(id="r2", target="receipts", label="Receipt reader")  # a run from today, not one shipped with the server
    upgrade = Run(id="r3", target="shareAI-lab/learn-claude-code/web", label="learn-claude-code", kind="upgrade", recorded=True)
    monkeypatch.setattr(server, "runs", {r.id: r for r in (recorded, live_only, upgrade)})
    flags = {p["id"]: p["recorded"] for p in server.presets()}
    assert flags["supportdesk"] is True and flags["receipts"] is False and flags["supportdesk-ts"] is False
    upgrade_flags = {p["id"]: p["recorded"] for p in server.upgrade_presets()}
    assert upgrade_flags == {p["id"]: upgrades.resolve_target(p["id"])[0] == upgrade.target for p in upgrades.PRESETS}
    assert any(upgrade_flags.values())


def test_an_expired_token_doesnt_stop_the_size_check(monkeypatch):
    """A token past its date (or revoked) gets 401 on every call: the check asks again without it, as anyone could."""
    import httpx

    asked = []

    def get(url, timeout=None, headers=None, follow_redirects=False):
        asked.append("Authorization" in (headers or {}))
        status = 401 if "Authorization" in (headers or {}) else 200
        return httpx.Response(status, json={"size": 2048} if status == 200 else {"message": "Bad credentials"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(workspace.httpx, "get", get)
    monkeypatch.setattr(workspace, "API_TOKEN", "ghp_expired")
    assert workspace.repo_size_mb("jsliapark", "preflight") == 2.0
    assert asked == [True, False]
    monkeypatch.setattr(workspace, "API_TOKEN", None)
    asked.clear()
    assert workspace.repo_size_mb("jsliapark", "preflight") == 2.0 and asked == [False]


def test_a_failed_clone_says_what_git_said(monkeypatch, tmp_path):
    import subprocess

    from openport import config

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(workspace, "repo_size_mb", lambda owner, name: 1.0)

    def clone(step, **kwargs):
        raise subprocess.CalledProcessError(128, step, stderr="Cloning...\nfatal: repository 'https://github.com/someone/missing.git/' not found\n")

    monkeypatch.setattr(workspace.subprocess, "run", clone)
    with pytest.raises(RuntimeError) as raised:
        workspace.fetch_repo("someone/missing")
    assert str(raised.value) == ("Couldn't fetch someone/missing from GitHub: check that it exists and is public. Git said: "
                                 "Cloning... fatal: repository 'https://github.com/someone/missing.git/' not found")
    assert not (tmp_path / "repos" / "someone__missing.partial").exists() and not (tmp_path / "repos" / "someone__missing").exists()
