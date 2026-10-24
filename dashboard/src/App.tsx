import { useEffect, useState } from "react";
import { Launcher, type Mode } from "./components/Launcher";
import { PortView } from "./components/PortView";
import { UpgradeView } from "./components/UpgradeView";

const runFromHash = () => location.hash.match(/run=([a-f0-9]+)/)?.[1] ?? null;

interface RunInfo {
  id: string;
  label: string;
  kind: Mode;
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
      .then((data) => current && setRun({ id: data.id, label: data.label, kind: data.kind === "upgrade" ? "upgrade" : "port" }))
      .catch(() => {
        if (current) location.hash = "";
      });
    return () => { current = false; };
  }, [runId]);

  const start = async (target: string, mode: Mode) => {
    const response = await fetch(mode === "upgrade" ? "/api/upgrades" : "/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail ?? "Couldn't start the run");
    location.hash = `run=${data.id}`;
  };

  if (!runId) return <Launcher onStart={start} />;
  if (run?.id !== runId) return null; // the run's details are still loading
  return run.kind === "upgrade" ? <UpgradeView runId={runId} label={run.label} /> : <PortView runId={runId} label={run.label} />;
}
