import { useEffect, useState } from "react";
import { CallCard } from "./components/CallCard";
import { DiffView } from "./components/DiffView";
import { Launcher } from "./components/Launcher";
import { Metrics } from "./components/Metrics";
import { PrView } from "./components/PrView";
import { Stages } from "./components/Stages";
import { tierName, useRun } from "./run";

const runFromHash = () => location.hash.match(/run=([a-f0-9]+)/)?.[1] ?? null;

export default function App() {
  const [runId, setRunId] = useState<string | null>(runFromHash);
  const [label, setLabel] = useState("");
  const [tab, setTab] = useState<"pr" | "diff">("pr");
  const state = useRun(runId);

  useEffect(() => {
    const onHash = () => setRunId(runFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!runId) return;
    fetch(`/api/runs/${runId}`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((run) => setLabel(run.label))
      .catch(() => {
        location.hash = "";
      });
  }, [runId]);

  const start = async (target: string) => {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Couldn't start the run");
    location.hash = `run=${data.id}`;
  };

  if (!runId || !state) return <Launcher onStart={start} />;

  const live = state.status === "running" || state.status === "queued";
  const changedFiles = state.ported ? state.ported.changed.length + state.ported.added.length : 0;

  return (
    <div className="min-h-screen">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <a href="#" className="shrink-0 font-semibold text-white">
            OpenPort <span className="hidden font-normal text-muted sm:inline">· Prove it, then port it.</span>
          </a>
          <div className="flex min-w-0 items-center gap-3">
            {live && (
              <span className="flex items-center gap-1.5 text-xs text-tune">
                <span className="h-2 w-2 animate-pulse rounded-full bg-tune" aria-hidden />
                {state.status === "queued" ? "Queued" : "Live"}
              </span>
            )}
            <span className="truncate font-mono text-sm text-slate-300">{label}</span>
            <a href="#" className="shrink-0 rounded-md border border-line px-3 py-1.5 text-xs text-slate-300 hover:border-emerald-500/60">
              New run
            </a>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6">
        <Stages state={state} />
        {state.error && (
          <div role="alert" className="rounded-lg border border-fail/40 bg-fail/10 p-3 text-sm text-red-200">{state.error}</div>
        )}
        <Metrics state={state} />
        {state.notes.length > 0 && (
          <ul className="space-y-1 text-sm text-muted">
            {state.notes.map((note) => <li key={note}>{note}</li>)}
          </ul>
        )}

        <section aria-labelledby="calls">
          <h2 id="calls" className="text-sm font-medium text-white">LLM calls</h2>
          {state.sites.length === 0 ? (
            <p className="mt-3 text-sm text-muted">{live ? "Looking for OpenAI and Anthropic calls…" : "No calls found."}</p>
          ) : (
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              {state.sites.map((site) => <CallCard key={site.id} site={site} state={state} />)}
            </div>
          )}
        </section>

        {(state.ported || state.pr) && (
          <section aria-label="Result">
            <div className="flex gap-1 border-b border-line" role="tablist">
              {state.pr && (
                <button role="tab" aria-selected={tab === "pr"} onClick={() => setTab("pr")}
                  className={`border-b-2 px-3 py-2 text-sm ${tab === "pr" ? "border-emerald-400 text-white" : "border-transparent text-muted hover:text-white"}`}>
                  Pull request
                </button>
              )}
              {state.ported && (
                <button role="tab" aria-selected={tab === "diff" || !state.pr} onClick={() => setTab("diff")}
                  className={`border-b-2 px-3 py-2 text-sm ${tab === "diff" || !state.pr ? "border-emerald-400 text-white" : "border-transparent text-muted hover:text-white"}`}>
                  Code changes <span className="text-muted">({changedFiles} files)</span>
                </button>
              )}
            </div>
            <div className="mt-4">
              {tab === "pr" && state.pr ? <PrView body={state.pr.body} /> : state.ported ? <DiffView diff={state.ported.diff} /> : null}
            </div>
          </section>
        )}

        {state.models && (
          <footer className="border-t border-line pt-4 text-xs text-muted">
            NVIDIA Nemotron on Nebius Token Factory:{" "}
            {Object.entries(state.models).map(([tier, model], i) => (
              <span key={tier}>{i > 0 && " · "}{tierName(tier)} <span className="font-mono">{model}</span></span>
            ))}
          </footer>
        )}
      </main>
    </div>
  );
}
