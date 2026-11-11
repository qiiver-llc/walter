// Walter companion app: the Remote screen (Walter tab > Remote) drives the wall browser (walter/app_remote.py).
// Live preview: frames are fetched (low fps) only while this tab is open and the app is
// visible. Tap mode: tap = click there, drag = scroll. Trackpad mode: drag moves an
// orange cursor, tap clicks at it, two fingers scroll.
// PRIVACY: the Type / Type password boxes are emptied the moment they're sent; their text
// is never stored, logged, added to chat history or shown anywhere.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("v-remote")) return;
  const FRAME_GAP = 330;          // ms between frames: about 3 fps at most

  let on = false, seq = -1, loopId = 0, imgUrl = "", st = { open: false, w: 1024, h: 810 };
  let mode = (() => { try { return localStorage.getItem("walter.rmMode") === '"pad"' ? "pad" : "touch"; } catch (e) { return "touch"; } })();
  let cur = { x: 512, y: 405 };   // trackpad cursor, page CSS px
  let q = [], busy = false, stateTimer = 0, lastTouch = 0;
  const screen = $("rmScreen"), img = $("rmImg"), cursorEl = $("rmCursor");

  function err(title, e) { A.toast(title, (e && e.message) || String(e || ""), "err"); }
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // ---------------------------------------------------------------- state
  function render() {
    $("rmTitle").textContent = st.open ? (st.title || "Wall browser") : "Wall browser (closed)";
    $("rmUrl").textContent = st.open ? (st.url || "") : (st.note || "Not open on the wall.");
    $("rmOpen").classList.toggle("hidden", !!st.open);
    $("rmClose").classList.toggle("hidden", !st.open);
    screen.classList.toggle("off", !st.open || !imgUrl);
    screen.classList.toggle("you", !!st.you);
    if (st.w && st.h) screen.style.aspectRatio = `${st.w} / ${st.h}`;
    $("rmField").textContent = st.open && st.field ? "Selected on the wall: " + st.field : "";
    $("rmEmpty").innerHTML = st.note ? esc(st.note) : "The browser isn't open on the wall.<br>Type an address above, or tap Open on wall.";
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  async function loadState(peek) {
    try {
      const d = await A.api("/api/app/remote/state" + (peek ? "?peek=1" : ""));
      const field = peek ? d.field : st.field;
      st = { ...d, field };
    } catch (e) {
      st = { open: false, w: st.w, h: st.h, note: /404/.test(e.message)
        ? "The remote needs Walter to restart once before it works." : "Can't reach Walter right now." };
    }
    if (!st.open && imgUrl) { URL.revokeObjectURL(imgUrl); imgUrl = ""; img.removeAttribute("src"); }
    render();
  }

  // ---------------------------------------------------------------- live preview
  async function frames(id) {
    while (on && id === loopId) {
      if (document.hidden || !st.open) { await sleep(800); continue; }
      const t0 = Date.now();
      try {
        const tok = A.token();
        const r = await fetch(`/api/app/remote/frame?after=${seq}`, { cache: "no-store",
          headers: tok ? { Authorization: "Bearer " + tok } : {} });
        if (id !== loopId) return;
        const s = parseInt(r.headers.get("X-Frame-Seq") || "", 10);
        if (r.status === 200) {
          const blob = await r.blob();
          const u = URL.createObjectURL(blob);
          img.onload = () => { if (imgUrl && imgUrl !== u) URL.revokeObjectURL(imgUrl); imgUrl = u; render(); };
          img.src = u;
          if (!isNaN(s)) seq = s;
        } else if (r.status === 204) {
          if (!isNaN(s)) { /* nothing new */ }
        } else {
          await sleep(2000);
        }
      } catch (e) { await sleep(2000); }
      const left = FRAME_GAP - (Date.now() - t0);
      if (left > 0) await sleep(left);
    }
  }
  function active(yes) {
    if (yes === on) { if (yes) loadState(true); return; }
    on = yes;
    clearInterval(stateTimer);
    if (yes) {
      loadState(true);
      stateTimer = setInterval(() => { if (!document.hidden) loadState(true); }, 3000);
      frames(++loopId);
      setMode(mode);
    } else {
      loopId++;
      flushRelease();
    }
  }
  document.addEventListener("visibilitychange", () => { if (on && !document.hidden) loadState(true); });

  // ---------------------------------------------------------------- input
  function send(ev) {   // merge moves and wheel ticks; one request in flight at a time
    const last = q[q.length - 1];
    if (ev.t === "move" && last && last.t === "move") q[q.length - 1] = ev;
    else if (ev.t === "wheel" && last && last.t === "wheel") { last.dx += ev.dx; last.dy += ev.dy; last.x = ev.x; last.y = ev.y; }
    else q.push(ev);
    flush();
  }
  async function flush() {
    if (busy || !q.length) return;
    busy = true;
    const batch = q; q = [];
    try {
      const r = await A.api("/api/app/remote/input", { json: { ev: batch } });
      if (r && r.ok === false && r.error) A.toast("Remote", r.error, "err", 4);
      if (r && r.ok) { st.you = true; screen.classList.add("you"); }
    } catch (e) { /* offline: drop it */ }
    busy = false;
    if (q.length) flush();
  }
  function flushRelease() { if (st.open) { q = [{ t: "release" }]; flush(); } }
  function toPage(clientX, clientY) {
    const r = screen.getBoundingClientRect();
    return { x: Math.round((clientX - r.left) / r.width * st.w), y: Math.round((clientY - r.top) / r.height * st.h) };
  }
  function ripple(x, y) {   // page px -> a ring on the preview where the click lands
    const d = document.createElement("div"); d.className = "rm-tap";
    d.style.left = (x / st.w * 100) + "%"; d.style.top = (y / st.h * 100) + "%";
    screen.appendChild(d); setTimeout(() => d.remove(), 520);
  }
  function click(x, y, b) {
    send({ t: "move", x, y }); send({ t: "down", x, y, b: b || 0, n: 1 }); send({ t: "up", x, y, b: b || 0, n: 1 });
    ripple(x, y);
  }
  function showCursor() {
    cursorEl.classList.toggle("hidden", mode !== "pad");
    cursorEl.style.left = (cur.x / st.w * 100) + "%"; cursorEl.style.top = (cur.y / st.h * 100) + "%";
  }
  function setMode(m) {
    mode = m;
    try { localStorage.setItem("walter.rmMode", JSON.stringify(m)); } catch (e) { /* ignore */ }
    document.querySelectorAll("[data-mode]").forEach((b) => { const yes = b.dataset.mode === m; b.classList.toggle("on", yes); b.setAttribute("aria-pressed", String(yes)); });
    $("rmHint").textContent = m === "pad" ? "Trackpad: drag to move the pointer, tap to click, two fingers to scroll."
      : "Tap to click, drag to scroll.";
    showCursor();
  }
  document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

  const pts = new Map();     // pointerId -> {x, y, sx, sy, t}
  let gesture = null;        // {moved, multi, t0}
  screen.addEventListener("pointerdown", (e) => {
    if (!st.open) return;
    e.preventDefault();
    try { screen.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY });
    if (pts.size === 1) gesture = { moved: false, multi: false, t0: Date.now(), button: e.button };
    else if (gesture) gesture.multi = true;
  });
  screen.addEventListener("pointermove", (e) => {
    const p = pts.get(e.pointerId);
    if (!p || !gesture) return;
    e.preventDefault();
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    if (!gesture.moved && Math.hypot(e.clientX - p.sx, e.clientY - p.sy) < 8) return;
    gesture.moved = true;
    p.x = e.clientX; p.y = e.clientY;
    const r = screen.getBoundingClientRect(), kx = st.w / r.width, ky = st.h / r.height;
    if (mode === "pad" && !gesture.multi) {               // move the pointer (a bit faster than the finger)
      cur.x = Math.max(0, Math.min(st.w - 1, cur.x + dx * kx * 1.6));
      cur.y = Math.max(0, Math.min(st.h - 1, cur.y + dy * ky * 1.6));
      showCursor();
      send({ t: "move", x: Math.round(cur.x), y: Math.round(cur.y) });
    } else {
      if (gesture.multi && e.pointerId !== pts.keys().next().value) return;   // one finger drives the scroll
      const at = mode === "pad" ? { x: Math.round(cur.x), y: Math.round(cur.y) } : toPage(e.clientX, e.clientY);
      send({ t: "wheel", x: at.x, y: at.y, dx: -dx * kx * 1.4, dy: -dy * ky * 1.4 });   // content follows the finger
    }
  });
  function up(e) {
    const p = pts.get(e.pointerId);
    if (!p) return;
    pts.delete(e.pointerId);
    if (pts.size || !gesture) return;
    const g = gesture; gesture = null;
    if (e.type !== "pointerup" || g.moved || g.multi) return;
    const long = Date.now() - g.t0 > 600;
    if (mode === "pad") click(Math.round(cur.x), Math.round(cur.y), long || g.button === 2 ? 2 : 0);
    else { const at = toPage(e.clientX, e.clientY); cur = at; click(at.x, at.y, long || g.button === 2 ? 2 : 0); }
  }
  screen.addEventListener("pointerup", up);
  screen.addEventListener("pointercancel", up);
  screen.addEventListener("contextmenu", (e) => e.preventDefault());
  screen.addEventListener("wheel", (e) => {   // a mouse or laptop trackpad on the app
    if (!st.open) return;
    e.preventDefault();
    const at = mode === "pad" ? cur : toPage(e.clientX, e.clientY), k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? st.h * 0.9 : 1;
    send({ t: "wheel", x: Math.round(at.x), y: Math.round(at.y), dx: e.deltaX * k, dy: e.deltaY * k });
  }, { passive: false });

  // ---------------------------------------------------------------- keyboard
  async function typeFrom(box, kind) {
    let text = box.value;
    if (!text) { box.focus(); return; }
    box.value = "";                         // never left lying around on the phone either
    const enter = $("rmEnter").checked;
    try {
      const r = await A.api("/api/app/remote/type", { json: { text, kind, enter } });
      text = null;
      if (r.ok) { A.toast("Typed", r.msg || "Done.", "", 3); if (r.field) { st.field = r.field; render(); } }
      else A.toast("Didn't type it", r.error || "", "err", 6);
    } catch (e) { text = null; err("Didn't type it", e); }
  }
  $("rmTypeForm").addEventListener("submit", (e) => { e.preventDefault(); typeFrom($("rmText"), "text"); });
  $("rmPassForm").addEventListener("submit", (e) => { e.preventDefault(); typeFrom($("rmPass"), "password"); });
  document.querySelectorAll("#v-remote [data-key]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const r = await A.api("/api/app/remote/key", { json: { key: b.dataset.key } });
      if (r && r.ok === false) A.toast("Remote", r.error || "", "err", 4);
      setTimeout(() => loadState(true), 400);
    } catch (e) { err("Remote", e); }
  }));

  // ---------------------------------------------------------------- navigation + panel
  async function nav(action, url) {
    const btns = document.querySelectorAll("#v-remote button");
    btns.forEach((b) => (b.disabled = true));
    if (action === "open" || action === "goto") { st.note = "Opening the page..."; render(); }
    try {
      const r = await A.api("/api/app/remote/nav", { json: { action, url: url || "" } });
      if (!r.ok) A.toast("Wall browser", r.error || "That didn't work.", "err", 6);
    } catch (e) { err("Wall browser", e); }
    st.note = "";
    btns.forEach((b) => (b.disabled = false));
    seq = -1;
    await loadState(true);
  }
  $("rmGo").addEventListener("submit", (e) => {
    e.preventDefault();
    const u = $("rmAddr").value.trim();
    if (!u) { $("rmAddr").focus(); return; }
    $("rmAddr").blur();
    nav(st.open ? "goto" : "open", u);
  });
  $("rmOpen").addEventListener("click", () => nav("open", $("rmAddr").value.trim()));
  $("rmClose").addEventListener("click", () => nav("close"));
  document.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", () => nav(b.dataset.nav)));

  window.walterRemote = { active, state: () => ({ on, seq, mode, st: { ...st } }) };
  setMode(mode);
  const v = document.querySelector(".view.active");
  if (v && v.id === "v-remote") active(true);
})();
