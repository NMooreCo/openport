import type { StageState } from "../run";

const DOT: Record<StageState, string> = {
  pending: "border-line bg-transparent",
  active: "border-tune bg-tune animate-pulse",
  done: "border-pass bg-pass",
  skipped: "border-line bg-line",
};

export function Stages<S extends string>({ order, labels, stages, failed }: {
  order: readonly S[];
  labels: Record<S, string>;
  stages: Record<S, StageState>;
  failed: boolean;
}) {
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-2 text-sm">
      {order.map((stage, index) => {
        const status = failed && stages[stage] === "active" ? "failed" : stages[stage];
        return (
          <li key={stage} className="flex items-center gap-2">
            {index > 0 && <span className="h-px w-5 bg-line sm:w-8" aria-hidden />}
            <span
              className={`h-2.5 w-2.5 rounded-full border-2 ${status === "failed" ? "border-fail bg-fail" : DOT[status]}`}
              aria-hidden
            />
            <span className={status === "pending" || status === "skipped" ? "text-muted" : "text-white"}>
              {labels[stage]}
              {status === "skipped" && <span className="ml-1 text-xs text-muted">(skipped)</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
