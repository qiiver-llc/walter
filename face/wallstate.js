/* Wall state reporter (the owner, 11 Nov 10:08: "sometimes the actual board and the PWA switch are not aligned").
 *
 * The face is the source of truth for what the wall is really showing: voice-picked themes held on the
 * face, the conversation layout and its hold timeout, the chat view, build mode ending on mute, the focus
 * ring, sleep, presentation demo data... none of that is (only) in display.json. So every ~2 s the face
 * works out a compact snapshot from its own live DOM / JS state and POSTs it to the local server when it
 * changed (plus a 10 s heartbeat): POST /api/wall/state (walter/wall_state.py; this machine / home network
 * only, never through the tunnel). The companion app reads it back via GET /api/app/wall.
 *
 * Privacy: no titles, no text. Only whitelisted words, booleans and the workspace id (none in demo mode).
 * Only the main wall face reports (window.WALTER_FACE.id "wall"); paired faces don't.
 *
 * Pure parts (classify, snapshot, same) are exported for node tests (tests/wallstate.test.js).
 */
(function (root) {
  "use strict";

  // Which mode the wall is showing, from plain flags (computed from the DOM by read() below).
  // Order = what wins on screen: setup > presentation > sleep > browser > canvas > focus > build >
  // chat > conversation > board > face.
  function classify(f) {
    f = f || {};
    if (f.setup) return "setup";
    if (f.demo) return "presentation";
    if (f.sleeping || f.dash) return "sleep";
    if (f.browser) return "browser";
    if (f.canvas) return "canvas";
    if (f.focus) return "focus";
    if (f.convoBuild || (f.buildSticky && f.convo)) return "build";
    if (f.chat) return "chat";
    if (f.convo) return "conversation";
    if (f.board) return "board";
    return "face";
  }

  const ZOOMS = ["diff", "tests", "board", "stream", "graph", "plan", "projects", "pipeline"];
  const word = (v, allowed, dflt) => (allowed.indexOf(v) >= 0 ? v : dflt);
  const ID = /^[a-z0-9][a-z0-9_-]{0,39}$/;

  // flags -> the snapshot the server accepts (walter/wall_state.py validates the same shape)
  function snapshot(f, nowMs) {
    f = f || {};
    const theme = typeof f.theme === "string" && /^[a-z]{1,20}$/.test(f.theme) ? f.theme : "";
    const ws = !f.demo && typeof f.workspace === "string" && ID.test(f.workspace) ? f.workspace : null;
    return {
      t: Math.round((nowMs || Date.now()) / 1000),
      mode: classify(f),
      build: { on: !!(f.convoBuild || f.buildSticky), sticky: !!f.buildSticky, showing: !!f.convoBuild,
               phase: word(f.buildPhase, ["plan", "build"], "build"), zoom: word(f.buildZoom, ZOOMS, ""), plan: !!f.buildPlan },
      theme, theme_auto: !!f.themeAuto,
      layout: f.board ? "large" : "compact",
      layout_pref: word(f.layoutPref, ["auto", "compact", "large"], "auto"),
      orientation: f.landscape ? "landscape" : "portrait",
      conversation: { on: !!f.convo, view: f.chat ? "chat" : f.convoBuild ? "build" : "default" },
      focus: { on: !!f.focus, status: word(f.focusStatus, ["running", "checkin"], "") },
      demo: !!f.demo,
      workspace: ws,
      asleep: !!(f.sleeping || f.dash),
      muted: !!f.muted,
      canvas: !!f.canvas, browser: !!f.browser,
    };
  }

  // same picture apart from the timestamp?
  function same(a, b) {
    if (!a || !b) return false;
    const x = Object.assign({}, a, { t: 0 }), y = Object.assign({}, b, { t: 0 });
    return JSON.stringify(x) === JSON.stringify(y);
  }

  const api = { classify, snapshot, same, ZOOMS };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.WalterWallState = api;

  // =================================================================================== the face
  const F = root.WALTER_FACE;
  if (F && F.id && F.id !== "wall") return;   // paired faces don't speak for the wall

  let workspace = null, wsAt = 0;
  async function pollWorkspace() {
    wsAt = Date.now();
    try {
      const r = await fetch("dashboard-workspace.json", { cache: "no-store" });
      const d = r.ok ? await r.json() : null;
      const was = workspace;
      workspace = d && typeof d.id === "string" && (!d.face || d.face === "wall") ? d.id : workspace;
      if (workspace !== was && last) setTimeout(tick, 0);   // report a project switch at once
    } catch (e) { /* keep the last one */ }
    wsAt = Date.now();
  }

  function call(fn) { try { return typeof fn === "function" ? fn() : undefined; } catch (e) { return undefined; } }

  function read() {
    const S = root.WALTER_STATE || {}, b = document.body ? document.body.classList : { contains: () => false };
    const FB = root.WALTER_FOCUS || {}, BV = call(root.WALTER_BUILD) || {}, L = call(root.WALTER_LAYOUT) || {};
    const setting = call(root.WALTER_THEME_SETTING);
    return {
      setup: !!S.setup,
      demo: b.contains("walter-demo"),
      sleeping: S.state === "sleeping", dash: !!S.dash || b.contains("dash-on"),
      browser: !!S.browser || b.contains("browser-on"),
      canvas: !!S.canvas || b.contains("canvas-on"),
      focus: !!FB.on, focusStatus: FB.status,
      convo: !!root.WALTER_CONVO || b.contains("convo"),
      convoBuild: !!root.WALTER_CONVO_BUILD || b.contains("convo-build"),
      buildSticky: !!root.WALTER_BUILD_STICKY,
      buildPhase: BV.phase, buildZoom: BV.zoom, buildPlan: !!BV.plan,
      chat: !!root.WALTER_CONVO_CHAT || b.contains("chat-on"),
      board: b.contains("board-on"),
      layoutPref: L.pref,
      theme: (document.documentElement && document.documentElement.dataset.theme) || "",
      themeAuto: setting === "auto",
      landscape: root.innerWidth > root.innerHeight,
      muted: !!S.muted,
      workspace,
    };
  }

  const BEAT = 10000;
  let last = null, sentAt = 0, sending = false;
  async function tick() {
    if (Date.now() - wsAt > 3000) pollWorkspace();   // the app waits for a project switch to show here
    const snap = snapshot(read(), Date.now());
    if (sending || (same(snap, last) && Date.now() - sentAt < BEAT)) return;
    sending = true;
    try {
      const r = await fetch("/api/wall/state", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snap), cache: "no-store" });
      if (r.ok || r.status === 404) { last = snap; sentAt = Date.now(); }   // 404 = server before the restart
    } catch (e) { /* server restarting: try again next tick */ } finally { sending = false; }
  }
  pollWorkspace();
  setTimeout(tick, 1500);
  setInterval(tick, 2000);
  // changes that matter show up within a moment, not a full tick
  for (const ev of ["resize", "walter-ws-switched"]) root.addEventListener(ev, () => setTimeout(tick, 300));
})(typeof window !== "undefined" ? window : this);
