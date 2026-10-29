import { useMemo, useState } from "react";

interface FileDiff {
  path: string;
  lines: string[];
  added: number;
  removed: number;
}

function parse(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  for (const line of diff.split("\n")) {
    if (line.startsWith("--- ")) continue;
    if (line.startsWith("+++ ")) {
      current = { path: line.slice(4).replace(/^b\//, ""), lines: [], added: 0, removed: 0 };
      files.push(current);
      continue;
    }
    if (!current) continue;
    current.lines.push(line);
    if (line.startsWith("+")) current.added++;
    else if (line.startsWith("-")) current.removed++;
  }
  return files;
}

function lineClass(line: string) {
  if (line.startsWith("@@")) return "text-sky-300/80 bg-sky-500/5";
  if (line.startsWith("+")) return "text-emerald-200 bg-emerald-500/10";
  if (line.startsWith("-")) return "text-red-200 bg-red-500/10";
  return "text-slate-400";
}

function FilePanel({ file }: { file: FileDiff }) {
  // The vendored runtime and big new files start collapsed; the interesting edits are small.
  const [open, setOpen] = useState(!file.path.endsWith("openport_llm.py") && file.lines.length < 160);
  return (
    <section className="panel overflow-hidden rounded-xl">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-white/[0.02]">
        <span className="truncate font-mono text-xs text-white">{file.path}</span>
        <span className="shrink-0 font-mono text-xs">
          <span className="text-emerald-300">+{file.added}</span> <span className="text-red-300">−{file.removed}</span>
          <span className="ml-2 text-muted">{open ? "▾" : "▸"}</span>
        </span>
      </button>
      {open && (
        <pre className="overflow-x-auto bg-ink py-2 font-mono text-xs leading-5">
          {file.lines.map((line, i) => (
            <div key={i} className={`px-3 whitespace-pre ${lineClass(line)}`}>{line || " "}</div>
          ))}
        </pre>
      )}
    </section>
  );
}

// Edited code first, then new files, with the vendored runtime last.
const rank = (file: FileDiff) => (file.path.endsWith("openport_llm.py") ? 2 : file.removed === 0 ? 1 : 0);

export function DiffView({ diff }: { diff: string }) {
  const files = useMemo(() => parse(diff).sort((a, b) => rank(a) - rank(b) || a.path.localeCompare(b.path)), [diff]);
  return (
    <div className="space-y-3">
      {files.map((file) => <FilePanel key={file.path} file={file} />)}
    </div>
  );
}
