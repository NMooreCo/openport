# The demo video

The submission video is built from this folder, end to end: narration in Nick's voice (Cartesia), screen captures
of the real dashboard replaying the Oct 28 recordings, motion slides, captions and a synthesized music bed. Every
number it says or shows comes from those recordings, the field study or a real `openport check` run.

| Step | Command | Writes |
|---|---|---|
| Narration | `uv run --no-sync python video/script.py narrate [scene ...]` | `out/narration/<scene>.wav` and word timings |
| Timeline | `uv run --no-sync python video/timeline.py` | prints where each scene sits |
| Dashboard shots | `uv run --no-project --with playwright --with httpx python video/capture.py [shot ...]` | `out/shots/{supportdesk,preflight,upgrade}.mp4` |
| Slides | `uv run --no-project --with playwright python video/render_slides.py [slide ...]` | `out/shots/slide-*.mp4` |
| Music | `uv run --no-project --with numpy python video/music.py` | `out/music.wav` |
| Cut | `uv run --no-project --with numpy python video/build.py` | `out/openport-demo.mp4`, a voice-only copy, `out/thumbnail.png` |

- **The script** is `script.py`: one line of narration per scene. Change a line, re-voice that scene, then rerun
  everything after it: the timeline moves, and the shots, slides and captions follow it.
- **The dashboard shots** are the product itself (`openport serve`), driven in headless Edge. `stage_server.py`
  replays the recorded runs with each event moved onto the narration's cue; the recordings on disk are untouched
  and nothing calls Token Factory. The cursor, spotlights and scrolling come from `overlay.js`.
- **The slides** (`slides/*.html`) are pure functions of time, rendered frame by frame. The CI slide shows the real
  output of `openport check` on SupportDesk with its triage prompt's priority rules deleted (10%, 3 of 31: FAIL).
- **The Cartesia key** is read from `C:\Nick\.cartesia` (or `CARTESIA_KEY_FILE`) and never printed.
- Renders go to `out/`, which git ignores. Captures and slides render in the installed Microsoft Edge.
