import { useState, type ReactNode } from "react";
import * as api from "../api";
import { useSite } from "../site";
import { JudgeUnlock } from "./JudgeUnlock";
import type { Mode } from "./Launcher";
import { CountUp, Reveal, useCount, useInView } from "./landing/motion";
import { NetworkCanvas } from "./landing/NetworkCanvas";
import { PrCard } from "./landing/PrCard";

const REPO = "https://github.com/NMooreCo/openport";

// Every number on this page is read off the recorded runs the demos replay, the field study, or a real `openport check`.
const STEPS = [
  { name: "Find", text: "Parses Python and TypeScript for every OpenAI and Anthropic call, on the SDKs or plain HTTP, and works out what each needs: JSON, tools, vision, reasoning." },
  { name: "Test", text: "Runs each call's own code on NVIDIA Nemotron, served by Nebius Token Factory, against labelled cases. A plain swap is the baseline." },
  { name: "Tune", text: "Tavily pulls NVIDIA's guidance for the exact model; Nemotron Ultra rewrites the prompt and settings from the failures." },
  { name: "Port", text: "Precise edits, plus a small runtime that runs Anthropic SDK code on Nemotron. A GitHub Action reruns the evals on every PR." },
  { name: "Prove", text: "A pull request with each call's pass rates, its cost before and after, and every source quoted word for word." },
];

const RESULTS = [
  { title: "SupportDesk", what: "5 calls on OpenAI and Anthropic", to: 96, from: 70, format: (n: number) => `${Math.round(n)}%`, detail: "of test cases pass after tuning, up from 70% on a plain swap; 98% of held-out ones" },
  { title: "preflight", what: "an open-source AI PR reviewer I didn't write", to: 94, format: (n: number) => `${Math.round(n)}%`, detail: "cheaper: all 4 Claude Opus calls on Nemotron, $22.08 → $1.32 per 1,000 runs" },
  { title: "Field study", what: "the 20 most-starred open-source AI apps", to: 207, format: (n: number) => `${Math.round(n)}`, detail: "LLM calls found; 79 move to Nemotron as they are, and every other one says why it stays" },
  { title: "AuthKit upgrade", what: "15 advisories in PyJWT and requests", to: 15, format: (n: number) => `${Math.round(n)}`, detail: "advisories fixed, proven by the app's own tests: 5 pass, 4 break, then all 5 pass after Super's fix" },
];

const MODELS = [
  { name: "Nemotron 3 Nano", tag: "fast", role: "Reads closed-model pricing pages. The first rung every call is tested on." },
  { name: "Nemotron 3 Super", tag: "balanced", role: "Writes realistic test inputs, and fixes the code a dependency upgrade broke." },
  { name: "Nemotron 3 Ultra", tag: "reasoning", role: "Tunes prompts and settings from failures, and judges free-text answers." },
  { name: "Nemotron 3.5 Super VL", tag: "vision", role: "Takes the calls that read images, like the receipt reader." },
  { name: "Nebius Token Factory", tag: "inference", role: "Serves every model call, with live prices from its catalog." },
  { name: "Tavily", tag: "research", role: "Grounds every change in a quoted source: NVIDIA's guidance, changelogs, pricing pages." },
];

const DEMOS: { target: string; mode: Mode; label: string; detail: string }[] = [
  { target: "supportdesk", mode: "port", label: "SupportDesk", detail: "5 calls on OpenAI + Anthropic, 118 labelled cases" },
  { target: "preflight", mode: "port", label: "jsliapark/preflight", detail: "an open-source AI PR reviewer: 4 Claude Opus calls" },
  { target: "receipts", mode: "port", label: "Receipt reader", detail: "a gpt-4o-mini call that reads photos, on Super VL" },
  { target: "authkit", mode: "upgrade", label: "AuthKit upgrade", detail: "vulnerable PyJWT and requests, proven by its tests" },
];

function youtubeId(url: string): string | null {
  return url.match(/(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|embed\/|shorts\/))([\w-]{11})/)?.[1] ?? null;
}

const scrollTo = (id: string) => (e: { preventDefault: () => void }) => {
  e.preventDefault();
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
};

function Heading({ kicker, title, children }: { kicker: string; title: ReactNode; children?: ReactNode }) {
  return (
    <Reveal className="max-w-3xl">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-400">{kicker}</p>
      <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-white sm:text-5xl">{title}</h2>
      {children && <p className="mt-4 text-lg leading-relaxed text-slate-300">{children}</p>}
    </Reveal>
  );
}

/** A labelled bar that fills to `value` (of `max`) when it scrolls into view. */
function Bar({ label, value, max, shown, good = false, delay = 0 }: {
  label: string; value: number; max: number; shown: (n: number) => string; good?: boolean; delay?: number;
}) {
  const [ref, seen] = useInView<HTMLDivElement>();
  const n = useCount(seen, value, { ms: 1300, delay });
  return (
    <div ref={ref}>
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className={good ? "text-white" : "text-slate-400"}>{label}</span>
        <span className={`font-mono tabular-nums ${good ? "text-emerald-300" : "text-slate-400"}`}>{shown(n)}</span>
      </div>
      <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-white/5">
        <div
          className={`h-full rounded-full ${good ? "bg-gradient-to-r from-emerald-400 to-cyan-400 shadow-[0_0_18px_rgba(52,211,153,0.6)]" : "bg-slate-500/50"}`}
          style={{ width: `${(n / max) * 100}%` }}
        />
      </div>
    </div>
  );
}

function Pipeline() {
  return (
    <div className="mt-14">
      <div className="relative hidden h-12 md:block" aria-hidden>
        <div className="absolute top-1/2 right-[10%] left-[10%] h-px -translate-y-1/2 bg-gradient-to-r from-emerald-400/10 via-emerald-400/50 to-cyan-400/10" />
        <div className="absolute top-1/2 right-[10%] left-[10%] -translate-y-1/2">
          <span className="travel absolute -top-1 h-2 w-16 -translate-x-1/2 rounded-full bg-gradient-to-r from-transparent via-emerald-300 to-transparent blur-[1px]" />
        </div>
        <div className="absolute inset-0 grid grid-cols-5">
          {STEPS.map((step, i) => (
            <div key={step.name} className="flex items-center justify-center">
              <span className="ring-pulse flex h-10 w-10 items-center justify-center rounded-full border border-emerald-400/50 bg-[#06110d] font-mono text-xs text-emerald-300" style={{ animationDelay: `${i * 0.72}s` }}>
                {String(i + 1).padStart(2, "0")}
              </span>
            </div>
          ))}
        </div>
      </div>
      <ol className="mt-6 grid gap-4 md:grid-cols-5">
        {STEPS.map((step, i) => (
          <li key={step.name}>
            <Reveal delay={i * 90} className="glass h-full rounded-2xl p-5 transition hover:border-emerald-400/40">
              <span className="font-mono text-xs text-emerald-400 md:hidden">{String(i + 1).padStart(2, "0")}</span>
              <h3 className="font-display text-lg font-semibold text-white">{step.name}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-300">{step.text}</p>
            </Reveal>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Ladder() {
  const tiers = [
    { name: "Nano", note: "tried first: the cheapest", tone: "border-white/10" },
    { name: "Super", note: "if Nano can't pass", tone: "border-white/10" },
    { name: "Ultra", note: "only when a call needs it", tone: "border-emerald-400/40 glow" },
  ];
  return (
    <div className="relative">
      <div className="space-y-3">
        {tiers.map((tier, i) => (
          <Reveal key={tier.name} delay={i * 140}>
            <div className={`glass flex items-center justify-between rounded-xl border px-5 py-4 ${tier.tone}`} style={{ marginLeft: `${i * 8}%` }}>
              <span className="font-display text-lg font-semibold text-white">Nemotron {tier.name}</span>
              <span className="text-xs text-muted">{tier.note}</span>
            </div>
          </Reveal>
        ))}
      </div>
      <Reveal delay={480}>
        <div className="mt-5 grid grid-cols-3 gap-2 text-center text-xs">
          {["Tavily reads NVIDIA's guidance", "Ultra rewrites prompt + settings", "Retest, held-out cases included"].map((step, i) => (
            <div key={step} className="glass rounded-lg px-2 py-3 text-slate-300">
              <span className="mb-1 block font-mono text-emerald-400">{["↻ research", "✎ rewrite", "✓ prove"][i]}</span>
              {step}
            </div>
          ))}
        </div>
      </Reveal>
    </div>
  );
}

function UpgradeTimeline() {
  const steps = [
    { label: "Before the upgrade", result: "5 passed", ok: true },
    { label: "PyJWT 1.7.1 → 2.14.0", result: "4 failed", ok: false },
    { label: "Nemotron Super's fix to authkit/tokens.py", result: "5 passed", ok: true },
  ];
  return (
    <ol className="relative space-y-4 border-l border-white/10 pl-6">
      {steps.map((step, i) => (
        <li key={step.label}>
          <Reveal delay={i * 220}>
            <span className={`absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full ${step.ok ? "bg-emerald-400 shadow-[0_0_12px_#34d399]" : "bg-red-400 shadow-[0_0_12px_#f87171]"}`} />
            <div className="glass flex flex-wrap items-center justify-between gap-2 rounded-xl px-4 py-3">
              <span className="text-sm text-slate-200">{step.label}</span>
              <span className={`font-mono text-sm ${step.ok ? "text-emerald-300" : "text-red-300"}`}>{step.ok ? "✓" : "✗"} {step.result}</span>
            </div>
          </Reveal>
        </li>
      ))}
      <li>
        <Reveal delay={700}>
          <blockquote className="text-sm leading-relaxed text-slate-400">
            Each change quotes PyJWT's own changelog: <span className="text-slate-200">"Tokens are returned as string instead of a byte string."</span> The
            tests are never edited to pass.
          </blockquote>
        </Reveal>
      </li>
    </ol>
  );
}

/** The front page: what OpenPort is, what it proved, how it's built, and the demos to try. */
export function Landing() {
  const site = useSite();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const video = site?.video_url ? youtubeId(site.video_url) : null;

  const open = async (demo: (typeof DEMOS)[number]) => {
    setBusy(demo.target);
    setError("");
    try {
      location.hash = `run=${await api.start(demo.target, demo.mode)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy("");
    }
  };

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#05070d] text-slate-200">
      <header className="fixed inset-x-0 top-3 z-50 px-3 sm:top-4">
        <nav className="glass mx-auto flex max-w-6xl items-center justify-between gap-3 rounded-2xl px-4 py-2.5 sm:px-5">
          <a href="#" className="flex items-center gap-2 font-display text-lg font-semibold text-white">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-400 to-cyan-500 shadow-[0_0_18px_rgba(52,211,153,0.5)]" aria-hidden>
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="#04121a" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 12h12" /><path d="M12 6l6 6-6 6" /><path d="M20 5v14" />
              </svg>
            </span>
            <span>Open<span className="text-emerald-300">Port</span></span>
          </a>
          <div className="hidden items-center gap-6 text-sm text-slate-300 md:flex">
            <a href="#how" onClick={scrollTo("how")} className="hover:text-white">How it works</a>
            <a href="#results" onClick={scrollTo("results")} className="hover:text-white">Results</a>
            <a href="#built" onClick={scrollTo("built")} className="hover:text-white">Built on</a>
            <a href="#try" onClick={scrollTo("try")} className="hover:text-white">Try it</a>
          </div>
          <a href="#app" className="rounded-xl bg-white px-3.5 py-2 text-xs font-semibold text-[#05070d] transition hover:bg-emerald-200 sm:text-sm">Open the dashboard</a>
        </nav>
      </header>

      <section className="relative isolate flex min-h-[100svh] items-center overflow-hidden pt-28 pb-20">
        <NetworkCanvas className="absolute inset-0 -z-10 h-full w-full opacity-80" />
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(60rem_40rem_at_15%_20%,rgba(16,185,129,0.16),transparent_60%),radial-gradient(50rem_35rem_at_85%_70%,rgba(34,211,238,0.12),transparent_60%)]" aria-hidden />
        <div className="pointer-events-none absolute inset-x-0 bottom-0 -z-10 h-40 bg-gradient-to-b from-transparent to-[#05070d]" aria-hidden />
        <div className="mx-auto grid w-full max-w-6xl items-center gap-14 px-4 lg:grid-cols-[1.2fr_1fr]">
          <Reveal>
            <span className="glass inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs text-slate-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_#34d399]" />
              Nebius × NVIDIA Global AI Hackathon · Coding and Agentic Engineering
            </span>
            <h1 className="mt-6 font-display text-5xl leading-[1.02] font-semibold tracking-tight text-white sm:text-7xl">
              Move to open models.
              <br />
              <span className="text-gradient">Prove it first.</span>
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-slate-300">
              OpenPort is an agent that finds every OpenAI and Anthropic call in your code, tests each one on NVIDIA Nemotron via Nebius
              Token Factory, tunes it until it passes, and opens the pull request with the evidence.
            </p>
            <div className="mt-9 flex flex-wrap items-center gap-3">
              <button
                disabled={Boolean(busy)}
                onClick={() => open(DEMOS[0])}
                className="glow rounded-xl bg-emerald-400 px-6 py-3.5 text-sm font-semibold text-[#04121a] transition hover:bg-emerald-300 disabled:opacity-50"
              >
                {busy === DEMOS[0].target ? "Opening…" : "Watch it work →"}
              </button>
              <a href="#app" className="glass rounded-xl px-6 py-3.5 text-sm font-medium text-white transition hover:border-emerald-400/40">Open the dashboard</a>
            </div>
            <a href="#how" onClick={scrollTo("how")} className="mt-6 inline-block text-sm text-slate-400 underline decoration-white/20 underline-offset-4 hover:text-white">
              See how it works
            </a>
          </Reveal>
          <Reveal delay={200}>
            <PrCard />
          </Reveal>
        </div>
        <div className="absolute bottom-6 left-1/2 hidden -translate-x-1/2 sm:block" aria-hidden>
          <div className="flex h-9 w-6 justify-center rounded-full border border-white/25 pt-2">
            <span className="scroll-hint h-1.5 w-1 rounded-full bg-white/70" />
          </div>
        </div>
      </section>

      <main className="relative mx-auto max-w-6xl px-4">
        <Reveal>
          <div className="glass grid grid-cols-2 gap-px overflow-hidden rounded-2xl md:grid-cols-4">
            {[
              { to: 81, format: (n: number) => `${Math.round(n)}%`, label: "lower cost per call on SupportDesk" },
              { to: 98, format: (n: number) => `${Math.round(n)}%`, label: "of held-out cases pass after tuning" },
              { to: 94, format: (n: number) => `${Math.round(n)}%`, label: "cheaper on preflight, code I didn't write" },
              { to: 207, format: (n: number) => `${Math.round(n)}`, label: "LLM calls found in the top 20 AI apps" },
            ].map((stat) => (
              <div key={stat.label} className="bg-[#05070d]/40 px-5 py-6">
                <p className="font-display text-4xl font-semibold text-white sm:text-5xl"><CountUp to={stat.to} format={stat.format} /></p>
                <p className="mt-2 text-sm text-slate-400">{stat.label}</p>
              </div>
            ))}
          </div>
        </Reveal>

        {video && (
          <Reveal className="mt-20">
            <div className="glass aspect-video w-full overflow-hidden rounded-2xl">
              <iframe
                className="h-full w-full"
                src={`https://www.youtube-nocookie.com/embed/${video}`}
                title="OpenPort demo video"
                allow="accelerometer; encrypted-media; gyroscope; picture-in-picture; fullscreen"
                allowFullScreen
              />
            </div>
          </Reveal>
        )}

        <section className="py-24 sm:py-32">
          <Heading kicker="The problem" title={<>Switching models is three lines of code. <span className="text-slate-500">Proving it's safe is the hard part.</span></>}>
            Most AI apps run on someone else's closed model: their prices, their roadmap, their outages. Pointing the client at an open
            model is easy. Nobody does it, because nobody can show the app still works.
          </Heading>
          <div className="mt-12 grid gap-5 md:grid-cols-2">
            <Reveal className="glass rounded-2xl p-6 sm:p-8">
              <p className="text-sm text-muted">Cost per 1,000 runs of each SupportDesk call</p>
              <div className="mt-6 space-y-5">
                <Bar label="On the closed models it was written for" value={11.7} max={11.7} shown={(n) => `$${n.toFixed(2)}`} />
                <Bar label="On Nemotron, after OpenPort" value={2.27} max={11.7} shown={(n) => `$${n.toFixed(2)}`} good delay={250} />
              </div>
            </Reveal>
            <Reveal delay={120} className="glass rounded-2xl p-6 sm:p-8">
              <p className="text-sm text-muted">Test cases passed on Nemotron</p>
              <div className="mt-6 space-y-5">
                <Bar label="A plain swap: same prompt, new model" value={70} max={100} shown={(n) => `${Math.round(n)}%`} />
                <Bar label="After OpenPort tunes it" value={96} max={100} shown={(n) => `${Math.round(n)}%`} good delay={250} />
                <Bar label="On cases tuning never saw" value={98} max={100} shown={(n) => `${Math.round(n)}%`} good delay={450} />
              </div>
            </Reveal>
          </div>
        </section>

        <section id="how" className="scroll-mt-28 py-24 sm:py-32">
          <Heading kicker="How it works" title="An agent that writes, runs and tests the code">
            Five steps, every one on Nemotron through Token Factory. Nothing moves until it's proven.
          </Heading>
          <Pipeline />
        </section>

        <section className="grid items-center gap-14 py-24 sm:py-32 lg:grid-cols-2">
          <Heading kicker="Tuning" title={<>The cheapest Nemotron <span className="text-gradient">that passes.</span></>}>
            Every call starts on Nano as written. When it fails, Tavily finds NVIDIA's own guidance for that exact model, Nemotron Ultra reads
            the failures and rewrites the prompt and settings, and the call is tested again. A fix only counts if it also passes cases the
            tuner never saw.
          </Heading>
          <Ladder />
        </section>

        <section id="results" className="scroll-mt-28 py-24 sm:py-32">
          <Heading kicker="Results" title="Measured on real code, not promised" />
          <div className="mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {RESULTS.map((r, i) => (
              <Reveal key={r.title} delay={i * 90} className="glass group h-full rounded-2xl p-6 transition hover:border-emerald-400/40">
                <p className="font-display text-lg font-semibold text-white">{r.title}</p>
                <p className="text-xs text-muted">{r.what}</p>
                <p className="mt-6 font-display text-5xl font-semibold tracking-tight text-emerald-300">
                  <CountUp to={r.to} from={r.from ?? 0} format={r.format} />
                </p>
                <p className="mt-3 text-sm leading-relaxed text-slate-300">{r.detail}</p>
              </Reveal>
            ))}
          </div>
        </section>

        <section className="grid items-center gap-14 py-24 sm:py-32 lg:grid-cols-2">
          <Heading kicker="And it keeps you patched" title="Security upgrades, proven by your own tests">
            <code className="font-mono text-base text-emerald-300">openport upgrade</code> finds dependencies with known advisories, picks the
            release that fixes them and has been out at least a week, runs your tests in a throwaway container, has Nemotron Super fix what
            the upgrade broke, and opens a pull request only if the tests prove it.
          </Heading>
          <UpgradeTimeline />
        </section>

        <section id="built" className="scroll-mt-28 py-24 sm:py-32">
          <Heading kicker="Built on" title={<>Every model inside OpenPort is <span className="text-gradient">NVIDIA Nemotron</span></>} />
          <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {MODELS.map((m, i) => (
              <Reveal key={m.name} delay={i * 70} className="glass h-full rounded-2xl p-5 transition hover:border-emerald-400/40">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="font-display font-semibold text-white">{m.name}</h3>
                  <span className="rounded-full border border-emerald-400/30 px-2 py-0.5 text-[11px] text-emerald-300">{m.tag}</span>
                </div>
                <p className="mt-2 text-sm leading-relaxed text-slate-300">{m.role}</p>
              </Reveal>
            ))}
          </div>
        </section>

        <section id="try" className="scroll-mt-28 py-24 sm:py-32">
          <Heading kicker="Try it" title="Watch the recorded runs, or run it live">
            The demos replay runs recorded live on Oct 28, faster than they ran, at no cost. The dashboard also scans and ports any public
            GitHub repo.
          </Heading>
          <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {DEMOS.map((demo, i) => (
              <Reveal key={demo.target} delay={i * 80}>
                <button
                  disabled={Boolean(busy)}
                  onClick={() => open(demo)}
                  className="glass group h-full w-full rounded-2xl p-5 text-left transition hover:-translate-y-0.5 hover:border-emerald-400/50 disabled:opacity-50"
                >
                  <span className="block font-mono text-sm text-white">{demo.label}</span>
                  <span className="mt-1 block text-xs leading-relaxed text-muted">{demo.detail}</span>
                  <span className="mt-4 block text-sm text-emerald-300 transition group-hover:translate-x-1">{busy === demo.target ? "Opening…" : "Watch the run →"}</span>
                </button>
              </Reveal>
            ))}
          </div>
          {error && <p role="alert" className="mt-4 text-sm text-red-300">{error}</p>}
          {site?.gated && (
            <Reveal className="mt-10">
              <div className="glass glow rounded-2xl p-6 sm:p-8">
                <h3 className="font-display text-xl font-semibold text-white">For the judges: live runs</h3>
                <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-300">
                  With the password from the submission's testing instructions, "Run it live" on any demo calls Nemotron on Token Factory for
                  real, with Tavily's research, and you watch the agent work as it happens.
                </p>
                <JudgeUnlock />
              </div>
            </Reveal>
          )}
        </section>
      </main>

      <footer className="border-t border-white/10">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-8 text-sm text-muted">
          <span>OpenPort · MIT licensed · <span className="text-slate-300">Prove it, then port it.</span></span>
          <span className="flex gap-5">
            <a href={REPO} className="hover:text-white">Source on GitHub</a>
            <a href={`${REPO}#readme`} className="hover:text-white">README</a>
            <a href="#app" className="hover:text-white">Dashboard</a>
          </span>
        </div>
      </footer>
    </div>
  );
}
