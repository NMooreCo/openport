"""The OpenPort dashboard: start a run on a repo and watch scan, tuning, port and PR happen live."""

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from . import pipeline, upgrades
from .pipeline import Run, load_runs, resolve_target, run_pipeline

STATIC = Path(__file__).parent / "static"
MAX_QUEUED = 3
IN_FLIGHT_USD = 1.0  # what a live run may still spend, counted against the daily budget until it finishes
REPLAY_SPEED, REPLAY_MAX_GAP = 20, 1.5  # a recorded run replays 20x faster, no pause over 1.5 s

app = FastAPI(title="OpenPort", docs_url="/api/docs", openapi_url="/api/openapi.json")
# Finished runs survive a restart; recorded runs of the demos come with the server.
runs: dict[str, Run] = {r.id: r for r in [*load_runs(config.RUNS_DIR / "dashboard"), *load_runs(pipeline.RECORDED, recorded=True)]}


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


def spent_today() -> float:
    """Live runs' Token Factory spend since midnight UTC, counting each unfinished run at IN_FLIGHT_USD."""
    midnight = time.time() // 86400 * 86400
    today = [r for r in runs.values() if not r.recorded and r.created >= midnight]
    return sum(r.cost_usd if r.status in ("done", "error") else IN_FLIGHT_USD for r in today)


def _preset(preset: dict) -> dict:
    target, _, local = resolve_target(preset["id"])
    return {**{k: v for k, v in preset.items() if k != "path"}, "tested": pipeline.tests_target(target, local)}


@app.get("/api/presets")
def presets() -> list[dict]:
    return [_preset(p) for p in pipeline.PRESETS]


@app.get("/api/upgrade-presets")
def upgrade_presets() -> list[dict]:
    return [{k: v for k, v in p.items() if k != "path"} for p in upgrades.PRESETS]


def _launch(kind: str, target: str, label: str, local: Path | None, runner, live: bool = False) -> dict:
    same = [r for r in runs.values() if r.kind == kind and r.target == target]
    shown = [r for r in same if r.status in ("queued", "running")]  # already running: watch that one
    if not live:  # else a recent run of it, or the recorded one, instead of spending credits again
        shown = shown or [r for r in same if r.status == "done" and not r.recorded and time.time() - r.created < pipeline.REUSE_SECONDS]
        shown = shown or [r for r in same if r.status == "done" and r.recorded]
    if shown:
        return {**shown[-1].summary(), "reused": True}
    if pipeline.DAILY_BUDGET_USD is not None and spent_today() >= pipeline.DAILY_BUDGET_USD:
        raise HTTPException(429, f"This demo's Token Factory budget for today (${pipeline.DAILY_BUDGET_USD:g}) is used up. "
                                 "The recorded runs of the demo apps are still here, and OpenPort runs anywhere: see the README.")
    if sum(r.status in ("queued", "running") for r in runs.values()) >= MAX_QUEUED:
        raise HTTPException(429, "OpenPort is busy with other runs. Try again in a few minutes.")
    run = Run(id=uuid.uuid4().hex[:12], target=target, label=label, kind=kind)
    runs[run.id] = run
    threading.Thread(target=runner, args=(run, target, local), daemon=True).start()
    return {**run.summary(), "reused": False}


@app.post("/api/runs")
def start_run(body: StartRun) -> dict:
    try:
        target, label, local = resolve_target(body.target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _launch("port", target, label, local, run_pipeline, body.live)


@app.post("/api/upgrades")
def start_upgrade(body: StartRun) -> dict:
    try:
        target, label, local = upgrades.resolve_target(body.target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _launch("upgrade", target, label, local, upgrades.run_pipeline, body.live)


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
    start = int(request.headers.get("last-event-id", -1)) + 1

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
        file = STATIC / path
        return FileResponse(file if path and file.is_file() else STATIC / "index.html")
