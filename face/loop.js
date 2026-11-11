// Loop indicator (owner, 9 Nov): a compact chip showing what my autonomy loop is working on.
// Data: face/dashboard-loop.json, written by `walterctl loop ...` (walter/loop.py) on each
// heartbeat. Shows a small orbiting-loop icon + "Loop: <focus, ~40 chars>", and an amber dot
// when something is blocked on the owner.
//  - portrait info board: a chip in the .sys row, just above the me row
//  - landscape face: a small chip at the bottom left
// Dim in quiet hours (10 PM-8 AM), hidden in the dark hours (11 PM-5 AM), under the canvas/browser,
// on the landscape sleep dashboard, and when there's no focus set and nothing blocked.
// The orbit stops spinning when the last heartbeat is over 45 min old (loop idle).
(function () {
  const css = document.createElement("style");
  css.textContent = `
  @keyframes lporbit { to { transform: rotate(360deg); } }
  @keyframes lpdot { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .lpchip { display: none; align-items: center; gap: 0.5em; white-space: nowrap; pointer-events: none;
    color: rgba(255,255,255,0.62); transition: opacity 1.5s; }
  .lpchip.on { display: inline-flex; }
  .lpchip .ic { position: relative; width: 1.15em; height: 1.15em; flex: none; }
  .lpchip .ic::before { content: ""; position: absolute; inset: 0; border-radius: 50%;
    border: 0.11em solid rgba(150,205,255,0.28); }
  .lpchip .ic i { position: absolute; inset: 0; animation: lporbit 3.2s linear infinite; }
  .lpchip .ic i::after { content: ""; position: absolute; top: -0.09em; left: 50%; width: 0.3em; height: 0.3em;
    margin-left: -0.15em; border-radius: 50%; background: rgba(150,205,255,0.9); }
  .lpchip.idle .ic i { animation: none; }
  .lpchip.idle .ic i::after { background: rgba(255,255,255,0.35); }
  .lpchip .lb { color: rgba(255,255,255,0.38); }
  .lpchip .tx { overflow: hidden; text-overflow: ellipsis; max-width: 22em; }
  .lpchip .bk { display: none; width: 0.55em; height: 0.55em; border-radius: 50%; background: #ffb347; flex: none;
    box-shadow: 0 0 0.4em rgba(255,179,71,0.7); animation: lpdot 2.4s ease-in-out infinite; }
  .lpchip.blk .bk { display: inline-block; }
  .lpchip.q { opacity: 0.5; filter: saturate(0.3); }
  .lpchip.q .ic i, .lpchip.q .bk { animation: none; }
  /* landscape face: bottom left */
  #loopchip { position: fixed; left: 3vh; bottom: 3vh; z-index: 3; font-size: 1.6vh;
    padding: 0.5vh 1.1vh; border-radius: 1.4vh; background: rgba(9,13,22,0.45); }
  body.dash-on #loopchip, body.canvas-on #loopchip, body.browser-on #loopchip,
  body.lp-dark #loopchip { display: none; }
  #dash .lpchip { display: none; }
  @media (orientation: portrait) {
    html.big #loopchip { display: none; }
    html.big body.board-on #dash .sys:has(.lpchip.on) { flex-wrap: wrap; }
    html.big body.board-on #dash .sys .lpchip.on { display: inline-flex; font-size: 2.4vw; width: 100%;
      justify-content: center; margin-bottom: 0.9vw; }
    html.big body.board-on.board-dark #dash .lpchip,
    body.lp-dark #dash .lpchip, body.canvas-on #dash .lpchip, body.browser-on #dash .lpchip { display: none !important; }
  }`;
  document.head.appendChild(css);

  const IDLE_AFTER = 45 * 60;   // seconds since the last heartbeat before the orbit stops
  const esc = (x) => String(x == null ? "" : x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const inHours = (h, r) => r[0] > r[1] ? (h >= r[0] || h < r[1]) : (h >= r[0] && h < r[1]);
  const trim = (s, n) => (s = String(s || "").trim()).length > n ? s.slice(0, n - 1).trimEnd() + "…" : s;

  const land = document.createElement("div");
  land.id = "loopchip"; land.className = "lpchip";
  document.body.appendChild(land);

  let last = null;

  function html(d) {
    const focus = d.focus || (d.blocked ? "waiting on you" : "");
    const title = d.blocked ? "Blocked on you: " + (d.blocked_first || d.blocked) : "";
    return '<span class="ic"><i></i></span><span class="lb">Loop:</span><span class="tx">' + esc(trim(focus, 40)) +
      '</span><span class="bk" title="' + esc(title) + '"></span>';
  }

  function paint(el, d, quiet) {
    const show = !!(d && (d.focus || d.blocked));
    el.classList.toggle("on", show);
    if (!show) { el.innerHTML = ""; return; }
    const h = html(d);
    if (el.dataset.h !== h) { el.innerHTML = h; el.dataset.h = h; }
    const now = Date.now() / 1000;
    el.classList.toggle("idle", !d.last_run || now - d.last_run > IDLE_AFTER);
    el.classList.toggle("blk", d.blocked > 0);
    el.classList.toggle("q", quiet);
  }

  function render(d) {
    const b = document.body.classList, hr = new Date().getHours();
    const dark = inHours(hr, [23, 5]), quiet = !dark && inHours(hr, [22, 8]);
    b.toggle("lp-dark", dark);
    paint(land, d, quiet);
    // portrait board chip, in the .sys row above the me row (dashboard.js may rebuild .sys)
    const sys = document.querySelector("#dash .sys");
    if (!sys) return;
    let chip = sys.querySelector(".lpchip");
    if (!chip) {
      chip = document.createElement("div"); chip.className = "lpchip";
      const mei = sys.querySelector(".mei");
      sys.insertBefore(chip, mei || null);
    }
    paint(chip, d, quiet);
  }

  async function poll() {
    try {
      const r = await fetch("dashboard-loop.json", { cache: "no-store" });
      if (r.ok) last = await r.json();
    } catch (e) { /* offline: keep the last state */ }
    try { render(last); } catch (e) {}
  }
  poll(); setInterval(poll, 5000);
})();
