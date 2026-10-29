import { useEffect, useState } from "react";
import * as api from "./api";
import { Landing } from "./components/Landing";
import { Launcher, type Mode } from "./components/Launcher";
import { PortView } from "./components/PortView";
import { UpgradeView } from "./components/UpgradeView";

const runFromHash = () => location.hash.match(/run=([a-f0-9]+)/)?.[1] ?? null;
// #app and #upgrade are the launcher (porting, upgrading), #run=<id> a run, anything else the front page.
const inLauncher = () => location.hash === "#app" || location.hash === "#upgrade";

export interface RunInfo {
  id: string;
  label: string;
  kind: Mode;
  target: string;
  recorded: boolean; // a recorded run of a demo, replayed
  created: number;
  github: string | null; // its pull request on GitHub, once opened
}

export default function App() {
  const [runId, setRunId] = useState<string | null>(runFromHash);
  const [launcher, setLauncher] = useState(inLauncher);
  const [run, setRun] = useState<RunInfo | null>(null);

  useEffect(() => {
    const onHash = () => {
      setRunId(runFromHash());
      setLauncher(inLauncher());
      if (!runFromHash()) window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!runId) return;
    let current = true;
    api.run(runId)
      .then((data) => current && setRun({
        id: data.id, label: data.label, kind: data.kind === "upgrade" ? "upgrade" : "port",
        target: data.target, recorded: Boolean(data.recorded), created: data.created, github: data.github ?? null,
      }))
      .catch(() => {
        if (current) location.hash = "app";
      });
    return () => { current = false; };
  }, [runId]);

  const start = async (target: string, mode: Mode, live = false) => {
    location.hash = `run=${await api.start(target, mode, live)}`;
  };

  if (!runId) return launcher ? <Launcher onStart={start} /> : <Landing />;
  if (run?.id !== runId) return null; // the run's details are still loading
  const runLive = api.STATIC ? undefined : () => start(run.target, run.kind, true); // a static page can't run anything
  return run.kind === "upgrade" ? <UpgradeView run={run} onRunLive={runLive} /> : <PortView run={run} onRunLive={runLive} />;
}
