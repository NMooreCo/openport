import { useEffect, useState } from "react";
import { Launcher, type Mode } from "./components/Launcher";
import { PortView } from "./components/PortView";
import { UpgradeView } from "./components/UpgradeView";

const runFromHash = () => location.hash.match(/run=([a-f0-9]+)/)?.[1] ?? null;

export interface RunInfo {
  id: string;
  label: string;
  kind: Mode;
  target: string;
  recorded: boolean; // a recorded run of a demo, replayed
  created: number;
}

export default function App() {
  const [runId, setRunId] = useState<string | null>(runFromHash);
  const [run, setRun] = useState<RunInfo | null>(null);

  useEffect(() => {
    const onHash = () => setRunId(runFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!runId) return;
    let current = true;
    fetch(`/api/runs/${runId}`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data) => current && setRun({
        id: data.id, label: data.label, kind: data.kind === "upgrade" ? "upgrade" : "port",
        target: data.target, recorded: Boolean(data.recorded), created: data.created,
      }))
      .catch(() => {
        if (current) location.hash = "";
      });
    return () => { current = false; };
  }, [runId]);

  const start = async (target: string, mode: Mode, live = false) => {
    const response = await fetch(mode === "upgrade" ? "/api/upgrades" : "/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, live }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Couldn't start the run");
    location.hash = `run=${data.id}`;
  };

  if (!runId) return <Launcher onStart={start} />;
  if (run?.id !== runId) return null; // the run's details are still loading
  const runLive = () => start(run.target, run.kind, true);
  return run.kind === "upgrade" ? <UpgradeView run={run} onRunLive={runLive} /> : <PortView run={run} onRunLive={runLive} />;
}
