"""Where every scene sits in the finished video, from the narration's own word timings.

Each scene's voice starts after the previous one ends plus a short breath; a scene's pictures run from halfway
through the breath before it to halfway through the breath after. `cue(scene, word)` is the moment a word is said.
"""

import json
import wave
from functools import cache
from pathlib import Path

from script import OUT, SCENES

LEAD = 0.8  # picture before the first word
BREATH = {"reveal": 0.9, "find": 0.7, "real": 0.8, "ci": 0.7, "upgrade": 0.8, "built": 0.8, "close": 1.0}
DEFAULT_BREATH = 0.55
TAIL = 3.0  # the end card holds after the last word


@cache
def narration(scene: str) -> dict:
    path = OUT / "narration" / f"{scene}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    with wave.open(str(path.with_suffix(".wav"))) as w:
        data["length"] = w.getnframes() / w.getframerate()
    return data


@cache
def voice_starts() -> dict[str, float]:
    starts, t = {}, LEAD
    for i, (name, _) in enumerate(SCENES):
        if i:
            t += BREATH.get(name, DEFAULT_BREATH)
        starts[name] = t
        t += narration(name)["length"]
    return starts


def total() -> float:
    last = SCENES[-1][0]
    return voice_starts()[last] + narration(last)["length"] + TAIL


def span(scene: str) -> tuple[float, float]:
    """The scene's pictures: (start, end) in seconds of the finished video."""
    names = [n for n, _ in SCENES]
    i = names.index(scene)
    start = 0.0 if i == 0 else voice_starts()[scene] - BREATH.get(scene, DEFAULT_BREATH) / 2
    if i + 1 == len(names):
        return start, total()
    following = names[i + 1]
    return start, voice_starts()[following] - BREATH.get(following, DEFAULT_BREATH) / 2


def cue(scene: str, word: str, nth: int = 1) -> float:
    """When `word` (its nth occurrence in the scene, ignoring case and punctuation) starts, in video seconds."""
    want, seen = word.lower().strip(".,:;"), 0
    for said, start, _ in narration(scene)["words"]:
        if said.lower().strip(".,:;!?") == want:
            seen += 1
            if seen == nth:
                return voice_starts()[scene] + start
    raise KeyError(f"{word!r} isn't said {nth} time(s) in {scene}")


if __name__ == "__main__":
    for name, _ in SCENES:
        a, b = span(name)
        print(f"{name:8} {a:7.2f} - {b:7.2f}  ({b - a:5.2f} s)  voice at {voice_starts()[name]:7.2f}")
    print(f"total {total():.2f} s")
