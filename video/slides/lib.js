// Motion for the video's slides. Each slide sets window.DURATION and window.render(t); the renderer calls
// seek(t) once per frame, so every frame is a pure function of time. CUES holds the scene's words with the
// second each is said, relative to the slide's start (set by render_slides.py).
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const ease = {
  out: (t) => 1 - Math.pow(1 - t, 3),
  inOut: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  back: (t) => 1 + 2.4 * Math.pow(t - 1, 3) + 1.4 * Math.pow(t - 1, 2),
  lin: (t) => t,
};
const prog = (t, start, dur, e = "out") => ease[e](clamp((t - start) / dur));
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

function cue(word, nth = 1) {
  const want = word.toLowerCase();
  let seen = 0;
  for (const [said, at] of window.CUES || []) {
    if (said.toLowerCase().replace(/[.,:;!?]+$/, "") === want && ++seen === nth) return at;
  }
  throw new Error(`no cue for ${word}`);
}

// Fade an element up into place from `start`, and out again from `end` if given.
function rise(el, t, start, { dur = 0.55, dist = 28, end = null, outDur = 0.35, scale = 0 } = {}) {
  if (typeof el === "string") el = $(el);
  let p = prog(t, start, dur);
  if (end !== null) p *= 1 - prog(t, end, outDur, "inOut");
  el.style.opacity = p;
  el.style.transform = `translateY(${(1 - p) * dist}px) scale(${1 - scale * (1 - p)})`;
  return p;
}

function count(el, t, start, dur, to, fmt = (n) => Math.round(n).toLocaleString("en-US")) {
  if (typeof el === "string") el = $(el);
  el.textContent = fmt(to * prog(t, start, dur, "out"));
}

// Background: a slow drift of light behind everything, so stills never sit dead.
function backdrop(t) {
  const b = $("#bg");
  if (!b) return;
  const x = 50 + 18 * Math.sin(t * 0.21), y = 40 + 12 * Math.cos(t * 0.17);
  b.style.background = `radial-gradient(1100px 700px at ${x}% ${y}%, rgba(52,211,153,.14), transparent 60%),
    radial-gradient(900px 600px at ${100 - x}% ${100 - y}%, rgba(56,189,248,.10), transparent 60%), #070b14`;
}

window.seek = (t) => {
  backdrop(t);
  window.render(t);
};

const BASE_CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=block');
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 1920px; height: 1080px; overflow: hidden; background: #070b14; color: #e5edf7;
    font-family: Inter, "Segoe UI", system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  #bg { position: absolute; inset: 0; }
  #grid { position: absolute; inset: 0; opacity: .5;
    background-image: radial-gradient(rgba(148,163,184,.13) 1.2px, transparent 1.2px); background-size: 36px 36px;
    mask-image: radial-gradient(ellipse 75% 70% at 50% 45%, #000 40%, transparent 100%); }
  .stage { position: absolute; inset: 0; }
  .mono { font-family: "JetBrains Mono", Consolas, monospace; }
  .emerald { color: #34d399; } .sky { color: #7dd3fc; } .red { color: #f87171; } .muted { color: #8a9ab3; }
  .mark { display: inline-flex; align-items: center; gap: 22px; font-weight: 800; letter-spacing: -0.035em; }
  .mark .glyph { width: 1.05em; height: 1.05em; border-radius: .26em; background: linear-gradient(135deg, #34d399, #0ea5e9);
    display: grid; place-items: center; box-shadow: 0 10px 40px rgba(52,211,153,.35); }
  .mark .glyph svg { width: 62%; height: 62%; }
  .chip { display: inline-flex; align-items: center; gap: 10px; padding: 10px 18px; border-radius: 999px;
    border: 1px solid rgba(148,163,184,.22); background: rgba(15,23,42,.7); font-weight: 600; font-size: 26px; color: #cbd5e1; }
`;
const GLYPH = `<svg viewBox="0 0 24 24" fill="none" stroke="#04121a" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 12h12"/><path d="M12 6l6 6-6 6"/><path d="M20 5v14"/></svg>`;

document.head.insertAdjacentHTML("beforeend", `<style>${BASE_CSS}</style>`);
document.addEventListener("DOMContentLoaded", () => {
  document.body.insertAdjacentHTML("afterbegin", `<div id="bg"></div><div id="grid"></div>`);
  $$(".glyph").forEach((g) => (g.innerHTML = GLYPH));
});
