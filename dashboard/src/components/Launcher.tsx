import { useEffect, useState } from "react";
import * as api from "../api";
import { JudgeUnlock } from "./JudgeUnlock";

export type Mode = "port" | "upgrade";

const MODES: Record<Mode, { tab: string; title: string; blurb: string; action: string; placeholder: string }> = {
  port: {
    tab: "Port to Nemotron",
    title: "Prove it, then port it.",
    blurb: "OpenPort finds every OpenAI and Anthropic call in a repo, tests each one on open NVIDIA Nemotron models " +
      "served by Nebius Token Factory, tunes the prompt until it passes, and writes the pull request with the evidence.",
    action: "Port it",
    placeholder: "owner/repo on GitHub",
  },
  upgrade: {
    tab: "Upgrade dependencies",
    title: "Prove it, then patch it.",
    blurb: "OpenPort finds dependencies with known vulnerabilities, upgrades each to a release that fixes them and is " +
      "at least a week old, runs the repo's own tests in a container, has Nemotron Super fix whatever the upgrade broke, " +
      "and writes the pull request with the test results.",
    action: "Upgrade it",
    placeholder: "owner/repo, or a monorepo project's GitHub folder URL",
  },
};

export const modeFromHash = (): Mode => (location.hash === "#upgrade" ? "upgrade" : "port");

export function Launcher({ onStart }: { onStart: (target: string, mode: Mode) => Promise<void> }) {
  const [mode, setMode] = useState<Mode>(modeFromHash);
  const [presets, setPresets] = useState<api.Preset[]>([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [vettedOnly, setVettedOnly] = useState(false);
  const copy = MODES[mode];

  useEffect(() => {
    api.info().then((data) => setVettedOnly(Boolean(data.vetted_only))).catch(() => {});
  }, []);

  useEffect(() => {
    const onHash = () => setMode(modeFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    let current = true;
    setPresets([]);
    api.presets(mode)
      .then((data) => current && setPresets(data))
      .catch(() => current && setPresets([]));
    return () => { current = false; };
  }, [mode]);

  const choose = (next: Mode) => {
    setMode(next);
    setError("");
    history.replaceState(null, "", `${location.pathname}${location.search}${next === "upgrade" ? "#upgrade" : "#app"}`);
  };

  const start = async (value: string) => {
    if (!value.trim()) return;
    setBusy(true);
    setError("");
    try {
      await onStart(value.trim(), mode);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-4 pt-16 pb-24 sm:pt-24">
      <p className="flex items-center justify-between gap-4 text-sm font-medium tracking-wide text-emerald-400">
        <a href="#" className="hover:text-emerald-300">OpenPort</a>
        <a href="#" className="text-xs font-normal text-muted hover:text-white">What is this?</a>
      </p>
      <div role="tablist" aria-label="What OpenPort should do" className="mt-4 inline-flex rounded-lg border border-line bg-panel p-1 text-sm">
        {(Object.keys(MODES) as Mode[]).map((m) => (
          <button
            key={m}
            role="tab"
            aria-selected={mode === m}
            onClick={() => choose(m)}
            className={`rounded-md px-3 py-1.5 transition ${mode === m ? "bg-emerald-500 font-semibold text-ink" : "text-muted hover:text-white"}`}
          >
            {MODES[m].tab}
          </button>
        ))}
      </div>
      <h1 className="mt-6 text-4xl font-semibold tracking-tight text-white sm:text-5xl">{copy.title}</h1>
      <p className="mt-5 text-lg leading-relaxed text-muted">{copy.blurb}</p>

      {api.STATIC ? (
        <p className="mt-10 rounded-lg border border-sky-500/30 bg-sky-500/5 px-4 py-3 text-sm leading-relaxed text-sky-100">
          This page replays recorded runs of OpenPort's demos, with no server behind it. To run OpenPort on your own
          repo, install it from <a className="underline" href="https://github.com/NMooreCo/openport">GitHub</a>.
        </p>
      ) : (
      <form
        className="mt-10 flex flex-col gap-3 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          start(target);
        }}
      >
        <label className="sr-only" htmlFor="repo">GitHub repository</label>
        <input
          id="repo"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder={copy.placeholder}
          className="min-w-0 flex-1 rounded-lg border border-line bg-panel px-4 py-3 font-mono text-sm text-white placeholder:text-muted focus:border-emerald-500 focus:outline-none"
        />
        <button
          disabled={busy || !target.trim()}
          className="rounded-lg bg-emerald-500 px-5 py-3 text-sm font-semibold text-ink transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {copy.action}
        </button>
      </form>
      )}
      {error && <p className="mt-3 text-sm text-fail">{error}</p>}
      <div className="mt-4"><JudgeUnlock compact /></div>
      {vettedOnly && mode === "port" && (
        <p className="mt-3 text-xs leading-relaxed text-muted">
          Testing a call runs the repo's code, so this public demo tests and tunes only the examples below that have eval
          sets. Any other repo is scanned and ported untuned; run OpenPort locally to test it.
        </p>
      )}

      <div className="mt-10">
        <p className="text-xs font-medium uppercase tracking-wider text-muted">{api.STATIC ? "Pick one" : "Or try one"}</p>
        {api.STATIC && presets.length === 0 && <p className="mt-3 text-sm text-muted">No recorded runs of this kind yet.</p>}
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          {presets.map((preset) => (
            <button
              key={preset.id}
              disabled={busy}
              onClick={() => start(preset.id)}
              className="rounded-xl border border-line bg-panel p-4 text-left transition hover:border-emerald-500/60 disabled:opacity-40"
            >
              <span className="block font-mono text-sm text-white">{preset.label}</span>
              <span className="mt-1 block text-xs leading-relaxed text-muted">{preset.detail}</span>
              {preset.tested === false && (
                <span className="mt-2 inline-block rounded-md bg-line px-2 py-0.5 text-xs text-slate-300">No eval set here: scan and port only</span>
              )}
            </button>
          ))}
        </div>
      </div>
    </main>
  );
}
