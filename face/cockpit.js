/* Build mode v2: the voice-driven developer cockpit (owner approved the plan 9 Nov 13:37,
 * journal/2026-11-09-walter-product/PLAN-buildmode-v2.md). buildview.js owns the gate, the data
 * polling and the show / hide; when this file is loaded it hands every render to the cockpit:
 *
 *   pipe    ship pipeline lights: build, test, sign, publish, live (dashboard-build.json cockpit.pipeline)
 *   gauge   radial gauges: CPU, memory, load, week %, workers
 *   stream  activity stream: every worker's steps, colour per worker, newest at the bottom, max 40 lines
 *           (dashboard-workers.json workers[].log, walter/workerlive.py)
 *   tests   test runner: big pass / fail counters, progress while running, failures pinned on top
 *           (face/dashboard-tests-<workspace>.json, written live by `walterctl test`; the current project's only)
 *           + GitHub Actions of the project's repos underneath (cockpit.actions, walter/ghactions.py; hidden if none)
 *   diff    diff viewer: file tabs, line numbers, syntax colour, added / removed glow, newest hunk
 *   board   task board: Ready / Building / Done (backlog.md + workers), cards slide between columns
 *   graph   git graph: a lane each for Walter and the site, new commits pop in
 *   plan    the plan pane (face/dashboard-plan-<workspace>.md via `walterctl display build plan FILE`): big in the
 *           planning phase with the closing question lit, a slim checklist once the owner says "build it"
 *   bar     "Say:" command bar with rotating examples; the cheat sheet overlay ("cheat sheet")
 *
 * Zoom: "zoom in on diff|tests|board|stream|graph|plan" -> one panel full size, "back" returns.
 * Workspaces (owner approved 11 Nov 09:37): a slim pill bar across the top (current lit, a lock on walled
 * ones, badges = running workers / ready tasks; the only tappable part: a tap switches this face's
 * workspace via POST /api/workspace/switch) and the "projects" zoom ("show all projects"): a card per
 * workspace. Walled workspaces other than the current one arrive with counts only (walter/cockpit.py).
 * Performance (weak laptop GPU): each panel rebuilds only when its HTML changed and at most twice a
 * second; motion is opacity / transform only; prefers-reduced-motion = no motion at all. Font sizes
 * are absolute (vmin) so textscale.js scales them. Sound cues (WebAudio, no files) and Walter's
 * reactions are driven from here too (see cues()).
 */
(() => {
  "use strict";
  const L = window.WalterCockpitLib;
  if (!L) return;
  const esc = L.esc;
  const REDUCED = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  const PANELS = ["pipe", "gauge", "stream", "tests", "diff", "board", "graph", "plan", "projects", "bar"];
  const WCOL = ["#5cd6ff", "#ffc35c", "#d78bff", "#7dffb0", "#ff8f8f", "#9db4ff"];
  const KIND = { edit: "EDIT", read: "READ", search: "FIND", web: "WEB", test: "TEST", git: "GIT", plan: "PLAN", run: "RUN", other: "STEP" };

  const css = document.createElement("style");
  css.dataset.portrait = "cockpit"; // per-orientation sizes are below: portrait.js leaves it alone
  css.textContent = `
  #buildview.cockpit { display: grid; gap: 1.1vmin; grid-template-columns: 1fr 1fr 0.9fr;
    grid-template-rows: auto auto 21fr 33fr 24fr auto;
    grid-template-areas: "ws ws ws" "pipe pipe gauge" "stream stream tests" "diff diff diff" "board board graph" "bar bar bar";
    font-size: 1.8vmin; color: rgba(225,240,255,0.86); }
  #buildview.cockpit.withplan { grid-template-areas: "ws ws ws" "pipe pipe gauge" "stream stream tests" "diff diff plan" "board board graph" "bar bar bar"; }
  #buildview.cockpit.planning { grid-template-rows: auto auto 52fr 20fr auto;
    grid-template-areas: "ws ws ws" "pipe pipe gauge" "plan plan plan" "stream stream tests" "bar bar bar"; }
  #buildview.cockpit.planning .cp.diff, #buildview.cockpit.planning .cp.board, #buildview.cockpit.planning .cp.graph { display: none; }
  #buildview.cockpit:not(.withplan):not(.planning) .cp.plan { display: none; }
  #buildview.cockpit::before { content: ""; position: absolute; inset: -1vmin; z-index: -1; pointer-events: none; border-radius: 2vmin;
    background-image: linear-gradient(rgba(92,214,255,0.035) 1px, transparent 1px), linear-gradient(90deg, rgba(92,214,255,0.035) 1px, transparent 1px);
    background-size: 4vmin 4vmin; }
  #buildview.cockpit::after { content: ""; position: absolute; left: 0; right: 0; top: 0; height: 18%; pointer-events: none;
    background: linear-gradient(180deg, transparent, rgba(92,214,255,0.035), transparent); animation: cpscan 9s linear infinite; will-change: transform; }
  @keyframes cpscan { from { transform: translateY(-20%); } to { transform: translateY(560%); } }
  #buildview .cp { position: relative; min-width: 0; min-height: 0; overflow: hidden; display: flex; flex-direction: column;
    padding: 1.0vmin 1.3vmin; border-radius: 1.4vmin; background: rgba(8,14,24,0.66);
    border: 1px solid rgba(92,214,255,0.14); box-shadow: inset 0 0 2.4vmin rgba(92,214,255,0.04); }
  #buildview .cp > .hd { flex: none; display: flex; align-items: center; gap: 1.2vmin; margin-bottom: 0.6vmin; min-width: 0; overflow: hidden;
    font-size: 1.55vmin; line-height: 1.3; letter-spacing: 0.16em; text-transform: uppercase; color: rgba(92,214,255,0.72); white-space: nowrap; }
  #buildview .cp > .hd .ht { flex: 0 1 auto; min-width: 0; max-width: 60%; overflow: hidden; text-overflow: ellipsis; font-weight: inherit; }
  #buildview .cp > .hd i { flex: 1 1 0; min-width: 0; font-style: normal; letter-spacing: 0.04em; text-transform: none; color: rgba(225,240,255,0.42); overflow: hidden; text-overflow: ellipsis; }
  #buildview .cp > .bd { flex: 1 1 auto; min-height: 0; position: relative; }
  #buildview .mono { font-family: ui-monospace, "DejaVu Sans Mono", monospace; }
  #buildview .cp.pipe { grid-area: pipe; } #buildview .cp.gauge { grid-area: gauge; } #buildview .cp.stream { grid-area: stream; }
  #buildview .cp.tests { grid-area: tests; } #buildview .cp.diff { grid-area: diff; } #buildview .cp.board { grid-area: board; }
  #buildview .cp.graph { grid-area: graph; } #buildview .cp.plan { grid-area: plan; } #buildview .cp.bar { grid-area: bar; }
  #buildview.cockpit.zoomed .cp { display: none; }
  #buildview.cockpit.zoomed .cp.z, #buildview.cockpit.zoomed .cp.bar { display: flex; }
  #buildview.cockpit.zoomed .cp.z { grid-area: 2 / 1 / -2 / -1; font-size: 2.3vmin; }
  #buildview.cockpit:not(.zoomed) .cp.projects { display: none; }
  /* workspace pills (the only part of the cockpit you can tap; the rest stays click-through) */
  #buildview .wsbar { grid-area: ws; min-width: 0; overflow: hidden; }
  #buildview .wsbar:empty { display: none; }
  #buildview .wsp { display: flex; gap: 0.8vmin; align-items: center; overflow-x: auto; scrollbar-width: none; pointer-events: auto; }
  #buildview .wsp::-webkit-scrollbar { display: none; }
  #buildview .wsp .pill { flex: 0 1 auto; min-width: 6vmin; max-width: 28%; display: inline-flex; align-items: center; gap: 0.6vmin;
    padding: 0.5vmin 1.3vmin; border-radius: 2vmin; border: 1px solid rgba(92,214,255,0.18); background: rgba(8,14,24,0.66);
    color: rgba(225,240,255,0.66); font: inherit; font-size: 1.7vmin; line-height: 1.3; cursor: pointer; white-space: nowrap; }
  #buildview .wsp.tight .pill { font-size: 1.5vmin; padding: 0.4vmin 1vmin; max-width: 20%; }
  #buildview .wsp .pill .nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  #buildview .wsp .pill.on { color: #fff; border-color: rgba(92,214,255,0.8); background: rgba(92,214,255,0.2); box-shadow: 0 0 1.6vmin rgba(92,214,255,0.25); }
  #buildview .wsp .pill.all { margin-left: auto; flex: none; max-width: none; color: rgba(92,214,255,0.8); }
  #buildview .wsp .pill.all.on { color: #fff; }
  #buildview .wsp .b { flex: none; min-width: 2.2vmin; padding: 0 0.5vmin; border-radius: 1vmin; text-align: center; font-size: 0.82em;
    font-weight: 700; font-variant-numeric: tabular-nums; }
  #buildview .wsp .b.run { background: rgba(92,214,255,0.85); color: rgba(8,14,24,0.95); }
  #buildview .wsp .b.rdy { background: rgba(255,195,92,0.22); color: #ffc35c; }
  #buildview .lock { flex: none; width: 1.2vmin; height: 1.45vmin; fill: rgba(255,195,92,0.85); stroke: rgba(255,195,92,0.85); vertical-align: -0.15vmin; }
  #buildview .wsp .pill.busy { opacity: 0.6; }
  /* all projects */
  #buildview .prj { position: absolute; inset: 0; overflow: hidden; display: grid; gap: 1.2vmin; align-content: start;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 34vmin), 1fr)); }
  #buildview .pj { min-width: 0; padding: 1.2vmin 1.5vmin; border-radius: 1.4vmin; background: rgba(225,240,255,0.05);
    border: 1px solid rgba(92,214,255,0.14); display: flex; flex-direction: column; gap: 0.7vmin; }
  #buildview .pj.on { border-color: rgba(92,214,255,0.7); background: rgba(92,214,255,0.1); }
  #buildview .pj .pn2 { font-size: 2.6vmin; color: #fff; display: flex; align-items: center; gap: 0.8vmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #buildview .pj .pn2 em { font-style: normal; font-size: 1.4vmin; letter-spacing: 0.14em; text-transform: uppercase; color: rgba(92,214,255,0.85); margin-left: auto; }
  #buildview .pj .lock { width: 1.8vmin; height: 2.1vmin; }
  #buildview .pj .pf { font-size: 1.9vmin; line-height: 1.35; color: rgba(225,240,255,0.82); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  #buildview .pj .pc { font-size: 1.6vmin; color: rgba(225,240,255,0.6); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #buildview .pj .dim { color: rgba(225,240,255,0.38); }
  #buildview .pj .ps { display: flex; flex-wrap: wrap; gap: 0.4vmin 1.6vmin; font-size: 1.6vmin; color: rgba(225,240,255,0.5); }
  #buildview .pj .ps b { color: rgba(225,240,255,0.92); font-weight: 600; font-variant-numeric: tabular-nums; }
  #buildview .pj .pu span { font-size: 1.4vmin; color: rgba(225,240,255,0.42); }
  #buildview .pj .pb { position: relative; height: 0.8vmin; margin-bottom: 0.4vmin; border-radius: 0.4vmin; background: rgba(225,240,255,0.08); overflow: hidden; }
  #buildview .pj .pb b { position: absolute; left: 0; top: 0; bottom: 0; width: 100%; transform-origin: left; background: #5cd6ff; transition: transform 0.6s ease; }
  #buildview .pj .pb i { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: #ffc35c; }
  /* pipeline */
  #buildview .pl { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.6vmin; }
  #buildview .pl .st { flex: 1 1 0; position: relative; text-align: center; min-width: 0; }
  #buildview .pl .st:not(:last-child)::after { content: ""; position: absolute; top: 1.15vmin; left: calc(50% + 1.6vmin); right: calc(-50% + 1.6vmin);
    height: 2px; background: rgba(225,240,255,0.12); }
  #buildview .pl .st.ok:not(:last-child)::after { background: rgba(110,255,170,0.4); }
  #buildview .pl .dot { display: inline-block; width: 2.3vmin; height: 2.3vmin; border-radius: 50%; background: rgba(225,240,255,0.12);
    border: 1px solid rgba(225,240,255,0.25); }
  #buildview .pl .ok .dot { background: rgba(110,255,170,0.85); border-color: rgba(110,255,170,1); box-shadow: 0 0 1.4vmin rgba(110,255,170,0.55); }
  #buildview .pl .run .dot { background: rgba(92,214,255,0.85); border-color: #5cd6ff; box-shadow: 0 0 1.4vmin rgba(92,214,255,0.6); animation: cppulse 1.6s ease-in-out infinite; }
  #buildview .pl .bad .dot { background: rgba(255,120,110,0.9); border-color: #ff7a6e; box-shadow: 0 0 1.4vmin rgba(255,120,110,0.6); }
  @keyframes cppulse { 50% { opacity: 0.45; } }
  #buildview .pl .nm { margin-top: 0.4vmin; font-size: 1.6vmin; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(225,240,255,0.78); }
  #buildview .pl .nt { font-size: 1.4vmin; color: rgba(225,240,255,0.42); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #buildview .lane .lh .br { margin-left: 0.6em; padding: 0 0.45em; border-radius: 0.8em; border: 1px solid var(--lc, #5cd6ff);
    color: var(--lc, #5cd6ff); font-size: 0.85em; font-weight: 600; }
  /* commits already on GitHub (the upstream) read green and calm; local-only ones stay amber */
  #buildview .lane .c.pushed .h { color: #7dffb0; }
  #buildview .lane .c.pushed { color: rgba(225,240,255,0.55); }
  #buildview .lane .c.local .h { color: #ffc35c; }
  /* gauges */
  #buildview .gg { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0.4vmin; }
  #buildview .gg .g { position: relative; text-align: center; min-width: 0; container-type: inline-size; }
  #buildview .gg svg { width: 4.6vmin; height: 4.6vmin; display: block; margin: 0 auto; transform: rotate(-90deg); }
  #buildview .gg circle { fill: none; stroke-width: 4; }
  #buildview .gg .bg { stroke: rgba(225,240,255,0.1); }
  #buildview .gg .fg { stroke: #5cd6ff; stroke-linecap: round; transition: stroke-dasharray 0.8s ease; }
  #buildview .gg .warn .fg { stroke: #ffc35c; } #buildview .gg .hot .fg { stroke: #ff7a6e; }
  #buildview .gg .v { position: absolute; left: 0; right: 0; top: 0; height: 4.6vmin; line-height: 4.6vmin; font-size: 1.6vmin; font-weight: 700; color: rgba(225,240,255,0.92); font-variant-numeric: tabular-nums; }
  #buildview .gg .n { margin-top: 0.4vmin; font-size: calc(min(3.4vmin, 25cqi) * var(--ts, 1)); font-weight: 700; letter-spacing: 0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-transform: uppercase; color: rgba(225,240,255,0.45); }
  /* activity stream */
  #buildview .sm { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: flex-end; overflow: hidden;
    -webkit-mask-image: linear-gradient(180deg, transparent 0, #000 30%); mask-image: linear-gradient(180deg, transparent 0, #000 30%); }
  #buildview .sm .ln { flex: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.5; font-size: 1.6vmin; color: rgba(225,240,255,0.66); }
  #buildview .sm .ln .tm { color: rgba(225,240,255,0.3); margin-right: 0.9vmin; }
  #buildview .sm .ln .w { display: inline-block; min-width: 3.4vmin; margin-right: 0.8vmin; font-weight: 600; }
  #buildview .sm .ln .k { display: inline-block; width: 5.2vmin; margin-right: 0.6vmin; color: rgba(225,240,255,0.45); }
  #buildview .sm .ln.edit .k { color: #ffc35c; } #buildview .sm .ln.test .k { color: #7dffb0; } #buildview .sm .ln.git .k { color: #d78bff; }
  #buildview .sm .ln.dim { opacity: 0.38; }
  #buildview .sm .ln:last-child { color: rgba(240,250,255,0.95); }
  #buildview .sm .ln.new { animation: cpin 0.6s ease-out; }
  @keyframes cpin { from { opacity: 0; transform: translateY(0.8vmin); } to { opacity: 1; transform: none; } }
  #buildview .empty { color: rgba(225,240,255,0.36); font-size: 1.7vmin; padding: 0.4vmin; }
  /* tests */
  #buildview .tr .big { display: flex; gap: 2vmin; align-items: baseline; }
  #buildview .tr .num { font-size: 5.2vmin; font-weight: 300; line-height: 1; font-variant-numeric: tabular-nums; }
  #buildview .tr .num.p { color: #7dffb0; text-shadow: 0 0 2vmin rgba(110,255,170,0.35); }
  #buildview .tr .num.f { color: rgba(225,240,255,0.3); } #buildview .tr .num.f.on { color: #ff7a6e; text-shadow: 0 0 2vmin rgba(255,120,110,0.4); }
  #buildview .tr .lb { font-size: 1.35vmin; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(225,240,255,0.42); }
  #buildview .tr .bar { position: relative; height: 0.8vmin; margin: 0.9vmin 0 0.7vmin; border-radius: 0.4vmin; background: rgba(225,240,255,0.08); overflow: hidden; }
  #buildview .tr .bar b { position: absolute; left: 0; top: 0; bottom: 0; width: 100%; transform-origin: left; background: #7dffb0; transition: transform 0.5s ease; }
  #buildview .tr .bar.bad b { background: #ff7a6e; } #buildview .tr .bar.run b { background: #5cd6ff; }
  #buildview .tr .bar.ind b { width: 30%; animation: cpind 1.4s ease-in-out infinite; }
  @keyframes cpind { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
  #buildview .tr .fl { font-size: 1.5vmin; color: rgba(255,190,180,0.92); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.45; }
  #buildview .tr .fl::before { content: "\\2715  "; color: #ff7a6e; }
  #buildview .tr .meta { font-size: 1.45vmin; color: rgba(225,240,255,0.45); }
  #buildview .acts { margin-top: 1.1vmin; padding-top: 0.8vmin; border-top: 1px solid rgba(225,240,255,0.1); }
  #buildview .acts .lb { margin-bottom: 0.3vmin; }
  #buildview .acts .ac { display: flex; align-items: center; gap: 0.9vmin; font-size: 1.6vmin; line-height: 1.5; color: rgba(225,240,255,0.85); white-space: nowrap; }
  #buildview .acts .dot { flex: none; width: 1.1vmin; height: 1.1vmin; border-radius: 50%; background: rgba(225,240,255,0.35); }
  #buildview .acts .ok .dot { background: #7dffb0; box-shadow: 0 0 0.8vmin rgba(110,255,170,0.5); }
  #buildview .acts .bad .dot { background: #ff7a6e; box-shadow: 0 0 0.8vmin rgba(255,120,110,0.55); }
  #buildview .acts .run .dot { background: #ffc35c; box-shadow: 0 0 0.8vmin rgba(255,195,92,0.5); }
  #buildview .acts .bad .an { color: rgba(255,190,180,0.95); }
  #buildview .acts .an { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  #buildview .acts .ar { flex: none; font-size: 1.3vmin; padding: 0 0.6vmin; border-radius: 0.5vmin; background: rgba(225,240,255,0.08); color: rgba(225,240,255,0.6); }
  #buildview .acts .at { flex: none; font-size: 1.4vmin; color: rgba(225,240,255,0.45); font-variant-numeric: tabular-nums; }
  #buildview .acts .more, #buildview .acts .note { font-size: 1.35vmin; color: rgba(225,240,255,0.42); }
  /* diff */
  #buildview .tabs { flex: none; display: flex; gap: 0.5vmin; overflow: hidden; margin-bottom: 0.6vmin; }
  #buildview .tabs span { flex: none; max-width: 34%; padding: 0.3vmin 1vmin; border-radius: 0.8vmin 0.8vmin 0 0; font-size: 1.45vmin;
    background: rgba(225,240,255,0.05); color: rgba(225,240,255,0.5); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #buildview .tabs span.on { background: rgba(92,214,255,0.16); color: #dff6ff; box-shadow: inset 0 -2px 0 #5cd6ff; }
  #buildview .df { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden; background: rgba(2,6,12,0.55); border-radius: 0.8vmin; }
  #buildview .df .l { flex: none; white-space: pre; overflow: hidden; text-overflow: ellipsis; line-height: 1.42; font-size: 1.6vmin; color: rgba(225,240,255,0.62); padding-right: 1vmin; }
  #buildview .df .l .no { display: inline-block; width: 5vmin; padding-right: 1vmin; text-align: right; color: rgba(225,240,255,0.22); }
  #buildview .df .l.p { background: rgba(110,255,170,0.09); color: rgba(230,255,240,0.95); box-shadow: inset 0.35vmin 0 0 rgba(110,255,170,0.8); }
  #buildview .df .l.d { background: rgba(255,120,110,0.09); color: rgba(255,215,210,0.72); box-shadow: inset 0.35vmin 0 0 rgba(255,120,110,0.8); }
  #buildview .df .l.p .no { color: rgba(110,255,170,0.6); } #buildview .df .l.d .no { color: rgba(255,120,110,0.6); }
  #buildview .df .l.h { color: rgba(92,214,255,0.72); margin-top: 0.5vmin; }
  #buildview .df .k { color: #c79bff; } #buildview .df .s { color: #ffc35c; } #buildview .df .c { color: rgba(225,240,255,0.36); font-style: italic; }
  #buildview .df .u { color: #5cd6ff; } #buildview .df .d { color: #7dffb0; }
  /* board */
  #buildview .bd3 { position: absolute; inset: 0; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.9vmin; }
  #buildview .bd3 .col { min-width: 0; overflow: hidden; }
  #buildview .bd3 .ch { font-size: 1.75vmin; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(225,240,255,0.5); margin-bottom: 0.5vmin; }
  #buildview .bd3 .ch b { color: rgba(225,240,255,0.85); font-weight: 500; margin-left: 0.5vmin; }
  #buildview .bd3 .card { padding: 0.55vmin 0.8vmin; margin-bottom: 0.55vmin; border-radius: 0.8vmin; background: rgba(225,240,255,0.05);
    border-left: 0.35vmin solid rgba(225,240,255,0.2); font-size: 1.9vmin; line-height: 1.3; color: rgba(225,240,255,0.8);
    display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; will-change: transform; }
  #buildview .bd3 .building .card { border-left-color: #5cd6ff; } #buildview .bd3 .building .card.worker { background: rgba(92,214,255,0.1); }
  #buildview .bd3 .done .card { border-left-color: rgba(110,255,170,0.7); color: rgba(225,240,255,0.55); }
  #buildview .bd3 .done .card.failed { border-left-color: #ff7a6e; }
  #buildview .bd3 .card .pos { color: #ffc35c; margin-right: 0.6vmin; font-weight: 600; font-size: 1.5vmin; }
  #buildview .bd3 .card .n { color: #5cd6ff; margin-right: 0.5vmin; font-weight: 600; }
  /* graph */
  #buildview .gl { position: absolute; inset: 0; display: grid; gap: 1vmin; }
  #buildview .gl .lane { min-width: 0; overflow: hidden; }
  #buildview .gl .lh { font-size: 1.75vmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(225,240,255,0.5); margin-bottom: 0.3vmin; }
  #buildview .gl .lh b { color: #ffc35c; font-weight: 500; letter-spacing: 0; text-transform: none; margin-left: 0.6vmin; }
  #buildview .gl .c { position: relative; padding-left: 2.6vmin; line-height: 1.45; font-size: 1.8vmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: rgba(225,240,255,0.68); }
  #buildview .gl .c::before { content: ""; position: absolute; left: 0.85vmin; top: 0; bottom: 0; width: 2px; background: var(--lc); opacity: 0.35; }
  #buildview .gl .c::after { content: ""; position: absolute; left: 0.45vmin; top: 50%; width: 1vmin; height: 1vmin; margin-top: -0.5vmin; border-radius: 50%; background: var(--lc); }
  #buildview .gl .c:first-of-type::after { box-shadow: 0 0 1vmin var(--lc); }
  #buildview .gl .c.pop::after { animation: cppop 1.2s ease-out; }
  @keyframes cppop { 0% { transform: scale(2.6); opacity: 0.3; } 60% { transform: scale(1.3); opacity: 1; } 100% { transform: none; } }
  #buildview .gl .c .h { color: rgba(225,240,255,0.32); margin-right: 0.7vmin; }
  /* plan */
  #buildview .pn { position: absolute; inset: 0; overflow: hidden; font-size: 2.0vmin; line-height: 1.4; }
  #buildview .planning .pn, #buildview.planning .pn { font-size: 2.3vmin; }
  #buildview .pn h2, #buildview .pn h3, #buildview .pn h4, #buildview .pn h5 { margin: 0.6vmin 0 0.4vmin; color: #bfefff; font-weight: 500; }
  #buildview .pn h2 { font-size: 1.35em; } #buildview .pn h3 { font-size: 1.15em; } #buildview .pn h4, #buildview .pn h5 { font-size: 1em; color: rgba(92,214,255,0.85); }
  #buildview .pn p { margin: 0.3vmin 0; } #buildview .pn ul, #buildview .pn ol { margin: 0.2vmin 0 0.5vmin; padding-left: 3vmin; }
  #buildview .pn li { margin: 0.15vmin 0; } #buildview .pn b { color: #fff; font-weight: 600; }
  #buildview .pn code { font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 0.9em; color: #ffc35c; }
  #buildview .pn li.cb { list-style: none; margin-left: -2.6vmin; } #buildview .pn li.cb.done { color: rgba(225,240,255,0.45); text-decoration: line-through; }
  #buildview .pn .box { display: inline-block; width: 1.9vmin; height: 1.9vmin; margin-right: 0.8vmin; border: 1px solid rgba(92,214,255,0.6); border-radius: 0.4vmin;
    text-align: center; line-height: 1.9vmin; font-size: 1.5vmin; color: #7dffb0; vertical-align: -0.2vmin; }
  #buildview .pn .ask { margin-top: 1vmin; padding: 0.8vmin 1.2vmin; border-radius: 1vmin; color: #fff; background: rgba(92,214,255,0.14);
    box-shadow: 0 0 2.2vmin rgba(92,214,255,0.25), inset 0 0 0 1px rgba(92,214,255,0.5); list-style-position: inside; }
  #buildview .pn hr { border: 0; border-top: 1px solid rgba(225,240,255,0.1); }
  #buildview .ck .i { font-size: 1.55vmin; line-height: 1.35; margin-bottom: 0.45vmin; color: rgba(225,240,255,0.78); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  #buildview .ck .i::before { content: "\\25CB  "; color: rgba(92,214,255,0.8); }
  #buildview .ck .i.done { color: rgba(225,240,255,0.42); } #buildview .ck .i.done::before { content: "\\2713  "; color: #7dffb0; }
  /* command bar + cheat sheet */
  #buildview .cp.bar { flex-direction: row; align-items: baseline; gap: 1.2vmin; padding: 0.7vmin 1.4vmin; border-color: rgba(92,214,255,0.24); }
  #buildview .cp.bar .say { color: rgba(92,214,255,0.8); letter-spacing: 0.14em; text-transform: uppercase; font-size: 1.5vmin; }
  #buildview .cp.bar .ex { font-size: 2.0vmin; color: #fff; transition: opacity 0.4s ease; }
  #buildview .cp.bar .ex.out { opacity: 0; }
  #buildview .cp.bar .rt { margin-left: auto; font-size: 1.45vmin; color: rgba(225,240,255,0.4); white-space: nowrap; }
  #cpcheat { position: absolute; inset: 6% 4%; z-index: 5; padding: 2.4vmin 3vmin; border-radius: 2vmin; background: rgba(6,12,22,0.94);
    border: 1px solid rgba(92,214,255,0.45); box-shadow: 0 0 5vmin rgba(92,214,255,0.18); opacity: 0; visibility: hidden;
    transform: scale(0.97); transition: opacity 0.3s ease, transform 0.3s ease, visibility 0s 0.3s; overflow: hidden;
    column-count: 2; column-gap: 3vmin; } /* two columns: four groups fit without clipping */
  #cpcheat h2 { column-span: all; } #cpcheat .grp { break-inside: avoid; }
  #cpcheat.on { opacity: 1; visibility: visible; transform: none; transition: opacity 0.3s ease, transform 0.3s ease, visibility 0s; }
  #cpcheat h2 { margin: 0 0 1.4vmin; font-size: 2.8vmin; font-weight: 500; color: #bfefff; }
  #cpcheat h2 i { font-style: normal; font-size: 1.6vmin; color: rgba(225,240,255,0.45); margin-left: 1.4vmin; }
  #cpcheat .grp { margin-bottom: 1.4vmin; } #cpcheat .gt { font-size: 1.55vmin; letter-spacing: 0.16em; text-transform: uppercase; color: rgba(92,214,255,0.75); margin-bottom: 0.5vmin; }
  #cpcheat .it { display: flex; gap: 1.6vmin; font-size: 2.2vmin; line-height: 1.55; }
  #cpcheat .it b { flex: none; min-width: 42%; color: #fff; font-weight: 500; } #cpcheat .it span { color: rgba(225,240,255,0.5); font-size: 1.8vmin; }
  @media (orientation: portrait) {
    #buildview.cockpit { left: 2.5vw; right: 2.5vw; top: 27vh; bottom: 1.5vh; }
  }
  @media (orientation: landscape) {
    #buildview.cockpit { left: 43vw; right: 1.5vw; top: 3vh; bottom: 3vh; grid-template-columns: 1fr 1.35fr 1fr;
      grid-template-rows: auto auto 1fr 1fr auto; grid-template-areas: "ws ws ws" "pipe pipe gauge" "stream diff tests" "board diff graph" "bar bar bar"; }
    #buildview.cockpit.withplan { grid-template-areas: "ws ws ws" "pipe pipe gauge" "stream diff plan" "board graph tests" "bar bar bar"; }
    #buildview.cockpit.planning { grid-template-rows: auto auto 1fr 1fr auto; grid-template-areas: "ws ws ws" "pipe pipe gauge" "plan plan stream" "plan plan tests" "bar bar bar"; }
  }
  @media (prefers-reduced-motion: reduce) {
    #buildview.cockpit::after { display: none; animation: none; }
    #buildview .cp *, #cpcheat, #buildview .wsbar * { animation: none !important; transition: none !important; }
  }`;
  document.head.appendChild(css);

  function create(root) {
    root.classList.add("cockpit");
    root.innerHTML = '<nav class="wsbar"></nav>' +
      PANELS.map((p) => `<section class="cp ${p}" data-p="${p}"><div class="hd"></div><div class="bd"></div></section>`).join("") +
      '<div id="cpcheat"></div>';
    const el = {};
    for (const p of PANELS) {
      const s = root.querySelector(`.cp.${p}`);
      el[p] = { s, hd: s.querySelector(".hd"), bd: s.querySelector(".bd"), html: "", hdh: "", t: 0 };
    }
    el.bar.s.innerHTML = '<span class="say">Say</span><span class="ex"></span><span class="rt">say "cheat sheet" for all commands</span>';
    const exEl = el.bar.s.querySelector(".ex");
    const cheat = root.querySelector("#cpcheat");
    const wsbar = root.querySelector(".wsbar");
    let wsHtml = "", wsLast = null, wsOverride = null, ctxLast = null;
    // tap a pill: switch this face's workspace (POST /api/workspace/switch, this computer / home network
    // only); "All projects" toggles the projects zoom. Paired faces elsewhere (/face/...) can't switch.
    const canTap = !/^\/face(\/|$)/.test(location.pathname);
    wsbar.addEventListener("click", (ev) => {
      const b = ev.target.closest && ev.target.closest(".pill");
      if (!b || !ctxLast) return;
      b.blur(); // Space mutes me: never let it re-press a focused pill
      const g = ctxLast.gate.g;
      if (b.dataset.ws === "*") { g.zoom = g.zoom === "projects" ? "" : "projects"; g.cheat = 0; render(ctxLast); return; }
      if (canTap) switchTo(b.dataset.ws, b);
    });
    async function switchTo(id, btn) {
      if (!id) return;
      if (btn) btn.classList.add("busy");
      try {
        const r = await fetch("/api/workspace/switch", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }) });
        if (r.ok) {
          wsOverride = { id, t: Date.now() };
          window.dispatchEvent(new CustomEvent("walter-ws-switched", { detail: { id } }));
          if (window.walter) window.walter.addAction("bounce");
        }
      } catch (e) {}
      if (btn) btn.classList.remove("busy");
      if (ctxLast) render(ctxLast);
    }
    // too many pills for the width: scroll the bar (only the bar, never the page) so the lit one shows
    function keepPillInView() {
      const bar = wsbar.querySelector(".wsp"), on = wsbar.querySelector(".pill.on:not(.all)");
      if (!bar || !on || bar.scrollWidth <= bar.clientWidth) return;
      const l = on.offsetLeft - bar.offsetLeft, r = l + on.offsetWidth;
      if (l < bar.scrollLeft) bar.scrollLeft = l;
      else if (r > bar.scrollLeft + bar.clientWidth) bar.scrollLeft = r - bar.clientWidth;
    }
    // "next project" / "previous project" (buildgate -> buildview.js -> here)
    function step(dir) {
      if (!ctxLast) return;
      const id = L.stepWs(wsLast, curId(ctxLast.data), dir);
      if (id && canTap) switchTo(id, null);
    }
    // the current workspace: a tap just now, else face/dashboard-workspace.json (written the moment any
    // switch happens: voice fast path, walterctl, a tap), else the list from dashboard-build.json
    function curId(d) {
      const C = (d.build && d.build.cockpit) || {};
      const f = d.workspaceFile;
      // the file wins only when it is newer than the last build snapshot (a switch since then)
      const fresh = f && f.id && (!f.face || f.face === "wall") && (!C.workspace || !(d.build && d.build.updated) || (f.updated || 0) > d.build.updated);
      const fileId = fresh ? f.id : null;
      if (wsOverride && Date.now() - wsOverride.t < 20000 && (!f || !f.updated || f.updated * 1000 < wsOverride.t)) return wsOverride.id;
      return fileId || (C.workspace && C.workspace.id) || null;
    }
    cheat.innerHTML = `<h2>Build mode: what you can say<i>say "close" or wait</i></h2>` + L.CHEAT.map((g) =>
      `<div class="grp"><div class="gt">${esc(g.group)}</div>` + g.items.map(([a, b]) => `<div class="it"><b>"${esc(a)}"</b><span>${esc(b)}</span></div>`).join("") + "</div>").join("");

    // one panel: rebuild only when changed, at most twice a second
    function put(p, head, body, now, after) {
      const e = el[p];
      // title in its own span so it and the <i> summary are separate flex items (no overlap, own ellipsis)
      if (head !== e.hdh) { e.hd.innerHTML = head.replace(/^([^<]*)/, '<span class="ht">$1</span>'); e.hdh = head; }
      if (body === e.html || now - e.t < 500) return false;
      e.t = now;
      e.html = body;
      if (after) after(e.bd, body); else e.bd.innerHTML = body;
      return true;
    }

    // --- panels -----------------------------------------------------------------------------
    function pipe(C, name) {
      const P = (C && C.pipeline) || [];
      if (C && C.pipeline_preset && !P.length) return `<div class="empty">No ship pipeline${name ? " for " + esc(name) : ""}.</div>`;
      return '<div class="pl">' + (P.length ? P : ["build", "test", "sign", "publish", "live"].map((s) => ({ stage: s, state: "idle", note: "" })))
        .map((s) => `<div class="st ${esc(s.state)}"><span class="dot"></span><div class="nm">${esc(s.stage)}</div><div class="nt">${esc(s.note || "")}</div></div>`).join("") + "</div>";
    }
    function gauge(name, pct, label) {
      const r = 15.5, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, pct == null ? 0 : pct));
      const cls = v >= 90 ? "hot" : v >= 75 ? "warn" : "";
      return `<div class="g ${cls}"><svg viewBox="0 0 36 36"><circle class="bg" cx="18" cy="18" r="${r}"/>` +
        `<circle class="fg" cx="18" cy="18" r="${r}" stroke-dasharray="${(c * v / 100).toFixed(1)} ${c.toFixed(1)}"/></svg>` +
        `<div class="v">${esc(label)}</div><div class="n">${esc(name)}</div></div>`;
    }
    function gauges(C, d) {
      const G = Object.assign({}, (C && C.gauges) || {}, d.gauges || {}); // d.gauges = live 3 s file
      const W = (d.workers && d.workers.workers) || [];
      const running = W.filter((w) => w.status === "running" && !w.cloud).length;   // cloud jobs use no local slot
      const max = (d.workers && d.workers.max_tasks) || 3;
      const week = d.usage && d.usage.week && typeof d.usage.week.pct === "number" ? d.usage.week.pct : G.week;
      const loadPct = G.load != null ? (100 * G.load) / (G.cores || 1) : null;
      const f = (v) => (v == null ? "–" : Math.round(v) + "%");
      return '<div class="gg">' + gauge("cpu", G.cpu, f(G.cpu)) + gauge("mem", G.mem, f(G.mem)) +
        gauge("load", loadPct, G.load == null ? "–" : G.load.toFixed(1)) + gauge("week", week, f(week)) +
        gauge("workers", (100 * running) / max, `${running}/${max}`) + "</div>";
    }
    function liveWorkers(d) {
      const W = (d.workers && d.workers.workers) || [], t = Date.now() / 1000;
      const C = (d.build && d.build.cockpit) || {};
      const cur = C.workspace ? (L.currentWs(C.workspaces, curId(d)) || C.workspace) : null; // follows the workspace
      return W.filter((w) => (w.status === "running" || w.status === "queued" || (w.finished && t - w.finished < 600)) &&
        L.workerVisible(w, cur)).slice(0, 6);
    }
    let seenLines = 0;
    function stream(d, g) {
      const list = liveWorkers(d);
      const logs = (d.build && d.build.worker_log) || {};
      const rows = [];
      list.forEach((w, i) => {
        const L2 = w.log && w.log.length ? w.log : (logs[String(w.id)] || []).map((x) => ({ t: 0, k: "other", s: x.s }));
        L2.forEach((x, j) => rows.push({ t: x.t || 0, j, i, k: x.k || "other", s: x.s, id: w.id }));
      });
      rows.sort((a, b) => a.t - b.t || a.j - b.j);
      const tail = rows.slice(-40); // virtualised: never more than 40 lines in the DOM
      if (!tail.length) return { html: '<div class="sm"><div class="empty">Quiet. Steps from the workers show up here as they work.</div></div>', n: 0 };
      const F = g.focus && list[g.focus - 1] ? list[g.focus - 1].id : null;
      const fresh = rows.length - seenLines;
      const html = '<div class="sm mono">' + tail.map((x, n) => {
        const tm = x.t ? new Date(x.t * 1000).toTimeString().slice(0, 8) : "";
        const isNew = fresh > 0 && n >= tail.length - Math.min(fresh, 3);
        return `<div class="ln ${esc(x.k)}${F != null && x.id !== F ? " dim" : ""}${isNew ? " new" : ""}">` +
          (tm ? `<span class="tm">${tm}</span>` : "") + `<span class="w" style="color:${WCOL[x.i % WCOL.length]}">#${x.i + 1}</span>` +
          `<span class="k">${KIND[x.k] || "STEP"}</span>${esc(x.s)}</div>`;
      }).join("") + "</div>";
      return { html, n: rows.length };
    }
    function ago(ts) {
      const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
      return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago";
    }
    function tests(T, name) {
      if (!T) return `<div class="tr"><div class="empty">No test run yet${name ? " for " + esc(name) : ""}. Say "run the tests".</div></div>`;
      const run = !!T.running, failed = T.failed || 0, passed = T.passed || 0;
      const frac = run ? (T.total ? Math.min(1, (T.done || passed + failed) / T.total) : null) : 1;
      const bar = `<div class="bar ${run ? "run" : T.ok ? "" : "bad"}${run && frac == null ? " ind" : ""}"><b style="transform:scaleX(${frac == null ? 1 : frac.toFixed(3)})"></b></div>`;
      const fl = (T.failures || []).slice(0, 4).map((f) => `<div class="fl mono">${esc(f)}</div>`).join("");
      const meta = run ? `running${T.total ? ` · ${T.done || passed + failed} of ~${T.total}` : ""}` :
        `${T.ok ? "all green" : "failing"}${T.skipped ? ` · ${T.skipped} skipped` : ""}${T.when ? " · " + ago(T.when) : ""}`;
      return `<div class="tr">${fl}<div class="big"><div><div class="num p">${passed}</div><div class="lb">passed</div></div>` +
        `<div><div class="num f${failed ? " on" : ""}">${failed}</div><div class="lb">failed</div></div></div>${bar}<div class="meta">${esc(meta)}</div></div>`;
    }
    function diff(B, g, zoomed) {
      const D = B.diff, ch = B.changes || [];
      const active = D ? D.file : "";
      const tabs = (ch.length ? ch.slice(0, 5).map((c) => c.file) : active ? [active] : []);
      if (active && !tabs.includes(active)) tabs.unshift(active);
      const tabHtml = tabs.length ? '<div class="tabs mono">' + tabs.slice(0, 5).map((f) =>
        `<span class="${f === active ? "on" : ""}">${esc(f.split("/").pop())}</span>`).join("") + "</div>" : "";
      if (!D || !D.lines || !D.lines.length) return { tabs: tabHtml, body: '<div class="empty">No changes to show yet.</div>' };
      // line numbers from the hunk headers ("line N ...")
      let no = 0;
      const rows = D.lines.map((x) => {
        if (x.k === "@") { const m = /^line (\d+)/.exec(x.s); no = m ? parseInt(m[1], 10) : no; return { x, n: "" }; }
        if (x.k === "-") return { x, n: "" };
        return { x, n: no ? no++ : "" };
      });
      const WIN = zoomed ? 70 : 28, STEP = 10;
      const maxBack = Math.max(0, Math.ceil((rows.length - WIN) / STEP));
      if (g.scroll > maxBack) g.scroll = maxBack;
      const end = rows.length - g.scroll * STEP;
      const lang = L.langOf(D.file);
      const body = '<div class="df mono">' + rows.slice(Math.max(0, end - WIN), end).map(({ x, n }) => {
        const cls = x.k === "+" ? "p" : x.k === "-" ? "d" : x.k === "@" ? "h" : "";
        return `<div class="l ${cls}"><span class="no">${x.k === "@" ? "@@" : n}</span>${x.k === "@" ? esc(x.s) : L.highlight(x.s, lang)}</div>`;
      }).join("") + "</div>";
      return { tabs: tabHtml, body };
    }
    // the diff starts at the top of the panel; only when it overflows, scroll so the newest hunk
    // (its @@ header, or the end if that hunk is taller than the panel) is in view
    function keepLatestHunk(bd) {
      const df = bd.querySelector(".df");
      if (!df) return;
      const over = df.scrollHeight - df.clientHeight;
      if (over <= 0) { df.scrollTop = 0; return; }
      const hs = df.querySelectorAll(".l.h"), last = hs.length ? hs[hs.length - 1] : null;
      df.scrollTop = last ? Math.min(over, Math.max(0, last.offsetTop - 4)) : over;
    }
    function board(C) {
      const Bd = (C && C.board) || { ready: [], building: [], done: [], counts: {} };
      const n = Bd.counts || {};
      const col = (k, title) => `<div class="col ${k}"><div class="ch">${title}<b>${n[k] != null ? n[k] : (Bd[k] || []).length}</b></div>` +
        (Bd[k] || []).slice(0, 12).map((c, i) => `<div class="card ${esc(c.kind)}${c.status === "failed" ? " failed" : ""}" data-id="${esc(c.id)}">` +
          (c.num ? `<span class="pos">T${esc(c.num)}</span>` : "") + (c.kind === "worker" && c.n != null ? `<span class="n">#${esc(c.n)}</span>` : "") + `${esc(c.title)}</div>`).join("") + "</div>";
      return '<div class="bd3">' + col("ready", "Ready") + col("building", "Building") + col("done", "Done") + "</div>";
    }
    const seenHash = new Set();
    let graphPrimed = false;
    function graph(C) {
      const lanes = (C && C.lanes) || [];
      if (!lanes.length) return '<div class="empty">No commits yet.</div>';
      const cols = ["#5cd6ff", "#ffc35c"];
      const html = `<div class="gl" style="grid-template-columns:repeat(${lanes.length},1fr)">` + lanes.map((ln, i) =>
        `<div class="lane mono" style="--lc:${cols[i % cols.length]}"><div class="lh">${esc(ln.repo)}${ln.branch ? `<span class="br">${esc(ln.branch)}</span>` : ""}${ln.dirty ? "<b>editing</b>" : ""}${ln.ahead ? `<b>${ln.ahead} unpushed</b>` : ""}</div>` +
        (ln.commits || []).slice(0, 10).map((c, ci) => `<div class="c${graphPrimed && !seenHash.has(c.h) ? " pop" : ""}${ln.ahead != null ? (ci < ln.ahead ? " local" : " pushed") : ""}"><span class="h">${esc(c.h)}</span>${esc(c.s)}</div>`).join("") + "</div>").join("") + "</div>";
      lanes.forEach((ln) => (ln.commits || []).forEach((c) => seenHash.add(c.h)));
      graphPrimed = true;
      return html;
    }
    function planPane(d, g, B) {
      if (d.planMd) {
        if (g.phase === "build" && g.zoom !== "plan") {
          const items = L.checklist(d.planMd);
          return items.length ? '<div class="ck">' + items.map((x) => `<div class="i${x.done ? " done" : ""}">${L.inline(x.text)}</div>`).join("") + "</div>"
            : `<div class="pn">${L.md(d.planMd)}</div>`;
        }
        return `<div class="pn">${L.md(d.planMd)}</div>`;
      }
      const Lp = d.loop || {};
      const bits = [];
      if (Lp.focus) bits.push(`<h4>Focus</h4><p>${esc(Lp.focus)}</p>`);
      if (Lp.next) bits.push(`<h4>Next</h4><p>${esc(Lp.next)}</p>`);
      const S = (B.shipped || []).slice(0, 5).map((c) => `<li>${esc(c.line)} <span style="opacity:.45">${esc(c.repo)}</span></li>`).join("");
      return `<div class="pn">${bits.join("") || "<p>No plan set. Walter shows one with walterctl display build plan FILE.</p>"}${S ? `<h4>Shipped lately</h4><ul>${S}</ul>` : ""}</div>`;
    }

    // FLIP: board cards slide from their old column to the new one (transform only)
    function flip(bd, html) {
      if (REDUCED.matches) { bd.innerHTML = html; return; }
      const old = {};
      bd.querySelectorAll(".card[data-id]").forEach((c) => { old[c.dataset.id] = c.getBoundingClientRect(); });
      bd.innerHTML = html;
      const moved = [];
      bd.querySelectorAll(".card[data-id]").forEach((c) => {
        const o = old[c.dataset.id];
        if (!o) { c.style.opacity = "0"; moved.push([c, null]); return; }
        const n = c.getBoundingClientRect();
        const dx = o.left - n.left, dy = o.top - n.top;
        if (Math.abs(dx) > 2 || Math.abs(dy) > 2) { c.style.transform = `translate(${dx}px,${dy}px)`; moved.push([c, 1]); }
      });
      if (!moved.length) return;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        for (const [c] of moved) { c.style.transition = "transform 0.7s ease, opacity 0.7s ease"; c.style.transform = ""; c.style.opacity = ""; }
      }));
    }

    // --- command bar rotation -------------------------------------------------------------------
    let sayI = 0, sayT = 0;
    function rotateSay(now) {
      if (now - sayT < 4500) return;
      sayT = now;
      const next = L.SAY[sayI++ % L.SAY.length];
      if (REDUCED.matches || !exEl.textContent) { exEl.textContent = `"${next}"`; return; }
      exEl.classList.add("out");
      setTimeout(() => { exEl.textContent = `"${next}"`; exEl.classList.remove("out"); }, 400);
    }

    // --- render -----------------------------------------------------------------------------------
    function render(ctx) {
      ctxLast = ctx;
      const now = Date.now(), d = ctx.data, g = ctx.gate.g, B = d.build || {}, C = B.cockpit || {};
      const planning = !!d.planMd && g.phase !== "build";
      const zoom = g.zoom && PANELS.includes(g.zoom) ? g.zoom : (g.zoom === "pipeline" ? "pipe" : g.zoom === "gauges" ? "gauge" : "");
      root.classList.toggle("planning", planning && !zoom);
      root.classList.toggle("withplan", !!d.planMd && !planning);
      root.classList.toggle("zoomed", !!zoom);
      for (const p of PANELS) el[p].s.classList.toggle("z", p === zoom);
      cheat.classList.toggle("on", ctx.gate.cheatOpen(now / 1000));

      // every card follows the current project; only the System gauges are for the whole machine
      const cur = curId(d);
      const cwN = C.workspace ? (L.currentWs(C.workspaces, cur) || C.workspace) : null;
      const sameWs = !C.workspace || !cur || C.workspace.id === cur; // the snapshot is this project's (not mid-switch)
      put("pipe", `Ship pipeline<i>${cwN ? esc(cwN.name) : ""}</i>`, sameWs ? pipe(C, cwN && cwN.name) : '<div class="empty">Switching…</div>', now);
      put("gauge", "System<i>all projects</i>", gauges(C, d), now);
      const W = liveWorkers(d), focusW = g.focus && W[g.focus - 1];
      const st = stream(d, g);
      if (put("stream", `Activity<i>${W.length ? W.length + " worker" + (W.length > 1 ? "s" : "") : ""}${focusW ? " · following #" + g.focus : ""}</i>`, st.html, now)) seenLines = st.n;
      // d.tests = this workspace's own live file; the snapshot's run only when it is this workspace's
      const T = d.tests || (C.tests_hidden || !sameWs ? null : (C.tests || B.tests || null));
      // GitHub Actions of this project's repos under the counters (hidden when it has none)
      const acts = sameWs ? L.actionsHtml(C.actions, Math.floor(now / 60000) * 60, 4) : "";
      put("tests", `Tests<i>${T && T.summary && !T.running ? esc(T.summary) : ""}</i>`, tests(T, cwN && cwN.name) + acts, now);
      const df = diff(B, g, zoom === "diff");
      const D = B.diff;
      put("diff", `${D ? (D.live ? "Editing" : "Last commit") : "Diff"}<i>${B.repo ? esc(B.repo) : ""}${B.changes_stat ? " · " + esc(B.changes_stat) : ""}${g.scroll ? " · scrolled up" : ""}</i>`,
        df.tabs + `<div class="bd" style="position:relative;flex:1 1 auto;min-height:0">${df.body}</div>`, now,
        (bd, html) => { bd.innerHTML = html; bd.style.display = "flex"; bd.style.flexDirection = "column"; keepLatestHunk(bd); });
      put("board", "Task board", board(C), now, flip);
      put("graph", "Git", graph(C), now);
      const planHead = d.planMd ? (planning ? `Plan<i>${esc(d.planName || "")} · say "build it" or "go" when it's right</i>` : `Plan<i>checklist</i>`) : "Plan";
      put("plan", planHead, planPane(d, g, B), now);
      // workspaces: the pill bar and the "projects" zoom
      // presentation demo (privacy): never show the real workspace names in a public room
      wsLast = document.body.classList.contains("walter-demo") ? [] : (C.workspaces || []);
      const pills = L.pillsHtml(wsLast, cur, { zoomed: zoom === "projects" });
      if (pills !== wsHtml) { wsHtml = pills; wsbar.innerHTML = pills; keepPillInView(); }
      const cw = L.currentWs(wsLast, cur);
      put("projects", `All projects<i>${cw ? "now in " + esc(cw.name) + " · " : ""}say "next project" or "back"</i>`, L.projectsHtml(wsLast, cur), now);
      rotateSay(now);
    }

    return { render, el, step, switchTo, curId };
  }

  // ---- sound cues + Walter's reactions ------------------------------------------------------------
  let actx = null;
  function tone(freqs, type, dur, vol) {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
      const t0 = actx.currentTime;
      freqs.forEach((f, i) => {
        const o = actx.createOscillator(), gn = actx.createGain();
        o.type = type; o.frequency.value = f;
        const s = t0 + i * 0.14;
        gn.gain.setValueAtTime(0, s);
        gn.gain.linearRampToValueAtTime(vol, s + 0.02);
        gn.gain.exponentialRampToValueAtTime(0.0001, s + dur);
        o.connect(gn).connect(actx.destination);
        o.start(s); o.stop(s + dur + 0.05);
      });
    } catch (e) {}
  }
  const SOUNDS = {
    pass: () => tone([784, 1175], "sine", 0.9, 0.05),
    release: () => tone([659, 988, 1319], "sine", 1.0, 0.045),
    fail: () => tone([196, 147], "triangle", 0.7, 0.06),
  };
  let prevTests = null, prevRelease = null, prevWs;
  // called by buildview.js after each data refresh while the cockpit shows
  function cues(ctx) {
    const d = ctx.data, B = d.build || {}, C = B.cockpit || {};
    if (d.testsWs !== prevWs) { prevWs = d.testsWs; prevTests = null; prevRelease = null; } // a switch is not a new run
    const T = d.tests || null;
    const cue = L.testCue(prevTests, T);
    if (T) prevTests = { when: T.when, running: !!T.running, ok: T.ok };
    const rel = (C.pipeline || []).find((s) => s.stage === "publish");
    const relNote = rel && rel.state === "ok" ? String(rel.note || "").split(" ")[0] : null;
    const released = prevRelease && relNote && relNote !== prevRelease;
    if (relNote) prevRelease = relNote;
    const ok = L.soundAllowed(new Date(), !!C.media, ctx.cfg.sound);
    const W = window.walter;
    if (cue === "pass") {
      if (ok) SOUNDS.pass();
      if (W) { W.setEmotion("happy", 6); W.addAction("bounce"); }
    } else if (cue === "fail") {
      if (ok) SOUNDS.fail();
      if (W) W.setEmotion("scared", 5); // the worried look
    }
    if (released) {
      if (ok && cue !== "pass") SOUNDS.release();
      if (W) { W.setEmotion("excited", 5); }
    }
    return cue || (released ? "release" : null);
  }

  window.WalterCockpit = { create, cues, SOUNDS };
})();
