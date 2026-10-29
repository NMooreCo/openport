import { useState, type ReactNode } from "react";
import type { RunInfo } from "../App";
import { useSite } from "../site";
import { JudgeUnlock } from "./JudgeUnlock";
import { Backdrop, Logo } from "./Logo";

function RecordedBanner({ created, onRunLive }: { created: number; onRunLive?: () => Promise<void> }) {
  const site = useSite();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const locked = Boolean(site?.gated && !site.judge); // the hosted demo keeps live runs for the judges
  const day = new Date(created * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const runLive = async () => {
    setBusy(true);
    setError("");
    try {
      await onRunLive?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-sky-400/25 bg-sky-400/[0.06] px-4 py-3 text-sm text-sky-100">
      <p className="flex items-center gap-2">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-sky-400 shadow-[0_0_8px_#38bdf8]" aria-hidden />
        A recorded run from {day}, replayed faster than it ran. Nothing is calling Token Factory now.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        {error && <span role="alert" className="text-xs text-red-300">{error}</span>}
        {onRunLive && locked ? (
          <JudgeUnlock compact />
        ) : onRunLive ? (
          <button
            disabled={busy}
            onClick={runLive}
            className="glow rounded-lg bg-emerald-400 px-3.5 py-1.5 text-xs font-semibold text-ink transition hover:bg-emerald-300 disabled:opacity-40"
          >
            Run it live
          </button>
        ) : (
          <a className="text-xs underline" href="https://github.com/NMooreCo/openport#quickstart">Run it yourself</a>
        )}
      </div>
    </div>
  );
}

/** The page around a run: the header with its status, a way back to the launcher, and a recorded run's banner. */
export function Shell({ run, status, home, tagline, onRunLive, children }: {
  run: RunInfo;
  status: string;
  home: string;
  tagline: string;
  onRunLive?: () => Promise<void>; // none on a static page, which can only replay
  children: ReactNode;
}) {
  const live = status === "running" || status === "queued";
  return (
    <div className="relative isolate min-h-screen">
      <Backdrop />
      <header className="sticky top-0 z-40 border-b border-white/10 bg-ink/85 backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <span className="flex min-w-0 items-center gap-3">
            <Logo />
            <span className="hidden truncate text-sm text-muted md:inline">{tagline}</span>
          </span>
          <div className="flex min-w-0 items-center gap-3">
            {live && (
              <span className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                run.recorded ? "border-sky-400/30 text-sky-300" : "border-amber-400/30 text-tune"
              }`}>
                <span className={`h-1.5 w-1.5 animate-pulse rounded-full ${run.recorded ? "bg-sky-400" : "bg-tune"}`} aria-hidden />
                {run.recorded ? "Replay" : status === "queued" ? "Queued" : "Live"}
              </span>
            )}
            <span className="truncate font-mono text-sm text-slate-200">{run.label}</span>
            <a href={home} className="shrink-0 rounded-lg border border-white/15 bg-white/[0.04] px-3 py-1.5 text-xs font-medium text-slate-200 transition hover:border-emerald-400/50 hover:text-white">
              New run
            </a>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-8">
        {run.recorded && <RecordedBanner created={run.created} onRunLive={onRunLive} />}
        {children}
      </main>
    </div>
  );
}
