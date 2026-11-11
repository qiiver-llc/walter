// Live worker status (owner, 9 Nov): what each background worker is doing right now.
// Data: face/dashboard-workers.json, written every ~3 s by walter-workers-live.service
// (walter/workerlive.py reads each worker's live transcript). Falls back to /api/tasks
// (titles only) if the helper isn't running.
//
// Shown in two places:
//  - the #workers panel (top right, landscape and portrait face): title, elapsed time and
//    one short line of what it's doing; queued jobs as "waiting"; finished ones fade after ~1 min
//  - the portrait info board: a compact "Workers" card above the me row (.sys)
// Dim in quiet hours, hidden in dark hours and under the canvas/browser (their CSS hides
// #workers; the board card is hidden by the same body classes below). Hidden entirely when
// there are no workers and no recent finishes.
(function () {
  const css = document.createElement("style");
  css.textContent = `
  #workers .job { white-space: normal; margin-top: 0.5vh; }
  #workers .job .tt { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: rgba(255,255,255,0.62); }
  #workers .job .act { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-size: 1.45vh; color: rgba(150,205,255,0.55); }
  #workers .job.done, #workers .job.failed { transition: opacity 2s; }
  #workers .job.done .tt { color: rgba(138,255,193,0.6); }
  #workers .job.failed .tt { color: rgba(255,170,150,0.6); }
  #workers .job.queued .tt, #workers .job.queued .act { color: rgba(255,210,122,0.5); }
  #workers .job.fade { opacity: 0.25; }
  body.wl-night #workers { filter: saturate(0.4); opacity: 0.55 !important; }
  body.wl-dark #workers { opacity: 0 !important; visibility: hidden; }
  #dash .wlive { display: none; }
  @media (orientation: portrait) {
    html.big body.board-on #dash .sys:has(.wlive.on) { flex-direction: column; gap: 1.2vw; }
    html.big body.board-on #dash .wlive.on { display: block; width: 100%; box-sizing: border-box;
      padding: 1.4vw 2.2vw; border-radius: 1.6vw; background: rgba(255,255,255,0.045);
      font-size: 2.5vw; line-height: 1.3; color: rgba(255,255,255,0.7); }
    #dash .wlive .h { font-size: 1.9vw; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(255,255,255,0.35); }
    #dash .wlive .r { display: flex; gap: 1.2vw; align-items: baseline; margin-top: 0.7vw; transition: opacity 2s; }
    #dash .wlive .r .ti { flex: 0 1 auto; max-width: 52%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #dash .wlive .r .ac { flex: 1 1 0; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      color: rgba(150,205,255,0.6); font-size: 2.2vw; }
    #dash .wlive .r .el { color: rgba(255,255,255,0.35); font-variant-numeric: tabular-nums; font-size: 2.1vw; }
    #dash .wlive .r.queued { color: rgba(255,210,122,0.6); }
    #dash .wlive .r.queued .ac { color: rgba(255,210,122,0.45); }
    #dash .wlive .r.done .ti { color: rgba(138,255,193,0.7); }
    #dash .wlive .r.failed .ti { color: rgba(255,170,150,0.7); }
    #dash .wlive .r.fade { opacity: 0.3; }
    html.big body.board-on.board-night #dash .wlive.on { opacity: 0.6; }
    html.big body.board-on.board-dark #dash .wlive.on,
    body.canvas-on #dash .wlive.on, body.browser-on #dash .wlive.on { display: none; }
  }`;
  document.head.appendChild(css);

  const el = document.getElementById("workers");
  if (!el) return;
  const FADE_AFTER = 45, GONE_AFTER = 70;   // seconds after finishing
  const esc = (x) => String(x == null ? "" : x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const dur = (s) => {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    return m < 60 ? m + " min" : Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0");
  };
  const inHours = (h, r) => r[0] > r[1] ? (h >= r[0] || h < r[1]) : (h >= r[0] && h < r[1]);

  async function load() {
    try {
      const r = await fetch("dashboard-workers.json", { cache: "no-store" });
      if (r.ok) {
        const d = await r.json();
        if (d && Date.now() / 1000 - (d.updated || 0) < 60) return d;   // helper alive
      }
    } catch (e) {}
    // fallback: titles only from Walter's API
    const items = await (await fetch("/api/tasks", { cache: "no-store" })).json();
    const now = Date.now() / 1000;
    const ws = items.filter((t) => t.status === "running" || (t.finished && now - t.finished < 90)).map((t) => ({
      id: t.id, title: t.title, status: t.status, started: t.started, finished: t.finished,
      elapsed: (t.finished || now) - t.started, activity: t.status === "running" ? "" : "Finished", files: 0 }));
    return { updated: now, max_tasks: 3, running: ws.filter((w) => w.status === "running").length, workers: ws };
  }

  function rows(d, now) {
    return (d.workers || []).map((w) => {
      const since = w.finished ? now - w.finished : 0;
      const el = w.status === "running" ? dur(now - (w.started || now)) : w.status === "queued" ? "waiting" : dur(w.elapsed || 0);
      return Object.assign({}, w, { since, el });
    }).filter((w) => w.status === "running" || w.status === "queued" || w.since < GONE_AFTER);
  }

  function slots(d, n) {
    let h = '<span class="slots">';
    for (let i = 0; i < Math.max(d.max_tasks || 0, n); i++) h += '<span class="slot' + (i < n ? " on" : "") + '"></span>';
    return h + "</span>";
  }

  function render(d) {
    const now = Date.now() / 1000;
    const rs = rows(d, now);
    const b = document.body.classList, hr = new Date().getHours();
    b.toggle("wl-dark", inHours(hr, [23, 5]));
    b.toggle("wl-night", !inHours(hr, [23, 5]) && inHours(hr, [22, 8]));
    const nRun = rs.filter((w) => w.status === "running").length;
    // top-right panel
    if (!rs.length) { el.style.opacity = 0; }
    else {
      let h = '<div class="hdr">Workers' + slots(d, nRun) + "</div>";
      for (const w of rs) {
        const cls = w.status === "running" ? "" : w.status === "queued" ? "queued" : w.status === "done" ? "done" : "failed";
        const mark = w.status === "done" ? "✓ " : w.status === "queued" ? "⏳ " : w.status === "running" ? "" : "✗ ";
        let act = w.activity || "";
        if (w.status === "running" && w.files) act += (act ? " · " : "") + w.files + (w.files === 1 ? " file" : " files");
        h += '<div class="job ' + cls + (w.since > FADE_AFTER ? " fade" : "") + '"><span class="tt">' + mark + esc(w.title) +
          '<span class="t">' + esc(w.el) + "</span></span>" + (act ? '<span class="act">' + esc(act) + "</span>" : "") + "</div>";
      }
      el.innerHTML = h; el.style.opacity = 1;
    }
    // portrait board card, above the me row
    const sys = document.querySelector("#dash .sys");
    if (!sys) return;
    let card = sys.querySelector(".wlive");
    if (!card) { card = document.createElement("div"); card.className = "wlive"; sys.insertBefore(card, sys.firstChild); }
    if (!rs.length) { card.classList.remove("on"); card.innerHTML = ""; return; }
    let h = '<div class="h">Workers</div>';
    for (const w of rs.slice(0, 4)) {
      const cls = w.status === "running" ? "" : w.status === "queued" ? "queued" : w.status === "done" ? "done" : "failed";
      const mark = w.status === "done" ? "✓ " : w.status === "queued" ? "⏳ " : w.status === "running" ? "" : "✗ ";
      h += '<div class="r ' + cls + (w.since > FADE_AFTER ? " fade" : "") + '"><span class="ti">' + mark + esc(w.title) +
        '</span><span class="ac">' + esc(w.activity || "") + '</span><span class="el">' + esc(w.el) + "</span></div>";
    }
    card.innerHTML = h; card.classList.add("on");
  }

  async function poll() {
    try { render(await load()); } catch (e) { /* offline: leave as is */ }
  }
  poll(); setInterval(poll, 3000);
})();
