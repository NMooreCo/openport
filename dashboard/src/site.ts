// The site's state every page shares: is there a judges' gate, has this browser unlocked it, and the video.
import { useEffect, useState } from "react";
import * as api from "./api";

let current: api.Site | null = null;
let loading: Promise<api.Site> | null = null;
const listeners = new Set<() => void>();

function publish(next: api.Site): api.Site {
  current = next;
  listeners.forEach((listener) => listener());
  return next;
}

function load(): Promise<api.Site> {
  loading ??= api.site().then(publish).catch(() => publish({ gated: false, judge: false, video_url: null }));
  return loading;
}

export function useSite(): api.Site | null {
  const [, rerender] = useState(0);
  useEffect(() => {
    const listener = () => rerender((n) => n + 1);
    listeners.add(listener);
    load();
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return current;
}

/** Try the judges' password: kept in this browser if it's right. Returns whether it was. */
export async function unlock(password: string): Promise<boolean> {
  const answer = await api.site(password);
  if (answer.judge) {
    api.rememberJudge(password);
    publish(answer);
  }
  return answer.judge;
}

export function lock(): void {
  api.rememberJudge(null);
  if (current) publish({ ...current, judge: false });
}
