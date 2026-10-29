// How the dashboard reaches OpenPort: the server's API, or, in the static replay build (`vite build --mode
// static`), the demos' recorded runs shipped next to the page and replayed here at the server's own pace.
import type { Mode } from "./components/Launcher";

export interface Preset {
  id: string;
  label: string;
  detail: string;
  tested?: boolean; // false on a public server that won't run this repo's code
}

export interface RunSummary {
  id: string;
  label: string;
  kind: string;
  target: string;
  recorded?: boolean;
  created: number;
  github?: string | null; // its pull request, once opened on GitHub
}

export interface ServerEvent {
  seq: number;
  type: string;
  time?: number;
  [key: string]: unknown;
}

/** A page with no server behind it: it replays recordings, and can't start live runs. */
export const STATIC = import.meta.env.MODE === "static";

const REPLAY_SPEED = 20; // as fast as the server replays a recorded run,
const REPLAY_MAX_GAP = 1.5; // with no pause longer than this, in seconds

interface Replay extends RunSummary {
  preset: string; // the launcher's preset it's the recording of
  detail: string;
}

let replays: Promise<Replay[]> | null = null;
const replayIndex = (): Promise<Replay[]> => (replays ??= fetch("replay/index.json").then((r) => r.json()));

async function json<T>(response: Response): Promise<T> {
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "OpenPort couldn't do that");
  return data;
}

export async function info(): Promise<{ vetted_only: boolean }> {
  return STATIC ? { vetted_only: false } : fetch("/api/info").then((r) => json(r));
}

/** Whether live runs need the judges' password, whether this browser has given it, the demo video, and the GitHub
 * organization the server opens pull requests in (null when it can't). */
export interface Site {
  gated: boolean;
  judge: boolean;
  video_url: string | null;
  github_org?: string | null;
}

// The judges' password: kept in this browser only, and sent only with the requests that start runs.
const JUDGE_KEY = "openport-judge";

export function judgePassword(): string {
  try {
    return localStorage.getItem(JUDGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberJudge(password: string | null): void {
  try {
    if (password) localStorage.setItem(JUDGE_KEY, password);
    else localStorage.removeItem(JUDGE_KEY);
  } catch {
    // private windows and blocked storage: the password lasts as long as the page
  }
}

const judgeHeader = (password = judgePassword()): Record<string, string> => (password ? { "x-openport-judge": password } : {});

export async function site(password = judgePassword()): Promise<Site> {
  if (STATIC) return { gated: false, judge: false, video_url: null }; // a static page runs nothing live
  return fetch("/api/site", { headers: judgeHeader(password) }).then((r) => json(r));
}

export async function presets(mode: Mode): Promise<Preset[]> {
  if (!STATIC) return fetch(mode === "upgrade" ? "/api/upgrade-presets" : "/api/presets").then((r) => json(r));
  return (await replayIndex()).filter((r) => r.kind === mode).map((r) => ({ id: r.preset, label: r.label, detail: r.detail }));
}

/** Start a run of a preset or repo (or, when there's a recent or recorded one, get that), and return its ID. */
export async function start(target: string, mode: Mode, live = false): Promise<string> {
  if (STATIC) {
    const replay = (await replayIndex()).find((r) => r.kind === mode && (r.preset === target || r.target === target));
    if (!replay) throw new Error("This page replays the demos' recorded runs only. To run OpenPort on another repo, install it: see the README.");
    return replay.id;
  }
  const response = await fetch(mode === "upgrade" ? "/api/upgrades" : "/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...judgeHeader() },
    body: JSON.stringify({ target, live }),
  });
  return (await json<RunSummary>(response)).id;
}

/** Open a finished live port's pull request on GitHub (or get the one already opened); returns its URL. */
export async function openOnGitHub(id: string): Promise<string> {
  const response = await fetch(`/api/runs/${id}/github`, { method: "POST", headers: judgeHeader() });
  return (await json<{ url: string }>(response)).url;
}

export async function run(id: string): Promise<RunSummary> {
  if (!STATIC) return fetch(`/api/runs/${id}`).then((r) => json(r));
  const replay = (await replayIndex()).find((r) => r.id === id);
  if (!replay) throw new Error("No such run");
  return { ...replay, recorded: true };
}

/** Follow a run's events as they happen; returns a function that stops following. */
export function follow(id: string, types: string[], onEvent: (event: ServerEvent) => void): () => void {
  if (!STATIC) {
    const source = new EventSource(`/api/runs/${id}/events`);
    const handler = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as ServerEvent;
      onEvent(event);
      if (event.type === "done" || event.type === "failed") source.close();
    };
    for (const type of types) source.addEventListener(type, handler);
    return () => source.close();
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  fetch(`replay/${id}.json`)
    .then((r) => r.json())
    .then(({ events }: { events: ServerEvent[] }) => {
      const play = (index: number) => {
        if (stopped || index >= events.length) return;
        if (types.includes(events[index].type)) onEvent(events[index]);
        const gap = index + 1 < events.length ? (events[index + 1].time ?? 0) - (events[index].time ?? 0) : 0;
        timer = setTimeout(() => play(index + 1), Math.min(Math.max(gap, 0) / REPLAY_SPEED, REPLAY_MAX_GAP) * 1000);
      };
      play(0);
    });
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
