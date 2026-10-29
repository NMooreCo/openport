import type { StageState } from "../run";

type Shown = StageState | "failed";

// Each stage a chip: its dot, and the chip itself lit while the stage runs.
const DOT: Record<Shown, string> = {
  pending: "border border-white/25 bg-transparent",
  active: "bg-tune animate-pulse shadow-[0_0_10px_#f59e0b]",
  done: "bg-pass shadow-[0_0_8px_#10b981]",
  skipped: "bg-white/15",
  failed: "bg-fail shadow-[0_0_8px_#ef4444]",
};
const CHIP: Record<Shown, string> = {
  pending: "border-white/10 text-muted",
  active: "border-amber-400/40 bg-amber-400/[0.08] text-white",
  done: "border-emerald-400/25 bg-emerald-400/[0.05] text-slate-100",
  skipped: "border-white/5 text-muted",
  failed: "border-red-400/40 bg-red-400/[0.08] text-white",
};

export function Stages<S extends string>({ order, labels, stages, failed }: {
  order: readonly S[];
  labels: Record<S, string>;
  stages: Record<S, StageState>;
  failed: boolean;
}) {
  return (
    <ol className="flex flex-wrap items-center gap-x-1.5 gap-y-2 text-sm">
      {order.map((stage, index) => {
        const status: Shown = failed && stages[stage] === "active" ? "failed" : stages[stage];
        return (
          <li key={stage} className="flex items-center gap-1.5">
            {/* the links between chips only where the chips sit in one row */}
            {index > 0 && <span className={`hidden h-px w-5 md:block ${status === "pending" ? "bg-white/10" : "bg-emerald-400/30"}`} aria-hidden />}
            <span className={`flex items-center gap-2 rounded-full border px-3 py-1 transition-colors duration-500 ${CHIP[status]}`}>
              <span className={`h-2 w-2 rounded-full ${DOT[status]}`} aria-hidden />
              {labels[stage]}
              {status === "skipped" && <span className="text-xs text-muted">(skipped)</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
