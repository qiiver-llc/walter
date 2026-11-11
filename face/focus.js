/* Focus mode on my face: the Now/Next strip under me and the time-box ring.
 *
 * Polls three small files:
 *   focus.json            the running time box (walter/focus.py, walterctl focus ...)
 *   dashboard-list.json   the person's list: the top open to-do when no box is running
 *   dashboard-events.json today's calendar: the next event, in 12-hour time
 *
 *   Now: Network app - 12 min left
 *   Next: 3:30 PM Dentist
 *
 * Hidden when there's nothing to show, while I sleep (the dashboard has its own list),
 * during first-time setup, or with `walterctl focus strip off` ("box": only during a box).
 * The big ring (owner, 5 Nov 11:33): during a box a large ring sits in the middle of the screen
 * (where I normally am) with the box's title and the time left inside, filling clockwise;
 * amber in the last five minutes, a slow pulse at time's up. I sit small in the corner meanwhile.
 * It dims while we talk or a card is up. While the canvas or browser panel is open, ALL focus
 * widgets hide (the big ring and the Now/Next strip; owner, 5 Nov) and come back when it closes.
 */
(() => {
  "use strict";
  const css = document.createElement("style");
  css.textContent = `
  #focusstrip { position: fixed; left: 3vh; top: 16vh; width: 19vw; z-index: 1; pointer-events: none;
    text-align: left; white-space: normal; opacity: 0; transition: opacity 0.8s;
    padding-left: 1.4vh; border-left: 0.3vh solid rgba(159,216,255,0.35);
    text-shadow: 0 0.3vh 1.2vh rgba(0,0,0,0.65); }
  #focusstrip.on { opacity: 1; }
  #focusstrip .now { font-size: 2.8vh; font-weight: 600; color: rgba(255,255,255,0.82); overflow-wrap: anywhere; }
  #focusstrip .next { margin-top: 1vh; font-size: 2.2vh; font-weight: 400; color: rgba(255,255,255,0.48); overflow-wrap: anywhere; }
  #focusstrip .lb { color: rgba(255,255,255,0.42); font-weight: 400; margin-right: 0.6vh; }
  #focusstrip .left { color: #9fd8ff; font-weight: 500; }
  #focusstrip .left.warn { color: #ffd27a; }
  #focusstrip .left.up { color: #ffb45a; animation: fspulse 1.6s ease-in-out infinite; }
  @keyframes fspulse { 50% { opacity: 0.45; } }
  #focusring { position: fixed; left: 50%; top: 47vh; width: 66vh; height: 66vh; pointer-events: none;
    transform: translate(-50%, -50%) scale(0.92); opacity: 0; transition: opacity 1.4s ease, transform 1.4s ease; }
  #focusring.on { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  #focusring.on.dim { opacity: 0.32; transform: translate(-50%, -50%) scale(0.96); }
  #focusring .disc { position: absolute; inset: 0; border-radius: 50%;
    background: radial-gradient(circle, rgba(4,6,12,0.62) 0%, rgba(4,6,12,0.5) 62%, rgba(4,6,12,0) 72%); }
  #focusring svg { position: absolute; inset: 0; width: 100%; height: 100%; transform: rotate(-90deg); overflow: visible; }
  #focusring .track { fill: none; stroke: rgba(159,216,255,0.13); stroke-width: 1.3; }
  #focusring .arc { fill: none; stroke: rgba(159,216,255,0.78); stroke-width: 2.1; stroke-linecap: round;
    transition: stroke-dashoffset 1s linear, stroke 2s; filter: drop-shadow(0 0 0.8vh rgba(159,216,255,0.35)); }
  #focusring.warn .arc { stroke: rgba(255,210,122,0.85); filter: drop-shadow(0 0 0.8vh rgba(255,210,122,0.35)); }
  #focusring.up .arc { stroke: rgba(255,180,90,0.9); filter: drop-shadow(0 0 1vh rgba(255,180,90,0.4)); }
  #focusring.up svg, #focusring.up .left { animation: frpulse 3.2s ease-in-out infinite; }
  @keyframes frpulse { 50% { opacity: 0.4; } }
  #focusring .in { position: absolute; left: 50%; top: 50%; width: 46vh; height: 40vh; transform: translate(-50%, -50%);
    display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;
    text-shadow: 0 0.3vh 1.2vh rgba(0,0,0,0.7); }
  #focusring .lbl { font-size: 1.9vh; letter-spacing: 0.22em; text-transform: uppercase; color: rgba(255,255,255,0.4); margin-bottom: 1.4vh; }
  #focusring .title { max-height: 21vh; overflow: hidden; color: rgba(255,255,255,0.88); font-weight: 600;
    line-height: 1.18; overflow-wrap: anywhere; }
  #focusring .left { margin-top: 2.6vh; font-size: 6.4vh; font-weight: 300; color: #9fd8ff; font-variant-numeric: tabular-nums; }
  #focusring.warn .left { color: #ffd27a; }
  #focusring.up .left { color: #ffb45a; }
  #focusring .until { margin-top: 0.8vh; font-size: 2vh; color: rgba(255,255,255,0.38); }`;
  document.head.appendChild(css);
  const el = document.createElement("div");
  el.id = "focusstrip";
  el.innerHTML = '<div class="now"></div><div class="next"></div>';
  document.body.appendChild(el);
  const nowEl = el.querySelector(".now"), nextEl = el.querySelector(".next");
  const CIRC = 2 * Math.PI * 47;
  const ring = document.createElement("div");
  ring.id = "focusring";
  ring.innerHTML = '<div class="disc"></div><svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="47"/>' +
    '<circle class="arc" cx="50" cy="50" r="47" stroke-dasharray="' + CIRC.toFixed(2) + '" stroke-dashoffset="' + CIRC.toFixed(2) + '"/></svg>' +
    '<div class="in"><div class="lbl">Focus</div><div class="title"></div><div class="left"></div><div class="until"></div></div>';
  // just above my canvas, below the card, captions and panels (they come later in the page)
  const cv = document.getElementById("c");
  if (cv && cv.nextSibling) document.body.insertBefore(ring, cv.nextSibling); else document.body.appendChild(ring);
  const rTitle = ring.querySelector(".title"), rLeft = ring.querySelector(".left"), rUntil = ring.querySelector(".until");
  const rArc = ring.querySelector(".arc");
  let fitFor = "";
  function fitTitle(text) {
    // shrink the title until it fits inside the ring (wraps first, then smaller type)
    // (portrait screens size in vw instead of vh: portrait.js)
    const portrait = !!(window.WALTER_PORTRAIT && window.WALTER_PORTRAIT());
    const unit = portrait ? "vw" : "vh", u = (portrait ? innerWidth : innerHeight) / 100;
    if (fitFor === text + "|" + innerHeight + "|" + innerWidth) return;
    fitFor = text + "|" + innerHeight + "|" + innerWidth;
    rTitle.textContent = text;
    const maxH = parseFloat(getComputedStyle(rTitle).maxHeight) || 21 * u;
    for (let fs = 5.6; fs >= 2.4; fs -= 0.2) {
      rTitle.style.fontSize = "calc(" + fs + unit + " * var(--ts, 1))"; // global text scale (textscale.js)
      if (rTitle.scrollHeight <= maxH + 1 && rTitle.scrollWidth <= rTitle.clientWidth + 1) break;
    }
  }
  function renderRing(box, t, S) {
    const card = document.querySelector("#card.on");
    const show = box && !S.dash && !S.setup && S.state !== "sleeping" && !S.canvas && !S.browser &&
      !document.body.classList.contains("canvas-on") && !document.body.classList.contains("browser-on");
    if (!show) { ring.classList.remove("on", "dim"); fitFor = ""; return; }
    const left = box.end - t, total = Math.max(1, box.end - box.start);
    const up = box.status === "checkin" || left <= 0;
    const frac = up ? 1 : Math.min(1, Math.max(0, (t - box.start) / total));
    fitTitle(String(box.task || "Focus"));
    rLeft.textContent = up ? "Time's up" : leftText(left);
    rUntil.textContent = up ? "" : "until " + hm(new Date(box.end * 1000));
    rArc.setAttribute("stroke-dashoffset", (CIRC * (1 - frac)).toFixed(2));
    ring.classList.add("on");
    ring.classList.toggle("warn", !up && left <= 300);
    ring.classList.toggle("up", up);
    ring.classList.toggle("dim", !!(S.focusTalk || card));
  }
  const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const hm = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  const pad = (n) => String(n).padStart(2, "0");
  const dayKey = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, "").trim();

  let focus = null, list = null, events = null;
  async function get(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  async function poll() {
    try { focus = await get("focus.json"); } catch (e) { focus = null; }
    try { list = await get("dashboard-list.json"); } catch (e) {}
    try { events = await get("dashboard-events.json"); } catch (e) {}
    render();
  }

  function openTasks() {
    const today = dayKey(new Date());
    const todo = ((list && list.items) || []).filter((i) => i.section === "tasks" && !i.done_at);
    const due = todo.filter((i) => i.due && i.due.slice(0, 10) <= today).sort((a, b) => (a.due < b.due ? -1 : 1));
    return due.concat(todo.filter((i) => due.indexOf(i) < 0));
  }
  function nextEvent(now) {
    const evs = ((events && events.events) || []).filter((e) => !e.all_day && String(e.start || "").length > 10)
      .map((e) => ({ ...e, s: new Date(e.start) }))
      .filter((e) => !isNaN(e.s) && e.s > now && dayKey(e.s) === dayKey(now))
      .sort((a, b) => a.s - b.s);
    return evs[0] || null;
  }
  function leftText(sec) {
    const m = Math.max(1, Math.ceil(sec / 60));
    if (m < 60) return m + " min left";
    const h = Math.floor(m / 60), r = m % 60;
    return h + " h" + (r ? " " + r + " min" : "") + " left";
  }

  function render() {
    const S = window.WALTER_STATE || {};
    const now = new Date(), t = now / 1000;
    const box = focus && focus.box;
    const mode = (focus && focus.strip) || "auto";
    let nowHTML = "", nowTask = "", nextHTML = "";
    window.WALTER_FOCUS = { on: false };
    if (box) {
      const left = box.end - t, total = Math.max(1, box.end - box.start);
      const up = box.status === "checkin" || left <= 0;
      nowTask = box.task;
      nowHTML = '<span class="lb">Now:</span>' + esc(box.task) + ' <span class="lb">-</span><span class="left' +
        (up ? " up" : left <= 300 ? " warn" : "") + '">' + (up ? "time's up" : leftText(left)) + "</span>";
      window.WALTER_FOCUS = { on: true, frac: Math.min(1, Math.max(0, (t - box.start) / total)), left, status: up ? "checkin" : "running" };
    } else if (mode === "auto") {
      const top = openTasks()[0];
      if (top) { nowTask = top.text; nowHTML = '<span class="lb">Now:</span>' + esc(top.text); }
    }
    if (box || mode === "auto") {
      const ev = nextEvent(now);
      if (ev) nextHTML = '<span class="lb">Next:</span>' + esc(hm(ev.s) + " " + ev.title);
      else {
        const rest = openTasks().filter((i) => norm(i.text) !== norm(nowTask));
        if (rest.length) nextHTML = '<span class="lb">Next:</span>' + esc(rest[0].text);
      }
    }
    renderRing(box, t, S);
    const panel = S.canvas || S.browser || document.body.classList.contains("canvas-on") ||
      document.body.classList.contains("browser-on"); // canvas/browser open: no focus widgets at all
    const hidden = mode === "off" || S.dash || S.setup || S.state === "sleeping" || panel || (!nowHTML && !nextHTML);
    if (hidden) { el.className = ""; return; }
    if (nowEl.innerHTML !== nowHTML) nowEl.innerHTML = nowHTML;
    if (nextEl.innerHTML !== nextHTML) nextEl.innerHTML = nextHTML;
    nowEl.style.display = nowHTML ? "" : "none";
    nextEl.style.display = nextHTML ? "" : "none";
    el.className = "on";
  }

  // follow my body: just under me (I step left for the canvas, shrink for setup)
  function place() {
    const S = window.WALTER_STATE || {}, p = S.pos;
    if (!p) return;
    // fixed on the right edge now (CSS): no longer follows my body
  }
  poll(); setInterval(poll, 3000); setInterval(render, 500); setInterval(place, 250);
})();
