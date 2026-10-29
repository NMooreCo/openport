import { useEffect, useRef } from "react";
import { reducedMotion } from "./motion";

interface Node { x: number; y: number; vx: number; vy: number; r: number; open: boolean; glow: number }
interface Packet { from: number; to: number; t: number; speed: number }

const LINK = 150; // px within which two nodes are joined

/**
 * The hero's backdrop: a drifting network where slate nodes are closed-model APIs and green ones are Nemotron,
 * with calls (bright packets) travelling from the one to the other. It leans toward the pointer, draws one still
 * frame when motion is reduced, and stops while it's off screen or the tab is hidden.
 */
export function NetworkCanvas({ className = "" }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const still = reducedMotion();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let width = 0, height = 0, frame = 0, visible = true, last = 0;
    let nodes: Node[] = [];
    let packets: Packet[] = [];
    const pointer = { x: -1e4, y: -1e4 };

    const resize = () => {
      const box = canvas.getBoundingClientRect();
      width = box.width;
      height = box.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.round(Math.min(95, Math.max(28, (width * height) / 13000)));
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width, y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.22, vy: (Math.random() - 0.5) * 0.22,
        r: 1 + Math.random() * 1.7, open: Math.random() > 0.38, glow: 0,
      }));
      packets = [];
    };

    const neighbours = (i: number) => nodes.map((n, j) => [j, Math.hypot(n.x - nodes[i].x, n.y - nodes[i].y)] as const)
      .filter(([j, d]) => j !== i && d < LINK);

    const spawn = () => {
      const closed = nodes.map((n, i) => [n, i] as const).filter(([n]) => !n.open);
      if (!closed.length) return;
      const [, from] = closed[Math.floor(Math.random() * closed.length)];
      const targets = neighbours(from).filter(([j]) => nodes[j].open);
      if (!targets.length) return;
      const [to] = targets[Math.floor(Math.random() * targets.length)];
      packets.push({ from, to, t: 0, speed: 0.008 + Math.random() * 0.01 });
    };

    const draw = (dt: number) => {
      context.clearRect(0, 0, width, height);
      for (const n of nodes) {
        if (!still) {
          // a gentle pull toward the pointer, then drift on
          const dx = pointer.x - n.x, dy = pointer.y - n.y, d = Math.hypot(dx, dy);
          if (d < 220) { n.vx += (dx / d) * 0.004; n.vy += (dy / d) * 0.004; }
          n.vx *= 0.995; n.vy *= 0.995;
          n.x += n.vx * dt; n.y += n.vy * dt;
          if (n.x < -20) n.x = width + 20; else if (n.x > width + 20) n.x = -20;
          if (n.y < -20) n.y = height + 20; else if (n.y > height + 20) n.y = -20;
        }
        n.glow = Math.max(0, n.glow - 0.02 * dt);
      }
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j], d = Math.hypot(a.x - b.x, a.y - b.y);
          if (d > LINK) continue;
          const alpha = (1 - d / LINK) * 0.32;
          context.strokeStyle = a.open && b.open ? `rgba(52,211,153,${alpha})` : `rgba(125,150,190,${alpha * 0.7})`;
          context.lineWidth = 0.7;
          context.beginPath();
          context.moveTo(a.x, a.y);
          context.lineTo(b.x, b.y);
          context.stroke();
        }
      }
      for (const n of nodes) {
        context.beginPath();
        context.arc(n.x, n.y, n.r + n.glow * 2.5, 0, Math.PI * 2);
        context.fillStyle = n.open ? `rgba(52,211,153,${0.75 + n.glow * 0.25})` : "rgba(148,163,184,0.55)";
        context.shadowBlur = n.open ? 10 + n.glow * 18 : 0;
        context.shadowColor = "rgba(52,211,153,0.9)";
        context.fill();
      }
      context.shadowBlur = 0;
      packets = packets.filter((p) => {
        p.t += p.speed * dt;
        const a = nodes[p.from], b = nodes[p.to];
        if (!a || !b || p.t >= 1) {
          if (b) b.glow = 1;
          return false;
        }
        const x = a.x + (b.x - a.x) * p.t, y = a.y + (b.y - a.y) * p.t;
        context.beginPath();
        context.arc(x, y, 2.1, 0, Math.PI * 2);
        context.fillStyle = "#a7f3d0";
        context.shadowBlur = 16;
        context.shadowColor = "#34d399";
        context.fill();
        context.shadowBlur = 0;
        return true;
      });
      if (!still && packets.length < 14 && Math.random() < 0.06 * dt) spawn();
    };

    const loop = (now: number) => {
      const dt = last ? Math.min(3, (now - last) / 16.7) : 1;
      last = now;
      draw(dt);
      if (visible && !document.hidden) frame = requestAnimationFrame(loop);
      else frame = 0;
    };
    const wake = () => {
      if (!still && visible && !document.hidden && !frame) {
        last = 0;
        frame = requestAnimationFrame(loop);
      }
    };

    resize();
    if (still) draw(1);
    else frame = requestAnimationFrame(loop);

    const onResize = () => { resize(); if (still) draw(1); };
    const onMove = (e: PointerEvent) => {
      const box = canvas.getBoundingClientRect();
      pointer.x = e.clientX - box.left;
      pointer.y = e.clientY - box.top;
    };
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; wake(); });
    observer.observe(canvas);
    window.addEventListener("resize", onResize);
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("visibilitychange", wake);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("visibilitychange", wake);
    };
  }, []);

  return <canvas ref={ref} aria-hidden className={className} />;
}
