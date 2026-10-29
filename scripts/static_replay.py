"""Build the dashboard as a static replay: the demos' recorded runs, playable with no server, no key and no credit.

The page, and next to it each recording in examples/recorded with its demo's name and description, go to site/
(or --out), ready for any static host, such as GitHub Pages (.github/workflows/pages.yml). The hosted dashboard
runs things live; this one only replays, so it keeps working whatever happens to the endpoint or the credit.

Usage: uv run python scripts/static_replay.py [--out DIR] [--no-build]
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from openport.server import pipeline, upgrades

ROOT = Path(__file__).resolve().parents[1]
# What a recording mustn't carry onto a public page: a home folder, or anything shaped like a key (as scripts/monday.py).
PRIVATE = re.compile(r"C:\\\\Users|/home/|/Users/|sk-[A-Za-z0-9]{12,}|tvly-[A-Za-z0-9]{8,}|Bearer [A-Za-z0-9._-]{12,}|v1\.[A-Za-z0-9]{20,}")


def replays(recorded: Path) -> list[tuple[dict, dict]]:
    """(index entry, recording) for each recording of a demo the launcher offers, newest first per demo."""
    presets = [*({**p, "kind": "port"} for p in pipeline.PRESETS), *({**p, "kind": "upgrade"} for p in upgrades.PRESETS)]
    found = []
    for path in sorted(recorded.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        if PRIVATE.search(text):
            raise SystemExit(f"{path.name} holds a local path or a key: fix it before it goes on a public page")
        data = json.loads(text)
        kind = data.get("kind", "port")
        preset = next((p for p in presets if p["kind"] == kind and data["target"] in (p["id"], p.get("repo"))), None)
        if preset is None or data.get("status") != "done":
            continue
        github = next((e["url"] for e in reversed(data["events"]) if e["type"] == "github"), None)  # its PR, opened on GitHub
        entry = {"id": data["id"], "preset": preset["id"], "kind": kind, "target": data["target"], "label": preset["label"],
                 "detail": preset["detail"], "created": data["created"], "github": github}
        found.append((entry, data))
    newest = {}
    for entry, data in sorted(found, key=lambda pair: pair[0]["created"]):
        newest[(entry["kind"], entry["preset"])] = (entry, data)
    order = {(p["kind"], p["id"]): i for i, p in enumerate(presets)}  # in the launcher's order
    return sorted(newest.values(), key=lambda pair: order[(pair[0]["kind"], pair[0]["preset"])])


def main(argv: list[str]) -> None:
    out = Path(argv[argv.index("--out") + 1]).resolve() if "--out" in argv else ROOT / "site"
    if "--no-build" not in argv:
        npm = shutil.which("npm") or "npm"
        subprocess.run([npm, "run", "build:static", "--", "--outDir", str(out)], cwd=ROOT / "dashboard", check=True)
    folder = out / "replay"
    folder.mkdir(parents=True, exist_ok=True)
    pairs = replays(pipeline.RECORDED)
    for entry, data in pairs:
        (folder / f"{entry['id']}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    (folder / "index.json").write_text(json.dumps([entry for entry, _ in pairs], indent=1), encoding="utf-8")
    print(f"{len(pairs)} recorded runs in {out}: " + ", ".join(f"{e['label']} ({e['kind']})" for e, _ in pairs))


if __name__ == "__main__":
    main(sys.argv[1:])
