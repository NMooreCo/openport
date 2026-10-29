import { useState } from "react";
import type { RunInfo } from "../App";
import * as api from "../api";
import { useSite } from "../site";
import { JudgeUnlock } from "./JudgeUnlock";

/** owner/name#12, from a pull request's URL. */
const shortName = (url: string) => url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/)?.slice(1).join("#") ?? url;

/**
 * The run's pull request on GitHub: a link once it's open, or, for a finished live port on a server that can open
 * it, the button that does (for the judges, where live runs need their password).
 */
export function GitHubPr({ run, ready }: { run: RunInfo; ready: boolean }) {
  const site = useSite();
  const [url, setUrl] = useState(run.github);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-emerald-400/30 bg-emerald-400/[0.06] px-4 py-3 text-sm transition hover:border-emerald-400/60"
      >
        <span className="text-slate-200">
          Opened on GitHub: <span className="font-mono text-emerald-300">{shortName(url)}</span>
        </span>
        <span className="font-medium text-white">View the pull request ↗</span>
      </a>
    );
  }
  const org = site?.github_org;
  if (api.STATIC || run.recorded || !ready || !org) return null;

  const fork = run.target.includes("/"); // a repo from GitHub, not a demo app
  const open = async () => {
    setBusy(true);
    setError("");
    try {
      setUrl(await api.openOnGitHub(run.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  };
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-panel px-4 py-3">
      <p className="max-w-xl text-sm text-muted">
        Open this pull request on GitHub, in the <span className="font-mono text-slate-200">{org}</span> organization
        {fork ? `, on a fork of ${run.target}: its maintainers aren't notified.` : ", in the demo app's own repo there."}
      </p>
      {site.gated && !site.judge ? (
        <JudgeUnlock compact />
      ) : (
        <button
          onClick={open}
          disabled={busy}
          className="rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-ink transition hover:bg-emerald-400 disabled:opacity-50"
        >
          {busy ? "Opening on GitHub…" : "Open on GitHub"}
        </button>
      )}
      {error && <p role="alert" className="w-full text-sm text-red-300">{error}</p>}
    </div>
  );
}
