/** OpenPort's mark: the arrow into the port, on its emerald-to-cyan tile. */
export function Mark({ size = "h-7 w-7" }: { size?: string }) {
  return (
    <span className={`flex shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-400 to-cyan-500 shadow-[0_0_18px_rgba(52,211,153,0.5)] ${size}`} aria-hidden>
      <svg viewBox="0 0 24 24" className="h-[57%] w-[57%]" fill="none" stroke="#04121a" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 12h12" /><path d="M12 6l6 6-6 6" /><path d="M20 5v14" />
      </svg>
    </span>
  );
}

/** The mark and the wordmark, linking to the front page. */
export function Logo({ className = "" }: { className?: string }) {
  return (
    <a href="#" className={`flex shrink-0 items-center gap-2 font-display text-lg font-semibold text-white ${className}`}>
      <Mark />
      <span>Open<span className="text-emerald-300">Port</span></span>
    </a>
  );
}

/** The app pages' backdrop, as on the front page: a faint grid fading out, and two soft glows. */
export function Backdrop() {
  return (
    <div className="pointer-events-none fixed inset-0 -z-10" aria-hidden>
      <div className="hero-grid absolute inset-0 opacity-70" />
      <div className="absolute inset-0 bg-[radial-gradient(50rem_30rem_at_15%_0%,rgba(16,185,129,0.10),transparent_65%),radial-gradient(45rem_30rem_at_90%_10%,rgba(34,211,238,0.07),transparent_65%)]" />
    </div>
  );
}
