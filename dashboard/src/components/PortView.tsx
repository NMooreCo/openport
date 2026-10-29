import type { RunInfo } from "../App";
import { STAGE_LABELS, STAGES, tierName, useRun } from "../run";
import { CallCard } from "./CallCard";
import { GitHubPr } from "./GitHubPr";
import { Metrics } from "./Metrics";
import { ResultTabs } from "./ResultTabs";
import { Shell } from "./Shell";
import { Stages } from "./Stages";

/** A port run: the calls found, each one's tuning on Nemotron, and the pull request. */
export function PortView({ run, onRunLive }: { run: RunInfo; onRunLive?: () => Promise<void> }) {
  const state = useRun(run.id);
  if (!state) return null;

  const live = state.status === "running" || state.status === "queued";
  const changedFiles = state.ported ? state.ported.changed.length + state.ported.added.length : 0;

  return (
    <Shell run={run} status={state.status} home="#app" tagline="Prove it, then port it." onRunLive={onRunLive}>
      <Stages order={STAGES} labels={STAGE_LABELS} stages={state.stages} failed={state.status === "failed"} />
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

      <ResultTabs
        body={state.pr?.body}
        diff={state.ported?.diff}
        files={changedFiles}
        github={<GitHubPr run={run} ready={state.status === "done" && state.pr != null && changedFiles > 0} />}
      />

      {state.models && (
        <footer className="border-t border-line pt-4 text-xs text-muted">
          NVIDIA Nemotron on Nebius Token Factory:{" "}
          {Object.entries(state.models).map(([tier, model], i) => (
            <span key={tier}>{i > 0 && " · "}{tierName(tier)} <span className="font-mono">{model}</span></span>
          ))}
        </footer>
      )}
    </Shell>
  );
}
