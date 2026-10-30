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
