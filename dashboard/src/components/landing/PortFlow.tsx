import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { reducedMotion } from "./motion";

// SupportDesk's recorded run (Oct 28): its five LLM calls, the closed model each one was on, the Nemotron tier its
// tuned prompt passed on, and its held-out pass rate swapped as-is and after tuning.
const CALLS = [
  { name: "draft_reply", file: "replies.py", was: "claude-sonnet-5-5", tier: 2, plain: 0, after: 92 },
  { name: "review_case", file: "policy.py", was: "claude-opus-5-5", tier: 0, plain: 86, after: 100 },
  { name: "triage_ticket", file: "triage.py", was: "gpt-5-mini", tier: 0, plain: 71, after: 94 },
  { name: "handle_refund_request", file: "refunds.py", was: "gpt-5", tier: 1, plain: 96, after: 100 },
  { name: "extract_pii", file: "pii.py", was: "gpt-5-mini", tier: 0, plain: 95, after: 95 },
];
// The pull request opened on GitHub from that run.
const PULL_REQUEST = "https://github.com/narralogy-labs/supportdesk/pull/1";
const TIERS = [
  { name: "Nano", size: "30B-A3B" },
  { name: "Super", size: "120B-A12B" },
  { name: "Ultra", size: "550B-A55B" },
];

// Each call takes a turn: it travels into the port, is tested and tuned there, travels out to its tier and lands.
// After the fifth, the pull request, and the run starts over.
const IN = 1000, TEST = 1300, OUT = 900, LANDED = 700;
const TURN = IN + TEST + OUT + LANDED;
const DONE = 3600;
const LOOP = CALLS.length * TURN + DONE;
const DRIFT = 2600; // ms for the faint dots to cross a path

type Phase = "in" | "test" | "out" | "landed" | "done";
type Moment = { turn: number; phase: Phase };
const FINISHED: Moment = { turn: CALLS.length, phase: "done" };

function momentAt(t: number): Moment & { local: number } {
  const turn = Math.floor(t / TURN);
  if (turn >= CALLS.length) return { ...FINISHED, local: t - CALLS.length * TURN };
  const local = t - turn * TURN;
  const phase = local < IN ? "in" : local < IN + TEST ? "test" : local < IN + TEST + OUT ? "out" : "landed";
  return { turn, phase, local };
}

type Point = [number, number];
type Curve = [Point, Point, Point, Point];

const at = ([a, b, c, d]: Curve, t: number): Point => {
  const u = 1 - t;
  const [wa, wb, wc, wd] = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
  return [wa * a[0] + wb * b[0] + wc * c[0] + wd * d[0], wa * a[1] + wb * b[1] + wc * c[1] + wd * d[1]];
};
const across = (a: Point, d: Point): Curve => {
  const dx = (d[0] - a[0]) * 0.55;
  return [a, [a[0] + dx, a[1]], [d[0] - dx, d[1]], d];
};
const down = (a: Point, d: Point): Curve => {
  const dy = (d[1] - a[1]) * 0.6;
  return [a, [a[0], a[1] + dy], [d[0], d[1] - dy], d];
};
const pathOf = (curve: Curve) => {
  const [a, b, c, d] = curve.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`);
  return `M${a} C${b} ${c} ${d}`;
};
const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);
const vendor = (model: string) => (model.startsWith("claude") ? "Anthropic" : "OpenAI");

/**
 * The hero: SupportDesk's calls on closed models flow into the port, are tested and tuned one at a time, and flow
 * out to the Nemotron tier each one passed on, replaying its recorded run on a loop. With "reduce motion" it shows
 * the run finished.
 */
export function PortFlow() {
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const hub = useRef<HTMLDivElement>(null);
  const gate = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const score = useRef<HTMLSpanElement>(null);
  const rows = useRef<(HTMLLIElement | null)[]>([]);
  const tiers = useRef<(HTMLLIElement | null)[]>([]);
  const curves = useRef<{ ins: Curve[]; outs: Curve[] }>({ ins: [], outs: [] });
  const [paths, setPaths] = useState({ width: 0, height: 0, ins: [] as string[], outs: [] as string[] });
  const [still] = useState(reducedMotion);
  const [moment, setMoment] = useState<Moment>(still ? FINISHED : { turn: 0, phase: "in" });

  // Where the paths run: across, from each call into the port and out to each tier, when the three sit side by
  // side; down, from the list into the port and out to the tiers, when they're stacked.
  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    let live = true;
    const measure = () => {
      if (!live || !gate.current || !list.current || !hub.current) return;
      const base = element.getBoundingClientRect();
      const box = (node: Element) => {
        const r = node.getBoundingClientRect();
        const left = r.left - base.left, top = r.top - base.top;
        return { left, right: left + r.width, top, bottom: top + r.height, x: left + r.width / 2, y: top + r.height / 2, width: r.width };
      };
      const g = box(gate.current);
      let ins: Curve[], outs: Curve[];
      if (matchMedia("(min-width: 1024px)").matches) {
        ins = rows.current.map((row) => { const r = box(row!); return across([r.right, r.y], [g.left, g.y]); });
        outs = tiers.current.map((tier) => { const t = box(tier!); return across([g.right, g.y], [t.left, t.y]); });
      } else {
        const l = box(list.current), h = box(hub.current);
        ins = CALLS.map((_, i) => down([l.left + (l.width * (i + 0.5)) / CALLS.length, l.bottom], [g.x, g.top]));
        outs = tiers.current.map((tier) => { const t = box(tier!); return down([h.x, h.bottom], [t.x, t.top]); });
      }
      curves.current = { ins, outs };
      setPaths({ width: base.width, height: base.height, ins: ins.map(pathOf), outs: outs.map(pathOf) });
      const c = canvas.current;
      if (c) {
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        c.width = Math.round(base.width * dpr);
        c.height = Math.round(base.height * dpr);
        c.getContext("2d")?.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    document.fonts?.ready.then(measure);
    return () => {
      live = false;
      observer.disconnect();
    };
  }, []);

  // One clock drives it all: the turn shown, the score counting up while a call is tuned, and the dots. It stops
  // while the hero is off screen or the tab is hidden, and picks up where it left off.
  useEffect(() => {
    const c = canvas.current, element = root.current;
    const ctx = c?.getContext("2d");
    if (still || !c || !element || !ctx) return;
    let frame = 0, last = performance.now(), clock = 0, visible = true;
    let shown: Moment = { turn: 0, phase: "in" };

    const dot = ([x, y]: Point, r: number, color: string) => {
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    };
    const comet = (curve: Curve, t: number, rgb: string) => {
      for (let n = 14; n >= 0; n--) {
        const v = t - n * 0.016;
        if (v >= 0) dot(at(curve, v), 3 * (1 - n / 17), `rgba(${rgb},${0.9 * (1 - n / 15)})`);
      }
      const [x, y] = at(curve, t);
      const glow = ctx.createRadialGradient(x, y, 0, x, y, 18);
      glow.addColorStop(0, `rgba(${rgb},0.4)`);
      glow.addColorStop(1, `rgba(${rgb},0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(x - 18, y - 18, 36, 36);
    };

    const tick = (now: number) => {
      clock += Math.min(now - last, 100);
      last = now;
      const m = momentAt(clock % LOOP);
      if (m.turn !== shown.turn || m.phase !== shown.phase) {
        shown = { turn: m.turn, phase: m.phase };
        setMoment(shown);
      }
      const call = CALLS[m.turn];
      if (m.phase === "test" && score.current) {
        const t = Math.min(1, (m.local - IN) / (TEST * 0.85));
        score.current.textContent = `${Math.round(call.plain + (call.after - call.plain) * (1 - (1 - t) ** 3))}%`;
      }

      const { ins, outs } = curves.current;
      ctx.clearRect(0, 0, c.width, c.height);
      const drift = clock / DRIFT;
      ins.forEach((curve, i) => {
        for (const k of [0, 0.5]) {
          const t = (drift + k + i * 0.17) % 1;
          dot(at(curve, t), 1.4, `rgba(203,213,225,${0.5 * Math.sin(Math.PI * t)})`);
        }
      });
      outs.forEach((curve, i) => {
        for (const k of [0, 0.5]) {
          const t = (drift + k + i * 0.29) % 1;
          dot(at(curve, t), 1.4, `rgba(52,211,153,${0.65 * Math.sin(Math.PI * t)})`);
        }
      });
      if (m.phase === "in" && ins[m.turn]) comet(ins[m.turn], ease(m.local / IN), "226,232,240");
      if (m.phase === "out" && outs[call.tier]) comet(outs[call.tier], ease((m.local - IN - TEST) / OUT), "52,211,153");

      frame = visible && !document.hidden ? requestAnimationFrame(tick) : 0;
    };
    const wake = () => {
      if (!visible || document.hidden || frame) return;
      last = performance.now();
      frame = requestAnimationFrame(tick);
    };
    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      wake();
    });
    observer.observe(element);
    document.addEventListener("visibilitychange", wake);
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      document.removeEventListener("visibilitychange", wake);
    };
  }, [still]);

  const { turn, phase } = moment;
  const current = phase === "done" ? undefined : CALLS[turn];
  const testing = phase === "test";
  const moved = (i: number) => i < turn || (i === turn && phase === "landed");
  const into = phase === "in" || testing ? turn : -1;
  const outTo = current && (phase === "out" || phase === "landed") ? current.tier : -1;

  return (
    <div ref={root} className="relative mx-auto max-w-xl text-left lg:max-w-5xl">
      <svg className="pointer-events-none absolute inset-0" width={paths.width} height={paths.height} fill="none" aria-hidden>
        {paths.ins.map((d, i) => (
          <path key={`in-${i}`} d={d} strokeWidth={1.25} className={`transition-colors duration-500 ${i === into ? "stroke-white/45" : "stroke-white/[0.09]"}`} />
        ))}
        {paths.outs.map((d, k) => (
          <path key={`out-${k}`} d={d} strokeWidth={1.25} className={`transition-colors duration-500 ${k === outTo ? "stroke-emerald-300/80" : "stroke-emerald-400/20"}`} />
        ))}
      </svg>
      <canvas ref={canvas} className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden />

      <div className="relative grid gap-y-16 lg:grid-cols-[minmax(0,1fr)_15rem_minmax(0,1fr)] lg:items-center lg:gap-x-16">
        <div>
          <p className="mb-3 text-[11px] font-medium tracking-wider text-slate-500 uppercase">Your code · calls on closed models</p>
          <ul ref={list} className="space-y-2">
            {CALLS.map((call, i) => {
              const active = i === turn && (phase === "in" || testing || phase === "out");
              const done = moved(i);
              return (
                <li
                  key={call.name}
                  ref={(node) => { rows.current[i] = node; }}
                  className={`flex items-center justify-between gap-3 rounded-xl border px-3.5 py-2 backdrop-blur-sm transition duration-500 ${
                    active ? "border-emerald-400/40 bg-emerald-400/[0.07] shadow-[0_0_28px_-8px_rgba(52,211,153,0.7)]" : "border-white/[0.08] bg-[#0a0f18]/80"
                  }`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-mono text-[12.5px] text-slate-100">{call.name}</span>
                    <span className="block truncate text-[11px] text-slate-500">{call.file} · {call.was}</span>
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium transition duration-500 ${
                      done ? "bg-emerald-400/15 text-emerald-300" : active ? "bg-amber-400/10 text-amber-300" : "text-slate-500"
                    }`}
                  >
                    {done ? `${TIERS[call.tier].name} ✓` : active ? "testing" : vendor(call.was)}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>

        <div ref={hub} className="flex flex-col items-center">
          <div ref={gate} className="relative grid h-28 w-28 place-items-center lg:h-36 lg:w-36">
            <div className="pointer-events-none absolute -inset-12 rounded-full bg-emerald-500/10 blur-3xl" aria-hidden />
            <div className="absolute inset-0 rounded-full border border-white/10" aria-hidden />
            <div className="gate-ring absolute inset-0 rounded-full" aria-hidden />
            <div className={`gate-ring gate-ring-fast absolute -inset-2 rounded-full transition-opacity duration-500 ${testing ? "opacity-100" : "opacity-0"}`} aria-hidden />
            <div
              className={`absolute inset-[7px] rounded-full border border-white/10 bg-[#060a12] transition-shadow duration-700 ${
                testing ? "shadow-[0_0_60px_-6px_rgba(52,211,153,0.75)]" : "shadow-[0_0_36px_-12px_rgba(52,211,153,0.5)]"
              }`}
              aria-hidden
            />
            <span className="relative flex flex-col items-center gap-1.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-400 to-cyan-500 shadow-[0_0_18px_rgba(52,211,153,0.5)]" aria-hidden>
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="#04121a" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 12h12" /><path d="M12 6l6 6-6 6" /><path d="M20 5v14" />
                </svg>
              </span>
              <span className="font-display text-sm font-semibold text-white">Open<span className="text-emerald-300">Port</span></span>
            </span>
          </div>
          <div className="mt-5 h-[4.5rem] w-60 text-center">
            {current ? (
              <>
                <p className={`text-[11px] font-medium tracking-wider uppercase ${phase === "in" ? "text-slate-400" : testing ? "text-amber-300" : "text-emerald-300"}`}>
                  {phase === "in" ? `Found on ${current.was}` : testing ? "Testing and tuning" : `Passes on Nemotron ${TIERS[current.tier].name}`}
                </p>
                <p className="mt-1 truncate font-mono text-sm text-white">{current.name}</p>
                <p className="mt-1 text-xs text-slate-400 tabular-nums">
                  {phase === "in" ? (
                    current.file
                  ) : (
                    <>
                      as-is {current.plain}% → tuned{" "}
                      <span key={`${turn}-${phase}`} ref={score} className="text-emerald-300">{testing ? current.plain : current.after}%</span>
                      {!testing && <span className="text-emerald-300"> ✓</span>}
                    </>
                  )}
                </p>
              </>
            ) : (
              <>
                <p className="text-[11px] font-medium tracking-wider text-emerald-300 uppercase">Pull request ready</p>
                <p className="mt-1 text-sm text-white">5 of 5 calls moved</p>
                <p className="mt-1 text-xs text-slate-400 tabular-nums">
                  <span className="line-through decoration-red-400/60">$11.70</span> → <span className="text-emerald-300">$2.27</span> per 1,000 runs
                </p>
              </>
            )}
          </div>
        </div>

        {/* Side by side, the tiers fill the calls' height, their label level with the calls'; stacked, the label goes
            under them, out of the paths' way. */}
        <div className="flex flex-col-reverse lg:flex-col lg:self-stretch">
          <p className="mt-3 text-[11px] font-medium tracking-wider text-slate-500 uppercase lg:mt-0 lg:mb-3">NVIDIA Nemotron · Nebius Token Factory</p>
          <ul className="grid grid-cols-3 gap-2 lg:flex-1 lg:grid-cols-1 lg:grid-rows-3 lg:gap-3">
            {TIERS.map((tier, k) => {
              const hit = outTo === k && phase === "landed";
              return (
                <li
                  key={tier.name}
                  ref={(node) => { tiers.current[k] = node; }}
                  className={`rounded-xl border px-3 py-2.5 backdrop-blur-sm transition duration-500 lg:px-4 lg:py-3 ${
                    hit ? "border-emerald-400/60 bg-emerald-400/[0.08] shadow-[0_0_32px_-6px_rgba(52,211,153,0.75)]" : "border-white/[0.08] bg-[#0a0f18]/80"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      <span className="block text-[10.5px] tracking-wider text-slate-500 uppercase">Nemotron 3</span>
                      <span className="block font-display text-base font-semibold text-white lg:text-lg">{tier.name}</span>
                    </span>
                    <span className="flex gap-1">
                      {CALLS.map((call, i) =>
                        call.tier === k ? (
                          <i key={call.name} className={`h-2 w-2 rounded-full transition duration-500 ${moved(i) ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : "bg-white/10"}`} />
                        ) : null,
                      )}
                    </span>
                  </div>
                  <span className="mt-0.5 hidden font-mono text-[11px] text-slate-500 lg:block">{tier.size}</span>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
      <p className="relative mt-10 text-center text-xs text-slate-500">
        SupportDesk's recorded run, replayed: each call moves to the smallest Nemotron that passes its held-out cases.{" "}
        <a href={PULL_REQUEST} target="_blank" rel="noreferrer" className="text-emerald-300 underline decoration-emerald-300/30 underline-offset-2 hover:text-emerald-200">
          See its pull request on GitHub ↗
        </a>
      </p>
    </div>
  );
}
