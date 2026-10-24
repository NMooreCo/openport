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

from . import pipeline
from .pipeline import Run, resolve_target, run_pipeline

STATIC = Path(__file__).parent / "static"
MAX_QUEUED = 3

app = FastAPI(title="OpenPort", docs_url="/api/docs", openapi_url="/api/openapi.json")
runs: dict[str, Run] = {}


class StartRun(BaseModel):
    target: str


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/presets")
def presets() -> list[dict]:
    return [{k: v for k, v in p.items() if k != "path"} for p in pipeline.PRESETS]


@app.post("/api/runs")
def start_run(body: StartRun) -> dict:
    try:
        target, label, local = resolve_target(body.target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    recent = [
        r for r in runs.values()
        if r.target == target and (r.status in ("queued", "running") or (r.status == "done" and time.time() - r.created < pipeline.REUSE_SECONDS))
    ]
    if recent:  # same target, recently: show that run instead of spending credits again
        return {**recent[-1].summary(), "reused": True}
    if sum(r.status in ("queued", "running") for r in runs.values()) >= MAX_QUEUED:
        raise HTTPException(429, "OpenPort is busy with other runs. Try again in a few minutes.")
    run = Run(id=uuid.uuid4().hex[:12], target=target, label=label)
    runs[run.id] = run
    threading.Thread(target=run_pipeline, args=(run, target, local), daemon=True).start()
    return {**run.summary(), "reused": False}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    return {**run.summary(), "events": run.events}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: everything so far, then new events as they happen."""
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    start = int(request.headers.get("last-event-id", -1)) + 1

    async def events():
        index = start
        while not await request.is_disconnected():
            while index < len(run.events):
                event = run.events[index]
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
