// The field study's 20 apps (docs/FIELD-STUDY.md), shared by the two slides that show them.
const APPS = ["hermes-agent", "markitdown", "AutoGPT", "dify", "open-webui", "langflow", "paperclip", "claude-mem",
  "OpenHands", "NextChat", "lobehub", "learn-claude-code", "ruflo", "headroom", "OpenMontage", "gpt_academic",
  "oh-my-openagent", "aider", "CowAgent", "LibreChat"];
// The one app with no locked version holding a known vulnerability.
const CLEAN = "markitdown";
function appGrid(el) {
  el.innerHTML = APPS.map((a) => `<div class="app" data-app="${a}"><span class="dot"></span><span class="mono">${a}</span></div>`).join("");
}
const APP_CSS = `
  .apps { display: flex; flex-wrap: wrap; gap: 16px; align-content: flex-start; }
  .app { display: flex; align-items: center; gap: 14px; padding: 16px 22px; border-radius: 16px; font-size: 24px;
    background: rgba(15,23,42,.72); border: 1px solid rgba(148,163,184,.16); color: #cbd5e1; white-space: nowrap; }
  .app .dot { width: 12px; height: 12px; border-radius: 50%; background: #475569; flex: none; }
`;
document.head.insertAdjacentHTML("beforeend", `<style>${APP_CSS}</style>`);
