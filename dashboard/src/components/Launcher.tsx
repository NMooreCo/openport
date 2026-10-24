import { useEffect, useState } from "react";

interface Preset {
  id: string;
  label: string;
  detail: string;
}

export function Launcher({ onStart }: { onStart: (target: string) => Promise<void> }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/presets").then((r) => r.json()).then(setPresets).catch(() => setPresets([]));
  }, []);

  const start = async (value: string) => {
    if (!value.trim()) return;
    setBusy(true);
    setError("");
    try {
      await onStart(value.trim());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-4 pt-16 pb-24 sm:pt-24">
      <p className="text-sm font-medium tracking-wide text-emerald-400">OpenPort</p>
      <h1 className="mt-3 text-4xl font-semibold tracking-tight text-white sm:text-5xl">Prove it, then port it.</h1>
      <p className="mt-5 text-lg leading-relaxed text-muted">
        OpenPort finds every OpenAI and Anthropic call in a repo, tests each one on open NVIDIA Nemotron models
        served by Nebius Token Factory, tunes the prompt until it passes, and writes the pull request with the evidence.
      </p>

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
          placeholder="owner/repo on GitHub"
          className="min-w-0 flex-1 rounded-lg border border-line bg-panel px-4 py-3 font-mono text-sm text-white placeholder:text-muted focus:border-emerald-500 focus:outline-none"
        />
        <button
          disabled={busy || !target.trim()}
          className="rounded-lg bg-emerald-500 px-5 py-3 text-sm font-semibold text-ink transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Port it
        </button>
      </form>
      {error && <p className="mt-3 text-sm text-fail">{error}</p>}

      <div className="mt-10">
        <p className="text-xs font-medium uppercase tracking-wider text-muted">Or try one</p>
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
            </button>
          ))}
        </div>
      </div>
    </main>
  );
}
