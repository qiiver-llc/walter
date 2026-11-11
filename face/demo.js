/* Demo data for presentation mode (walter/present.py, `walterctl present ...`).
 *
 * While face/display.json says {"demo": true} the wall must not show ANY real personal data (a public
 * venue): this wraps window.fetch and answers the data files the face reads (the list, calendar,
 * "while you were away", workers, the loop chip, the build cockpit, sports, podcasts, the to-do
 * checklist, /api/tasks, /api/reminders) with built-in sample data for a generic household
 * (Sam and Alex). It also puts body.walter-demo on the page, which hides the usage meter, the Wi-Fi
 * chip, the canvas, the browser panel and cards. With demo off (or the flag missing) it does nothing.
 *
 * display.json "demo_board_until" (epoch s): the presentation's board step; the demo list carries it as
 * show_until, so the dashboard previews (same as `walterctl dash show N`) on a landscape screen too.
 *
 * Loaded right after faceid.js (so another face's display-<id>.json is honoured). Pure helpers are
 * exported for node (tests/demo.test.js).
 */
(function (root) {
  "use strict";

  const DAY = 86400;
  const pad = (n) => String(n).padStart(2, "0");

  // "2026-11-10T18:00:00-06:00" in local time
  function iso(d) {
    const off = -d.getTimezoneOffset();
    const s = off >= 0 ? "+" : "-";
    const a = Math.abs(off);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00` +
      `${s}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
  }
  function ymd(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
  function at(nowS, dayOff, h, m) {
    const d = new Date(nowS * 1000);
    d.setDate(d.getDate() + dayOff);
    d.setHours(h, m || 0, 0, 0);
    return d;
  }

  const CAL_COLORS = { Family: "#5fa8ff", Kids: "#6fd49a", Work: "#e9b65e", Home: "#d58fe6" };

  // [dayOffset, hour, minute, minutes long, title, calendar]  (hour < 0 = all day)
  const EVENTS = [
    [0, 7, 30, 30, "School drop-off", "Kids"],
    [0, 9, 0, 30, "Team standup", "Work"],
    [0, 12, 30, 60, "Lunch with Alex", "Family"],
    [0, 16, 0, 60, "Soccer practice", "Kids"],
    [0, 18, 0, 90, "Movie night", "Family"],
    [1, -1, 0, 0, "Recycling day", "Home"],
    [1, 8, 30, 60, "Dentist", "Family"],
    [1, 15, 45, 90, "Piano lesson", "Kids"],
    [1, 19, 0, 60, "Book club", "Family"],
    [2, 9, 0, 30, "Team standup", "Work"],
    [2, 17, 30, 90, "Soccer game", "Kids"],
    [3, 10, 0, 60, "Vet check-up", "Home"],
    [4, -1, 0, 0, "Sam's birthday", "Family"],
    [4, 18, 30, 120, "Pizza and a movie", "Family"],
    [5, 9, 0, 120, "Farmers market", "Family"],
    [6, 11, 0, 60, "Brunch with the neighbours", "Family"],
    [8, 16, 0, 60, "Soccer practice", "Kids"],
    [9, 9, 0, 30, "Team standup", "Work"],
    [10, 19, 0, 90, "School concert", "Kids"],
    [12, -1, 0, 0, "Long weekend", "Family"],
  ];

  function events(nowS) {
    return {
      updated: nowS,
      events: EVENTS.map(([off, h, m, len, title, cal]) => {
        if (h < 0) {
          const d = at(nowS, off, 0, 0);
          return { title, start: ymd(d), end: ymd(at(nowS, off + 1, 0, 0)), all_day: true, calendar: cal, outdoor: false };
        }
        const s = at(nowS, off, h, m);
        return { title, start: iso(s), end: iso(new Date(s.getTime() + len * 60000)), all_day: false, calendar: cal,
          outdoor: /soccer|market/i.test(title) };
      }),
    };
  }

  const LIST = [
    ["tasks", "Book the car in for a service"],
    ["tasks", "Order Sam's birthday cake"],
    ["tasks", "Sign Alex's field trip form"],
    ["shopping", "Milk"], ["shopping", "Coffee beans"], ["shopping", "Bananas"], ["shopping", "Birthday candles"],
    ["notes", "Soccer carpool: our turn on Thursday"],
    ["ideas", "Plant tulip bulbs before the frost"],
  ];

  const isVerse = (i) => !!i && i.section === "notes" && /^\s*verse\s*:/i.test(i.text || "");

  // The real list file -> the demo one. Keeps the settings (hours, verse switch) and a verse note; drops
  // every real item, the title, the LAN QR code and the real calendar names.
  function list(real, nowS, boardUntil) {
    real = real && typeof real === "object" ? real : {};
    const items = LIST.map(([section, text], i) => ({ id: 9001 + i, text, section, created: nowS - 3600 * (i + 1) }));
    const verse = Array.isArray(real.items) ? real.items.find(isVerse) : null;
    if (verse) items.push({ id: 9100, text: String(verse.text).slice(0, 400), section: "notes", created: nowS - 60 });
    return {
      title: "Our list", items, changed_at: 0, qr: "", url: "",
      quiet: Array.isArray(real.quiet) ? real.quiet : [22, 8], dark: Array.isArray(real.dark) ? real.dark : [23, 5],
      enabled: true, show_until: +boardUntil > nowS ? Math.round(+boardUntil) : 0,
      cal_colors: CAL_COLORS, verse: real.verse !== false,
    };
  }

  const WORKERS = [
    ["Add a recipe card to the kitchen screen", "running", 260, "Editing face/recipes.js", [["edit", "Editing face/recipes.js"], ["run", "Running the tests"]]],
    ["Morning briefing: add the school bus time", "running", 95, "Reading the calendar code", [["search", "Searching files"], ["read", "Reading the calendar code"]]],
    ["Bedtime story mode", "done", 1300, "Finished: 12 tests passing", [["git", "Committed the change"]]],
  ];

  function workers(nowS) {
    return {
      updated: nowS, max_tasks: 3, running: 2,
      workers: WORKERS.map(([title, status, el, act, log], i) => ({
        id: 41 + i, title, status, started: nowS - el, finished: status === "done" ? nowS - 600 : null, elapsed: el,
        activity: act, last_activity: nowS - 4, files: status === "done" ? 3 : i + 1, steps: 10 + i * 7,
        log: log.map(([k, s], j) => ({ t: nowS - 20 + j * 8, k, s })),
      })),
    };
  }

  function tasks(nowS) {
    return WORKERS.map(([title, status, el], i) => ({ id: 41 + i, title, status, started: nowS - el,
      result: status === "done" ? "Done: bedtime story mode works, 12 tests passing." : "" }));
  }

  function loop(nowS) {
    return { updated: nowS, focus: "Kitchen screen: recipe cards", workspace: "Home", next: "Bedtime story mode polish",
      blocked: 0, blocked_first: "", wins: 3, running: 2, queued: 0, last_run: nowS - 300 };
  }

  const COMMITS = ["face: recipe card with big, readable steps", "briefing: school bus time in the morning summary",
    "story mode: softer voice and a sleepy theme", "tests: recipe card timing", "face: smoother theme fades"];

  const DIFF = [
    ["@", "line 42"], [" ", "  function showRecipe(r) {"], ["-", "    card.textContent = r.title;"],
    ["+", "    card.innerHTML = `<h2>${esc(r.title)}</h2>`;"], ["+", "    r.steps.forEach((s, i) => addStep(i + 1, s));"],
    ["+", "    speakStep(0);   // read the first step out loud"], [" ", "  }"],
  ];

  const PLAN = "# Plan: recipe cards on the kitchen screen\n\n**Goal:** show a recipe big enough to read from the stove," +
    " and read the steps out loud.\n\n## Steps\n- [x] Recipe card layout\n- [x] Big step numbers\n- [ ] \"Next step\" by voice\n" +
    "- [ ] Timers for each step\n\n## Out of scope\n- Shopping for the ingredients (that's Sam's job)\n";

  // The real build cockpit file -> the demo one: real numbers (tests, release, gauges, pipeline lights) stay,
  // every piece of text (task titles, commit lines, worker logs, the diff, the workspace) is sample text.
  function build(real, nowS) {
    real = real && typeof real === "object" ? real : {};
    const ago = (m) => (m < 60 ? `${m} min ago` : `${Math.round(m / 60)} hours ago`);
    const commits = COMMITS.map((s, i) => ({ h: (0xa1b2c3 + i * 4099).toString(16).slice(0, 7), s, ago: ago(4 + i * 17),
      ts: nowS - (4 + i * 17) * 60 }));
    const tests = real.tests && typeof real.tests === "object" ? real.tests : { passed: 120, failed: 0, skipped: 0, ok: true };
    const rc = real.cockpit && typeof real.cockpit === "object" ? real.cockpit : {};
    const card = (w, i) => ({ id: "w:" + (41 + i), title: w[0], kind: "worker", n: 41 + i, status: w[1] });
    const pipeline = (Array.isArray(rc.pipeline) && rc.pipeline.length ? rc.pipeline : ["build", "test", "sign", "publish", "live"].map((stage) => ({ stage, state: "ok" })))
      .map((p) => ({ stage: String(p.stage || ""), state: String(p.state || "ok"),
        note: p.stage === "build" ? `${commits[0].h} ${commits[0].ago}` : p.stage === "test" ? `${tests.passed || 0} passing` : "" }));
    return {
      updated: nowS, repo: "Walter",
      shipped: commits.slice(0, 3).map((c) => ({ repo: "Walter", line: c.s, ts: c.ts, ago: c.ago })),
      release: real.release && typeof real.release === "object" ? real.release : { version: "0.1", date: "" },
      tests, changes: [], changes_stat: "", changes_more: 0,
      diff: { file: "face/recipes.js", lines: DIFF.map(([k, s]) => ({ k, s })), live: true },
      graph: commits.map((c, i) => ({ h: c.h, merge: false, ref: i === 0 ? "main" : "", s: c.s, ago: c.ago, ts: c.ts })),
      worker_log: { 41: [{ s: "Editing face/recipes.js", ago: "just now" }, { s: "Running the tests", ago: "1 min ago" }],
        42: [{ s: "Reading the calendar code", ago: "just now" }] },
      cockpit: {
        board: {
          ready: [{ id: "b:1", title: "Grocery list by voice in the kitchen", kind: "backlog", num: 12, status: "ready" },
            { id: "b:2", title: "Weekend weather summary on Fridays", kind: "backlog", num: 13, status: "ready" }],
          building: WORKERS.slice(0, 2).map(card),
          done: [{ ...card(WORKERS[2], 2), finished: nowS - 600 }],
          counts: { ready: 2, building: 2, done: 1 },
        },
        lanes: [{ repo: "Walter", commits: commits.map((c) => ({ h: c.h, s: c.s, ago: c.ago })) }],
        pipeline, tests: rc.tests && typeof rc.tests === "object" ? rc.tests : tests,
        gauges: rc.gauges && typeof rc.gauges === "object" ? { ...rc.gauges, workers: 2 } : { cpu: 30, mem: 30, load: 1, cores: 4, workers: 2 },
        media: false, workspace: { id: "home", name: "Home", walled: false }, plan: { set: true, mtime: nowS },
      },
    };
  }

  const CHECKLIST = [["Recipe cards on the kitchen screen", "in progress"], ["Bedtime story mode", "done"],
    ["School bus time in the briefing", "ready"]];

  // name of a data file the face reads -> the demo answer (null = pass the real one through)
  function answer(name, real, nowS, disp) {
    disp = disp || {};
    switch (name) {
      case "dashboard-list.json": return list(real, nowS, disp.demo_board_until);
      case "dashboard-events.json": return events(nowS);
      case "dashboard-away.json": return { items: [], unseen: 0, changed_at: 0 };
      case "dashboard-wrapup.json": return { updated: nowS, until: 0, line: "", lines: [] };
      case "dashboard-workers.json": return workers(nowS);
      case "dashboard-loop.json": return loop(nowS);
      case "dashboard-workspace.json": return { updated: nowS, face: (real && real.face) || "wall", id: "home", name: "Home", walled: false };
      case "dashboard-build.json": return build(real, nowS);
      case "dashboard-sports.json": return { teams: [], tz: (real && real.tz) || "", rows: 0, ok: true, updated: nowS, live: false };
      case "dashboard-podcast.json": return { playing: false, updated: nowS };
      case "dashboard-podcast-upnext.json": return { items: [], updated: nowS };
      case "checklist.json": return { items: CHECKLIST.map(([text, status]) => ({ text, status, done_at: status === "done" ? nowS - 900 : 0 })), changed_at: 0, show_until: 0 };
      case "focus.json": // only the show's own demo box may appear (never the owner's real task)
        return { box: real && real.box && real.box.task === "Finish the pitch deck" ? real.box : null,
                 strip: (real && real.strip) || "off", changed_at: (real && real.changed_at) || 0 };
      case "card.json": return { id: 0, title: "", body: "", until: 1 };
      case "api/tasks": return tasks(nowS);
      case "api/reminders": return [];
      default: return null;
    }
  }

  const TEXT = { "dashboard-plan.md": PLAN };

  // "./dashboard-list.json?x=1" / "/api/tasks" / "http://host:8765/api/tasks" -> "dashboard-list.json" / "api/tasks"
  function nameOf(url) {
    let u = String(url || "");
    u = u.replace(/^[a-z]+:\/\/[^/]+/i, "").split(/[?#]/)[0].replace(/^\.?\//, "").replace(/^face\//, "");
    return u;
  }

  const isDemo = (d) => !!(d && d.demo === true);

  const api = { answer, nameOf, isDemo, list, events, workers, build, TEXT, CAL_COLORS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (!root || typeof root.fetch !== "function" || !root.document) return;

  // ---------------------------------------------------------------- the page
  const orig = root.fetch.bind(root);
  let disp = {};
  let on = false;

  const css = root.document.createElement("style");
  css.textContent = "body.walter-demo #usage, body.walter-demo #wifichip, body.walter-demo:not(.walter-demo-canvas) #canvas," +
    " body.walter-demo:not(.walter-demo-browser) #browser, body.walter-demo #card { display: none !important; }";
  (root.document.head || root.document.documentElement).appendChild(css);

  function setDisp(d) {
    disp = d && typeof d === "object" ? d : {};
    on = isDemo(disp);
    const b = root.document.body;
    if (b) {
      b.classList.toggle("walter-demo", on);
      // the show's own browser step (public site only) may appear while demo_browser_until is in the future
      b.classList.toggle("walter-demo-browser", on && Number(disp.demo_browser_until || 0) > Date.now() / 1000);
      // the show's own sample slide (presentation/family-board.html) may show in the canvas panel
      b.classList.toggle("walter-demo-canvas", on && Number(disp.demo_canvas_until || 0) > Date.now() / 1000);
    }
  }
  setInterval(() => setDisp(disp), 1000);

  const jsonResponse = (obj) => new Response(JSON.stringify(obj), { status: 200, headers: { "Content-Type": "application/json" } });

  root.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    const name = nameOf(url);
    if (/^display(-[a-z0-9-]+)?\.json$/.test(name)) {
      const r = await orig(input, init);
      if (r.ok) r.clone().json().then(setDisp).catch(() => {});
      return r;
    }
    if (!on || method !== "GET") return orig(input, init);
    if (/^dashboard-plan(-[a-z0-9-]+)?\.md$/.test(name)) return new Response(PLAN, { status: 200, headers: { "Content-Type": "text/markdown" } });
    if (TEXT[name]) return new Response(TEXT[name], { status: 200, headers: { "Content-Type": "text/markdown" } });
    const nowS = Date.now() / 1000;
    if (answer(name, null, nowS, disp) === null) return orig(input, init);
    let real = null;
    if (name === "dashboard-list.json" || name === "dashboard-build.json" || name === "focus.json" ||
        name === "dashboard-workspace.json" || name === "dashboard-sports.json") {
      try { const r = await orig(input, init); if (r.ok) real = await r.json(); } catch (e) { real = null; }
    }
    return jsonResponse(answer(name, real, nowS, disp));
  };

  // our own quick poll, so the switch is near-instant (others poll display.json every ~2 s anyway)
  const poll = () => root.fetch("display.json", { cache: "no-store" }).catch(() => {});
  poll();
  setInterval(poll, 1000);
  if (!root.document.body) root.document.addEventListener("DOMContentLoaded", () => setDisp(disp));
})(typeof window !== "undefined" ? window : null);
