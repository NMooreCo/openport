import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo, useState } from "react";

export function PrView({ body }: { body: string }) {
  // The description includes text from the scanned repo and from the model: sanitize before rendering.
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(body, { async: false })), [body]);
  const [copied, setCopied] = useState(false);
  return (
    <div className="panel rounded-2xl">
      <div className="flex items-center justify-between border-b border-line px-4 py-2">
        <p className="text-xs text-muted">Pull request description (Markdown)</p>
        <button
          onClick={() => navigator.clipboard.writeText(body).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}
          className="rounded-md border border-line px-2 py-1 text-xs text-slate-300 hover:border-emerald-500/60"
        >
          {copied ? "Copied" : "Copy Markdown"}
        </button>
      </div>
      <div className="prose-pr px-5 py-4 text-sm text-slate-200" dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
