// node tests/demo.test.js : presentation demo data (face/demo.js)
const assert = require("assert");
const D = require("../face/demo.js");

const now = Date.UTC(2026, 10, 10, 15, 0, 0) / 1000;

// file names, however the face asks for them
assert.strictEqual(D.nameOf("dashboard-list.json"), "dashboard-list.json");
assert.strictEqual(D.nameOf("./dashboard-list.json?x=1"), "dashboard-list.json");
assert.strictEqual(D.nameOf("/api/tasks"), "api/tasks");
assert.strictEqual(D.nameOf("http://127.0.0.1:8765/api/reminders#a"), "api/reminders");
assert.strictEqual(D.nameOf("/face/checklist.json"), "checklist.json");

// the switch
assert.strictEqual(D.isDemo({ demo: true }), true);
for (const d of [null, {}, { demo: false }, { demo: "true" }, { demo: 1 }]) assert.strictEqual(D.isDemo(d), false);

// a real list full of private things never comes through
const SECRET = "PRIVATE-ITEM-XYZ";
const real = {
  title: "Private title " + SECRET, qr: "data:image/svg+xml;" + SECRET, url: "http://192.168.1.9:8765/x",
  items: [{ id: 1, text: "call " + SECRET, section: "tasks" }, { id: 2, text: "Verse: Be still.", section: "notes" }],
  cal_colors: { Main: "#fff", [SECRET]: "#000" }, quiet: [21, 7], dark: [0, 5], verse: true, show_until: now + 999,
};
const L = D.answer("dashboard-list.json", real, now, {});
const ls = JSON.stringify(L);
assert.ok(!ls.includes(SECRET) && !ls.includes("192.168"), "no real list data");
assert.ok(L.items.some((i) => i.text === "Verse: Be still."), "the verse note is fine to keep");
assert.deepStrictEqual(L.quiet, [21, 7]);
assert.strictEqual(L.qr, "");
assert.strictEqual(L.show_until, 0, "the real preview time is not used");
assert.ok(L.items.filter((i) => i.section === "shopping").length >= 3);
// the board step: the dashboard previews until demo_board_until
assert.strictEqual(D.answer("dashboard-list.json", real, now, { demo_board_until: now + 12 }).show_until, now + 12);
assert.strictEqual(D.answer("dashboard-list.json", real, now, { demo_board_until: now - 1 }).show_until, 0);

// calendar: today and tomorrow are filled, calendars all have colours, times parse
const E = D.answer("dashboard-events.json", null, now, {}).events;
assert.ok(E.length >= 10);
for (const e of E) {
  assert.ok(D.CAL_COLORS[e.calendar], e.calendar);
  assert.ok(!Number.isNaN(Date.parse(e.start)), e.start);
  if (!e.all_day) assert.ok(Date.parse(e.end) > Date.parse(e.start));
}
const today = new Date(now * 1000);
const sameDay = (s) => { const d = new Date(s.length === 10 ? s + "T12:00:00" : s); return d.toDateString() === today.toDateString(); };
assert.ok(E.filter((e) => sameDay(e.start)).length >= 3, "several events today");

// the build cockpit: real numbers stay, real text goes
const rb = { tests: { passed: 77, failed: 0, skipped: 1, ok: true }, release: { version: "9.9.9", date: "" },
  diff: { file: "secret.py", lines: [{ k: "+", s: SECRET }] }, graph: [{ h: "abc", s: SECRET }],
  shipped: [{ line: SECRET }], worker_log: { 7: [{ s: SECRET }] },
  cockpit: { board: { ready: [{ title: SECRET }], building: [], done: [] }, lanes: [{ repo: "x", commits: [{ s: SECRET }] }],
    pipeline: [{ stage: "build", state: "ok", note: SECRET }, { stage: "test", state: "fail", note: SECRET }],
    workspace: { id: "w", name: SECRET }, gauges: { cpu: 12, mem: 34, load: 1, cores: 4 } } };
const B = D.answer("dashboard-build.json", rb, now, {});
const bs = JSON.stringify(B);
assert.ok(!bs.includes(SECRET), "no real build text");
assert.strictEqual(B.tests.passed, 77);
assert.strictEqual(B.release.version, "9.9.9");
assert.strictEqual(B.cockpit.pipeline[1].state, "fail");
assert.strictEqual(B.cockpit.gauges.cpu, 12);
assert.ok(B.cockpit.board.building.length && B.cockpit.lanes[0].commits.length && B.diff.lines.length);
assert.ok(D.answer("dashboard-build.json", null, now, {}).cockpit.board, "copes with no real file");

// workers, tasks, loop: sample titles only
const W = D.answer("dashboard-workers.json", null, now, {});
assert.ok(W.workers.length >= 2 && W.workers.every((w) => typeof w.title === "string" && Array.isArray(w.log)));
assert.ok(Array.isArray(D.answer("api/tasks", null, now, {})));
assert.deepStrictEqual(D.answer("api/reminders", null, now, {}), []);
assert.strictEqual(D.answer("dashboard-loop.json", null, now, {}).workspace, "Home");
assert.deepStrictEqual(D.answer("dashboard-away.json", { items: [{ title: SECRET }] }, now, {}).items, []);
assert.deepStrictEqual(D.answer("dashboard-sports.json", { teams: [{ name: SECRET }] }, now, {}).teams, []);
assert.ok(!JSON.stringify(D.answer("dashboard-workspace.json", { name: SECRET, face: "wall" }, now, {})).includes(SECRET));
assert.ok(!JSON.stringify(D.answer("card.json", null, now, {})).includes(SECRET));
assert.ok(D.TEXT["dashboard-plan.md"].startsWith("# Plan"));

// everything else passes through untouched
for (const n of ["display.json", "dashboard-weather.json", "usage.json", "themes.js", "background.json"]) {
  assert.strictEqual(D.answer(n, {}, now, {}), null, n);
}

// no real names in the sample data (generic household only)
const all = JSON.stringify([L, E, B, W, D.TEXT]);
assert.ok(!/[a-z0-9._-]+@[a-z0-9-]+\.[a-z]/i.test(all), "no email addresses");

console.log("demo.test.js ok");
