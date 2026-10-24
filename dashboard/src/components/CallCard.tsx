import { attemptsFor, pct, researchFor, tierName, tunedFor, type Finding, type RunState, type Source } from "../run";

// Links come from web search results, so only plain web URLs are rendered as links.
const isWebUrl = (url: string) => /^https?:\/\//i.test(url);

function SourceLinks({ sources }: { sources: Source[] }) {
  return (
    <>
      {sources.filter((s) => isWebUrl(s.url)).map((s, i) => (
        <span key={s.url}>
          {i > 0 && ", "}
          <a href={s.url} target="_blank" rel="noreferrer noopener" className="break-words underline decoration-sky-500/40 hover:text-white">
            {s.title}
          </a>
        </span>
      ))}
    </>
  );
}

const PORTABILITY = {
  auto: { label: "swap as-is", className: "bg-emerald-500/10 text-emerald-300" },
  translate: { label: "translate", className: "bg-sky-500/10 text-sky-300" },
  review: { label: "review", className: "bg-amber-500/10 text-amber-300" },
};

function scoreTone(rate: number, meets: boolean) {
  if (meets) return "border-pass/50 bg-pass/15 text-emerald-200";
  if (rate >= 0.6) return "border-tune/40 bg-tune/10 text-amber-200";
  return "border-fail/40 bg-fail/10 text-red-200";
}

export function CallCard({ site, state }: { site: Finding; state: RunState }) {
  const attempts = attemptsFor(state, site);
  const research = researchFor(state, site);
  const tuned = tunedFor(state, site);
  const ported = state.ported?.sites.find((p) => p.id === site.id);
  const tuning = state.stages.tune === "active" && !tuned;
  const latest = attempts[attempts.length - 1];
  const portability = PORTABILITY[site.portability];

  let status: { text: string; tone: string };
  if (tuned?.chosen) {
    const c = tuned.chosen;
    status = { text: `Passes on ${tierName(c.tier)}: ${pct(c.pass_rate)}${c.holdout_pass != null ? `, held-out ${pct(c.holdout_pass)}` : ""}`, tone: "text-pass" };
  } else if (tuned) {
    const best = Math.max(...attempts.map((a) => a.pass_rate), 0);
    status = { text: `No tier reached the bar (best ${pct(best)}). Stays on ${site.model ?? "the original"}.`, tone: "text-tune" };
  } else if (tuning && latest) {
    status = { text: `Tuning on ${tierName(latest.tier)}, round ${latest.round + 1}…`, tone: "text-tune" };
  } else if (tuning) {
    status = { text: "Waiting to be tested…", tone: "text-muted" };
  } else if (ported?.status === "kept") {
    status = { text: `Kept: ${ported.reason}`, tone: "text-tune" };
  } else if (ported) {
    status = { text: "Moved without tuning (no eval spec)", tone: "text-muted" };
  } else {
    status = { text: site.notes[0] ?? "Found", tone: "text-muted" };
  }

  const change = tuned?.chosen && tuned.chosen.variant !== "as written" ? tuned.chosen : null;

  return (
    <article className="rounded-xl border border-line bg-panel p-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-sm text-white">{site.id}</h3>
          <p className="mt-0.5 truncate font-mono text-xs text-muted">{site.file}:{site.line}</p>
        </div>
        <div className="flex shrink-0 gap-1.5 text-xs">
          <span className="rounded-md bg-line px-2 py-0.5 text-slate-300">{site.sdk === "openai" ? "OpenAI" : "Anthropic"} · {site.output.replace(/_/g, " ")}</span>
          <span className={`rounded-md px-2 py-0.5 ${portability.className}`}>{portability.label}</span>
        </div>
      </header>

      <p className="mt-3 flex flex-wrap items-center gap-2 font-mono text-xs">
        <span className="text-slate-300">{site.model ?? site.model_expr ?? "?"}</span>
        <span className="text-muted" aria-hidden>→</span>
        {ported?.status === "ported" ? (
          <span className="text-emerald-300">{ported.now}</span>
        ) : ported?.status === "kept" ? (
          <span className="text-amber-300">kept</span>
        ) : (
          <span className="text-muted">Nemotron</span>
        )}
      </p>

      {attempts.length > 0 && (
        <ol className="mt-3 flex flex-wrap gap-1.5" aria-label="Tuning attempts">
          {attempts.map((a, i) => (
            <li
              key={i}
              title={`${tierName(a.tier)} round ${a.round + 1}: ${a.variant}. Tuning cases ${pct(a.tune_pass)}${a.holdout_pass != null ? `, held-out ${pct(a.holdout_pass)}` : ""}`}
              className={`rounded-md border px-2 py-0.5 font-mono text-xs tabular-nums ${scoreTone(a.pass_rate, a.meets)}`}
            >
              {tierName(a.tier)}{a.round ? ` r${a.round + 1}` : ""} {pct(a.pass_rate)}
            </li>
          ))}
        </ol>
      )}

      {research.length > 0 && (
        <ul className="mt-3 space-y-1.5 text-xs" aria-label="NVIDIA guidance found with Tavily">
          {research.map((r) => (
            <li key={`${r.model}-${r.topic}`} className="rounded-lg border border-sky-500/20 bg-sky-500/5 p-2 leading-relaxed text-sky-200/90">
              <span className="text-muted">Tavily searched NVIDIA's docs: </span>
              <span className="break-words">{r.query}</span>
              <span className="mt-1 block"><SourceLinks sources={r.sources} /></span>
            </li>
          ))}
        </ul>
      )}

      <p className={`mt-3 text-sm ${status.tone}`}>{status.text}</p>

      {change && (
        <div className="mt-2 rounded-lg bg-ink/60 p-2.5 text-xs leading-relaxed text-slate-300">
          <span className="text-muted">Change: </span>{change.variant}
          {change.addendum && <p className="mt-1 italic text-slate-400">“{change.addendum}”</p>}
          {change.sources && change.sources.length > 0 && (
            <p className="mt-1 text-sky-300/90">
              <span className="text-muted">Follows NVIDIA's guidance: </span>
              <SourceLinks sources={change.sources} />
            </p>
          )}
        </div>
      )}

      {ported?.notes && ported.notes.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-amber-200/90">
          {ported.notes.map((note) => <li key={note}>• {note}</li>)}
        </ul>
      )}
    </article>
  );
}
