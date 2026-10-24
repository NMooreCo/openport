import { enterStage, finishStages, pendingStages, useEvents, type RunEvent, type Source, type StageState } from "./run";

export const UPGRADE_STAGES = ["fetch", "find", "test", "fix", "pr"] as const;
export type UpgradeStage = (typeof UPGRADE_STAGES)[number];
export const UPGRADE_STAGE_LABELS: Record<UpgradeStage, string> = {
  fetch: "Fetch", find: "Find vulnerabilities", test: "Upgrade & test", fix: "Fix breakages", pr: "Pull request",
};

export interface Package {
  name: string;
  version: string;
  ecosystem: string;
  source: string;
  status: string; // "upgrade" | "partial" | "held: fix too new" | "held: no release dates" | "no fix available"
  target: string | null;
  age_days: number | null;
  fixed: string[];
  still_open: string[];
  advisories: { id: string; summary: string }[];
  bumpable: boolean; // pinned where OpenPort can bump it (requirements.txt); lockfiles need their package manager
}

export interface TestResult {
  ok: boolean;
  summary: string;
  seconds: number;
  output: string;
}

export interface FixRound {
  files: string[];
  rationale: string;
  sources: Source[];
  rejected: { file: string; reason: string }[];
  tests: TestResult | null; // null when every proposed edit was rejected, so the tests didn't run again
}

export interface UpgradeState {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  stages: Record<UpgradeStage, StageState>;
  deps?: { count: number; files: string[] };
  packages?: Package[];
  bumped?: string[]; // the packages actually upgraded, once the bump is done
  before?: TestResult;
  afterBump?: TestResult;
  research: Source[];
  fixes: FixRound[];
  pr?: { body: string; diff: string; proven: boolean };
  notes: string[];
  error?: string;
}

export const UPGRADE_EVENT_TYPES = ["stage", "note", "deps", "advisories", "bumped", "tests", "research", "fix", "upgrade_pr", "done", "failed"];

export function initialUpgrade(id: string): UpgradeState {
  return { id, status: "queued", stages: pendingStages(UPGRADE_STAGES), research: [], fixes: [], notes: [] };
}

const testResult = (event: RunEvent): TestResult => ({ ok: event.ok, summary: event.summary, seconds: event.seconds, output: event.output });

export function reduceUpgrade(state: UpgradeState, event: RunEvent): UpgradeState {
  switch (event.type) {
    case "stage":
      return { ...state, status: "running", stages: enterStage(UPGRADE_STAGES, state.stages, event.stage) };
    case "note":
      return { ...state, notes: [...state.notes, event.message] };
    case "deps":
      return { ...state, deps: { count: event.count, files: event.files } };
    case "advisories":
      return { ...state, packages: event.packages };
    case "bumped":
      return { ...state, bumped: event.packages };
    case "tests":
      return event.step === "before" ? { ...state, before: testResult(event) } : { ...state, afterBump: testResult(event) };
    case "research":
      return { ...state, research: event.sources };
    case "fix": {
      const round = { files: event.files, rationale: event.rationale, sources: event.sources, rejected: event.rejected, tests: event.tests };
      return { ...state, fixes: [...state.fixes, round] };
    }
    case "upgrade_pr":
      return { ...state, pr: { body: event.body, diff: event.diff, proven: event.proven } };
    case "done":
      return { ...state, status: "done", stages: finishStages(UPGRADE_STAGES, state.stages) };
    case "failed":
      return { ...state, status: "failed", error: event.message };
    default:
      return state;
  }
}

export const useUpgrade = (runId: string | null) => useEvents(runId, initialUpgrade, reduceUpgrade, UPGRADE_EVENT_TYPES);

/** The packages this run upgrades: those actually bumped once that's done, else the ones it means to bump. */
export const upgraded = (state: UpgradeState) =>
  (state.packages ?? []).filter((p) => (state.bumped ? state.bumped.includes(p.name) : p.target && p.bumpable));

/** The latest test result: after the last fix that re-ran them, else after the bump. */
export function finalTests(state: UpgradeState): TestResult | undefined {
  const fixed = state.fixes.filter((f) => f.tests);
  return fixed.length ? fixed[fixed.length - 1].tests ?? undefined : state.afterBump;
}

/** pytest's summary without the timing or warnings, e.g. "4 failed, 1 passed". */
export const shortSummary = (result: TestResult) =>
  result.summary.replace(/\s+in\s+[\d.]+s$/, "").replace(/,\s*\d+ warnings?/, "") || (result.ok ? "passed" : "failed");

export const advisoryUrl = (id: string) =>
  id.startsWith("CVE-") ? `https://nvd.nist.gov/vuln/detail/${id}` : `https://osv.dev/vulnerability/${id}`;
