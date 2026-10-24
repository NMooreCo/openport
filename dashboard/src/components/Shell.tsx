import type { ReactNode } from "react";

/** The page around a run: the header with its live status and a way back to the launcher. */
export function Shell({ label, status, home, tagline, children }: {
  label: string;
  status: string;
  home: string;
  tagline: string;
  children: ReactNode;
}) {
  const live = status === "running" || status === "queued";
  return (
    <div className="min-h-screen">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <a href={home} className="shrink-0 font-semibold text-white">
            OpenPort <span className="hidden font-normal text-muted sm:inline">· {tagline}</span>
          </a>
          <div className="flex min-w-0 items-center gap-3">
            {live && (
              <span className="flex items-center gap-1.5 text-xs text-tune">
                <span className="h-2 w-2 animate-pulse rounded-full bg-tune" aria-hidden />
                {status === "queued" ? "Queued" : "Live"}
              </span>
            )}
            <span className="truncate font-mono text-sm text-slate-300">{label}</span>
            <a href={home} className="shrink-0 rounded-md border border-line px-3 py-1.5 text-xs text-slate-300 hover:border-emerald-500/60">
              New run
            </a>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6">{children}</main>
    </div>
  );
}
