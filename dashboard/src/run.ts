import { useEffect, useState } from "react";

export const STAGES = ["fetch", "scan", "capture", "tune", "port", "pr"] as const;
export type Stage = (typeof STAGES)[number];
export type StageState = "pending" | "active" | "done" | "skipped";

export interface Finding {
  id: string;
  file: string;
  line: number;
  function: string;
  sdk: "openai" | "anthropic";
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
  attempts: Attempt[];
  tuned: Tuned[];
  cost?: { evals_usd: number; helper_usd: number };
  ported?: { sites: PortedSite[]; changed: string[]; added: string[]; diff: string };
  pr?: { body: string; rows: PrRow[] };
  notes: string[];
  error?: string;
}

type RunEvent = { type: string; [key: string]: any };

export const EVENT_TYPES = ["stage", "note", "scan", "models", "captured", "attempt", "tuned", "cost", "ported", "pr", "done", "failed"];

export function initialState(id: string): RunState {
  return {
    id,
    status: "queued",
    stages: { fetch: "pending", scan: "pending", capture: "pending", tune: "pending", port: "pending", pr: "pending" },
    sites: [],
    attempts: [],
    tuned: [],
    notes: [],
  };
}

export function reduce(state: RunState, event: RunEvent): RunState {
  switch (event.type) {
    case "stage": {
      const stages = { ...state.stages };
      for (const stage of STAGES) if (stages[stage] === "active") stages[stage] = "done";
      // Stages the pipeline jumped over (e.g. tuning without an eval spec) are skipped, not pending.
      const index = STAGES.indexOf(event.stage);
      for (const stage of STAGES.slice(0, index)) if (stages[stage] === "pending") stages[stage] = "skipped";
      stages[event.stage as Stage] = "active";
      return { ...state, status: "running", stages };
    }
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
    case "done": {
      const stages = { ...state.stages };
      for (const stage of STAGES) stages[stage] = stages[stage] === "pending" ? "skipped" : stages[stage] === "active" ? "done" : stages[stage];
      return { ...state, status: "done", stages };
    }
    case "failed":
      return { ...state, status: "failed", error: event.message };
    default:
      return state;
  }
}

/** Follow a run's live event stream. */
export function useRun(runId: string | null): RunState | null {
  const [state, setState] = useState<RunState | null>(null);
  useEffect(() => {
    if (!runId) return;
    let current = initialState(runId);
    setState(current);
    const source = new EventSource(`/api/runs/${runId}/events`);
    const onEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RunEvent;
      current = reduce(current, event);
      setState(current);
      if (event.type === "done" || event.type === "failed") source.close();
    };
    for (const type of EVENT_TYPES) source.addEventListener(type, onEvent);
    return () => source.close();
  }, [runId]);
  return state;
}

/** Tuning attempts for a scanned call site (matched on file and function). */
export function attemptsFor(state: RunState, site: Finding): Attempt[] {
  return state.attempts.filter((a) => a.file === site.file && a.function === site.function);
}

export function tunedFor(state: RunState, site: Finding): Tuned | undefined {
  return state.tuned.find((t) => t.file === site.file && t.function === site.function);
}

export const pct = (value: number | null | undefined) => (value == null ? "–" : `${Math.round(value * 100)}%`);

export const usd = (value: number | null | undefined) =>
  value == null ? "–" : value >= 0.1 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;

export const tierName = (tier: string) => ({ nano: "Nano", super: "Super", ultra: "Ultra" })[tier] ?? tier;
