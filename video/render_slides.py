"""Render the video's motion slides (video/slides/*.html) to clips, one frame at a time.

Each slide gets the words of its scene with the second each is said, relative to where the slide starts in the
video (timeline.py), then is stepped through at 30 fps: the page's seek(t) poses it and a screenshot is taken.

Usage: uv run --no-project --with playwright python video/render_slides.py [slide ...]
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from timeline import cue, narration, span, voice_starts

HERE = Path(__file__).parent
OUT = HERE / "out"
FPS = 30

# slide -> (scene whose words it follows, where it starts and ends in the video)
SLIDES = {
    "hook": ("hook", *span("hook")),
    "reveal": ("reveal", *span("reveal")),
    "field": ("real", cue("real", "Across") - 0.25, span("real")[1]),
    "ci": ("ci", *span("ci")),
    "vuln": ("upgrade", span("upgrade")[0], cue("upgrade", "So") - 0.35),
    "built": ("built", *span("built")),
    "close": ("close", *span("close")),
}


async def render(browser, name: str, gate: asyncio.Semaphore) -> None:
    scene, start, end = SLIDES[name]
    cues = [[w, round(voice_starts()[scene] + s - start, 3)] for w, s, _ in narration(scene)["words"]]
    frames = OUT / "frames" / f"slide-{name}"
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob("*.jpg"):
        old.unlink()
    async with gate:
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.add_init_script(f"window.CUES = {json.dumps(cues)};")
        await page.goto((HERE / "slides" / f"{name}.html").as_uri(), wait_until="networkidle")
        await page.evaluate("Promise.all(['400 20px Inter', '800 40px Inter', '900 40px Inter', '500 20px \"JetBrains Mono\"'].map((f) => document.fonts.load(f)))")
        await page.evaluate("document.fonts.ready")
        if not await page.evaluate("document.fonts.check('800 40px Inter')"):
            print(f"  ! {name}: Inter didn't load; falling back to system fonts")
        count = round((end - start) * FPS)
        for i in range(count):
            await page.evaluate(f"seek({i / FPS})")
            (frames / f"{i:05d}.jpg").write_bytes(await page.screenshot(type="jpeg", quality=94))
        await page.close()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames / "%05d.jpg"),
                    "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", str(OUT / "shots" / f"slide-{name}.mp4")], check=True)
    print(f"{name}: {count} frames, {end - start:.2f} s")


async def main(names: list[str]) -> None:
    (OUT / "shots").mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        gate = asyncio.Semaphore(4)
        await asyncio.gather(*(render(browser, n, gate) for n in (names or SLIDES)))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
