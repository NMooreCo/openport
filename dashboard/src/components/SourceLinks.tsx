import type { Source } from "../run";

// Links come from web search results, so only plain web URLs are rendered as links.
export const isWebUrl = (url: string) => /^https?:\/\//i.test(url);

/** The pages a change followed, each with the sentence it quoted. Citations without a checked quote aren't shown. */
export function Citations({ label, sources }: { label: string; sources: Source[] }) {
  const quoted = sources.filter((s) => s.quote && isWebUrl(s.url));
  if (!quoted.length) return null;
  return (
    <ul className="mt-1 space-y-1 text-xs leading-relaxed text-sky-300/90">
      {quoted.map((s) => (
        <li key={`${s.url} ${s.quote}`}>
          <span className="text-muted">{label} </span>
          <a href={s.url} target="_blank" rel="noreferrer noopener" className="break-words underline decoration-sky-500/40 hover:text-white">
            {s.title}
          </a>
          <span className="text-slate-300">: “{s.quote}”</span>
        </li>
      ))}
    </ul>
  );
}

export function SourceLinks({ sources }: { sources: Source[] }) {
  return (
    <>
      {sources.filter((s) => isWebUrl(s.url)).map((s, i) => (
        <span key={s.url}>
          {i > 0 && ", "}
          <a href={s.url} target="_blank" rel="noreferrer noopener" className="break-words underline decoration-sky-500/40 hover:text-white">
            {s.title}
          </a>
        </span>
      ))}
    </>
  );
}
