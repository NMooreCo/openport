"""Cut the demo video together: the clips in order, the narration on its timeline, captions, music, master.

Clips come from capture.py (the dashboard) and render_slides.py (the slides); the music from music.py.
Writes video/out/openport-demo.mp4, a narration-only copy (openport-demo-voice-only.mp4) and a thumbnail.
Usage: uv run --no-project --with numpy python video/build.py
"""

import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from script import OUT, SCENES
from timeline import cue, narration, span, total, voice_starts

FPS = 30
DIP = 0.16  # each cut dips through the dark background for this long on either side
MUSIC_DB = -4  # the bed's level going into the mix, before it ducks under the voice
CAPTIONED = {"find", "test", "tune", "result", "code", "real", "ci", "upgrade", "built"}  # the rest show their words on screen

# (clip, where it starts in the video); each runs until the next one starts
CUTS = [
    ("slide-hook", 0.0),
    ("slide-reveal", span("reveal")[0]),
    ("supportdesk", span("find")[0]),
    ("preflight", span("real")[0]),
    ("slide-field", cue("real", "Across") - 0.25),
    ("slide-ci", span("ci")[0]),
    ("slide-vuln", span("upgrade")[0]),
    ("authkit", cue("upgrade", "So") - 0.35),
    ("slide-built", span("built")[0]),
    ("slide-close", span("close")[0]),
]


def ffmpeg(*args: str) -> str:
    done = subprocess.run(["ffmpeg", "-y", "-hide_banner", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise SystemExit(done.stderr[-3000:])
    return done.stderr


def voice_track() -> Path:
    rate = 44100
    track = np.zeros(int(total() * rate), dtype=np.float32)
    for name, _ in SCENES:
        with wave.open(str(OUT / "narration" / f"{name}.wav")) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        at = int(voice_starts()[name] * rate)
        track[at:at + len(pcm)] += pcm[: len(track) - at]
    path = OUT / "voice.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(track, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _ts(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def captions() -> Path:
    """Short caption lines from the narration's word timings; each word brightens as it's said."""
    events = []
    for name, _ in SCENES:
        if name not in CAPTIONED:
            continue
        words = [(w, voice_starts()[name] + s, voice_starts()[name] + e) for w, s, e in narration(name)["words"]]
        chunks, chunk = [], []
        for i, word in enumerate(words):
            chunk.append(word)
            text = " ".join(w for w, _, _ in chunk)
            nxt = words[i + 1] if i + 1 < len(words) else None
            if (not nxt or len(chunk) >= 7 or len(text) >= 40 or (re.search(r"[.,:;]$", word[0]) and len(chunk) >= 3)
                    or (nxt and nxt[1] - word[2] > 0.45)):
                chunks.append(chunk)
                chunk = []
        for i, chunk in enumerate(chunks):
            start = chunk[0][1] - 0.06
            end = chunk[-1][2] + 0.3
            if i + 1 < len(chunks):
                end = min(end, chunks[i + 1][0][1] - 0.08)
            # Each {\kN} lasts N centiseconds and lights the word after it when it begins: an empty one first holds
            # the line until its first word, then each word lasts until the next one starts.
            line = f"{{\\k{max(0, round((chunk[0][1] - start) * 100))}}}"
            for j, (w, s, e) in enumerate(chunk):
                until = chunk[j + 1][1] if j + 1 < len(chunk) else e
                line += f"{{\\k{max(1, round((until - s) * 100))}}}{w} "
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{line.strip()}")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Segoe UI Semibold,46,&H00FFFFFF,&H00D8C4AE,&H1E140C07,&H00000000,0,0,0,0,100,100,0,0,3,15,0,2,120,120,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path = OUT / "captions.ass"
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def picture(subs: Path | None) -> Path:
    inputs, chains = [], []
    for i, (clip, start) in enumerate(CUTS):
        end = CUTS[i + 1][1] if i + 1 < len(CUTS) else total()
        length = end - start
        inputs += ["-i", str(OUT / "shots" / f"{clip}.mp4")]
        fades = f"fade=t=in:st=0:d={DIP}," if i else ""
        fades += f"fade=t=out:st={length - DIP:.3f}:d={DIP}" if i + 1 < len(CUTS) else "fade=t=out:st=" + f"{length - 1.2:.3f}:d=1.2"
        chains.append(f"[{i}:v]trim=duration={length:.3f},setpts=PTS-STARTPTS,fps={FPS},settb=1/{FPS},{fades}[v{i}]")
    joined = "".join(f"[v{i}]" for i in range(len(CUTS)))
    tail = f"{joined}concat=n={len(CUTS)}:v=1:a=0,fade=t=in:st=0:d=0.4"
    if subs:
        tail += f",subtitles='{subs.as_posix().replace(':', chr(92) + ':')}'"
    out = OUT / ("picture.mp4" if subs else "picture-nocaps.mp4")
    ffmpeg(*inputs, "-filter_complex", ";".join(chains) + ";" + tail + "[v]", "-map", "[v]",
           "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-pix_fmt", "yuv420p", "-r", str(FPS), str(out))
    return out


def mix(voice: Path, with_music: bool) -> Path:
    if with_music:
        graph = ("[0:a]aresample=48000,pan=stereo|c0=c0|c1=c0,asplit=2[v1][v2];"
                 f"[1:a]aresample=48000,volume={MUSIC_DB}dB[m];"
                 "[m][v1]sidechaincompress=threshold=0.05:ratio=3:attack=25:release=450:makeup=1[duck];"
                 "[duck][v2]amix=inputs=2:normalize=0[mix]")
        inputs = ["-i", str(voice), "-i", str(OUT / "music.wav")]
    else:
        graph = "[0:a]aresample=48000,pan=stereo|c0=c0|c1=c0[mix]"
        inputs = ["-i", str(voice)]
    report = ffmpeg(*inputs, "-filter_complex", graph + ";[mix]loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-")
    m = json.loads(report[report.rindex("{"): report.rindex("}") + 1])
    norm = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    out = OUT / ("mix.wav" if with_music else "mix-voice-only.wav")
    ffmpeg(*inputs, "-filter_complex", graph + f";[mix]{norm},aresample=48000[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(out))
    return out


def main(audio_only: bool = False) -> None:
    voice = voice_track()
    video = OUT / "picture.mp4" if audio_only else picture(captions())
    for with_music, name in ((True, "openport-demo.mp4"), (False, "openport-demo-voice-only.mp4")):
        audio = mix(voice, with_music)
        ffmpeg("-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
               "-shortest", "-movflags", "+faststart", str(OUT / name))
        print(f"{OUT / name}")
    ffmpeg("-ss", "4.0", "-i", str(OUT / "shots" / "slide-reveal.mp4"), "-frames:v", "1", "-vf", "scale=1280:720", str(OUT / "thumbnail.png"))


if __name__ == "__main__":
    import sys

    main(audio_only="--audio-only" in sys.argv)  # remix onto the picture already cut
