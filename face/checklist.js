/* Walter's to-do list (lower right): polls checklist.json, written by bin/walter-checklist
 * from ~/.walter/home/backlog.md. Small and quiet; hidden while the canvas is open. */
(() => {
  "use strict";
  const css = document.createElement("style");
  css.textContent = `
  #checklist { position: fixed; left: 3vh; top: 21vh; width: 26vw; max-width: 380px; pointer-events: none;
    color: rgba(255,255,255,0.45); font-size: 1.45vh; line-height: 1.45; transition: opacity 0.6s, top 0.6s; opacity: 0; text-align: left; }
  #checklist.on { opacity: 1; }
  #checklist .hdr { font-size: 1.3vh; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(255,255,255,0.28); margin-bottom: 0.5vh; }
  #checklist .it { display: flex; gap: 0.8vh; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #checklist .it span:last-child { overflow: hidden; text-overflow: ellipsis; }
  #checklist .bx { flex: none; width: 1.3vh; text-align: center; }
  #checklist .done { color: rgba(138,255,193,0.45); text-decoration: line-through; }
  #checklist .doing { color: rgba(255,255,255,0.75); }
  #checklist .doing .bx { color: #f0b45a; }
  #checklist .ask { color: rgba(255,210,122,0.5); }
  body.canvas-on #checklist { visibility: hidden; }`;
  document.head.appendChild(css);
  const el = document.createElement("div");
  el.id = "checklist";
  document.body.appendChild(el);
  const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const BOX = { done: "✓", doing: "●", ask: "?", todo: "○" };
  const ORDER = { doing: 0, todo: 1, ask: 2, done: 3 };
  let last = "";
  async function poll() {
    try {
      const r = await fetch("checklist.json", { cache: "no-store" });
      const txt = await r.text();
      if (txt === last) return;
      last = txt;

      data = JSON.parse(txt);
    } catch (e) { /* not there yet */ }
    render();
  }
  let data = { items: [] };
  function render() {
    {
      const now = Date.now() / 1000;
      const visible = (data.items || []).some((i) => i.status === "doing")
        || now - (data.changed_at || 0) < 60 || now < (data.show_until || 0);
      if (!visible) { el.className = ""; return; }
      const items = (data.items || []).filter((i) => i.status !== "done" || now - (i.done_at || 0) < 900).slice().sort((a, b) => ORDER[a.status] - ORDER[b.status]).slice(0, 7);
      if (!items.length) { el.className = ""; return; }
      el.innerHTML = '<div class="hdr">My to-do list</div>' + items.map((i) =>
        `<div class="it ${esc(i.status)}"><span class="bx">${BOX[i.status] || "○"}</span><span>${esc(i.text)}</span></div>`).join("");
      el.className = "on";
    }
  }
  poll(); setInterval(poll, 3000); setInterval(render, 5000);
  // sit just below the Claude usage meter (left column, under the clock)
  function place() {
    const u = document.getElementById("usage");
    let top = window.innerHeight * 0.14;
    if (u && parseFloat(getComputedStyle(u).opacity) > 0.05 && u.offsetHeight) {
      top = u.getBoundingClientRect().bottom + window.innerHeight * 0.025;
    }
    el.style.top = Math.round(top) + "px";
  }
  place(); setInterval(place, 500);
})();
