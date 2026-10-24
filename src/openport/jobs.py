"""Tune on Nebius Serverless Jobs: one job per call site, all at once, merged back into one report and plan.

Each job runs the OpenPort image (see the Dockerfile) on a CPU platform and tunes a single call site
with `openport-job tune-site`, which ends by printing the tune report and plan into the job's logs as
base64 chunks, so long JSON survives log-line limits. From your machine, OpenPort submits the jobs with
the `nebius` CLI, waits for them, reads their logs and writes the same two files a local `openport tune`
writes. Keys reach the jobs as MysteryBox secrets (`--env-secret`), never as plain values in a job spec.
"""

import base64
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config
from .evals.spec import load_spec

MARKER = "OPENPORT-RESULT"
CHUNK = 3000  # base64 characters per log line
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "ERROR", "DELETING"}

# run(command) -> stdout; raises on a non-zero exit
Run = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8").stdout


# --- inside a job ---------------------------------------------------------------------------------


def emit(runs_dir: Path, out=None) -> None:
    """Print the newest tune report and plan as numbered base64 chunks."""
    out = out or sys.stdout
    report = sorted(Path(runs_dir).glob("tune-*.json"))[-1]
    plan = sorted(Path(runs_dir).glob("plan-*.json"))[-1]
    payload = {"report": json.loads(report.read_text(encoding="utf-8")), "plan": json.loads(plan.read_text(encoding="utf-8"))}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    chunks = [encoded[i : i + CHUNK] for i in range(0, len(encoded), CHUNK)]
    for index, chunk in enumerate(chunks):
        print(f"{MARKER} {index} {len(chunks)} {chunk}", file=out)
    out.flush()


def parse_result(logs: str) -> dict:
    """Reassemble a job's emitted result from its logs, whatever else (timestamps, other output) is in them."""
    parts: dict[int, str] = {}
    total = None
    for line in logs.splitlines():
        at = line.find(MARKER + " ")
        if at < 0:
            continue
        fields = line[at:].split()
        if len(fields) != 4 or not (fields[1].isdigit() and fields[2].isdigit()):
            continue
        parts[int(fields[1])] = fields[3]
        total = int(fields[2])
    if total is None or sorted(parts) != list(range(total)):
        raise ValueError(f"the job's result is incomplete in its logs ({len(parts)} of {total or '?'} chunks)")
    return json.loads(base64.b64decode("".join(parts[i] for i in range(total))).decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """The `openport-job` entrypoint: `tune-site SPEC SITE [tune flags...]` or `emit [RUNS_DIR]`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 1 and " " in args[0]:
        args = shlex.split(args[0])  # the platform passed every argument as one string
    if not args:
        print("usage: openport-job tune-site SPEC SITE [flags] | emit [RUNS_DIR]", file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    if command == "emit":
        emit(Path(rest[0]) if rest else config.RUNS_DIR)
        return 0
    if command == "tune-site" and len(rest) >= 2:
        from .cli import app

        spec, site, *flags = rest
        code = app(["tune", spec, "--sites", site, *flags], standalone_mode=False)
        if code:
            return int(code)
        emit(config.RUNS_DIR)
        return 0
    print(f"openport-job: unknown command {' '.join(args)!r}", file=sys.stderr)
    return 2


# --- from your machine ----------------------------------------------------------------------------


@dataclass
class JobSettings:
    image: str
    platform: str
    preset: str
    secrets: dict[str, str] = field(default_factory=dict)  # env var -> MysteryBox selector
    registry_secret: str | None = None  # MysteryBox selector with REGISTRY_USERNAME / REGISTRY_PASSWORD
    timeout: str = "2h"
    subnet_id: str | None = None
    parent_id: str | None = None
    allow_gpu: bool = False

    def check(self) -> None:
        if not (self.image and self.platform and self.preset):
            raise ValueError("Serverless Jobs need an image, a platform and a preset")
        if self.platform.lower().startswith("gpu") and not self.allow_gpu:
            raise ValueError(f"{self.platform} is a GPU platform. Tuning only calls Token Factory, so use a CPU platform.")
        if "NEBIUS_API_KEY" not in self.secrets:
            raise ValueError("The jobs need NEBIUS_API_KEY as a secret: --job-secret NEBIUS_API_KEY=<MysteryBox secret>")


@dataclass
class JobRun:
    site: str
    job_id: str
    state: str = "SUBMITTED"
    detail: str = ""


def job_name(target: str, site_id: str, stamp: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"openport-{target}-{site_id}-{stamp}".lower()).strip("-")[:63]


def create_command(name: str, settings: JobSettings, spec: str, site_id: str, tune_flags: list[str]) -> list[str]:
    command = [
        "nebius", "ai", "job", "create", "--name", name, "--image", settings.image,
        "--container-command", "openport-job",
        "--args", " ".join(shlex.quote(arg) for arg in ["tune-site", spec, site_id, *tune_flags]),
        "--platform", settings.platform, "--preset", settings.preset, "--timeout", settings.timeout,
        "--async", "--format", "jsonpath={.resource_id}",
    ]
    for env, selector in settings.secrets.items():
        command += ["--env-secret", f"{env}={selector}"]
    for flag, value in (("--registry-secret", settings.registry_secret), ("--subnet-id", settings.subnet_id), ("--parent-id", settings.parent_id)):
        if value:
            command += [flag, value]
    return command


def job_state(output: str) -> tuple[str, str]:
    """(state, detail) from `nebius ai job get --format json`; the CLI may use camelCase or snake_case."""
    status = json.loads(output).get("status") or {}
    details = status.get("stateDetails") or status.get("state_details") or {}
    return str(status.get("state") or "STATE_UNSPECIFIED").upper(), str(details.get("message") or "")


def tune_on_jobs(
    target_dir: Path,
    settings: JobSettings,
    *,
    site_ids: list[str] | None = None,
    tune_flags: list[str] | tuple = (),
    run: Run = _run,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 20,
    deadline_seconds: float = 3 * 3600,
    on_event: Callable[[str, JobRun], None] | None = None,
) -> tuple[list[JobRun], Path, Path]:
    """Tune each call site in its own Serverless Job, in parallel. Returns the jobs and the merged report and plan."""
    settings.check()
    spec = load_spec(Path(target_dir))
    spec_arg = Path(target_dir).as_posix()  # jobs run from /app, where the image has the same paths
    sites = [s.id for s in spec.call_sites if not site_ids or s.id in site_ids]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    jobs: list[JobRun] = []
    for site in sites:
        job_id = run(create_command(job_name(spec.target, site, stamp), settings, spec_arg, site, list(tune_flags))).strip()
        jobs.append(JobRun(site, job_id))
        if on_event:
            on_event("submitted", jobs[-1])

    started = time.monotonic()
    while True:
        for job in jobs:
            if job.state in TERMINAL:
                continue
            state, detail = job_state(run(["nebius", "ai", "job", "get", job.job_id, "--format", "json"]))
            if state != job.state:
                job.state, job.detail = state, detail
                if on_event:
                    on_event("state", job)
        if all(job.state in TERMINAL for job in jobs):
            break
        if time.monotonic() - started > deadline_seconds:
            raise TimeoutError(f"jobs still running after {deadline_seconds / 3600:.1f}h: " + ", ".join(j.job_id for j in jobs if j.state not in TERMINAL))
        sleep(poll_seconds)

    results: list[dict] = []
    call_sites: dict = {}
    helper_cost = 0.0
    header: dict = {}
    for job in jobs:
        if job.state != "COMPLETED":
            continue
        out = parse_result(run(["nebius", "ai", "job", "logs", job.job_id]))
        report = out["report"]
        header = header or {key: report.get(key) for key in ("target_pass_rate", "models")}
        results += report["results"]
        helper_cost += report.get("helper_cost_usd") or 0
        call_sites.update(out["plan"]["call_sites"])
    if not results:
        raise RuntimeError("no job finished: " + "; ".join(f"{j.site} {j.state} {j.detail}".strip() for j in jobs))

    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = config.RUNS_DIR / f"tune-{spec.target}-{stamp}.json"
    report_path.write_text(json.dumps({
        "kind": "tune",
        "target": spec.target,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target_pass_rate": header.get("target_pass_rate"),
        "models": header.get("models"),
        "helper_cost_usd": helper_cost,
        "results": results,
        "jobs": [asdict(job) for job in jobs],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    plan_path = config.RUNS_DIR / f"plan-{spec.target}.json"
    plan_path.write_text(json.dumps({"target": header.get("target_pass_rate"), "call_sites": call_sites}, indent=2, ensure_ascii=False), encoding="utf-8")
    return jobs, report_path, plan_path


if __name__ == "__main__":
    raise SystemExit(main())
