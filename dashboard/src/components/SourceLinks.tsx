import type { Source } from "../run";

// Links come from web search results, so only plain web URLs are rendered as links.
const isWebUrl = (url: string) => /^https?:\/\//i.test(url);

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
