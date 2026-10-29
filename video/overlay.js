// Injected into every page the video captures: a visible cursor, spotlights and eased scrolling, all driven
// from the capture script. Positions follow the page each frame, so cards that grow as events arrive stay framed.
(() => {
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const tween = (ms, step) =>
    new Promise((done) => {
      const start = performance.now();
      const frame = (now) => {
        const t = Math.min(1, (now - start) / ms);
        step(ease(t));
        if (t < 1) requestAnimationFrame(frame);
        else done();
      };
      requestAnimationFrame(frame);
    });

  // The first, innermost element whose text starts with `text` (and that sits inside `closest`, when given),
  // inside the element `within` names, if any.
  const starts = (e, text) => e.textContent.trim().startsWith(text);
  const find = (text, closest, within) => {
    const scope = within ? find(within, "article, section, li, div.panel") : document;
    if (!scope) return null;
    for (const e of scope.querySelectorAll("body *")) {
      if (!starts(e, text) || [...e.children].some((c) => starts(c, text)) || e.closest("#__cursor, script, style")) continue;
      const hit = closest ? e.closest(closest) : e;
      if (hit) return hit;
    }
    return null;
  };

  const style = document.createElement("style");
  style.textContent = `
    html { scroll-behavior: auto !important; }
    ::-webkit-scrollbar { display: none; }
    #__cursor { position: fixed; z-index: 2147483647; width: 30px; height: 30px; pointer-events: none;
      left: 0; top: 0; transform: translate(-3px, -2px); filter: drop-shadow(0 3px 6px rgba(0,0,0,.55));
      transition: opacity 400ms ease; }
    .__ripple { position: fixed; z-index: 2147483646; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
      border: 2px solid #34d399; pointer-events: none; animation: __ripple 600ms ease-out forwards; }
    @keyframes __ripple { from { transform: scale(.4); opacity: 1 } to { transform: scale(3.2); opacity: 0 } }
    .__spot { position: fixed; z-index: 2147483640; pointer-events: none; border-radius: 14px;
      border: 2px solid rgba(52,211,153,.95); transition: opacity 350ms ease, transform 350ms ease;
      box-shadow: 0 0 0 4px rgba(52,211,153,.18), 0 0 36px rgba(52,211,153,.35), 0 0 0 200vmax rgba(3,7,18,.62); }
  `;
  const install = () => {
    if (document.getElementById("__cursor")) return;
    document.head.appendChild(style);
    const cursor = document.createElement("div");
    cursor.id = "__cursor";
    cursor.innerHTML = `<svg viewBox="0 0 24 24" width="30" height="30"><path d="M4 2.5 L4 19.5 L8.6 15.2 L11.6 21.8 L14.4 20.6 L11.4 14.1 L17.8 14.1 Z"
      fill="#fff" stroke="#0b1220" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
    document.body.appendChild(cursor);
    pos = pos || { x: innerWidth * 0.62, y: innerHeight * 0.7 };
    place();
  };
  let pos = null;
  const place = () => {
    const c = document.getElementById("__cursor");
    if (c) c.style.left = `${pos.x}px`, c.style.top = `${pos.y}px`;
  };

  window.__video = {
    find,
    async moveTo(text, closest, ms = 700) {
      install();
      const el = find(text, closest);
      if (!el) throw new Error(`no element for ${text}`);
      const r = el.getBoundingClientRect();
      document.getElementById("__cursor").style.opacity = "1";
      const from = { ...pos }, to = { x: r.left + Math.min(r.width * 0.35, 120), y: r.top + r.height * 0.55 };
      await tween(ms, (k) => { pos = { x: from.x + (to.x - from.x) * k, y: from.y + (to.y - from.y) * k }; place(); });
    },
    click(text, closest) {
      const ripple = document.createElement("div");
      ripple.className = "__ripple";
      ripple.style.left = `${pos.x}px`;
      ripple.style.top = `${pos.y}px`;
      document.body.appendChild(ripple);
      setTimeout(() => ripple.remove(), 700);
      setTimeout(() => (document.getElementById("__cursor").style.opacity = "0"), 700);  // out of the way once it's clicked
      const el = find(text, closest);
      (el.closest("button, a, [role=tab]") || el).click();
    },
    async scrollToText(text, closest, ms = 1000, offset = 90) {
      const el = find(text, closest);
      if (!el) throw new Error(`no element for ${text}`);
      return this.scrollTo(el.getBoundingClientRect().top + scrollY - offset, ms);
    },
    async scrollBy(dy, ms = 1000) {
      return this.scrollTo(scrollY + dy, ms);
    },
    async scrollTo(y, ms = 1000) {
      const from = scrollY, to = Math.max(0, Math.min(y, document.documentElement.scrollHeight - innerHeight));
      await tween(ms, (k) => scrollTo(0, from + (to - from) * k));
    },
    // Keep an element `offset` px from the top for `ms`, easing after it as the page grows around it.
    pin(text, closest, offset = 80, ms = 3000, within = null, lag = 0.1) {
      const until = performance.now() + ms;
      return new Promise((done) => {
        const frame = () => {
          const el = find(text, closest, within);
          if (el) {
            const max = document.documentElement.scrollHeight - innerHeight;
            const target = Math.max(0, Math.min(el.getBoundingClientRect().top + scrollY - offset, max));
            scrollTo(0, scrollY + (target - scrollY) * lag);
          }
          if (performance.now() < until) requestAnimationFrame(frame);
          else done();
        };
        requestAnimationFrame(frame);
      });
    },
    async spot(text, closest, ms = 2000, pad = 8, within = null) {
      let el = find(text, closest, within);
      for (let tries = 0; !el && tries < 10; tries++) {  // it may be a render away
        await new Promise((r) => setTimeout(r, 100));
        el = find(text, closest, within);
      }
      if (!el) throw new Error(`no element for ${text}`);
      const ring = document.createElement("div");
      ring.className = "__spot";
      ring.style.opacity = "0";
      ring.style.transform = "scale(1.04)";
      document.body.appendChild(ring);
      let alive = true;
      const follow = () => {
        if (!alive) return;
        if (!el.isConnected) el = find(text, closest, within) || el;  // re-rendered as a new element: a step that finished
        const r = el.getBoundingClientRect();
        Object.assign(ring.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px` });
        requestAnimationFrame(follow);
      };
      follow();
      requestAnimationFrame(() => { ring.style.opacity = "1"; ring.style.transform = "scale(1)"; });
      setTimeout(() => { ring.style.opacity = "0"; setTimeout(() => { alive = false; ring.remove(); }, 400); }, ms);
    },
    install,
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
  else install();
})();
