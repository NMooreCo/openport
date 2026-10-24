import { useState, type ReactNode } from "react";
import { DiffView } from "./DiffView";
import { PrView } from "./PrView";

function Tab({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button role="tab" aria-selected={selected} onClick={onClick}
      className={`border-b-2 px-3 py-2 text-sm ${selected ? "border-emerald-400 text-white" : "border-transparent text-muted hover:text-white"}`}>
      {children}
    </button>
  );
}

/** A run's result: the pull request description and the code changes behind it. */
export function ResultTabs({ body, diff, files }: { body?: string; diff?: string; files: number }) {
  const [tab, setTab] = useState<"pr" | "diff">("pr");
  if (body == null && diff == null) return null;
  const showDiff = diff != null && (tab === "diff" || body == null);
  return (
    <section aria-label="Result">
      <div className="flex gap-1 border-b border-line" role="tablist">
        {body != null && <Tab selected={!showDiff} onClick={() => setTab("pr")}>Pull request</Tab>}
        {diff != null && (
          <Tab selected={showDiff} onClick={() => setTab("diff")}>
            Code changes <span className="text-muted">({files} files)</span>
          </Tab>
        )}
      </div>
      <div className="mt-4">{showDiff ? <DiffView diff={diff} /> : body != null ? <PrView body={body} /> : null}</div>
    </section>
  );
}
