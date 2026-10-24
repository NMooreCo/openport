import { pct, usd, type RunState } from "../run";

export function Tile({ label, value, sub, tone = "text-white" }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-xl border border-line bg-panel px-4 py-3">
      <p className="text-xs text-muted">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${tone}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
    </div>
  );
}

const mean = (values: number[]) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length : null);

export function Metrics({ state }: { state: RunState }) {
  const review = state.sites.filter((s) => s.portability === "review").length;

  // Plain swap: each call's first attempt (Nano, prompt as written). After: each call's chosen attempt.
  const firstBySite = new Map<string, number>();
  for (const a of state.attempts) if (!firstBySite.has(a.site)) firstBySite.set(a.site, a.pass_rate);
  const plain = mean([...firstBySite.values()]);
  const chosen = state.tuned.flatMap((t) => (t.chosen ? [t.chosen] : []));
  const after = state.tuned.length ? mean(state.tuned.map((t) => t.chosen?.pass_rate ?? 0)) : null;
  const heldOut = mean(chosen.flatMap((c) => (c.holdout_pass == null ? [] : [c.holdout_pass])));

  // Costs come from Token Factory's price list; if any attempt couldn't be priced, the total is unknown, not zero.
  const costKnown = state.attempts.length > 0 && state.attempts.every((a) => a.cost_usd != null);
  const evalCost = state.attempts.reduce((sum, a) => sum + (a.cost_usd ?? 0), 0);
  const ownCost = !costKnown ? null : state.cost ? state.cost.evals_usd + state.cost.helper_usd : evalCost;

  const rows = state.pr?.rows.filter((r) => r.status === "ported") ?? [];
  const priced = rows.length > 0 && rows.every((r) => r.cost_was != null && r.cost_now != null);
  const before = priced ? rows.reduce((s, r) => s + (r.cost_was ?? 0), 0) : null;
  const now = priced ? rows.reduce((s, r) => s + (r.cost_now ?? 0), 0) : null;

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      <Tile label="LLM calls found" value={String(state.sites.length)} sub={state.files ? `in ${state.files} files${review ? `, ${review} need review` : ""}` : undefined} />
      <Tile label="Plain swap passes" value={pct(plain)} sub="Nano, prompt as written" tone={plain == null ? "text-white" : "text-fail"} />
      <Tile label="After OpenPort" value={pct(after)} sub={heldOut != null ? `held-out cases: ${pct(heldOut)}` : "cheapest tier that passes"} tone={after == null ? "text-white" : "text-pass"} />
      <Tile
        label="OpenPort's own cost"
        value={usd(ownCost)}
        sub={ownCost != null ? "evals, engineer and judge" : state.attempts.length ? "prices unavailable" : "while testing and tuning"}
      />
      <Tile label="Cost per 1k calls" value={priced ? `${usd(before)} → ${usd(now)}` : "–"} sub={priced ? "est. at list prices" : "after tuning and pricing"} tone={priced ? "text-pass" : "text-white"} />
    </div>
  );
}
