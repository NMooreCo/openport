import { useState, type ReactNode } from "react";
import type { RunInfo } from "../App";
import type { Source } from "../run";
import {
  UPGRADE_STAGE_LABELS, UPGRADE_STAGES, advisoryUrl, finalTests, shortSummary, upgraded, useUpgrade,
  type Changelog, type FixRound, type Package, type TestResult, type UpgradeState,
} from "../upgrade";
import { GitHubPr } from "./GitHubPr";
import { Tile } from "./Metrics";
import { ResultTabs } from "./ResultTabs";
import { Shell } from "./Shell";
import { Citations, SourceLinks, isWebUrl } from "./SourceLinks";
import { Stages } from "./Stages";

const STATUS: Record<string, { label: string; className: string }> = {
  upgrade: { label: "upgrade", className: "bg-emerald-500/10 text-emerald-300" },
  partial: { label: "partial fix", className: "bg-sky-500/10 text-sky-300" },
  "held: fix too new": { label: "held: fix too new", className: "bg-amber-500/10 text-amber-300" },
  "held: no release dates": { label: "held: no release dates", className: "bg-amber-500/10 text-amber-300" },
  "no fix available": { label: "no fix yet", className: "bg-red-500/10 text-red-300" },
};
const SHOWN_ADVISORIES = 5;
const SHOWN_SOURCES = 4;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function UpgradeMetrics({ state }: { state: UpgradeState }) {
  const packages = state.packages ?? [];
  const bumped = upgraded(state);
  const known = new Set(packages.flatMap((p) => p.advisories.map((a) => a.id)));
  const fixed = new Set(bumped.flatMap((p) => p.fixed));
  const open = [...known].filter((id) => !fixed.has(id)).length;
  const final = finalTests(state);

  let tests = { value: "–", sub: state.status === "done" ? "not run on this server" : "the repo's own, in a container", tone: "text-white" };
  if (state.pr && final) {
    tests = state.pr.proven
      ? { value: "Proven", sub: `pass before and after: ${shortSummary(final)}`, tone: "text-pass" }
      : { value: "Not proven", sub: state.before?.ok ? `after the upgrade: ${shortSummary(final)}` : "already failing before the upgrade", tone: "text-fail" };
  } else if (final) {
    tests = { value: shortSummary(final), sub: "after the upgrade", tone: final.ok ? "text-pass" : "text-fail" };
  } else if (state.before) {
    tests = { value: shortSummary(state.before), sub: "before the upgrade", tone: "text-white" };
  }

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Tile label="Dependencies checked" value={state.deps ? String(state.deps.count) : "–"} sub={state.deps?.files.length ? `in ${state.deps.files.join(", ")}` : "from the repo's lockfiles"} />
      <Tile label="Known advisories" value={state.packages ? String(known.size) : "–"} sub={state.packages ? `in ${plural(packages.length, "package")}` : "from OSV.dev"} tone={known.size ? "text-fail" : "text-white"} />
      <Tile label="Fixed by upgrading" value={state.packages ? String(fixed.size) : "–"} sub={state.packages ? (open ? `${open} still open` : "none left open") : "to releases at least a week old"} tone={fixed.size ? "text-pass" : "text-white"} />
      <Tile label="Tests" value={tests.value} sub={tests.sub} tone={tests.tone} />
    </div>
  );
}

function PackageCard({ pkg }: { pkg: Package }) {
  const status = STATUS[pkg.status] ?? { label: pkg.status, className: "bg-line text-slate-300" };
  const line = (a: Package["advisories"][number]) => {
    const fixed = pkg.fixed.includes(a.id);
    return (
      <li key={a.id} className="flex gap-2 leading-relaxed">
        <span className={fixed ? "text-pass" : "text-fail"} title={fixed ? "Fixed by the upgrade" : "Still open"}>{fixed ? "✓" : "●"}</span>
        <span className="sr-only">{fixed ? "Fixed:" : "Still open:"}</span>
        <span className="min-w-0">
          <a href={advisoryUrl(a.id)} target="_blank" rel="noreferrer noopener" className="font-mono text-slate-200 underline decoration-line hover:text-white">{a.id}</a>
          {a.summary && <span className="text-muted"> {a.summary}</span>}
        </span>
      </li>
    );
  };
  const rest = pkg.advisories.slice(SHOWN_ADVISORIES);
  return (
    <article className="panel rounded-2xl p-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-sm text-white">{pkg.name}</h3>
          <p className="mt-0.5 truncate font-mono text-xs text-muted">{pkg.source} · {pkg.ecosystem}</p>
        </div>
        <span className={`shrink-0 rounded-md px-2 py-0.5 text-xs ${status.className}`}>{status.label}</span>
      </header>

      <p className="mt-3 flex flex-wrap items-center gap-2 font-mono text-xs">
        <span className="text-red-300">{pkg.version}</span>
        <span className="text-muted" aria-hidden>→</span>
        {pkg.target ? <span className="text-emerald-300">{pkg.target}</span> : <span className="text-muted">no safe release yet</span>}
        {pkg.age_days != null && <span className="font-sans text-muted">released {pkg.age_days} days ago</span>}
      </p>

      <ul className="mt-3 space-y-1.5 text-xs" aria-label={`Advisories for ${pkg.name}`}>
        {pkg.advisories.slice(0, SHOWN_ADVISORIES).map(line)}
      </ul>
      {rest.length > 0 && (
        <details className="mt-1.5 text-xs">
          <summary className="cursor-pointer text-muted hover:text-white">{rest.length} more</summary>
          <ul className="mt-1.5 space-y-1.5">{rest.map(line)}</ul>
        </details>
      )}
      {pkg.target && !pkg.bumpable && (
        <p className="mt-3 text-xs text-amber-200/90">Pinned in {pkg.source}, so it needs its package manager to update. Not upgraded here.</p>
      )}
    </article>
  );
}

function Step({ title, result, active, children }: { title: string; result?: TestResult | null; active?: boolean; children?: ReactNode }) {
  const dot = result ? (result.ok ? "border-pass bg-pass" : "border-fail bg-fail") : active ? "border-tune bg-tune animate-pulse" : "border-line";
  return (
    <li className="relative pl-6">
      <span className={`absolute top-1.5 left-0 h-2.5 w-2.5 rounded-full border-2 ${dot}`} aria-hidden />
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="min-w-0 text-sm break-words text-white">{title}</p>
        {result ? (
          <p className={`shrink-0 font-mono text-xs ${result.ok ? "text-pass" : "text-fail"}`}>
            {result.ok ? "✓ " : "✗ "}{shortSummary(result)} <span className="text-muted">· {result.seconds}s</span>
          </p>
        ) : active ? (
          <p className="shrink-0 text-xs text-tune">running…</p>
        ) : null}
      </div>
      {children}
      {result?.output && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-xs text-muted hover:text-white">Test output</summary>
          <pre className="mt-1.5 max-h-72 overflow-auto rounded-lg bg-ink p-3 font-mono text-xs leading-5 text-slate-400">{result.output}</pre>
        </details>
      )}
    </li>
  );
}

function ResearchStep({ sources }: { sources: Source[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? sources : sources.slice(0, SHOWN_SOURCES);
  return (
    <li className="pl-6 text-xs leading-relaxed text-sky-200/90">
      <span className="text-muted">Tavily read the release notes and advisories: </span>
      <SourceLinks sources={shown} />
      {shown.length < sources.length && (
        <button onClick={() => setAll(true)} className="ml-1 text-muted underline decoration-line hover:text-white">
          and {sources.length - shown.length} more
        </button>
      )}
    </li>
  );
}

function FixStep({ fix, index }: { fix: FixRound; index: number }) {
  const title = fix.files.length
    ? `Fix ${index + 1}: Nemotron Super edited ${fix.files.join(", ")}`
    : `Fix ${index + 1}: every proposed edit was rejected, so the tests didn't run again`;
  return (
    <Step title={title} result={fix.tests}>
      {fix.rationale && <p className="mt-1 text-xs leading-relaxed text-slate-300">{fix.rationale}</p>}
      <Citations label="Based on" sources={fix.sources} />
      {fix.rejected.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-amber-200/90">
          {fix.rejected.map((r, i) => <li key={i}>Rejected an edit to {r.file}: {r.reason}</li>)}
        </ul>
      )}
    </Step>
  );
}

function Changelogs({ changelogs }: { changelogs: Changelog[] }) {
  return (
    <section aria-labelledby="changelogs">
      <h2 id="changelogs" className="font-display text-lg font-semibold text-white">What changed upstream</h2>
      <p className="mt-1 text-xs text-muted">
        Quoted from each package's own changelog, found and read with Tavily, for the releases between the two versions.
      </p>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        {changelogs.map((log) => (
          <article key={`${log.name}@${log.from}`} className="min-w-0 panel rounded-2xl p-4">
            <p className="text-sm break-words text-white">
              <span className="font-medium">{log.name}</span> <span className="font-mono text-xs text-muted">{log.from} → {log.to}</span>
            </p>
            <p className="mt-0.5 text-xs text-muted">
              {plural(log.releases.length, "release")} ·{" "}
              {isWebUrl(log.url) ? (  // a changelog's URL can come from the package's own metadata
                <a href={log.url} target="_blank" rel="noreferrer noopener" className="underline decoration-line hover:text-white">{log.title}</a>
              ) : log.title}
            </p>
            {log.quotes.length ? (
              <ul className="mt-2 space-y-1.5 text-xs leading-relaxed text-slate-300">
                {log.quotes.map((quote, i) => (
                  <li key={i} className="break-words">
                    <span className="font-mono text-muted">{quote.version}</span> “{quote.text}”
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-xs text-muted">None of them says anything was removed or now works differently.</p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

function Tests({ state }: { state: UpgradeState }) {
  const bumps = upgraded(state).map((p) => `${p.name} ${p.target}`).join(", ");
  const fixing = state.stages.fix === "active" && state.status === "running";
  return (
    <section aria-labelledby="tests">
      <h2 id="tests" className="font-display text-lg font-semibold text-white">The repo's own tests</h2>
      <p className="mt-1 text-xs text-muted">
        Run in a throwaway container with only the upgraded copy mounted. OpenPort changes application code to fix what breaks, never the tests.
      </p>
      <ol className="mt-3 space-y-4 panel rounded-2xl p-4">
        <Step title="Before the upgrade" result={state.before} active={!state.before && state.status === "running"} />
        {state.before && <Step title={`After the version bump${bumps ? `: ${bumps}` : ""}`} result={state.afterBump} active={!state.afterBump && state.status === "running"} />}
        {state.research.length > 0 && <ResearchStep sources={state.research} />}
        {state.fixes.map((fix, i) => <FixStep key={i} fix={fix} index={i} />)}
        {fixing && state.afterBump && !state.afterBump.ok && !state.fixes.some((f) => f.tests?.ok) && (
          <Step title={`Fix ${state.fixes.length + 1}: Nemotron Super is reading the failures and the code`} active />
        )}
      </ol>
    </section>
  );
}

/** An upgrade run: the vulnerable packages, the versions chosen, the test proof and the pull request. */
export function UpgradeView({ run, onRunLive }: { run: RunInfo; onRunLive?: () => Promise<void> }) {
  const state = useUpgrade(run.id);
  if (!state) return null;

  const live = state.status === "running" || state.status === "queued";
  const packages = state.packages ?? [];
  const files = state.pr ? state.pr.diff.split("\n").filter((line) => line.startsWith("+++ ")).length : 0;

  return (
    <Shell run={run} status={state.status} home="#upgrade" tagline="Prove it, then patch it." onRunLive={onRunLive}>
      <Stages order={UPGRADE_STAGES} labels={UPGRADE_STAGE_LABELS} stages={state.stages} failed={state.status === "failed"} />
      {state.error && (
        <div role="alert" className="rounded-lg border border-fail/40 bg-fail/10 p-3 text-sm text-red-200">{state.error}</div>
      )}
      <UpgradeMetrics state={state} />
      {state.notes.length > 0 && (
        <ul className="space-y-1 text-sm text-muted">
          {state.notes.map((note) => <li key={note}>{note}</li>)}
        </ul>
      )}

      <section aria-labelledby="packages">
        <h2 id="packages" className="font-display text-lg font-semibold text-white">Vulnerable packages</h2>
        {!state.packages ? (
          <p className="mt-3 text-sm text-muted">{live ? "Checking each pinned dependency against OSV.dev…" : "No dependencies were checked."}</p>
        ) : packages.length === 0 ? (
          <p className="mt-3 text-sm text-muted">No dependency has a known vulnerability.</p>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {packages.map((pkg) => <PackageCard key={`${pkg.ecosystem}:${pkg.name}@${pkg.version}:${pkg.source}`} pkg={pkg} />)}
          </div>
        )}
      </section>
      {state.changelogs && state.changelogs.length > 0 && <Changelogs changelogs={state.changelogs} />}

      {(state.before || state.stages.test === "active") && <Tests state={state} />}

      {/* An upgrade's pull request is opened from the CLI, where its tests can run: a recorded one links to it. */}
      <ResultTabs body={state.pr?.body} diff={state.pr?.diff} files={files} github={<GitHubPr run={run} ready={false} />} />

      <footer className="border-t border-line pt-4 text-xs text-muted">
        Advisories from OSV.dev · release dates from PyPI and npm · fixes by NVIDIA Nemotron Super on Nebius Token Factory
      </footer>
    </Shell>
  );
}
