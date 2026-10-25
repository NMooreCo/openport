import { useEffect, useState } from "react";

export const STAGES = ["fetch", "scan", "capture", "tune", "port", "pr"] as const;
export type Stage = (typeof STAGES)[number];
export type StageState = "pending" | "active" | "done" | "skipped";
export const STAGE_LABELS: Record<Stage, string> = {
  fetch: "Fetch", scan: "Find calls", capture: "Capture evals", tune: "Test & tune", port: "Rewrite", pr: "Pull request",
};

export const pendingStages = <S extends string>(order: readonly S[]) =>
  Object.fromEntries(order.map((stage) => [stage, "pending"])) as Record<S, StageState>;

/** The pipeline moved on to `stage`: the active stage is done, and stages it jumped over were skipped. */
export function enterStage<S extends string>(order: readonly S[], stages: Record<S, StageState>, stage: S): Record<S, StageState> {
  const next = { ...stages };
  for (const s of order) if (next[s] === "active") next[s] = "done";
  // Stages the pipeline jumped over (e.g. tuning without an eval spec) are skipped, not pending.
  for (const s of order.slice(0, order.indexOf(stage))) if (next[s] === "pending") next[s] = "skipped";
  next[stage] = "active";
  return next;
}

/** The run ended: the active stage is done, and stages it never reached were skipped. */
export function finishStages<S extends string>(order: readonly S[], stages: Record<S, StageState>): Record<S, StageState> {
  const next = { ...stages };
  for (const s of order) next[s] = next[s] === "pending" ? "skipped" : next[s] === "active" ? "done" : next[s];
  return next;
}

export interface Finding {
  id: string;
  file: string;
  line: number;
  function: string;
  sdk: "openai" | "anthropic" | "http"; // http: a plain HTTP call to an LLM endpoint, found but not ported
  api: string;
  model: string | null;
  model_expr: string | null;
  output: string;
  features: string[];
  portability: "auto" | "translate" | "review";
  notes: string[];
}

export interface Attempt {
  site: string;
  title: string;
  file: string;
  function: string;
  tier: "nano" | "super" | "ultra";
  model: string;
  round: number;
  variant: string;
  addendum: string;
  rationale: string;
  tune_pass: number;
  holdout_pass: number | null;
  pass_rate: number;
  passed: number;
  total: number;
  cost_usd: number | null;
  meets: boolean;
  sources?: Source[];
}

export interface Source {
  title: string;
  url: string;
  quote?: string; // a change's citation: the sentence it followed, checked against the page
}

/** NVIDIA guidance Tavily found for a call before the engineer proposed a fix. */
export interface Research {
  site: string;
  file: string;
  function: string;
  model: string;
  topic: string;
  query: string;
  sources: Source[];
}

export interface Tuned {
  site: string;
  file: string;
  function: string;
  chosen: Attempt | null;
  plain: number | null;
}

export interface PortedSite {
  id: string;
  file: string;
  line: number;
  status: "ported" | "kept";
  was: string | null;
  now: string | null;
  reason: string;
  notes: string[];
}

export interface PrRow {
  id: string;
  status: string;
  cost_was: number | null;
  cost_now: number | null;
}

export interface Captured {
  sites: { site: string; cases: number; grader: string }[];
  note: string;
}

export interface RunState {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  stages: Record<Stage, StageState>;
  files?: number;
  sites: Finding[];
  models?: Record<string, string>;
  captured?: Captured;
  research: Research[];
  attempts: Attempt[];
  tuned: Tuned[];
  cost?: { evals_usd: number; helper_usd: number };
  ported?: { sites: PortedSite[]; changed: string[]; added: string[]; diff: string };
  pr?: { body: string; rows: PrRow[] };
  notes: string[];
  error?: string;
}

export type RunEvent = { type: string; [key: string]: any };

export const EVENT_TYPES = ["stage", "note", "scan", "models", "captured", "research", "attempt", "tuned", "cost", "ported", "pr", "done", "failed"];

export function initialState(id: string): RunState {
  return { id, status: "queued", stages: pendingStages(STAGES), sites: [], research: [], attempts: [], tuned: [], notes: [] };
}

export function reduce(state: RunState, event: RunEvent): RunState {
  switch (event.type) {
    case "stage":
      return { ...state, status: "running", stages: enterStage(STAGES, state.stages, event.stage) };
    case "note":
      return { ...state, notes: [...state.notes, event.message] };
    case "scan":
      return { ...state, sites: event.sites, files: event.files };
    case "models":
      return { ...state, models: event.models };
    case "captured": {
      const total = event.sites.reduce((sum: number, s: { cases: number }) => sum + s.cases, 0);
      const summary = `Captured a starter eval set: ${total} generated case${total === 1 ? "" : "s"} across ${event.sites.length} call${event.sites.length === 1 ? "" : "s"}.`;
      return { ...state, captured: { sites: event.sites, note: event.note }, notes: [...state.notes, summary, event.note] };
    }
    case "research":
      return { ...state, research: [...state.research, event as unknown as Research] };
    case "attempt":
      return { ...state, attempts: [...state.attempts, event as unknown as Attempt] };
    case "tuned":
      return { ...state, tuned: [...state.tuned, event as unknown as Tuned] };
    case "cost":
      return { ...state, cost: { evals_usd: event.evals_usd, helper_usd: event.helper_usd } };
    case "ported":
      return { ...state, ported: { sites: event.sites, changed: event.changed, added: event.added, diff: event.diff } };
    case "pr":
      return { ...state, pr: { body: event.body, rows: event.rows } };
    case "done":
      return { ...state, status: "done", stages: finishStages(STAGES, state.stages) };
    case "failed":
      return { ...state, status: "failed", error: event.message };
    default:
      return state;
  }
}

/** Follow a run's live event stream, folding each event into the state. */
export function useEvents<T>(runId: string | null, init: (id: string) => T, reducer: (state: T, event: RunEvent) => T, types: string[]): T | null {
  const [state, setState] = useState<T | null>(null);
  useEffect(() => {
    if (!runId) return;
    let current = init(runId);
    setState(current);
    const source = new EventSource(`/api/runs/${runId}/events`);
    const onEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RunEvent;
      current = reducer(current, event);
      setState(current);
      if (event.type === "done" || event.type === "failed") source.close();
    };
    for (const type of types) source.addEventListener(type, onEvent);
    return () => source.close();
  }, [runId]);
  return state;
}

export const useRun = (runId: string | null) => useEvents(runId, initialState, reduce, EVENT_TYPES);

/** Tuning attempts for a scanned call site (matched on file and function). */
export function attemptsFor(state: RunState, site: Finding): Attempt[] {
  return state.attempts.filter((a) => a.file === site.file && a.function === site.function);
}

export function researchFor(state: RunState, site: Finding): Research[] {
  return state.research.filter((r) => r.file === site.file && r.function === site.function);
}

export function tunedFor(state: RunState, site: Finding): Tuned | undefined {
  return state.tuned.find((t) => t.file === site.file && t.function === site.function);
}

export const pct = (value: number | null | undefined) => (value == null ? "–" : `${Math.round(value * 100)}%`);

export const usd = (value: number | null | undefined) =>
  value == null ? "–" : value >= 0.1 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;

export const tierName = (tier: string) => ({ nano: "Nano", super: "Super", ultra: "Ultra" })[tier] ?? tier;
