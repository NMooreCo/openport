"""The demo video's narration, one entry per scene, and the step that voices it.

Every number here is read off the recordings the video shows (docs/VIDEO.md, "Numbers"), the field study
(docs/FIELD-STUDY.md) or the learn-claude-code upgrade recording. Numbers are spelled out so the voice reads them as meant.

Usage: uv run --no-sync python video/script.py narrate [scene ...]   (writes video/out/narration/<scene>.wav + .json)
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "out"
VOICE = "1698ad63-a7a9-4ac4-87d7-f62fcc83490d"  # Nick Moore, on Cartesia

SCENES = [
    ("hook", "Your AI app runs on someone else's model. Their prices. Their roadmap. Their outages. "
             "Switching to open models is a three-line change, and almost nobody makes it, because nobody can prove it won't break."),
    ("reveal", "So I built OpenPort: an agent that proves the switch, then makes it."),
    ("find", "Point it at a repo. It finds every OpenAI and Anthropic call, in Python and TypeScript, "
             "and works out what each one needs: JSON, tools, vision, reasoning."),
    ("test", "Then it runs each call, the app's own code, on NVIDIA Nemotron, served by Nebius Token Factory, against hand-labeled cases. "
             "Swapped in as written, the reply drafter passes nothing."),
    ("tune", "So the agent goes to work. Tavily pulls NVIDIA's own guidance for that exact model. "
             "Nemotron Ultra reads the failures and rewrites the prompt and settings. "
             "Each fix is retested on Nano first, then Super, then Ultra, and it only counts if it passes cases Ultra never saw."),
    ("result", "All five calls move. A plain swap passed seventy percent of their cases; now it's ninety-six, "
               "and ninety-eight on cases tuning never saw. Cost per thousand runs: eleven seventy, down to two twenty-seven."),
    ("code", "Then it writes the code: precise edits, a small runtime that runs Anthropic SDK code on Nemotron, "
             "and a real pull request on GitHub, with every number and every source."),
    ("real", "It works on code I didn't write. Preflight, an open-source AI code reviewer: all four Claude Opus calls move, ninety-four percent cheaper. "
             "Across the twenty most-starred open-source AI apps, OpenPort finds two hundred seven LLM calls, and moves seventy-nine of them as they are."),
    ("ci", "And it keeps you honest. A GitHub Action reruns the evals on every pull request. Break a prompt, and the build fails."),
    ("upgrade", "Nineteen of those twenty apps also pin a dependency with a known vulnerability. So the same agent fixes those too. "
                "On learn-claude-code, a repo with seventy-nine thousand stars, it finds forty-eight advisories, picks fixes that have been out "
                "at least a week, and runs the repo's own CI in a container. The upgrade breaks the build. "
                "Nemotron Super's fix fails, so it's undone. Ultra's fix passes, and it's proven."),
    ("built", "Every model inside OpenPort is Nemotron on Token Factory. Nano reads pricing pages. Super writes test inputs and fixes code. "
              "Ultra tunes prompts and judges answers. Super VL takes the calls that read images. And Tavily grounds every change in a quoted source."),
    ("close", "Open models, proven before you ship. OpenPort. Prove it, then port it. Try it now at nebius dot narralogy dot com."),
]


def narrate(names: list[str], speed: float = 1.0) -> None:
    from cartesia import speak

    for name, text in SCENES:
        if names and name not in names:
            continue
        wav = OUT / "narration" / f"{name}.wav"
        words = speak(text, VOICE, wav, speed=speed)
        (wav.with_suffix(".json")).write_text(json.dumps({"text": text, "words": words}, indent=1), encoding="utf-8")
        end = words[-1][2] if words else 0
        print(f"{name:8} {end:6.2f}s  {len(text.split()):3} words")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    if sys.argv[1:2] == ["narrate"]:
        narrate(sys.argv[2:])
    else:
        sys.exit(__doc__)
