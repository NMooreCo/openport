"""Tests for tuning on Nebius Serverless Jobs, with a scripted stand-in for the `nebius` CLI."""

import io
import json
import shlex
from pathlib import Path

import pytest

from openport import config
from openport.jobs import JobSettings, create_command, emit, job_state, main, parse_result, tune_on_jobs

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
SETTINGS = JobSettings(image="cr.eu-north1.nebius.cloud/reg/openport:dev", platform="cpu-d3", preset="4vcpu-16gb",
                       secrets={"NEBIUS_API_KEY": "openport-keys", "TAVILY_API_KEY": "openport-keys"})


def write_run(folder: Path, site: str, tier: str = "nano") -> dict:
    """The two files a one-site `openport tune` leaves in runs/."""
    folder.mkdir(parents=True, exist_ok=True)
    chosen = {"tier": tier, "passed": 9, "total": 10, "holdout_pass": 1.0, "cost_usd": 0.01}
    report = {"kind": "tune", "target": "supportdesk", "target_pass_rate": 0.9, "models": {"nano": "nvidia/nano"},
              "helper_cost_usd": 0.05, "results": [{"site_id": site, "chosen": chosen, "attempts": [chosen]}]}
    plan = {"target": 0.9, "call_sites": {f"{site}.fn": {"model": f"nvidia/{tier}"}}}
    (folder / "tune-supportdesk-20261024-120000.json").write_text(json.dumps(report), encoding="utf-8")
    (folder / "plan-supportdesk.json").write_text(json.dumps(plan), encoding="utf-8")
    return {"report": report, "plan": plan}


def test_results_survive_the_logs(tmp_path):
    payload = write_run(tmp_path, "triage")
    payload["report"]["results"][0]["system_prompts"] = ["x" * 20_000]  # long enough for several chunks
    (tmp_path / "tune-supportdesk-20261024-120000.json").write_text(json.dumps(payload["report"]), encoding="utf-8")
    out = io.StringIO()
    emit(tmp_path, out)
    lines = out.getvalue().splitlines()
    assert len(lines) > 3
    # Logs come back with timestamps and other output mixed in.
    noisy = "\n".join(["Collecting packages...", *[f"2026-10-24T12:00:0{i % 10}Z {line}" for i, line in enumerate(lines)], "done"])
    assert parse_result(noisy) == payload
    with pytest.raises(ValueError, match="incomplete"):
        parse_result("\n".join(lines[:-1]))


def test_job_entrypoint_accepts_arguments_as_one_string(tmp_path, capsys):
    payload = write_run(tmp_path, "pii")
    assert main([f"emit {tmp_path.as_posix()}"]) == 0
    assert parse_result(capsys.readouterr().out) == payload
    assert main(["nonsense"]) == 2


def test_create_command_runs_on_cpu_with_secrets():
    command = create_command("openport-supportdesk-triage-x", SETTINGS, "examples/supportdesk", "triage", ["--target", "0.9"])
    assert command[:4] == ["nebius", "ai", "job", "create"]
    assert command[command.index("--platform") + 1] == "cpu-d3"
    assert shlex.split(command[command.index("--args") + 1]) == ["tune-site", "examples/supportdesk", "triage", "--target", "0.9"]
    assert "NEBIUS_API_KEY=openport-keys" in command and "--env" not in command  # keys only ever as secret references
    assert "--async" in command and "jsonpath={.resource_id}" in command


def test_settings_refuse_gpu_platforms_and_missing_keys():
    with pytest.raises(ValueError, match="GPU platform"):
        JobSettings(image="i", platform="gpu-h100-sxm", preset="1gpu-16vcpu-200gb", secrets={"NEBIUS_API_KEY": "s"}).check()
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        JobSettings(image="i", platform="cpu-d3", preset="4vcpu-16gb").check()


def test_job_state_reads_camel_or_snake_case():
    assert job_state(json.dumps({"status": {"state": "running"}})) == ("RUNNING", "")
    assert job_state(json.dumps({"status": {"state": "FAILED", "state_details": {"message": "image pull"}}})) == ("FAILED", "image pull")
    assert job_state(json.dumps({"status": {"state": "ERROR", "stateDetails": {"message": "quota"}}})) == ("ERROR", "quota")


class FakeNebius:
    """Answers `nebius ai job create/get/logs` from scripted job states and per-site run folders."""

    def __init__(self, tmp_path: Path, states: dict[str, list[str]]):
        self.tmp_path, self.states, self.sites, self.commands = tmp_path, states, {}, []

    def __call__(self, command: list[str]) -> str:
        self.commands.append(command)
        verb = command[3]
        if verb == "create":
            site = shlex.split(command[command.index("--args") + 1])[2]
            self.sites[f"aijob-{site}"] = site
            return f"aijob-{site}\n"
        site = self.sites[command[4]]
        if verb == "get":
            states = self.states[site]
            state = states.pop(0) if len(states) > 1 else states[0]
            return json.dumps({"metadata": {"id": command[4]}, "status": {"state": state, "stateDetails": {"message": "boom" if state == "FAILED" else ""}}})
        out = io.StringIO()
        write_run(self.tmp_path / site, site, tier="super" if site == "pii" else "nano")
        emit(self.tmp_path / site, out)
        return out.getvalue()


def test_jobs_run_in_parallel_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    fake = FakeNebius(tmp_path, {"triage": ["PROVISIONING", "RUNNING", "COMPLETED"], "pii": ["RUNNING", "COMPLETED"], "replies": ["FAILED"]})
    events, sleeps = [], []
    jobs, report_path, plan_path = tune_on_jobs(
        SUPPORTDESK, SETTINGS, site_ids=["triage", "pii", "replies"], run=fake, sleep=sleeps.append,
        on_event=lambda kind, job: events.append((kind, job.site, job.state)),
    )
    # All three were submitted before any was polled: they run at the same time.
    assert [c[3] for c in fake.commands[:3]] == ["create"] * 3
    assert {j.site: j.state for j in jobs} == {"triage": "COMPLETED", "pii": "COMPLETED", "replies": "FAILED"}
    assert ("state", "replies", "FAILED") in events and sleeps  # it waited between polls

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [r["site_id"] for r in report["results"]] == ["triage", "pii"]  # the failed job contributes nothing
    assert report["helper_cost_usd"] == pytest.approx(0.10)
    assert [j["state"] for j in report["jobs"]] == ["COMPLETED", "COMPLETED", "FAILED"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan == {"target": 0.9, "call_sites": {"triage.fn": {"model": "nvidia/nano"}, "pii.fn": {"model": "nvidia/super"}}}


def test_no_finished_job_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    fake = FakeNebius(tmp_path, {"triage": ["ERROR"]})
    with pytest.raises(RuntimeError, match="no job finished"):
        tune_on_jobs(SUPPORTDESK, SETTINGS, site_ids=["triage"], run=fake, sleep=lambda s: None)
