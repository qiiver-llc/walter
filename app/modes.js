// Wall <-> app state sync (the owner approved 11 Nov 10:08: "sometimes the actual board and the PWA switch are
// not aligned"). The wall FACE reports what it is really showing (face/wallstate.js -> walter/wall_state.py);
// GET /api/app/wall returns that snapshot + its age, {stale: true} after 15 s without a report. The Mode,
// Rotate and Projects sheets below light their buttons from it, falling back to the server's own derivation
// only when the snapshot is stale or the route isn't there yet (404 = Walter before the restart). After a tap
// the button shows "pending" until the wall's snapshot confirms the change, else after 6 s a toast says why.
// Pure parts (usable, modeOf, matches, check, reason, orientBase) are exported for node (tests/wallsync.test.js).
(function (root) {
  "use strict";
  const PENDING_MS = 6000;
  const LABEL = { board: "Board", face: "Face", build: "Build", conversation: "Conversation", chat: "Chat", focus: "Focus",
    sleep: "Sleep", presentation: "Presentation", canvas: "Canvas panel", browser: "Browser panel", setup: "Setup" };

  function usable(w) { return !!(w && w.reported && !w.stale && typeof w.mode === "string"); }
  function modeOf(w, fallback) { return usable(w) ? w.mode : fallback; }
  function orientBase(o) { return String(o || "").replace(/-flipped$/, ""); }

  // does the wall's snapshot show what was asked for?  kind: mode | build | orientation | workspace
  function matches(kind, want, w) {
    if (!usable(w)) return false;
    if (kind === "mode") {
      if (want === "sleep") return w.mode === "sleep" || !!w.asleep;
      return w.mode === want;
    }
    if (kind === "build") {
      const b = w.build || {};
      if (w.mode !== "build") return false;
      if (want.phase && b.phase !== want.phase) return false;
      if (want.zoom) return want.zoom === "back" ? !b.zoom : b.zoom === want.zoom;
      return true;
    }
    if (kind === "orientation") return w.orientation === orientBase(want);
    if (kind === "workspace") return w.workspace === want;
    return false;
  }

  // why the wall didn't do it (short, for a toast)
  function reason(kind, want, w) {
    if (!w || !w.reported) return "the wall isn't reporting yet (is the face page open?)";
    if (w.stale) return "the wall is not responding";
    const now = LABEL[w.mode] || w.mode;
    if (kind === "mode") {
      if (want === "board" && w.orientation === "landscape") return "the info board needs a portrait screen";
      if (w.mode === "setup") return "it is in first-time setup";
      if (w.mode === "presentation") return "a presentation is running";
      if ((w.mode === "canvas" || w.mode === "browser") && want !== "sleep") return "a " + now.toLowerCase() + " is open on top";
      if (w.mode === "focus" && want !== "focus") return "a focus box is running";
      return "it is still showing " + now;
    }
    if (kind === "orientation") return "the screen still looks " + (w.orientation || "unchanged");
    if (kind === "workspace") return "it is still on another project";
    return "it is still showing " + now;
  }

  // a pending tap: {kind, want, at}. -> "confirmed" | "pending" | "timeout"
  function check(p, w, nowMs) {
    if (!p) return "confirmed";
    if (matches(p.kind, p.want, w)) return "confirmed";
    return nowMs - p.at >= PENDING_MS ? "timeout" : "pending";
  }

  const api = { PENDING_MS, LABEL, usable, modeOf, matches, reason, check, orientBase };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.WalterWallSync = api;

  // ---- the shared client: one GET at a time, the last answer for every sheet
  const A = root.walterApp;
  if (!A) return;
  let last = null, missing = false, inflight = null;
  async function get() {
    if (inflight) return inflight;
    inflight = (async () => {
      const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
      const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 6000);
      try {
        const r = await fetch("/api/app/wall", { headers, cache: "no-store", signal: ctl.signal });
        if (r.status === 404) { missing = true; last = null; }
        else if (r.ok) { missing = false; last = await r.json(); }
      } catch (e) { /* offline: keep the last one, it goes stale by its age */ }
      finally { clearTimeout(tm); inflight = null; }
      return last;
    })();
    return inflight;
  }
  root.walterWall = {
    get, last: () => last, missing: () => missing,
    // confirmations only make sense once the route exists (after the restart that added it)
    known: () => !missing && !!last,
    // ticks every second while something is pending; resolves "confirmed" or "timeout"
    async await(kind, want) {
      const p = { kind, want, at: Date.now() };
      for (;;) {
        const w = await get();
        const s = check(p, w, Date.now());
        if (s !== "pending") return { state: s, wall: w, why: s === "timeout" ? reason(kind, want, w) : "" };
        await new Promise((res) => setTimeout(res, 1000));
      }
    },
    ago(w) { return w && w.age != null ? Math.round(w.age) + " s ago" : ""; },
  };
  // a little "waiting for the wall" look for pending buttons (kept here, app.css is shared)
  const css = document.createElement("style");
  css.textContent = ".ms-btn.pending, .pj-row.pending .pj-pick { outline: 2px dashed currentColor; outline-offset: -4px; opacity: 0.75; }" +
    " #msWall { margin: 0.2em 0 0; font-size: 0.8em; opacity: 0.7; }";
  document.head.appendChild(css);
})(typeof window !== "undefined" ? window : this);

// Walter companion app: the floating wall-mode switcher (the owner approved 9 Nov 20:03).
// A small round button on every tab opens a bottom sheet of big mode buttons:
// Board, Face, Build, Conversation, Chat, Focus, Sleep. The wall's current mode is lit and kept
// live (GET /api/app/mode every 3 s while the sheet is open; once a minute for the button's label
// while the app is in front; nothing while hidden). Tap = POST /api/app/mode (walter/app_mode.py),
// the same code paths as the voice / walterctl commands. Build adds Plan/Build phase + cockpit zoom;
// Focus asks for a task and minutes. Chat only shows when the wall has the chat view (capabilities).
// A running focus box is never ended silently: switching away asks Park / Done / Cancel first.
// A Walter from before the restart that added the route answers 404: "Needs one restart to enable".
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("modeFab")) return;
  const FAST = 3000, SLOW = 60000;
  const LABEL = { board: "Board", face: "Face", build: "Build", conversation: "Conversation", chat: "Chat", focus: "Focus", sleep: "Sleep" };
  const HINT = { board: "Info board", face: "Just me, big", build: "Dev cockpit", conversation: "Me + captions",
    chat: "Chat bubbles", focus: "Time box", sleep: "Dashboard, mic off" };
  const ZOOM = { diff: "Diff", tests: "Tests", board: "Tasks", stream: "Activity", graph: "Git", plan: "Plan", projects: "Projects", back: "Back" };
  const MINS = [15, 25, 45, 60];

  let st = null, needs = false, open = false, timer = 0, busy = false, pending = null, focusMins = 25;
  // the wall's own report (face/wallstate.js via GET /api/app/wall) wins over the server's guess
  const WS = window.WalterWallSync, WW = window.walterWall;
  let wall = null, wpend = null;

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  async function raw(json) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch("/api/app/mode", { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store",
        signal: ctl.signal, body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  function haptic() { try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* no vibration */ } }

  async function refresh() {
    try {
      const [r] = await Promise.all([raw(), WW ? WW.get() : null]);
      wall = WW ? WW.last() : null;
      if (r.status === 404) { needs = true; st = null; }
      else if (r.status === 200 && r.data && r.data.mode) { needs = false; st = r.data; }
    } catch (e) { /* offline: keep the last state */ }
    render();
  }
  function schedule() {
    clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(async () => { await refresh(); schedule(); }, open ? FAST : SLOW);
  }

  // ---- the floating button sits in the top-right row (.fabrow, with the Rotate button); nothing to place
  function place() { const fab = $("modeFab"); if (fab && fab.style.bottom) fab.style.bottom = ""; }

  function render() {
    const fab = $("modeFab");
    const cur = WS ? WS.modeOf(wall, st ? st.mode : "") : st ? st.mode : "";
    const wallOk = !!(WS && WS.usable(wall));
    $("modeFabLb").textContent = cur ? (LABEL[cur] || (WS && WS.LABEL[cur]) || cur) : "Mode";
    fab.classList.toggle("lit", cur === "build" || cur === "focus");
    if (!open) return;
    $("msNeeds").classList.toggle("hidden", !needs);
    $("msBody").classList.toggle("hidden", needs || !st);
    if (!st) return;
    const caps = st.capabilities || [];
    $("msNow").textContent = wpend ? "Waiting for the wall to show " + (LABEL[wpend.label] || wpend.label) + "..." :
      "On the wall now: " + (LABEL[cur] || (WS && WS.LABEL[cur]) || cur) +
      (st.focus && cur === "focus" ? ` (${st.focus.task}, ${Math.round(st.focus.left_min)} min left)` : "");
    let wl = $("msWall");
    if (!wl) { wl = document.createElement("p"); wl.id = "msWall"; wl.className = "muted small"; $("msNow").after(wl); }
    wl.textContent = !WW || WW.missing() ? "" : wallOk ? "wall: " + (WS.LABEL[wall.mode] || wall.mode).toLowerCase() + " \u00b7 updated " + WW.ago(wall)
      : wall && wall.reported ? "wall not responding (last report " + WW.ago(wall) + ")" : wall ? "wall: no report yet" : "";
    const grid = $("msGrid");
    if (grid.dataset.caps !== caps.join(",")) {
      grid.dataset.caps = caps.join(",");
      grid.innerHTML = caps.map((m) => `<button class="ms-btn" data-mode="${esc(m)}" aria-pressed="false">` +
        `<span class="ms-l">${esc(LABEL[m] || m)}</span><span class="ms-h">${esc(HINT[m] || "")}</span></button>`).join("");
    }
    grid.querySelectorAll(".ms-btn").forEach((b) => {
      const on = b.dataset.mode === cur;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("pending", !!wpend && wpend.label === b.dataset.mode && !on);
      b.disabled = busy;
    });
    const b = (wallOk && cur === "build" && wall.build) || st.build || {};
    $("msBuild").classList.toggle("hidden", cur !== "build");
    document.querySelectorAll("#msPhase button").forEach((x) => x.classList.toggle("on", x.dataset.phase === (b.phase || "build")));
    const zooms = $("msZoom");
    if (!zooms.childElementCount) {
      zooms.innerHTML = (st.zooms || Object.keys(ZOOM)).map((z) =>
        `<button class="btn ms-z" data-zoom="${esc(z)}">${esc(ZOOM[z] || z)}</button>`).join("");
    }
    zooms.querySelectorAll("button").forEach((x) => x.classList.toggle("on", !!b.zoom && x.dataset.zoom === b.zoom));
  }

  function msg(text, bad) { const m = $("msMsg"); m.textContent = text || ""; m.classList.toggle("bad", !!bad); }

  async function send(body) {
    if (busy) return;
    busy = true; render(); msg("");
    try {
      const r = await raw(body);
      if (r.status === 404) { needs = true; st = null; }
      else if (r.status === 409 && r.data.code === "focus_running") {
        pending = body; askFocus(r.data);
      } else if (r.status !== 200) {
        msg(r.data.error || "Couldn't switch. Try again.", true);
        if (r.data.mode) st = r.data;
      } else {
        st = r.data; pending = null; $("msAsk").classList.add("hidden");
        if (body.mode === "focus") $("msFocus").classList.add("hidden");
        confirm(body);
      }
    } catch (e) {
      msg("Walter didn't answer. Check the wall, then try again.", true);
    } finally { busy = false; render(); }
  }

  // after a tap: pending until the wall's own report shows it (6 s), else a toast with the reason
  function confirm(body) {
    if (!WW || !WW.known()) return;   // no reports (Walter before the restart): the old behaviour
    const sub = body.mode === "build" && (body.phase || body.zoom);
    const p = { kind: sub ? "build" : "mode", want: sub ? { phase: body.phase, zoom: body.zoom } : body.mode, label: body.mode };
    wpend = p; render();
    WW.await(p.kind, p.want).then((res) => {
      if (wpend !== p) return;   // a newer tap took over
      wpend = null; wall = res.wall || wall;
      if (res.state === "timeout") A.toast("Mode", "The wall did not change: " + res.why + ".", "err", 6);
      render();
    });
  }

  function askFocus(d) {
    const f = d.focus || {};
    $("msAskLine").textContent = `A focus box is running: ${f.task || "a task"}. End it and switch?`;
    $("msAsk").classList.remove("hidden");
  }

  function pickMode(m) {
    haptic();
    if (m === "focus") {
      $("msFocus").classList.remove("hidden");
      $("msTask").focus();
      return;
    }
    $("msFocus").classList.add("hidden");
    send({ mode: m });
  }

  function startFocus() {
    const task = $("msTask").value.trim();
    const n = parseInt($("msMins").value, 10);
    if (!task) { msg("What's the box for?", true); $("msTask").focus(); return; }
    if (!(n >= 1 && n <= 240)) { msg("Minutes: 1 to 240.", true); return; }
    send({ mode: "focus", task: task.slice(0, 80), minutes: n });
  }

  function setMins(n) {
    focusMins = n; $("msMins").value = String(n);
    document.querySelectorAll("#msMinBtns button").forEach((x) => x.classList.toggle("on", +x.dataset.min === n));
  }

  function openSheet() {
    haptic();
    open = true; pending = null;
    $("modeSheet").classList.remove("hidden");
    $("msAsk").classList.add("hidden"); $("msFocus").classList.add("hidden"); msg("");
    render(); refresh().then(schedule);
  }
  function closeSheet() {
    open = false; $("modeSheet").classList.add("hidden"); schedule();
  }

  // ---- wiring
  $("modeFab").addEventListener("click", openSheet);
  $("msClose").addEventListener("click", closeSheet);
  $("modeSheet").addEventListener("click", (e) => { if (e.target.id === "modeSheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (open && e.key === "Escape") closeSheet(); });
  $("msGrid").addEventListener("click", (e) => { const b = e.target.closest(".ms-btn"); if (b && !busy) pickMode(b.dataset.mode); });
  $("msPhase").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { haptic(); send({ mode: "build", phase: b.dataset.phase }); } });
  $("msZoom").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { haptic(); send({ mode: "build", zoom: b.dataset.zoom }); } });
  $("msMinBtns").innerHTML = MINS.map((n) => `<button type="button" class="btn ms-m" data-min="${n}">${n}</button>`).join("");
  $("msMinBtns").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMins(+b.dataset.min); });
  $("msMins").addEventListener("input", () => setMins(parseInt($("msMins").value, 10) || 0));
  $("msFocusForm").addEventListener("submit", (e) => { e.preventDefault(); startFocus(); });
  $("msAskBtns").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.end && pending) send({ ...pending, end_focus: b.dataset.end });
    else { pending = null; $("msAsk").classList.add("hidden"); }
  });
  setMins(focusMins);

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); schedule(); });
  place();
  refresh().then(schedule);
})();

// The screen Rotate button (the owner approved 10 Nov 12:57): a round button beside the Mode button, and
// "Rotate screen" inside the Mode sheet. Opens a tiny sheet: Portrait / Landscape / Flip, with the current
// orientation lit. GET/POST /api/app/rotate (walter/app_rotate.py -> screen_rotate.py: wlr-randr live, saved in
// kiosk.env so it survives a screen restart). A Walter from before the restart that added it answers 404.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("rotFab") || !$("rotSheet")) return;
  const NAME = { portrait: "Portrait", landscape: "Landscape", "portrait-flipped": "Portrait, upside down",
                 "landscape-flipped": "Landscape, upside down" };
  // which of the four buttons matches what the screen is doing (transform of the first output)
  function srvBtn() {
    if (!st) return "";
    const t = st.outputs && Object.values(st.outputs)[0];
    if (!t) return st.orientation || "";
    if (t === "normal") return "landscape";
    if (t === "180") return "landscape-flipped";
    return t === (st.portrait_transform || "270") ? "portrait" : "portrait-flipped";
  }
  // the wall face's own report (window shape) wins when it disagrees; it can't tell flipped from upright
  const WS = window.WalterWallSync, WW = window.walterWall;
  function curBtn() {
    const c = srvBtn(), w = WW && WW.last();
    if (!WS || !WS.usable(w)) return c;
    return WS.orientBase(c) === w.orientation ? c : w.orientation;
  }
  let st = null, needs = false, busy = false, rotOpen = false, rotTimer = 0;

  async function raw(json) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 15000);
    try {
      const r = await fetch("/api/app/rotate", { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store",
        signal: ctl.signal, body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  function haptic() { try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* no vibration */ } }
  function msg(text, bad) { const m = $("rsMsg"); m.textContent = text || ""; m.classList.toggle("bad", !!bad); }

  function render() {
    const cur = curBtn();
    $("rsNeeds").classList.toggle("hidden", !needs);
    $("rsGrid").classList.toggle("hidden", needs);
    $("rsNow").textContent = needs ? "" : cur ? "The screen is " + (NAME[cur] || cur).toLowerCase() + " now." +
      (st.live === false && st.error ? " (" + st.error + ")" : "") : "Checking the screen...";
    $("rsGrid").querySelectorAll(".ms-btn").forEach((b) => {
      const on = b.dataset.o === cur;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false"); b.disabled = busy;
    });
    $("rotFab").classList.toggle("busy", busy);
    const now = $("msRotNow"); if (now) now.textContent = cur ? "(" + NAME[cur] + ")" : "";
  }

  async function refresh() {
    try {
      const [r] = await Promise.all([raw(), WW ? WW.get() : null]);
      if (r.status === 404) { needs = true; st = null; }
      else if (r.status === 200 && r.data && r.data.orientation) { needs = false; st = r.data; }
    } catch (e) { /* offline: keep the last state */ }
    render();
  }

  async function pick(o) {
    if (busy) return;
    haptic(); busy = true; msg("Turning the screen..."); render();
    try {
      const r = await raw({ orientation: o });
      if (r.status === 404) { needs = true; st = null; msg(""); }
      else if (r.status !== 200) { msg(r.data.error || "Couldn't turn the screen.", true); A.toast("Rotate", r.data.error || "Couldn't turn the screen.", "err", 6); }
      else {
        const d = r.data; st = { ...(st || {}), ...d };
        const nm = NAME[d.orientation] || d.orientation;
        if (d.ok && WW && WW.known()) {
          // wait for the wall's own report before saying it worked
          msg("Waiting for the wall...");
          const res = await WW.await("orientation", o);
          if (res.state === "confirmed") { msg(""); A.toast("Rotated", "The screen is " + nm.toLowerCase() + " now.", "", 4); closeSheet(); }
          else { msg("The wall did not change: " + res.why + ".", true); A.toast("Rotate", "The wall did not change: " + res.why + ".", "err", 6); }
        } else if (d.ok) { msg(""); A.toast("Rotated", "The screen is " + nm.toLowerCase() + " now.", "", 4); closeSheet(); }
        else {
          const saved = d.saved ? " Saved for the next screen start." : "";
          msg((d.error || "Couldn't turn the screen.") + saved, true);
          A.toast("Rotate", (d.error || "Couldn't turn the screen.") + saved, "err", 8);
          refresh();
        }
      }
    } catch (e) {
      msg("Walter didn't answer. Check the wall, then try again.", true);
    } finally { busy = false; render(); }
  }

  function openSheet() {
    haptic();
    const ms = $("modeSheet"); if (ms && !ms.classList.contains("hidden")) $("msClose").click();
    $("rotSheet").classList.remove("hidden"); msg(""); render(); refresh();
    rotOpen = true; clearInterval(rotTimer);
    rotTimer = setInterval(() => { if (rotOpen && !document.hidden && !busy) refresh(); }, 3000);
  }
  function closeSheet() { rotOpen = false; clearInterval(rotTimer); $("rotSheet").classList.add("hidden"); }

  $("rotFab").addEventListener("click", openSheet);
  if ($("msRotate")) $("msRotate").addEventListener("click", openSheet);
  $("rsClose").addEventListener("click", closeSheet);
  $("rotSheet").addEventListener("click", (e) => { if (e.target.id === "rotSheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("rotSheet").classList.contains("hidden")) closeSheet(); });
  $("rsGrid").addEventListener("click", (e) => { const b = e.target.closest(".ms-btn"); if (b && !busy) pick(b.dataset.o); });
  if ($("modeFab")) $("modeFab").addEventListener("click", refresh);
  // keep the header's right-hand pills (Stop, Wall browser) clear of the button row
  const row = document.querySelector(".fabrow");
  if (row && window.ResizeObserver) new ResizeObserver(() => {
    const w = Math.round(row.getBoundingClientRect().width);
    if (w) document.documentElement.style.setProperty("--fabw", w + "px");
  }).observe(row);   // keeps "Rotate screen (Portrait)" current
})();

// Projects (the owner approved 11 Nov 10:05): which project (workspace) the wall is working on.
// A "Projects" button beside Rotate in the Mode sheet opens this sheet; Settings has the same list.
// Tap a project = switch the wall to it; "+ Add project" (name + Walled off); "Edit" = name, goals, share,
// walled. Opening up a walled project asks first (the server also refuses without confirm). No delete.
// GET/POST /api/app/workspaces (walter/app_workspaces.py). Walled projects only ever show counts.
// The last list is cached on this device so it still shows (greyed note) when Walter can't be reached.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("pjSheet")) return;
  const CACHE = "walter.projects.v1";
  const LOCK = '<svg class="pj-lock" viewBox="0 0 24 24" aria-label="Walled off"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
  let st = null, needs = false, busy = false, stale = false, editing = null, open = false, pjTimer = 0, wpend = null;
  // the wall face's own report of its workspace (id only) wins over the server's "current"
  const WS = window.WalterWallSync, WW = window.walterWall;
  function curId() {
    const w = WW && WW.last();
    if (WS && WS.usable(w) && w.workspace && st && st.workspaces.some((x) => x.id === w.workspace)) return w.workspace;
    const c = st && st.workspaces.find((x) => x.current);
    return c ? c.id : null;
  }

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function haptic() { try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* no vibration */ } }
  function msg(text, bad) { const m = $("pjMsg"); m.textContent = text || ""; m.classList.toggle("bad", !!bad); }
  async function raw(json) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 10000);
    try {
      const r = await fetch("/api/app/workspaces", { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store",
        signal: ctl.signal, body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  function save(d) { try { localStorage.setItem(CACHE, JSON.stringify({ t: Date.now(), d })); } catch (e) { /* storage full / private */ } }
  function load() { try { const c = JSON.parse(localStorage.getItem(CACHE) || "null"); return c && c.d && Array.isArray(c.d.workspaces) ? c.d : null; } catch (e) { return null; } }

  function take(d) { if (d && Array.isArray(d.workspaces)) { st = d; stale = false; needs = false; save(d); } }
  async function refresh() {
    try {
      const [r] = await Promise.all([raw(), WW ? WW.get() : null]);
      if (r.status === 404) { needs = true; }
      else if (r.status === 200) take(r.data);
      else if (!st) { st = load(); stale = !!st; }
    } catch (e) { if (!st) st = load(); stale = !!st; }
    render();
  }

  function rows() {
    if (!st) return '<p class="muted small">' + (needs ? "Needs one restart to enable." : "Loading...") + "</p>";
    if (!st.workspaces.length) return '<p class="muted small">No projects yet.</p>';
    const cid = curId();
    return st.workspaces.map((x) => {
      const w = { ...x, current: x.id === cid }, pend = wpend === x.id && !w.current;
      const b = (w.running ? `<span class="badge running">${w.running} running</span>` : "") +
        (w.ready ? `<span class="badge">${w.ready} ready</span>` : "");
      const sub = w.walled ? "Walled off" : (w.focus || (w.goals && w.goals[0]) || "");
      return `<div class="pj-row${w.current ? " on" : ""}${pend ? " pending" : ""}" data-id="${esc(w.id)}">` +
        `<button class="pj-pick" data-id="${esc(w.id)}" aria-pressed="${w.current ? "true" : "false"}"${busy ? " disabled" : ""}>` +
        `<span class="pj-name">${w.walled ? LOCK : ""}${esc(w.name)}${w.current ? '<span class="pj-now">on the wall</span>' : ""}</span>` +
        `<span class="pj-sub">${b}${esc(sub)}</span></button>` +
        `<button class="chip pj-edit" data-edit="${esc(w.id)}" aria-label="Edit ${esc(w.name)}">Edit</button></div>`;
    }).join("");
  }

  function render() {
    const html = rows();
    const cid = curId(), cur = st && st.workspaces.find((w) => w.id === cid);
    const set = $("pjSetList"); if (set) set.innerHTML = html + (stale ? '<p class="muted small">Offline: last list seen.</p>' : "");
    const now = $("msPjNow"); if (now) now.textContent = cur ? "(" + cur.name + ")" : "";
    if (!open) return;
    $("pjNeeds").classList.toggle("hidden", !needs || !!st);
    $("pjList").innerHTML = html;
    $("pjNow").textContent = stale ? "Walter can't be reached: this is the last list seen." :
      cur ? "The wall is working on " + cur.name + "." : "";
  }

  function ok(d, fallback) {
    take(d); msg("");
    A.toast("Projects", d.done || fallback, "", 4);
  }
  async function send(body) {
    if (busy) return null;
    busy = true; render();
    try {
      const r = await raw(body);
      if (r.status === 404 && !r.data.code) { needs = true; msg("Needs one restart to enable.", true); return null; }
      return r;
    } catch (e) {
      msg("Walter didn't answer. Check the wall, then try again.", true);
      A.toast("Projects", "Walter didn't answer.", "err", 5);
      return null;
    } finally { busy = false; render(); }
  }
  function fail(r) {
    const e = (r && r.data && r.data.error) || "That didn't work. Try again.";
    msg(e, true); A.toast("Projects", e, "err", 6);
  }

  async function pick(id) {
    const w = st && st.workspaces.find((x) => x.id === id);
    if (!w || w.id === curId()) return;
    haptic(); msg("Switching to " + w.name + "...");
    const r = await send({ action: "switch", id });
    if (r && r.status === 200 && WW && WW.known()) {
      take(r.data); wpend = id; msg("Waiting for the wall..."); render();
      const res = await WW.await("workspace", id);
      if (wpend !== id) return;   // a newer tap took over
      wpend = null;
      if (res.state === "confirmed") ok(r.data, "The wall is on " + w.name + " now.");
      else { msg("The wall did not change: " + res.why + ".", true); A.toast("Projects", "The wall did not change: " + res.why + ".", "err", 6); render(); }
    } else if (r && r.status === 200) ok(r.data, "The wall is on " + w.name + " now.");
    else if (r) fail(r);
  }

  function show(which) {
    $("pjMain").classList.toggle("hidden", which !== "main");
    $("pjAdd").classList.toggle("hidden", which !== "add");
    $("pjEdit").classList.toggle("hidden", which !== "edit");
  }
  function openSheet(which) {
    haptic();
    const ms = $("modeSheet"); if (ms && !ms.classList.contains("hidden")) $("msClose").click();
    open = true; $("pjSheet").classList.remove("hidden"); msg(""); show(which || "main"); render(); refresh();
    clearInterval(pjTimer);
    pjTimer = setInterval(() => { if (open && !document.hidden && !busy && !editing) refresh(); }, 3000);
  }
  function closeSheet() { open = false; editing = null; clearInterval(pjTimer); $("pjSheet").classList.add("hidden"); }

  function startAdd() {
    openSheet("add"); $("pjAddName").value = ""; $("pjAddWalled").checked = false; $("pjAddName").focus();
  }
  async function submitAdd() {
    const name = $("pjAddName").value.replace(/\s+/g, " ").trim();
    if (!name) { msg("Give it a name.", true); $("pjAddName").focus(); return; }
    if (name.length > 40) { msg("Names: 40 characters at most.", true); return; }
    const r = await send({ action: "add", name, walled: $("pjAddWalled").checked });
    if (r && r.status === 200) { ok(r.data, "Added " + name + "."); show("main"); }
    else if (r) fail(r);
  }

  function shareText() { $("pjShareVal").textContent = $("pjEditShare").value + "%"; }
  function startEdit(id) {
    const w = st && st.workspaces.find((x) => x.id === id);
    if (!w) return;
    editing = w;
    openSheet("edit");
    $("pjEditTitle").textContent = "Edit " + w.name;
    $("pjEditName").value = w.name;
    $("pjGoalsBox").classList.toggle("hidden", w.walled);
    $("pjGoalsHidden").classList.toggle("hidden", !w.walled);
    $("pjEditGoals").value = (w.goals || []).join("\n");
    $("pjEditShare").value = String(Math.round((w.share || 0) * 100)); shareText();
    $("pjEditWalled").checked = !!w.walled;
    $("pjUnlock").classList.add("hidden");
  }
  async function submitEdit(confirmed) {
    const w = editing; if (!w) return;
    const ch = [];
    const name = $("pjEditName").value.replace(/\s+/g, " ").trim();
    if (!name) { msg("Give it a name.", true); return; }
    if (name !== w.name) ch.push({ key: "name", value: name });
    if (!w.walled) {
      const goals = $("pjEditGoals").value.split("\n").map((g) => g.trim()).filter(Boolean);
      if (goals.length > 8) { msg("Up to 8 goals.", true); return; }
      if (goals.some((g) => g.length > 120)) { msg("Goals: 120 characters each at most.", true); return; }
      if (goals.join("\n") !== (w.goals || []).join("\n")) ch.push({ key: "goals", value: goals });
    }
    const share = Math.max(0, Math.min(100, parseInt($("pjEditShare").value, 10) || 0)) / 100;
    if (Math.round((w.share || 0) * 100) !== Math.round(share * 100)) ch.push({ key: "budget_share", value: share });
    const walled = $("pjEditWalled").checked;
    if (walled !== !!w.walled) {
      if (!walled && !confirmed) {
        $("pjUnlockLine").textContent = `Open up ${w.name}? Its notes, files and workers will be visible to your other ` +
          "projects and to Walter's shared memory. Only do this if nothing private lives there.";
        $("pjUnlock").classList.remove("hidden"); return;
      }
      ch.push({ key: "privacy", value: walled ? "walled" : "normal", confirm: !walled });
    }
    if (!ch.length) { show("main"); editing = null; return; }
    let last = null;
    for (const c of ch) {
      const r = await send({ action: "set", id: w.id, ...c });
      if (!r) return;
      if (r.status !== 200) { fail(r); return; }
      last = r.data;
    }
    $("pjUnlock").classList.add("hidden");
    ok(last, "Saved."); editing = null; show("main");
  }

  // ---- wiring
  if ($("msProjects")) $("msProjects").addEventListener("click", () => openSheet("main"));
  $("pjClose").addEventListener("click", closeSheet);
  $("pjSheet").addEventListener("click", (e) => { if (e.target.id === "pjSheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (open && e.key === "Escape") closeSheet(); });
  function onList(e) {
    const ed = e.target.closest("[data-edit]"); if (ed) { startEdit(ed.dataset.edit); return; }
    const p = e.target.closest(".pj-pick"); if (p && !busy) pick(p.dataset.id);
  }
  $("pjList").addEventListener("click", onList);
  if ($("pjSetList")) $("pjSetList").addEventListener("click", onList);
  $("pjAddBtn").addEventListener("click", () => startAdd());
  if ($("pjSetAdd")) $("pjSetAdd").addEventListener("click", () => startAdd());
  $("pjAdd").addEventListener("submit", (e) => { e.preventDefault(); submitAdd(); });
  $("pjEdit").addEventListener("submit", (e) => { e.preventDefault(); submitEdit(false); });
  [$("pjAdd"), $("pjEdit")].forEach((f) => f.addEventListener("click", (e) => {
    if (e.target.closest("[data-cancel]")) { editing = null; msg(""); show("main"); }
  }));
  $("pjEditShare").addEventListener("input", shareText);
  $("pjUnlockYes").addEventListener("click", () => submitEdit(true));
  $("pjUnlockNo").addEventListener("click", () => { $("pjEditWalled").checked = true; $("pjUnlock").classList.add("hidden"); });
  if ($("modeFab")) $("modeFab").addEventListener("click", refresh);   // keeps "Projects (Acme)" current

  // the Settings list refreshes when Settings opens (it's a sub-view of the Walter tab)
  const setView = $("v-settings");
  if (setView && window.MutationObserver) new MutationObserver(() => { if (setView.classList.contains("active")) refresh(); })
    .observe(setView, { attributes: true, attributeFilter: ["class"] });
  st = load(); stale = !!st; render();
  refresh();
})();
