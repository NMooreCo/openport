"""A quiet music bed for the demo video, synthesized so there's nothing to license.

Warm pads over a four-chord loop (Am9, Fmaj7, Cadd9, G6) at 96 BPM; a sub bass, a soft kick and a faint shaker come
in at the reveal and drop out for the close, where the pad resolves. A short noise swell leads into the reveal.
Usage: uv run --no-project --with numpy python video/music.py
"""

import wave

import numpy as np

from timeline import span, total
from script import OUT

SR = 48000
BEAT = 60 / 96
BAR = 4 * BEAT
CHORDS = [  # MIDI notes: pad voicing, and the bass root
    ([57, 60, 64, 67, 71], 33),  # Am9
    ([53, 57, 60, 64, 67], 29),  # Fmaj7
    ([48, 55, 60, 62, 64], 36),  # Cadd9
    ([55, 59, 62, 64, 67], 31),  # G6
]


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def lowpass(x: np.ndarray, cutoff: float, slope: float = 2.0) -> np.ndarray:
    spectrum = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    curve = 1 / np.sqrt(1 + (freqs / cutoff) ** (2 * slope))
    return np.fft.irfft(spectrum * curve[:, None] if x.ndim == 2 else spectrum * curve, n=len(x), axis=0)


def band(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    curve = (1 / np.sqrt(1 + (lo / np.maximum(freqs, 1)) ** 4)) / np.sqrt(1 + (freqs / hi) ** 4)
    return np.fft.irfft(spectrum * curve, n=len(x))


def main() -> None:
    length = total()
    n = int(length * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2))
    rng = np.random.default_rng(7)

    beat_in, beat_out = span("reveal")[0], span("close")[0]
    chord_len = 2 * BAR

    # Pads: each chord swells in, overlaps the next, and the last one holds to the end.
    pad = np.zeros((n, 2))
    starts = np.arange(0, beat_out, chord_len)
    for i, start in enumerate(list(starts) + [beat_out]):
        notes, _ = CHORDS[2] if start == beat_out else CHORDS[i % len(CHORDS)]
        end = length if start == beat_out else start + chord_len
        a, b = int(start * SR), min(n, int((end + 1.4) * SR))
        tt = t[a:b] - start
        env = np.minimum(1, tt / 0.9) * np.where(tt > end - start, np.exp(-(tt - (end - start)) / 0.5), 1.0)
        for k, note in enumerate(notes):
            for side, cents in ((0, -5), (1, 6)):
                f = hz(note) * 2 ** (cents / 1200)
                phase = rng.uniform(0, 2 * np.pi)
                voice = sum(np.sin(2 * np.pi * f * h * tt + phase * h) / h ** 1.7 for h in range(1, 6))
                pad[a:b, side] += 0.06 * env * voice * (1 + 0.15 * np.sin(2 * np.pi * 0.11 * tt + k))
    pad = lowpass(pad, 1900)
    pad *= np.clip(0.55 + 0.45 * t / 10, 0, 1)[:, None]  # the intro swells
    out += pad

    # Sub bass on beats 1 and 3, kick on every beat, a shaker on the off-beats, between the reveal and the close.
    for i, beat in enumerate(np.arange(beat_in, beat_out, BEAT)):
        chord = CHORDS[int(beat // chord_len) % len(CHORDS)]
        a = int(beat * SR)
        if i % 2 == 0:
            m = min(n - a, int(1.3 * SR))
            tt = np.arange(m) / SR
            sub = np.sin(2 * np.pi * hz(chord[1]) * tt) + 0.25 * np.sin(4 * np.pi * hz(chord[1]) * tt)
            out[a:a + m] += (0.16 * np.minimum(1, tt / 0.012) * np.exp(-tt / 0.55) * sub)[:, None]
        m = min(n - a, int(0.35 * SR))
        tt = np.arange(m) / SR
        kick = np.sin(2 * np.pi * np.cumsum(48 + 75 * np.exp(-tt / 0.035)) / SR)
        out[a:a + m] += (0.22 * np.exp(-tt / 0.16) * kick)[:, None]
        h = int((beat + BEAT / 2) * SR)
        m = min(n - h, int(0.09 * SR))
        if m > 0:
            hiss = band(rng.standard_normal(m), 5500, 11000)
            out[h:h + m, 0] += 0.035 * np.exp(-np.arange(m) / SR / 0.025) * hiss
            out[h:h + m, 1] += 0.03 * np.exp(-np.arange(m) / SR / 0.025) * hiss

    # A swell of noise into the reveal.
    a, b = int((beat_in - 1.6) * SR), int(beat_in * SR)
    swell = band(rng.standard_normal(b - a), 1200, 9000)
    ramp = np.linspace(0, 1, b - a) ** 3
    out[a:b, 0] += 0.12 * ramp * swell
    out[a:b, 1] += 0.12 * ramp * np.roll(swell, 211)

    fade = np.clip((length - t) / 3.5, 0, 1) * np.clip(t / 0.6, 0, 1)
    out *= fade[:, None]
    out *= 10 ** (-6 / 20) / np.max(np.abs(out))  # peak at -6 dBFS; the mix sets its level under the voice
    path = OUT / "music.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((out * 32767).astype("<i2").tobytes())
    print(f"{path}: {length:.2f} s")


if __name__ == "__main__":
    main()
