"""The OpenPort dashboard: start a run on a repo and watch scan, tuning, port and PR happen live."""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from .. import config, report, sandbox
from ..port import _diff
from . import pipeline, upgrades
from .pipeline import Run, load_runs, resolve_target, run_pipeline

STATIC = Path(__file__).parent / "static"
MAX_QUEUED = 3
KEPT_RUNS = 100  # finished runs kept in memory; older ones go (their files in runs/dashboard stay)
IN_FLIGHT_USD = 1.0  # what a live run may still spend, counted against the daily budget until it finishes
REPLAY_SPEED, REPLAY_MAX_GAP = 20, 1.5  # a recorded run replays 20x faster, no pause over 1.5 s
# With a token for a GitHub organization set aside for it, a finished port's pull request can be opened there
# (src/openport/sandbox.py): in a fork, for a repo from GitHub, so the original's maintainers aren't notified.
GITHUB_TOKEN = os.getenv("OPENPORT_GITHUB_TOKEN") or None
SANDBOX_ORG = os.getenv("OPENPORT_SANDBOX_ORG") or None
OPENING = threading.Lock()  # one pull request at a time
COMMIT = re.compile(r"^[0-9a-f]{40}$")

app = FastAPI(title="OpenPort", docs_url="/api/docs", openapi_url="/api/openapi.json")

# The page shows text from scanned repos and from models, so it allows only its own scripts, and nobody can frame
# it. The API docs load their viewer from a CDN, so they go without the policy.
SECURITY_HEADERS = {
    "content-security-policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com; "
                               "frame-src https://www.youtube-nocookie.com; object-src 'none'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


class SecurityHeaders:
    """Adds SECURITY_HEADERS to every response, streams included (plain ASGI, so server-sent events pass untouched)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        docs = scope["path"].startswith(("/api/docs", "/api/openapi.json"))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if not (docs and name == "content-security-policy"):
                        headers.setdefault(name, value)
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)
# Finished runs survive a restart; recorded runs of the demos come with the server.
runs: dict[str, Run] = {r.id: r for r in [*load_runs(config.RUNS_DIR / "dashboard"), *load_runs(pipeline.RECORDED, recorded=True)]}
forgotten_spend: dict[float, float] = {}  # UTC day -> what runs since dropped from memory spent


class StartRun(BaseModel):
    target: str
    live: bool = False  # run it now, even when a recorded or recent run of the same target could be shown


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/info")
def info() -> dict:
    """What this server will do with a repo, for the launcher to say up front."""
    return {"vetted_only": pipeline.VETTED_ONLY, "daily_budget_usd": pipeline.DAILY_BUDGET_USD}


@app.get("/api/site")
def site() -> dict:
    """Whether live runs need the judges' password (and whether this visitor has given it), and the demo video.
    Run by itself, the server has no gate; behind deploy/cloudflare, the Worker answers this instead."""
    return {"gated": False, "judge": True, "video_url": os.getenv("OPENPORT_VIDEO_URL") or None, "github_org": github_org()}


def github_org() -> str | None:
    """The organization this server opens pull requests in, if it has a token for one."""
    return SANDBOX_ORG if GITHUB_TOKEN and SANDBOX_ORG else None


def spent_today() -> float:
    """Live runs' Token Factory spend since midnight UTC, counting each unfinished run at IN_FLIGHT_USD."""
    midnight = time.time() // 86400 * 86400
    today = [r for r in runs.values() if not r.recorded and r.created >= midnight]
    return forgotten_spend.get(midnight, 0.0) + sum(r.cost_usd if r.status in ("done", "error") else IN_FLIGHT_USD for r in today)


def _preset(preset: dict) -> dict:
    target, _, local = resolve_target(preset["id"])
    return {**{k: v for k, v in preset.items() if k != "path"}, "tested": pipeline.tests_target(target, local)}


@app.get("/api/presets")
def presets() -> list[dict]:
    return [_preset(p) for p in pipeline.PRESETS]


@app.get("/api/upgrade-presets")
def upgrade_presets() -> list[dict]:
    return [{k: v for k, v in p.items() if k != "path"} for p in upgrades.PRESETS]


def _forget_old_runs() -> None:
    """Keep at most KEPT_RUNS finished runs in memory, the newest (each can hold a large diff); the recorded demos stay."""
    finished = sorted((r for r in runs.values() if not r.recorded and r.status in ("done", "error")), key=lambda r: r.created)
    for run in finished[: max(0, len(finished) - KEPT_RUNS)]:
        day = run.created // 86400 * 86400
        forgotten_spend[day] = forgotten_spend.get(day, 0.0) + run.cost_usd  # still counts against its day's budget
        del runs[run.id]


def _launch(kind: str, target: str, label: str, local: Path | None, runner, live: bool = False, reuse_only: str | None = None) -> dict:
    same = [r for r in runs.values() if r.kind == kind and r.target == target]
    shown = [r for r in same if r.status in ("queued", "running")]  # already running: watch that one
    if not live:  # else a recent run of it, or the recorded one, instead of spending credits again
        shown = shown or [r for r in same if r.status == "done" and not r.recorded and time.time() - r.created < pipeline.REUSE_SECONDS]
        shown = shown or [r for r in same if r.status == "done" and r.recorded]
    if shown:
        return {**shown[-1].summary(), "reused": True}
    if reuse_only == "judges":  # the host keeps live runs for whoever has the judges' password (deploy/cloudflare)
        raise HTTPException(403, "Live runs are for the hackathon's judges: unlock them with the password in the submission's "
                                 "testing instructions. The recorded runs play for everyone, and OpenPort runs anywhere: see the README.")
    if reuse_only:  # the host has counted today's new runs, which outlives this process (deploy/cloudflare)
        raise HTTPException(429, "This demo has started all the new runs it can today. The recorded runs of the demo apps are "
                                 "still here, and OpenPort runs anywhere: see the README.")
    if pipeline.DAILY_BUDGET_USD is not None and spent_today() >= pipeline.DAILY_BUDGET_USD:
        raise HTTPException(429, f"This demo's Token Factory budget for today (${pipeline.DAILY_BUDGET_USD:g}) is used up. "
                                 "The recorded runs of the demo apps are still here, and OpenPort runs anywhere: see the README.")
    if sum(r.status in ("queued", "running") for r in runs.values()) >= MAX_QUEUED:
        raise HTTPException(429, "OpenPort is busy with other runs. Try again in a few minutes.")
    run = Run(id=uuid.uuid4().hex[:12], target=target, label=label, kind=kind)
    runs[run.id] = run
    _forget_old_runs()
    threading.Thread(target=runner, args=(run, target, local), daemon=True).start()
    return {**run.summary(), "reused": False}


def _reuse_only(request: Request) -> str | None:
    """Whoever hosts the server can say it's to start no new runs, only show ones it has (a header only narrows),
    and why: "judges" (live runs need the judges' password) or anything else (today's new runs are used up)."""
    return request.headers.get("x-openport-reuse-only") or None


@app.post("/api/runs")
def start_run(body: StartRun, request: Request) -> dict:
    try:
        target, label, local = resolve_target(body.target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _launch("port", target, label, local, run_pipeline, body.live, _reuse_only(request))


@app.post("/api/upgrades")
def start_upgrade(body: StartRun, request: Request) -> dict:
    try:
        target, label, local = upgrades.resolve_target(body.target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _launch("upgrade", target, label, local, upgrades.run_pipeline, body.live, _reuse_only(request))


def _head(folder: Path) -> str | None:
    """The commit a clone (and so its port, which copies the clone's .git) started from."""
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(folder), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if COMMIT.match(out) else None


@app.post("/api/runs/{run_id}/github")
def open_on_github(run_id: str, request: Request) -> dict:
    """Open the pull request a finished port wrote, in this server's GitHub organization: in its fork of the repo
    (so the repo's maintainers aren't notified), or for a demo app, in the app's own repo there."""
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    if run.github:
        return {"url": run.github, "opened": False}
    org = github_org()
    if org is None:
        raise HTTPException(503, "This server isn't set up to open pull requests on GitHub. `openport pr --open` opens one "
                                 "from your own machine, with your GitHub login.")
    reuse_only = _reuse_only(request)
    if reuse_only == "judges":
        raise HTTPException(403, "Opening pull requests on GitHub is for the hackathon's judges: unlock it with the password in "
                                 "the submission's testing instructions.")
    if reuse_only:
        raise HTTPException(429, "This demo has opened all the pull requests it can today. `openport pr --open` opens one from "
                                 "your own machine, with your GitHub login.")
    ported = next((e for e in run.events if e["type"] == "ported"), None)
    pr = next((e for e in run.events if e["type"] == "pr"), None)
    if run.kind != "port" or run.recorded or run.status != "done" or ported is None or pr is None:
        raise HTTPException(409, "Only a finished live port has a pull request to open.")
    files = [*ported["changed"], *ported["added"]]
    if not files:
        raise HTTPException(409, "This port changed no files, so there's no pull request to open.")

    target, _, local = resolve_target(run.target)
    if local is None:
        pack = pipeline.eval_pack_for(target)
        source = config.RUNS_DIR / "repos" / (pack.commit[:12] if pack and pack.commit else "") / target.replace("/", "__")
    else:
        source = local
    folder = config.RUNS_DIR / "ports" / source.resolve().name
    # The port's folder is reused by the target's next run: open only the change this run made (a diff the run had
    # to cut short can't be compared, so it's taken as is).
    try:
        cut = len(ported["diff"]) >= 300_000
        same = cut or sorted(_diff(source, folder, files).splitlines()) == sorted(ported["diff"].splitlines())
    except OSError:
        same = False
    if not same:
        raise HTTPException(409, f"{run.label} has been ported again since this run, so its pull request is no longer here to "
                                 "open. Run it again and open that one.")
    where = (f"a fork of [{target}](https://github.com/{target}), so its maintainers weren't notified" if local is None
             else "the demo app's own repo, whose main branch is the app before the port")
    note = f"> Opened by OpenPort's hosted demo in `{org}`, an organization for its pull requests, on {where}.\n\n"
    with OPENING:
        if run.github:  # opened while this request waited
            return {"url": run.github, "opened": False}
        try:
            opened = sandbox.open_pr(GITHUB_TOKEN, org, folder, files, report.TITLE, note + pr["body"], f"openport/nemotron-{run.id}",
                                     upstream=None if local else target, original=local, base_commit=None if local else _head(folder))
        except (sandbox.GitHubError, httpx.HTTPError) as error:
            raise HTTPException(502, f"GitHub didn't take the pull request: {error}") from None
        run.emit("github", url=opened.url, repo=opened.repo)
        pipeline._save(run)
    return {"url": opened.url, "opened": opened.new}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    return {**run.summary(), "events": run.events}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: everything so far, then new events as they happen. A recorded run replays with its
    own timing, sped up, so the board fills in the way it did live."""
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    try:  # a client resuming says which event it saw last
        start = max(0, int(request.headers.get("last-event-id", -1)) + 1)
    except ValueError:
        start = 0

    async def events():
        index = start
        while not await request.is_disconnected():
            while index < len(run.events):
                event = run.events[index]
                if run.recorded and index > start:
                    gap = (event.get("time") or 0) - (run.events[index - 1].get("time") or 0)
                    await asyncio.sleep(min(max(gap, 0) / REPLAY_SPEED, REPLAY_MAX_GAP))
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
                index += 1
            if run.status in ("done", "error") and index >= len(run.events):
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        # The path is decoded, so "..%2F" arrives as "../": serve only files that resolve inside the build.
        file = (STATIC / path).resolve()
        if path and file.is_relative_to(STATIC.resolve()) and file.is_file():
            return FileResponse(file)
        # The page names its hashed scripts, so browsers ask for it again each time: a redeploy shows at once.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
