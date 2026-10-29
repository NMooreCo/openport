"""Screen-capture the dashboard shots for the demo video, timed to the narration (timeline.py).

Each shot runs in real time in headless Edge at 1280x720 CSS pixels and 1.5x, so frames come out at 1920x1080.
Frames are taken from the browser's own screencast as they render, then laid onto a steady 30 fps clip that
lasts exactly as long as the shot's place in the video. A local dashboard server (stage_server.py) replays each
recorded run with its events moved onto the narration's cues; nothing calls Token Factory.

Usage: uv run --no-project --with playwright --with httpx python video/capture.py [shot ...]
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from playwright.async_api import Page, async_playwright

from timeline import cue, span

HERE = Path(__file__).parent
ROOT = HERE.parent
OUT = HERE / "out"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
OPEN_DELAY = 0.05  # from a preset's click to its event stream opening (measured: about 0.01 s)
FPS = 30
DEBUG = bool(os.getenv("CAPTURE_DEBUG"))  # print probes of the page's state during a take

RUNS = {"supportdesk": "9b7c60061434", "preflight": "9c19765c2483", "upgrade": "9f2984826d79"}  # the last: learn-claude-code
# Notes when each replayed event reaches the page, to check the schedule held.
ARRIVALS = """(() => {
  window.__arrivals = [];
  const add = EventSource.prototype.addEventListener;
  EventSource.prototype.addEventListener = function (type, handler, options) {
    return add.call(this, type, (event) => { window.__arrivals.push([type, Date.now() / 1000, Number(event.lastEventId)]); handler(event); }, options);
  };
})();"""


@dataclass
class Shot:
    name: str
    start: float  # where the shot sits in the video, seconds
    end: float
    url: str  # the page it opens on, before recording starts
    actions: list = field(default_factory=list)  # (video seconds, JS run in the page)
    events: dict = field(default_factory=dict)  # recorded run id -> its schedule
    click: float = 0.0  # when the click that opens the run's event stream lands

    def at(self, t: float, js: str):
        self.actions.append((t, js))


def schedule(click: float, targets: list[float]) -> list[float]:
    """Each event's delay after its stream opens, given the click that opens it and where each should land."""
    opened, out, last = click + OPEN_DELAY, [], 0.0
    for t in targets:
        last = max(last, t - opened)
        out.append(round(last, 3))
    return out


def v(call: str) -> str:
    return f"window.__video.{call}"


def supportdesk() -> Shot:
    F, T, U, R, C = "find", "test", "tune", "result", "code"
    shot = Shot("supportdesk", span(F)[0], span(C)[1], f"{BASE}/#app")
    click = cue(F, "repo.") + 0.15
    shot.at(cue(F, "Point") - 0.1, v('moveTo("SupportDesk", "button", 800)'))
    shot.at(click, v('click("SupportDesk", "button")'))
    shot.at(cue(F, "works") - 0.2, v('scrollToText("LLM calls found", "div.panel", 1400, 64)'))
    shot.at(cue(F, "JSON") - 0.1, v('spot("OpenAI · json", null, 1000, 6)'))
    shot.at(cue(F, "tools") - 0.1, v('spot("OpenAI · tools", null, 1400, 6)'))
    shot.at(span(T)[0] + 0.1, v("scrollTo(0, 900)"))
    follow_card = cue(T, "against") - 0.3
    follow_result = cue(U, "passes") + 0.6
    shot.at(follow_card, v(f'pin("replies.draft_reply", "article", 64, {(follow_result - follow_card) * 1000:.0f}, null, 0.07)'))
    shot.at(follow_result, v(f'pin("Passes on", null, 430, {(span(R)[0] - follow_result) * 1000:.0f}, "replies.draft_reply")'))
    shot.at(cue(T, "passes") - 0.15, v('spot("Nano 0%", "li", 1900, 6)'))
    shot.at(cue(U, "Tavily") + 0.3, v('spot("Tavily searched", "li", 2900, 6, "replies.draft_reply")'))
    shot.at(cue(U, "Nano") - 0.25, v('spot("Nano 0%", "ul, ol", 3700, 8)'))
    shot.at(cue(U, "passes") + 0.9, v('spot("Passes on", null, 2400, 8, "replies.draft_reply")'))
    shot.at(span(R)[0] + 0.05, v("scrollTo(0, 1000)"))
    shot.at(cue(R, "plain") - 0.1, v('spot("Plain swap passes", "div.panel", 2500, 6)'))
    shot.at(cue(R, "now") - 0.1, v('spot("After OpenPort", "div.panel", 3600, 6)'))
    shot.at(cue(R, "Cost") - 0.15, v('spot("Cost per 1k", "div.panel", 3900, 6)'))
    shot.at(span(C)[0] + 0.05, v('scrollToText("Code changes", "section", 1200, 70)'))
    shot.at(cue(C, "writes") + 0.2, v('moveTo("Code changes", "button", 700)'))
    shot.at(cue(C, "code:") + 0.35, v('click("Code changes", "button")'))
    shot.at(cue(C, "precise") + 0.2, v("scrollBy(520, 4300)"))
    shot.at(cue(C, "and") - 0.9, v('scrollToText("Code changes", "section", 700, 70)'))
    shot.at(cue(C, "and") - 0.3, v('moveTo("Pull request", "button", 500)'))
    shot.at(cue(C, "real") - 0.05, v('click("Pull request", "button")'))
    shot.at(cue(C, "GitHub,") - 0.2, v('spot("Opened on GitHub", "a", 1300, 6)'))
    shot.at(cue(C, "number") - 0.2, v('spot("Call", "table", 1900, 8)'))

    tune_events = {  # event index -> where it lands
        5: cue(T, "runs") + 0.3, 6: cue(T, "NVIDIA"), 7: cue(T, "Factory,"), 8: cue(T, "hand-labeled"), 9: cue(T, "reply"),
        10: cue(U, "Tavily"), 11: cue(U, "rewrites"), 12: cue(U, "settings."),
        13: cue(U, "Super"), 14: cue(U, "Super") + 0.3, 15: cue(U, "Super") + 0.55, 16: cue(U, "Super") + 0.8,
        17: cue(U, "Ultra", 2), 18: cue(U, "Ultra", 2) + 0.3, 19: cue(U, "Ultra", 2) + 0.55, 20: cue(U, "Ultra", 2) + 0.8,
        21: cue(U, "only"), 22: cue(U, "only") + 0.35, 23: cue(U, "counts"), 24: cue(U, "counts") + 0.1,
        25: cue(U, "if") + 0.1, 26: cue(U, "passes"),
    }
    targets = [click + OPEN_DELAY + i * 0.15 for i in range(5)]
    targets += [tune_events[i] for i in range(5, 27)]
    targets += [cue(U, "passes") + 0.4 + 0.1 * i for i in range(5)]  # tuned
    targets += [span(R)[0] + 0.6 + 0.15 * i for i in range(6)]  # cost, port, ported, pr stage, pr, done
    shot.click, shot.events[RUNS["supportdesk"]] = click, schedule(click, targets)
    return shot


def preflight() -> Shot:
    S = "real"
    shot = Shot("preflight", span(S)[0], cue(S, "Across") - 0.25, f"{BASE}/#app")
    click = cue(S, "write.") - 0.2
    shot.at(span(S)[0] + 0.3, v('moveTo("jsliapark/preflight", "button", 900)'))
    shot.at(click, v('click("jsliapark/preflight", "button")'))
    shot.at(cue(S, "move,") + 0.1, v('spot("After OpenPort", "div.panel", 1900, 6)'))
    shot.at(cue(S, "ninety-four") - 0.1, v('spot("Cost per 1k", "div.panel", 2000, 6)'))
    first, settled = cue(S, "Preflight,") - 0.3, cue(S, "move,") - 1.4
    targets = [click + OPEN_DELAY + i * 0.12 for i in range(7)]
    targets += [first + (settled - first) * i / 7 for i in range(8)]  # the eight attempts
    targets += [settled + 0.08 * i for i in range(4)]  # tuned
    targets += [settled + 0.4 + 0.08 * i for i in range(6)]  # cost, port, ported, pr stage, pr, done
    shot.click, shot.events[RUNS["preflight"]] = click, schedule(click, targets)
    return shot


def upgrade() -> Shot:
    S = "upgrade"
    shot = Shot("upgrade", cue(S, "So") - 0.35, span(S)[1], f"{BASE}/#upgrade")
    click = cue(S, "On") + 0.05
    shot.at(cue(S, "So") - 0.2, v('moveTo("shareAI-lab/learn-claude-code", "button", 1100)'))
    shot.at(click, v('click("shareAI-lab/learn-claude-code", "button")'))
    shot.at(cue(S, "forty-eight") + 0.1, v('spot("Known advisories", "div.panel", 1300, 6)'))
    shot.at(cue(S, "picks") - 0.1, v('spot("next", "article", 2300, 6)'))
    follow = cue(S, "container.") + 0.25  # once the changelogs are in above the tests, so the view moves once
    shot.at(follow, v(f'pin("The repo", "h2", 64, {(shot.end - follow) * 1000:.0f}, null, 0.22)'))
    shot.at(cue(S, "breaks") + 0.05, v('spot("After the version bump", "li", 1900, 6)'))
    shot.at(cue(S, "Super's") - 0.1, v('spot("Fix 1", "li", 2600, 6)'))
    shot.at(cue(S, "Ultra's") - 0.1, v('spot("Fix 2", "li", 3000, 6)'))
    targets = [click + OPEN_DELAY, click + OPEN_DELAY + 0.1, click + OPEN_DELAY + 0.3,  # fetch, find, deps
               cue(S, "finds") + 0.1, cue(S, "forty-eight"),  # advisories, test stage
               cue(S, "CI") + 0.2, cue(S, "container.") - 0.1, cue(S, "container.") + 0.2,  # before, bumped, changelogs
               cue(S, "breaks"), cue(S, "breaks") + 0.1, cue(S, "build.") + 0.3,  # after the bump, fix stage, research
               cue(S, "fails,"), cue(S, "passes,"),  # Super's fix, undone; Ultra's, passing
               cue(S, "passes,") + 0.05, cue(S, "passes,") + 0.1, cue(S, "passes,") + 0.2, cue(S, "proven."), cue(S, "proven.") + 0.1]
    shot.click, shot.events[RUNS["upgrade"]] = click, schedule(click, targets)
    return shot


SHOTS = {"supportdesk": supportdesk, "preflight": preflight, "upgrade": upgrade}


async def record(page: Page, shot: Shot, frames: Path) -> tuple[float, list[tuple[float, Path]]]:
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob("*.jpg"):
        old.unlink()
    cdp = await page.context.new_cdp_session(page)
    got: list[tuple[float, Path]] = []

    def on_frame(params: dict) -> None:
        path = frames / f"{len(got):05d}.jpg"
        path.write_bytes(base64.b64decode(params["data"]))
        got.append((params["metadata"].get("timestamp") or time.time(), path))
        asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

    cdp.on("Page.screencastFrame", on_frame)
    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 93, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
    started_wall, started = time.time(), time.monotonic()
    failures = []

    async def run(js: str) -> None:
        try:
            if js.startswith("log:"):  # a probe: print what the page says at this moment
                print(f"  @{time.monotonic() - started:6.2f}  {await page.evaluate(js[4:])}")
                return
            await page.evaluate(js)
        except Exception as error:  # a missed spotlight shouldn't stop the take; it's reported below
            failures.append(f"{js}: {str(error).splitlines()[0]}")

    for t, js in sorted(shot.actions, key=lambda a: a[0]):
        await asyncio.sleep(max(0.0, (t - shot.start) - (time.monotonic() - started)))
        asyncio.ensure_future(run(js))
    await asyncio.sleep(max(0.0, (shot.end - shot.start) - (time.monotonic() - started)))
    await cdp.send("Page.stopScreencast")
    for failure in failures:
        print(f"  ! {failure}")
    return started_wall, got


def assemble(frames: list[tuple[float, Path]], started: float, length: float, out: Path) -> None:
    """Lay the frames onto a steady 30 fps clip exactly `length` seconds long."""
    listing = out.with_suffix(".txt")
    lines = []
    for i, (ts, path) in enumerate(frames):
        begin = 0.0 if i == 0 else ts - started
        end = frames[i + 1][0] - started if i + 1 < len(frames) else length
        lines += [f"file '{path.as_posix()}'", f"duration {max(end - begin, 0.001):.4f}"]
    lines.append(f"file '{frames[-1][1].as_posix()}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-vf", f"fps={FPS},scale=1920:1080:flags=lanczos,format=yuv420p", "-t", f"{length:.3f}",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-r", str(FPS), str(out)], check=True)


async def main(names: list[str]) -> None:
    shots = [SHOTS[n]() for n in (names or SHOTS)]
    plan = OUT / "capture-schedule.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({run: times for s in shots for run, times in s.events.items()}), encoding="utf-8")
    server = subprocess.Popen(["uv", "run", "--no-sync", "--project", str(ROOT), "python", str(HERE / "stage_server.py"), str(plan), str(PORT)],
                              cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="msedge", headless=True)
            for shot in shots:
                context = await browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5, color_scheme="dark")
                await context.add_init_script(path=str(HERE / "overlay.js"))
                await context.add_init_script(ARRIVALS)
                page = await context.new_page()
                opened = []
                page.on("request", lambda r: opened.append(time.time()) if "/events" in r.url else None)
                await page.goto(shot.url, wait_until="networkidle")
                await page.wait_for_timeout(600)
                print(f"{shot.name}: {shot.end - shot.start:.2f} s from {shot.start:.2f}")
                started, frames = await record(page, shot, OUT / "frames" / shot.name)
                if opened:
                    print(f"  event stream opened {opened[0] - started:.2f} s in (planned {shot.click - shot.start + OPEN_DELAY:.2f})")
                print(f"  {len(frames)} frames ({len(frames) / (shot.end - shot.start):.1f} per second)")
                arrivals = await page.evaluate("window.__arrivals")
                planned = [shot.click + OPEN_DELAY + d for d in next(iter(shot.events.values()))]
                # matched on each event's id (its index in the recording): the page doesn't listen for every type
                late = [(round(wall - started + shot.start - planned[seq], 2), seq, kind) for kind, wall, seq in arrivals if seq < len(planned)]
                if late:
                    print(f"  events: {len(arrivals)} of {len(planned)} arrived; latest {max(late)} (seconds late, index, type)")
                    if DEBUG:
                        print("  " + " ".join(f"{i}:{d:+.2f}" for d, i, _ in late))
                assemble(frames, started, shot.end - shot.start, OUT / "shots" / f"{shot.name}.mp4")
                await context.close()
            await browser.close()
    finally:  # uv starts the server as a child of its own: end the whole tree
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)


if __name__ == "__main__":
    (OUT / "shots").mkdir(parents=True, exist_ok=True)
    asyncio.run(main(sys.argv[1:]))
