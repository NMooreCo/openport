import { useState } from "react";
import { lock, unlock, useSite } from "../site";

/** Where the hosted demo keeps live runs for the hackathon's judges: the password field, or that it's unlocked. */
export function JudgeUnlock({ compact = false }: { compact?: boolean }) {
  const site = useSite();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!site?.gated) return null; // no gate (a server of your own, or the static replay)

  if (site.judge) {
    return (
      <p className={`flex flex-wrap items-center gap-3 text-sm text-emerald-300 ${compact ? "" : "mt-4"}`}>
        <span>Live runs unlocked. "Run it live" on any demo calls Token Factory for real.</span>
        <button onClick={lock} className="text-xs text-muted underline hover:text-white">Lock</button>
      </p>
    );
  }

  const submit = async () => {
    if (!password.trim()) return;
    setBusy(true);
    setError("");
    try {
      if (!(await unlock(password.trim()))) setError("That isn't the password. It's in the submission's testing instructions.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  };

  return (
    <form
      className={compact ? "flex flex-wrap items-center gap-2" : "mt-4 flex flex-col gap-3 sm:flex-row"}
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label className="sr-only" htmlFor={compact ? "judge-compact" : "judge"}>Judges' password</label>
      <input
        id={compact ? "judge-compact" : "judge"}
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Judges' password"
        className={`min-w-0 rounded-lg border border-line bg-panel px-3 text-sm text-white placeholder:text-muted focus:border-emerald-500 focus:outline-none ${compact ? "w-44 py-1.5" : "flex-1 py-3"}`}
      />
      <button
        disabled={busy || !password.trim()}
        className={`rounded-lg bg-emerald-500 font-semibold text-ink transition hover:bg-emerald-400 disabled:opacity-40 ${compact ? "px-3 py-1.5 text-xs" : "px-5 py-3 text-sm"}`}
      >
        Unlock live runs
      </button>
      {error && <p role="alert" className="w-full text-xs text-red-300">{error}</p>}
    </form>
  );
}
