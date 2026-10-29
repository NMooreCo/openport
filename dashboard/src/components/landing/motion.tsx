// Scroll-driven motion for the front page: things rise into view, numbers count up, bars fill. With "reduce
// motion" set, everything simply appears in its final state.
import { useEffect, useRef, useState, type ReactNode } from "react";

export const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Whether the element has scrolled into view (once it has, it stays true). */
export function useInView<T extends Element>(margin = "0px 0px -12% 0px") {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element || seen) return;
    if (reducedMotion() || typeof IntersectionObserver === "undefined") return setSeen(true);
    const observer = new IntersectionObserver(([entry]) => entry.isIntersecting && setSeen(true), { rootMargin: margin });
    observer.observe(element);
    return () => observer.disconnect();
  }, [seen, margin]);
  return [ref, seen] as const;
}

export function Reveal({ children, delay = 0, className = "" }: { children: ReactNode; delay?: number; className?: string }) {
  const [ref, seen] = useInView<HTMLDivElement>();
  return (
    <div ref={ref} className={`reveal ${seen ? "in" : ""} ${className}`} style={{ transitionDelay: `${delay}ms` }}>
      {children}
    </div>
  );
}

/** A number that eases from `from` to `to` once `run` turns true. */
export function useCount(run: boolean, to: number, { from = 0, ms = 1400, delay = 0 } = {}) {
  const [value, setValue] = useState(reducedMotion() ? to : from);
  useEffect(() => {
    if (!run) return;
    if (reducedMotion()) return setValue(to);
    let frame = 0;
    const begin = performance.now() + delay;
    const tick = (now: number) => {
      const t = Math.min(1, Math.max(0, (now - begin) / ms));
      setValue(from + (to - from) * (1 - Math.pow(1 - t, 3)));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [run, to, from, ms, delay]);
  return value;
}

export function CountUp({ to, from = 0, format = (n) => String(Math.round(n)), ms, delay }: {
  to: number; from?: number; format?: (n: number) => string; ms?: number; delay?: number;
}) {
  const [ref, seen] = useInView<HTMLSpanElement>();
  const value = useCount(seen, to, { from, ms, delay });
  return <span ref={ref} className="tabular-nums">{format(value)}</span>;
}
