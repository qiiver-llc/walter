/* Build view in conversation mode (owner, 9 Nov 13:14: "more about development building").
 *
 * When conversation mode is on (converse.js puts body.convo), instead of an empty screen I sit large
 * at the top (portrait, ~40% height) or on the left (landscape) and a calm live BUILD VIEW of what
 * we're making fades in beside / below me:
 *   strip:   loop focus + blocked-on-you count (dashboard-loop.json), weekly usage (usage.json),
 *            latest release, last test run (dashboard-build.json, from ~/.walter/last-test.json)
 *   left:    workers: running cards (title, elapsed, live one-line activity, slow shimmer) and
 *            queued ones as "waiting" (dashboard-workers.json, walter/workerlive.py)
 *   right:   "Shipped today": the last 5-6 commits of ~/walter and ~/walter-site, plain and redacted
 *            (dashboard-build.json, walter/buildview.py via walter-build.timer every 30 s)
 * Data is only fetched while the view is showing. Opacity/transform only; prefers-reduced-motion =
 * no fades, no shimmer. Text sizes are absolute (vw/vh) so textscale.js scales them.
 *
 * GATED (the owner, 9 Nov 13:15): only while we're actually building (face/buildgate.js: workers running or
 * just finished AND build talk in the last 3 captions, or "build mode" / "let's build"; off with "exit
 * build mode" / "done building" or 3 quiet minutes). Otherwise conversation mode stays plain.
 * display.json build_mode auto|on|off (walterctl display build ...).
 * STICKY (the owner, 9 Nov 13:25): "build mode" / "let's build" / "show me the build" / "we're building" or
 * build_mode "on" holds conversation mode + this view up (no timeout) until "done building" / "exit
 * build mode" / "back to the board", Escape, or `walterctl display build off|auto`. A "BUILD MODE" chip
 * shows meanwhile. Asleep: the board with sleeping eyes, and back to the build view on wake.
 *
 * VOICE-IDE deep view (the owner, 9 Nov 13:16), from dashboard-build.json (walter/buildview.py, every 15 s):
 *   left:  workers (numbered, the focused one highlighted) + its recent steps + a mini git graph
 *   right: changed files of the busiest repo + a syntax-coloured live diff of the newest change,
 *          bottom-anchored so the latest change shows; or the plan (loop focus / next + shipped)
 *   voice: "focus worker 2", "show the plan", "show the diff" / "what changed", "scroll up|down";
 *          CLI: walterctl display build focus N|diff|plan|scroll down|up (build_seq / build_cmd).
 *
 * Setting: face/display.json {"conversation_view": "build|clear"} (default build;
 * walterctl display conversation view build|clear). "clear" = the old empty screen.
 * walter.js reads window.WALTER_CONVO_BUILD to move me up (portrait) or left (landscape).
 */
(() => {
  "use strict";
  let view = "build", showing = false, lastData = 0, lastWorkers = 0;
  const data = { workers: null, build: null, loop: null, usage: null, tests: null, testsWs: null, planMd: "", planName: "", workspaceFile: null };
  // build mode v2 cockpit settings from display.json (walterctl display build plan|zoom|sound|phase)
  const cfg = { sound: "on", plan: "", planWs: null };
  const gate = window.WalterBuildGate.create();
  // sticky build mode survives a face reload (the owner, 9 Nov 13:25)
  const STORE = "walter.buildSticky";
  let saved = "";
  try { saved = localStorage.getItem(STORE) || ""; gate.restore(JSON.parse(saved || "null")); } catch (e) {}
  function persist() {
    const v = JSON.stringify(gate.save());
    if (v !== saved) { saved = v; try { localStorage.setItem(STORE, v); } catch (e) {} }
  }
  let disp = null; // last display.json build_* values seen (CLI commands apply when they change)
  // Mute exits build mode at once (the owner, 9 Nov 13:33): on every switch to muted, and when the face
  // starts already muted (first mute message). Runs before converse.js reads WALTER_BUILD_STICKY.
  let micMuted = null;
  window.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    if (m.t !== "mute" && m.t !== "hwmute") return;
    const was = micMuted;
    if (m.t === "mute") micMuted = !!m.on;
    if (!m.on || was === true) return; // unmute never re-enters; repeats while muted change nothing
    if (m.t === "hwmute" && micMuted) return;
    gate.mute(nowS());
    window.WALTER_BUILD_STICKY = false;
    persist();
    tick();
  });
  window.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    if (m.t !== "caption" || m.partial || m.live || !m.text) return;
    if (m.who === "you" && m.muted) return;
    const c = gate.text(nowS(), m.text, m.who === "you");
    if (c && c.cmd === "go") persistPhase();
    if (c && c.cmd === "ws" && showing && cockpit) cockpit.step(c.dir); // "next project" / "previous project"
    if (c && showing) render();
  });
  // a pill tap or "next project" switched the workspace: the server rebuilds dashboard-build.json at once;
  // fetch it again shortly (and once more in case git was slow)
  window.addEventListener("walter-ws-switched", () => {
    setTimeout(() => { if (showing) refresh(); }, 1500);
    setTimeout(() => { if (showing) refresh(); }, 5000);
  });

  const css = document.createElement("style");
  css.dataset.portrait = "buildview"; // per-orientation sizes below: portrait.js leaves it alone
  css.textContent = `
  #buildview { position: fixed; z-index: 2; pointer-events: none; opacity: 0; visibility: hidden;
    transform: translateY(1.5vh); transition: opacity 0.6s ease, transform 0.6s ease, visibility 0s 0.6s;
    display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: auto 1fr; gap: 1.6vmin;
    color: rgba(255,255,255,0.82); font-family: inherit; will-change: opacity, transform; }
  #buildview.on { opacity: 1; visibility: visible; transform: none; transition: opacity 0.6s ease 0.15s, transform 0.6s ease 0.15s, visibility 0s; }
  #buildview .bvstrip { grid-column: 1 / 3; display: flex; flex-wrap: wrap; gap: 1vmin 2.4vmin; align-items: baseline;
    padding: 1.2vmin 1.8vmin; border-radius: 1.6vmin; background: rgba(12,16,26,0.55);
    border: 1px solid rgba(255,255,255,0.07); font-size: 2.1vmin; color: rgba(255,255,255,0.62); }
  #buildview .bvstrip b { color: rgba(255,255,255,0.9); font-weight: 500; }
  #buildview .bvstrip .warn b { color: rgba(255,200,120,0.95); }
  #buildview .bvstrip .bad b { color: rgba(255,140,130,0.95); }
  #buildview .bvcol { display: flex; flex-direction: column; gap: 1.2vmin; min-width: 0; overflow: hidden; }
  #buildview h3 { margin: 0.4vmin 0.4vmin 0.2vmin; font-size: 2.0vmin; font-weight: 500; letter-spacing: 0.14em;
    text-transform: uppercase; color: rgba(160,190,255,0.6); }
  #buildview .bvcard { position: relative; overflow: hidden; padding: 1.4vmin 1.8vmin; border-radius: 1.6vmin;
    background: rgba(12,16,26,0.6); border: 1px solid rgba(255,255,255,0.07); }
  #buildview .bvcard .t { font-size: 2.5vmin; color: rgba(255,255,255,0.92); line-height: 1.25;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  #buildview .bvcard .a { margin-top: 0.6vmin; font-size: 2.0vmin; color: rgba(255,255,255,0.58);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #buildview .bvcard .e { float: right; margin-left: 1vmin; font-size: 2.0vmin; color: rgba(160,255,210,0.75);
    font-variant-numeric: tabular-nums; }
  #buildview .bvcard.waiting { opacity: 0.6; }
  #buildview .bvcard.waiting .e { color: rgba(255,214,140,0.8); }
  #buildview .bvcard.done .e { color: rgba(255,255,255,0.45); }
  #buildview .bvcard.running::after { content: ""; position: absolute; left: 0; bottom: 0; height: 2px; width: 40%;
    background: linear-gradient(90deg, transparent, rgba(160,255,210,0.55), transparent);
    animation: bvshimmer 4.5s ease-in-out infinite; }
  @keyframes bvshimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(250%); } }
  #buildview .bvship { padding: 1.1vmin 1.6vmin; border-radius: 1.4vmin; background: rgba(12,16,26,0.5);
    border: 1px solid rgba(255,255,255,0.06); }
  #buildview .bvship .t { font-size: 2.2vmin; color: rgba(255,255,255,0.86); line-height: 1.25;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  #buildview .bvship .m { margin-top: 0.3vmin; font-size: 1.8vmin; color: rgba(255,255,255,0.45); }
  #buildview .bvempty { font-size: 2.1vmin; color: rgba(255,255,255,0.4); padding: 0.6vmin 0.6vmin; }
  #buildview .bvcard .n { display: inline-block; min-width: 2.6vmin; margin-right: 0.8vmin; padding: 0 0.6vmin;
    border-radius: 0.8vmin; background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.6); text-align: center;
    font-size: 1.9vmin; vertical-align: 0.2vmin; }
  #buildview .bvcard.focus { border-color: rgba(160,190,255,0.35); background: rgba(20,28,46,0.7); }
  #buildview .bvcard.focus .n { background: rgba(160,190,255,0.35); color: #fff; }
  #buildview .bvsteps, #buildview .bvgraph, #buildview .bvfiles { padding: 1.0vmin 1.6vmin; border-radius: 1.4vmin;
    background: rgba(12,16,26,0.5); border: 1px solid rgba(255,255,255,0.06); font-size: 1.9vmin; }
  #buildview .bvsteps div { color: rgba(255,255,255,0.62); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.45; }
  #buildview .bvsteps div:last-child { color: rgba(255,255,255,0.9); }
  #buildview .bvsteps i, #buildview .bvfiles i { font-style: normal; float: right; margin-left: 1vmin; color: rgba(255,255,255,0.35); }
  #buildview .bvgraph .g { position: relative; padding-left: 3.2vmin; line-height: 1.5; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; color: rgba(255,255,255,0.66); }
  #buildview .bvgraph .g::before { content: ""; position: absolute; left: 0.9vmin; top: 0; bottom: 0; width: 2px;
    background: rgba(160,190,255,0.22); }
  #buildview .bvgraph .g:first-child::before { top: 50%; }
  #buildview .bvgraph .g:last-child::before { bottom: 50%; }
  #buildview .bvgraph .g::after { content: ""; position: absolute; left: 0.45vmin; top: 50%; width: 1.1vmin; height: 1.1vmin;
    margin-top: -0.55vmin; border-radius: 50%; background: rgba(160,190,255,0.75); }
  #buildview .bvgraph .g.m::after { background: rgba(255,214,140,0.85); }
  #buildview .bvgraph .g:first-child::after { background: rgba(160,255,210,0.9); }
  #buildview .bvgraph .h { color: rgba(255,214,140,0.7); font-family: ui-monospace, "DejaVu Sans Mono", monospace; margin-right: 0.8vmin; }
  #buildview .bvgraph .r { color: rgba(160,255,210,0.8); margin-right: 0.8vmin; }
  #buildview .bvgraph .a { color: rgba(255,255,255,0.35); margin-left: 0.8vmin; }
  #buildview .bvfiles .f { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.45; color: rgba(255,255,255,0.78); }
  #buildview .bvfiles .st { display: inline-block; width: 3vmin; color: rgba(255,214,140,0.85); font-family: ui-monospace, "DejaVu Sans Mono", monospace; }
  #buildview .bvfiles .st.A, #buildview .bvfiles .st.q { color: rgba(160,255,210,0.85); }
  #buildview .bvfiles .st.D { color: rgba(255,140,130,0.85); }
  #buildview .bvdiff { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; justify-content: flex-end;
    overflow: hidden; padding: 1.0vmin 0; border-radius: 1.4vmin; background: rgba(6,8,14,0.72);
    border: 1px solid rgba(255,255,255,0.06); font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 1.75vmin; }
  #buildview .bvdiff .l { white-space: pre; overflow: hidden; text-overflow: ellipsis; line-height: 1.42; padding: 0 1.4vmin;
    color: rgba(255,255,255,0.6); flex: none; }
  #buildview .bvdiff .l.p { background: rgba(80,200,140,0.10); color: rgba(225,255,235,0.92); }
  #buildview .bvdiff .l.d { background: rgba(240,100,100,0.10); color: rgba(255,215,215,0.75); }
  #buildview .bvdiff .l.h { color: rgba(160,190,255,0.7); margin-top: 0.6vmin; }
  #buildview .bvdiff .l::before { display: inline-block; width: 2vmin; color: rgba(255,255,255,0.35); content: " "; }
  #buildview .bvdiff .l.p::before { content: "+"; color: rgba(160,255,210,0.8); }
  #buildview .bvdiff .l.d::before { content: "-"; color: rgba(255,140,130,0.8); }
  #buildview .bvdiff .l.h::before { content: "@"; }
  #buildview .bvdiff .k { color: rgba(200,170,255,0.95); }
  #buildview .bvdiff .s { color: rgba(255,214,140,0.9); }
  #buildview .bvdiff .c { color: rgba(255,255,255,0.38); font-style: italic; }
  #buildview .bvdiff .u { color: rgba(140,210,255,0.9); }
  #buildview .bvhint { font-size: 1.7vmin; color: rgba(255,255,255,0.3); text-align: right; }
  @media (orientation: portrait) {
    #buildview { left: 3vw; right: 3vw; top: 20vh; bottom: 1.5vh; }
    html body.convo.convo-build #caption, html.big body.convo.convo-build #caption { top: 15vh; bottom: auto;
      font-size: 3.4vw; max-width: 90vw; padding: 0.8vw 2.4vw; }
  }
  @media (orientation: landscape) {
    #buildview { left: 47vw; right: 3vw; top: 6vh; bottom: 6vh; }
    body.convo.convo-build #caption { left: 24vw; max-width: 42vw; font-size: 2.8vh; bottom: 6vh; }
  }
  @media (prefers-reduced-motion: reduce) {
    #buildview, #buildview.on { transition: none !important; transform: none !important; }
    #buildview .bvcard.running::after { animation: none; display: none; }
  }`;
  document.head.appendChild(css);

  const root = document.createElement("div");
  root.id = "buildview";
  root.setAttribute("aria-hidden", "true");
  document.body.appendChild(root);
  // build mode v2: the developer cockpit (face/cockpit.js) takes over the drawing when loaded
  const cockpit = window.WalterCockpit ? window.WalterCockpit.create(root) : null;
  const PHASE = "walter.buildPhase"; // {plan, phase}: "build it" survives a face reload
  const planKey = () => (cfg.planWs ? cfg.planWs + ":" : "") + cfg.plan; // a plan belongs to its workspace
  function persistPhase() { try { localStorage.setItem(PHASE, JSON.stringify({ plan: planKey(), phase: gate.g.phase })); } catch (e) {} }
  function restorePhase() {
    try { const o = JSON.parse(localStorage.getItem(PHASE) || "null"); if (o && o.plan === planKey()) gate.g.phase = o.phase === "build" ? "build" : "plan"; } catch (e) {}
  }
  // the current workspace (cockpit.js: a tap, face/dashboard-workspace.json or dashboard-build.json); null
  // without workspaces. The plan and the tests follow it.
  const LIB = window.WalterCockpitLib || null;
  function curWs() { try { return cockpit && cockpit.curId ? cockpit.curId(data) : null; } catch (e) { return null; } }
  let lastDisp = null;
  function syncPlan(force) {
    if (!lastDisp) return;
    const id = curWs();
    const plan = LIB ? LIB.planFor(lastDisp, id) : String(lastDisp.build_plan || "");
    if (force || plan !== cfg.plan || id !== cfg.planWs) {
      cfg.plan = plan; cfg.planWs = id; planFetched = 0; data.planMd = "";
      gate.g.phase = String(lastDisp.build_phase || "plan") === "build" ? "build" : "plan";
      restorePhase();
    }
  }

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const nowS = () => Date.now() / 1000;
  function ago(ts) {
    const d = Math.max(0, Math.floor(nowS() - ts));
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) { const h = Math.floor(d / 3600); return h + (h > 1 ? " hours ago" : " hour ago"); }
    const days = Math.floor(d / 86400);
    return days === 1 ? "yesterday" : days + " days ago";
  }
  function elapsed(s) {
    s = Math.max(0, Math.floor(s || 0));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
    return Math.floor(s / 3600) + "h " + String(Math.floor(s / 60) % 60).padStart(2, "0") + "m";
  }

  async function getJSON(name) {
    try {
      const r = await fetch(name, { cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }
  let inflight = false;
  async function refresh() {
    if (inflight) return;
    inflight = true;
    const [w, b, l, u] = await Promise.all(["dashboard-workers.json", "dashboard-build.json",
      "dashboard-loop.json", "usage.json"].map(getJSON));
    if (w) data.workers = w;
    if (b) data.build = b;
    if (l) data.loop = l;
    if (u) data.usage = u;
    lastData = lastWorkers = Date.now();
    if (w) gate.workers(nowS(), w);
    if (cockpit) await cockpitExtras();
    inflight = false;
    render();
  }
  // the fast test file (walterctl test writes it live) and the plan markdown
  let planFetched = 0;
  // this workspace's test file; a switch drops the old project's run at once
  function takeTests(t, id) {
    if (id !== data.testsWs) { data.testsWs = id; data.tests = null; }
    const T = LIB ? LIB.testsFor(t, id) : t;
    if (T) data.tests = T;
  }
  const testsName = (id) => (LIB ? LIB.wsFile("tests", id) : "dashboard-tests.json");
  async function cockpitExtras() {
    data.workspaceFile = await getJSON("dashboard-workspace.json"); // the current workspace, published the moment it changes
    const id = curWs();
    takeTests(await getJSON(testsName(id)), id);
    syncPlan(false);
    if (cfg.plan) {
      if (Date.now() - planFetched > 4000) {
        planFetched = Date.now();
        const want = cfg.planWs;
        try {
          const r = await fetch(LIB ? LIB.wsFile("plan", want) : "dashboard-plan.md", { cache: "no-store" });
          const txt = r.ok ? (await r.text()).slice(0, 40000) : "";
          if (want === cfg.planWs) data.planMd = txt; // still the same workspace
        } catch (e) {}
      }
    } else data.planMd = "";
    data.planName = cfg.plan;
    if (showing) window.WalterCockpit.cues({ data, cfg });
  }
  let testsT = 0;
  // live CPU/memory/load (walter-gauges-live.service writes dashboard-gauges.json every 3 s)
  let gaugesT = 0;
  async function refreshGauges() {
    gaugesT = Date.now();
    const g = await getJSON("dashboard-gauges.json");
    if (g && g.ts && Date.now() / 1000 - g.ts < 20) { data.gauges = g; render(); }
  }
  async function refreshTests() {
    testsT = Date.now();
    const id = curWs();
    const t = await getJSON(testsName(id));
    takeTests(t, id);
    if (t) { window.WalterCockpit.cues({ data, cfg }); render(); }
  }

  function strip() {
    const bits = [], L = data.loop, U = data.usage, B = data.build || {};
    if (L && L.focus) bits.push(`<span>Focus <b>${esc(L.focus)}</b></span>`);
    if (L && L.blocked) bits.push(`<span class="warn">Waiting on you <b>${esc(L.blocked)}</b></span>`);
    const pct = U && U.week && U.week.pct;
    if (typeof pct === "number") bits.push(`<span${pct >= 75 ? ' class="warn"' : ""}>Week <b>${Math.round(pct)}%</b></span>`);
    if (B.release && B.release.version) bits.push(`<span>Release <b>v${esc(B.release.version)}</b></span>`);
    const T = B.tests;
    if (T) {
      const n = T.failed ? `${T.failed} failing` : (T.passed != null ? `${T.passed} passing` : (T.ok ? "passing" : "failing"));
      bits.push(`<span class="${T.ok ? "" : "bad"}">Tests <b>${esc(n)}</b>${T.when ? " " + esc(ago(T.when)) : ""}</span>`);
    }
    return bits.length ? `<div class="bvstrip">${bits.join("")}</div>` : `<div class="bvstrip"><span>Building</span></div>`;
  }

  function liveWorkers() {
    const W = (data.workers && data.workers.workers) || [];
    const t = nowS();
    const live = W.filter((w) => w.status === "running" || w.status === "queued");
    const recent = W.filter((w) => w.status !== "running" && w.status !== "queued" && w.finished && t - w.finished < 600);
    return live.concat(recent).slice(0, 4);
  }
  function focused(list) {
    const n = gate.g.focus;
    if (n && list[n - 1]) return list[n - 1];
    return list.find((w) => w.status === "running") || list[0] || null;
  }

  function workers() {
    const list = liveWorkers(), F = focused(list);
    const t = nowS(), upd = (data.workers && data.workers.updated) || t;
    const cards = list.slice(0, 3).map((w, i) => {
      const running = w.status === "running";
      const queued = w.status === "queued";
      const el = running ? elapsed((w.elapsed || 0) + Math.max(0, t - upd)) : queued ? "waiting" : "done";
      const act = queued ? "Waiting for a free slot" : (w.activity || (running ? "Working" : "Finished"));
      const cls = (running ? "running" : queued ? "waiting" : "done") + (w === F && list.length > 1 ? " focus" : "");
      return `<div class="bvcard ${cls}"><span class="e">${esc(el)}</span>` +
        `<div class="t"><span class="n">${i + 1}</span>${esc(w.title || "Worker")}</div><div class="a">${esc(act)}</div></div>`;
    });
    const logs = (data.build && data.build.worker_log) || {};
    const steps = (F && F.id != null && logs[String(F.id)]) || [];
    const stepHtml = steps.length ? `<h3>Steps${list.length > 1 ? " · worker " + (list.indexOf(F) + 1) : ""}</h3><div class="bvsteps">` +
      steps.slice(-5).map((x) => `<div><i>${esc(x.ago)}</i>${esc(x.s)}</div>`).join("") + `</div>` : "";
    const G = ((data.build && data.build.graph) || []).slice(0, 8);
    const graph = G.length ? `<h3>Commits${data.build.repo ? " · " + esc(data.build.repo) : ""}</h3><div class="bvgraph">` +
      G.map((c) => `<div class="g${c.merge ? " m" : ""}"><span class="h">${esc(c.h)}</span>` +
        `${c.ref ? `<span class="r">${esc(c.ref)}</span>` : ""}${esc(c.s)}<span class="a">${esc(ago(c.ts))}</span></div>`).join("") +
      `</div>` : "";
    return `<div class="bvcol"><h3>Building now</h3>${cards.length ? cards.join("") : '<div class="bvempty">No workers running right now.</div>'}` +
      stepHtml + graph + `</div>`;
  }

  // light syntax colouring on raw text; every piece escaped
  const KW = "def|class|return|if|else|elif|for|while|const|let|var|function|import|from|try|except|catch|finally|new|async|await|with|as|in|of|not|and|or|None|True|False|null|true|false|undefined|self|this|raise|throw|yield|lambda|pass|break|continue";
  function colour(text, lang) {
    const cm = lang === "js" ? "//.*$" : lang === "py" || lang === "sh" ? "#.*$" : "(?!x)x";
    const re = new RegExp(`(${cm})|("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|\`[^\`]*\`)|\\b(${KW})\\b|(\\b\\d+(?:\\.\\d+)?\\b)`, "g");
    let out = "", i = 0, m;
    while ((m = re.exec(text))) {
      if (!m[0]) { re.lastIndex++; continue; }
      out += esc(text.slice(i, m.index));
      const cls = m[1] ? "c" : m[2] ? "s" : m[3] ? "k" : "u";
      out += `<span class="${cls}">${esc(m[0])}</span>`;
      i = m.index + m[0].length;
    }
    return out + esc(text.slice(i));
  }
  function langOf(f) {
    const e = String(f || "").split(".").pop().toLowerCase();
    return ["js", "mjs", "ts", "css", "json"].includes(e) ? "js" : e === "py" || e === "toml" ? "py"
      : ["sh", "bash", "service", "timer"].includes(e) || !String(f).includes(".") ? "sh" : "";
  }

  const SCROLL_STEP = 10, WINDOW = 25;
  function ide() {
    const B = data.build || {};
    if (gate.g.pane === "plan") {
      const L = data.loop || {};
      const plan = [];
      if (L.focus) plan.push(`<div class="bvship"><div class="m">Focus</div><div class="t">${esc(L.focus)}</div></div>`);
      if (L.next) plan.push(`<div class="bvship"><div class="m">Next</div><div class="t">${esc(L.next)}</div></div>`);
      const S = (B.shipped || []).slice(0, 4).map((c) => `<div class="bvship"><div class="t">${esc(c.line)}</div>` +
        `<div class="m">${esc(c.repo)} · ${esc(ago(c.ts))}</div></div>`);
      return `<div class="bvcol"><h3>The plan</h3>${plan.join("") || '<div class="bvempty">No plan set in the loop.</div>'}` +
        `<h3>Shipped lately</h3>${S.join("") || '<div class="bvempty">Nothing shipped yet today.</div>'}` +
        `<div class="bvhint">say "show the diff"</div></div>`;
    }
    const ch = B.changes || [];
    const files = ch.length ? `<div class="bvfiles">` + ch.slice(0, 5).map((c) => {
      const st = c.st === "??" ? "new" : c.st;
      return `<div class="f"><i>${esc(c.ago)}</i><span class="st ${c.st === "??" ? "q" : esc(c.st[0])}">${esc(st === "new" ? "+" : st)}</span>${esc(c.file)}</div>`;
    }).join("") + (B.changes_more ? `<div class="f"><span class="st"></span>and ${B.changes_more} more</div>` : "") + `</div>` : "";
    const head = `<h3>${ch.length ? "Changed" : "Last commit"}${B.repo ? " · " + esc(B.repo) : ""}${B.changes_stat ? " · " + esc(B.changes_stat) : ""}</h3>`;
    const D = B.diff;
    let diff = '<div class="bvempty">No changes to show yet.</div>';
    if (D && D.lines && D.lines.length) {
      const maxBack = Math.max(0, Math.ceil((D.lines.length - WINDOW) / SCROLL_STEP));
      if (gate.g.scroll > maxBack) gate.g.scroll = maxBack;
      const end = D.lines.length - gate.g.scroll * SCROLL_STEP;
      const lang = langOf(D.file);
      const rows = D.lines.slice(Math.max(0, end - WINDOW), end).map((x) => {
        const cls = x.k === "+" ? "p" : x.k === "-" ? "d" : x.k === "@" ? "h" : "";
        return `<div class="l ${cls}">${x.k === "@" ? esc(x.s) : colour(x.s, lang)}</div>`;
      });
      diff = `<h3>${D.live ? "Editing" : "Diff"} · ${esc(D.file)}</h3><div class="bvdiff">${rows.join("")}</div>`;
    }
    return `<div class="bvcol">${ch.length ? head + files : ""}${diff}` +
      `<div class="bvhint">${gate.g.scroll ? "scrolled up · " : ""}say "show the plan"</div></div>`;
  }

  function render() {
    if (!showing) return;
    if (cockpit) { cockpit.render({ data, gate, cfg }); return; }
    root.innerHTML = strip() + workers() + ide();
  }

  const pollSettings = async () => {
    const d = await getJSON("display.json");
    if (!d) return;
    view = String(d.conversation_view || "build").toLowerCase() === "clear" ? "clear" : "build";
    gate.setMode(d.build_mode);
    cfg.sound = String(d.build_sound || "on").toLowerCase() === "off" ? "off" : "on";
    lastDisp = d;
    syncPlan(disp === null);
    if (disp === null && d.build_zoom) gate.g.zoom = String(d.build_zoom);
    const cur = { focus: d.build_focus, pane: d.build_pane, seq: d.build_seq };
    if (disp) { // apply only what changed since the last poll (voice commands win in between)
      if (cur.focus !== disp.focus && cur.focus != null) gate.g.focus = Number(cur.focus) || 0;
      if (cur.pane !== disp.pane && cur.pane) { gate.g.pane = cur.pane === "plan" ? "plan" : "diff"; gate.g.scroll = 0; }
      if (cur.seq !== disp.seq && d.build_cmd) {
        const c = String(d.build_cmd);
        if (c.startsWith("mode ")) gate.cliMode(c.slice(5)); // walterctl display build on|off|auto
        if (c.startsWith("zoom ")) { const z = c.slice(5); gate.g.zoom = z === "back" ? "" : z; gate.g.cheat = 0; }
        if (c === "cheat on") gate.g.cheat = nowS();
        if (c === "cheat off") gate.g.cheat = 0;
        if (c.startsWith("phase ")) { gate.g.phase = c.slice(6) === "build" ? "build" : "plan"; persistPhase(); }
        if (c === "plan set") { gate.g.phase = "plan"; gate.g.zoom = ""; planFetched = 0; persistPhase(); }
        if (c === "scroll up") gate.g.scroll += 1;
        else if (c === "scroll down") gate.g.scroll = Math.max(0, gate.g.scroll - 1);
      }
      if (showing) render();
    }
    disp = cur;
  };
  pollSettings(); setInterval(pollSettings, 3000);

  // the BUILD MODE chip while sticky (hidden while asleep / on the sleep dashboard)
  const chip = document.createElement("div");
  chip.id = "buildchip";
  chip.innerHTML = '<b>BUILD MODE</b><span>say "done building" or press Esc</span>';
  document.body.appendChild(chip);
  const chipCss = document.createElement("style");
  chipCss.textContent = `
  #buildchip { position: fixed; top: 1.2vmin; left: 50%; transform: translateX(-50%); z-index: 40; pointer-events: none;
    display: flex; gap: 1.2vmin; align-items: baseline; padding: 0.6vmin 1.6vmin; border-radius: 2vmin;
    background: rgba(12,16,26,0.7); border: 1px solid rgba(160,255,210,0.35); font-size: 1.7vmin;
    color: rgba(255,255,255,0.55); opacity: 0; transition: opacity 0.4s ease; white-space: nowrap; }
  #buildchip b { color: rgba(160,255,210,0.95); font-weight: 600; letter-spacing: 0.12em; }
  #buildchip.on { opacity: 1; }
  @media (prefers-reduced-motion: reduce) { #buildchip { transition: none; } }`;
  document.head.appendChild(chipCss);

  function asleep() {
    const S = window.WALTER_STATE || {}, b = document.body.classList;
    return S.state === "sleeping" || !!S.dash || b.contains("dash-on");
  }

  // Escape on the wall keyboard leaves sticky build mode (not while typing, not while a canvas /
  // browser panel is open: Escape closes those first, and not on the sleep dashboard: it wakes me).
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !gate.sticky() || asleep()) return;
    const b = document.body.classList, t = e.target || {};
    if (b.contains("canvas-on") || b.contains("browser-on")) return;
    if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable) return;
    // the cockpit's cheat sheet / zoom close first, then build mode itself
    if (gate.cheatOpen(nowS())) { gate.g.cheat = 0; render(); return; }
    if (gate.g.zoom) { gate.g.zoom = ""; render(); return; }
    gate.exit(nowS());
    persist();
  });

  // what the build cockpit is doing, for face/wallstate.js (the app's Mode sheet); no text
  window.WALTER_BUILD = () => ({ sticky: gate.sticky(), showing, phase: gate.g.phase, zoom: gate.g.zoom, plan: !!cfg.plan });

  function tick() {
    const sticky = gate.sticky();
    window.WALTER_BUILD_STICKY = sticky; // converse.js holds conversation mode up while this is true
    chip.classList.toggle("on", sticky && !asleep());
    persist();
    const convo = document.body.classList.contains("convo");
    // gating needs fresh worker data while we talk, even before the view shows
    if (convo && !showing && Date.now() - lastWorkers > 5000) refresh();
    const want = (view === "build" || sticky) && convo && gate.tick(nowS());
    if (want !== showing) {
      showing = want;
      window.WALTER_CONVO_BUILD = want;
      document.body.classList.toggle("convo-build", want);
      root.classList.toggle("on", want);
      if (want) { render(); refresh(); }
    }
    if (showing && cockpit && Date.now() - gaugesT > 3000) refreshGauges();
    if (showing && cockpit && Date.now() - testsT > (data.tests && data.tests.running ? 1000 : 3000)) refreshTests();
    if (!showing || ++ticks % 4) return; // toggle fast (250 ms), redraw once a second
    if (Date.now() - lastData > 5000) refresh();
    else render(); // ticking elapsed times and "min ago"
  }
  let ticks = 0;
  setInterval(tick, 250);
  window.walterBuildView = () => ({ view, showing, data }); // for the console
})();
