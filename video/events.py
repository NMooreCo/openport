"""List a recording's events, to pick the moments the video's capture schedule lands on."""

import json
import sys

for path in sys.argv[1:]:
    events = json.load(open(path, encoding="utf-8"))["events"]
    t0 = events[0].get("time", 0)
    print(f"== {path}: {len(events)} events over {events[-1].get('time', 0) - t0:.0f} s of real time")
    for i, e in enumerate(events):
        detail = {k: e[k] for k in ("stage", "site", "tier", "round", "status", "step", "ok", "summary") if k in e}
        print(f"{i:3} {e.get('time', 0) - t0:7.1f}  {e['type']:12} {json.dumps(detail)[:150]}")
