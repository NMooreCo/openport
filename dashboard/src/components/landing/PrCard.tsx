import { useCount, useInView } from "./motion";

// SupportDesk's pull request, from its recorded run (Oct 28): each call, its tier, plain swap → after tuning.
const ROWS = [
  { call: "replies.draft_reply", was: "claude-sonnet-5-5", tier: "Ultra", plain: 0, after: 92 },
  { call: "policy.review_case", was: "claude-opus-5-5", tier: "Nano", plain: 86, after: 100 },
  { call: "triage.triage_ticket", was: "gpt-5-mini", tier: "Nano", plain: 71, after: 94 },
  { call: "refunds.handle_refund_request", was: "gpt-5", tier: "Super", plain: 96, after: 100 },
  { call: "pii.extract_pii", was: "gpt-5-mini", tier: "Nano", plain: 95, after: 95 },
];
const STEP_MS = 520; // between one call's climb and the next

function Row({ row, index, run }: { row: (typeof ROWS)[number]; index: number; run: boolean }) {
  // the plain swap fills in first, then the tuned rate climbs from it, then the call lands on its tier
  const start = index * STEP_MS;
  const plain = useCount(run, row.plain, { ms: 500, delay: start });
  const after = useCount(run, row.after, { from: row.plain, ms: 900, delay: start + 650 });
  const climbing = useCount(run, 1, { ms: 1, delay: start + 650 }) >= 1;
  const tuned = useCount(run, 1, { ms: 1, delay: start + 1550 }) >= 1;
  const shown = climbing ? after : plain;
  return (
    <li className="px-4 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <span className="min-w-0">
          <span className="block truncate font-mono text-xs text-slate-200">{row.call}</span>
          <span className="block truncate text-[11px] text-muted">
            {row.was} → <span className={tuned ? "text-emerald-300" : ""}>Nemotron {row.tier}</span>
          </span>
        </span>
        <span className={`shrink-0 font-mono text-xs tabular-nums ${tuned ? "text-emerald-300" : shown < 90 ? "text-red-300" : "text-slate-300"}`}>
          {Math.round(shown)}%
        </span>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/5">
        <div
          className={`h-full rounded-full ${tuned ? "bg-gradient-to-r from-emerald-400 to-cyan-400" : shown < 90 ? "bg-red-400/70" : "bg-slate-400/60"}`}
          style={{ width: `${shown}%` }}
        />
      </div>
    </li>
  );
}

/** The pull request OpenPort wrote for SupportDesk, playing out: each call climbs from its plain swap to tuned. */
export function PrCard() {
  const [ref, seen] = useInView<HTMLDivElement>("0px");
  const cost = useCount(seen, 2.27, { from: 11.7, ms: 1600, delay: ROWS.length * STEP_MS + 700 });
  const moved = cost <= 2.28;
  return (
    <div ref={ref} className="glass float relative rounded-2xl shadow-2xl shadow-emerald-950/50">
      <div className="pointer-events-none absolute -inset-px rounded-2xl bg-gradient-to-br from-emerald-400/25 via-transparent to-cyan-400/20 opacity-60" aria-hidden />
      <div className="relative flex items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
        <span className="flex items-center gap-2 text-sm font-semibold text-white">
          <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_10px_#34d399]" aria-hidden />
          Move LLM calls to NVIDIA Nemotron
        </span>
        <span className={`rounded-full px-2 py-0.5 text-[11px] transition-colors duration-700 ${moved ? "bg-emerald-400/15 text-emerald-300" : "bg-white/5 text-muted"}`}>
          {moved ? "5 of 5 moved" : "testing…"}
        </span>
      </div>
      <ul className="relative divide-y divide-white/5">
        {ROWS.map((row, i) => <Row key={row.call} row={row} index={i} run={seen} />)}
      </ul>
      <div className="relative flex items-center justify-between gap-3 border-t border-white/10 px-4 py-3">
        <span className="text-xs text-muted">Cost per 1,000 runs of each call</span>
        <span className="font-display text-lg font-semibold tabular-nums text-white">
          <span className="text-muted line-through decoration-red-400/60">$11.70</span>{" "}
          <span className="text-emerald-300">${cost.toFixed(2)}</span>
        </span>
      </div>
    </div>
  );
}
