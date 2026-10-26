"""The static replay (scripts/static_replay.py): each demo's newest recording, under its launcher name, and nothing
with a local path or a key in it."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("static_replay", ROOT / "scripts" / "static_replay.py")
static_replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(static_replay)


def recording(folder: Path, name: str, **fields) -> None:
    data = {"id": name, "kind": "port", "target": "supportdesk", "label": "x", "status": "done", "created": 1.0,
            "events": [{"seq": 0, "type": "done", "time": 1.0}], **fields}
    (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_each_demo_gets_its_newest_recording_under_its_launcher_name(tmp_path):
    recording(tmp_path, "aaa111", created=1.0)
    recording(tmp_path, "bbb222", created=2.0)  # a newer run of the same demo
    recording(tmp_path, "ccc333", target="jsliapark/preflight")  # a repo preset, recorded by its repo
    recording(tmp_path, "ddd444", target="someone/else")  # not a demo the launcher offers
    recording(tmp_path, "eee555", kind="upgrade", target="authkit", status="error")  # a failed run
    entries = [entry for entry, _ in static_replay.replays(tmp_path)]
    assert [(e["id"], e["preset"]) for e in entries] == [("bbb222", "supportdesk"), ("ccc333", "preflight")]
    assert entries[0]["label"] == "SupportDesk" and "118 labelled" in entries[0]["detail"]


def test_a_recording_with_a_local_path_or_key_stops_the_build(tmp_path):
    recording(tmp_path, "aaa111", events=[{"seq": 0, "type": "note", "message": "C:\\Users\\nick\\app"}])
    with pytest.raises(SystemExit, match="local path or a key"):
        static_replay.replays(tmp_path)


def test_the_shipped_recordings_all_make_it():
    entries = [entry for entry, _ in static_replay.replays(ROOT / "examples" / "recorded")]
    assert {(e["kind"], e["preset"]) for e in entries} >= {("upgrade", "authkit"), ("upgrade", "authkit-node")}
