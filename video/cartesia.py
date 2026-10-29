"""Cartesia text-to-speech for the demo video's narration: the voice lookup, and speech with word timestamps.

The API key is read from the file named by CARTESIA_KEY_FILE (default ~/.cartesia) and never printed.
"""

import base64
import json
import os
from pathlib import Path

import httpx

API = "https://api.cartesia.ai"
VERSION = "2026-08-14"
MODEL = "sonic-3.6"
SAMPLE_RATE = 44100


def _headers() -> dict:
    key = Path(os.getenv("CARTESIA_KEY_FILE", Path.home() / ".cartesia")).read_text(encoding="utf-8").strip()
    return {"Authorization": f"Bearer {key}", "Cartesia-Version": VERSION}


def voices(query: str) -> list[dict]:
    """The voices matching `query` (name, description or id), the account's own first."""
    found, cursor = [], None
    while True:
        params = {"q": query, "limit": 100, **({"starting_after": cursor} if cursor else {})}
        page = httpx.get(f"{API}/voices", params=params, headers=_headers(), timeout=30).raise_for_status().json()
        found += page["data"]
        if not page.get("has_more"):
            return sorted(found, key=lambda v: not v["is_owner"])
        cursor = page["next_page"]


def speak(text: str, voice_id: str, out: Path, speed: float = 1.0, emotion: str | None = None) -> list[tuple[str, float, float]]:
    """Synthesize `text` to a 16-bit mono WAV at `out`; returns its words as (word, start, end) in seconds."""
    body = {
        "model_id": MODEL,
        "transcript": text,
        "voice": {"id": voice_id},
        "language": "en",
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "add_timestamps": True,
        "generation_config": {"speed": speed, "volume": 1, **({"emotion": emotion} if emotion else {})},
    }
    pcm, words = bytearray(), []
    with httpx.stream("POST", f"{API}/tts/sse", json=body, headers=_headers(), timeout=120) as response:
        if response.status_code >= 400:
            raise RuntimeError(f"Cartesia {response.status_code}: {response.read().decode(errors='replace')[:400]}")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if event["type"] == "chunk":
                pcm += base64.b64decode(event["data"])
            elif event["type"] == "timestamps":
                stamps = event["word_timestamps"]
                words += zip(stamps["words"], stamps["start"], stamps["end"])
            elif event["type"] == "error":
                raise RuntimeError(f"Cartesia: {event.get('title')}: {event.get('message')}")
    _write_wav(out, bytes(pcm))
    return words


def _write_wav(path: Path, pcm: bytes) -> None:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


if __name__ == "__main__":
    import sys

    for v in voices(sys.argv[1] if len(sys.argv) > 1 else "Nick"):
        print(v["id"], "|", v["name"], "| own" if v["is_owner"] else "| library", "|", v.get("tagline") or "", "|", v.get("status"))
