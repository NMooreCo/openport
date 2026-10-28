"""The dashboard server for the video's screen captures: the recorded runs only, each replayed on a schedule.

The schedule (JSON: {run id: [seconds after its event stream opens, one per event]}) moves when each recorded
event reaches the page, so the board fills in on the narration's cues. The recordings on disk are untouched.
Usage: uv run --no-sync python video/stage_server.py SCHEDULE.json PORT
"""

import json
import sys
from pathlib import Path

import uvicorn

from openport import server

schedule = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
server.REPLAY_SPEED, server.REPLAY_MAX_GAP = 1, 1e9  # the gaps below are the real delays
server.runs = {run_id: run for run_id, run in server.runs.items() if run.recorded}
for run_id, times in schedule.items():
    for event, at in zip(server.runs[run_id].events, times):
        event["time"] = at

uvicorn.run(server.app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
