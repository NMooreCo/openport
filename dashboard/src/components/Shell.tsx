import { useState, type ReactNode } from "react";
import type { RunInfo } from "../App";

function RecordedBanner({ created, onRunLive }: { created: number; onRunLive?: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
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
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-sm text-sky-100">
      <p>A recorded run from {day}, replayed faster than it ran. Nothing is calling Token Factory now.</p>
      <div className="flex flex-wrap items-center gap-3">
        {error && <span role="alert" className="text-xs text-red-300">{error}</span>}
        {onRunLive ? (
          <button
            disabled={busy}
            onClick={runLive}
            className="rounded-md bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-ink transition hover:bg-emerald-400 disabled:opacity-40"
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
    <div className="min-h-screen">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <a href={home} className="shrink-0 font-semibold text-white">
            OpenPort <span className="hidden font-normal text-muted sm:inline">· {tagline}</span>
          </a>
          <div className="flex min-w-0 items-center gap-3">
            {live && (
              <span className={`flex items-center gap-1.5 text-xs ${run.recorded ? "text-sky-300" : "text-tune"}`}>
                <span className={`h-2 w-2 animate-pulse rounded-full ${run.recorded ? "bg-sky-400" : "bg-tune"}`} aria-hidden />
                {run.recorded ? "Replay" : status === "queued" ? "Queued" : "Live"}
              </span>
            )}
            <span className="truncate font-mono text-sm text-slate-300">{run.label}</span>
            <a href={home} className="shrink-0 rounded-md border border-line px-3 py-1.5 text-xs text-slate-300 hover:border-emerald-500/60">
              New run
            </a>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6">
        {run.recorded && <RecordedBanner created={run.created} onRunLive={onRunLive} />}
        {children}
      </main>
    </div>
  );
}
