// node tests/workspaces-view.test.js : the workspaces view in the build cockpit (face/cockpitlib.js pills,
// projects cards, next/previous, the stream filter) + its voice phrases and zoom state (face/buildgate.js)
const assert = require("assert");
const L = require("../face/cockpitlib.js");
const { create, command } = require("../face/buildgate.js");

const LIST = [
  { id: "home", name: "Home", current: false, walled: false, badges: { running: 0, queued: 0, ready: 0, done_today: 0 },
    usage_share: 0, budget_share: 0.2, focus: null, last_commit: null },
  { id: "acme", name: "Acme", current: true, walled: false, badges: { running: 2, queued: 1, ready: 5, done_today: 3 },
    usage_share: 0.75, budget_share: 0.6, focus: "Ship the <beta>", last_commit: { subject: "Pills & zoom", ago: "2 min ago" } },
  { id: "vault", name: "Vault", current: false, walled: true, badges: { running: 1, queued: 0, ready: 4, done_today: 0 },
    usage_share: 0.25, budget_share: 0.2, focus: null, last_commit: null },
];

// ---- pills
let h = L.pillsHtml(LIST, null);
assert.strictEqual((h.match(/class="pill/g) || []).length, 4, "three workspaces + All projects");
assert(/class="pill on" data-ws="acme"/.test(h), "current one lit");
assert(/class="pill walled" data-ws="vault"[^>]*><svg class="lock"/.test(h), "lock on the walled one");
assert(h.includes('<span class="b run" title="workers running">2</span>') && h.includes('<span class="b rdy" title="ready tasks">5</span>'));
assert(!/data-ws="home">[^]*?class="b /.test(h.split('data-ws="acme"')[0]), "no zero badges");
assert(!h.includes("tight"), "roomy with 3");
assert(h.includes('data-ws="*">All projects'));
// the current id from dashboard-workspace.json wins over the list (a switch since the last snapshot)
h = L.pillsHtml(LIST, "home");
assert(/class="pill on" data-ws="home"/.test(h) && !/class="pill on" data-ws="acme"/.test(h));
// an unknown id is ignored
assert(/class="pill on" data-ws="acme"/.test(L.pillsHtml(LIST, "gone")));
// 6 workspaces: tighter pills; names escaped
const SIX = LIST.concat([{ id: "a", name: "A<b>" }, { id: "b", name: "B" }, { id: "c", name: "C" }]);
h = L.pillsHtml(SIX, null);
assert(h.includes('class="wsp tight"') && h.includes("A&lt;b&gt;") && !h.includes("A<b>"));
assert(L.pillsHtml(SIX, null, { zoomed: true }).includes('class="pill all on"'));
assert.strictEqual(L.pillsHtml([], null), "", "no workspaces: no bar");
assert.strictEqual(L.pillsHtml(null, null), "");

// ---- next / previous project (wraps)
assert.strictEqual(L.stepWs(LIST, null, 1), "vault");
assert.strictEqual(L.stepWs(LIST, null, -1), "home");
assert.strictEqual(L.stepWs(LIST, "vault", 1), "home");
assert.strictEqual(L.stepWs(LIST, "home", -1), "vault");
assert.strictEqual(L.stepWs([LIST[0]], null, 1), null, "one workspace: nowhere to go");
assert.strictEqual(L.currentWs(LIST, "home").id, "home");

// ---- all projects cards
h = L.projectsHtml(LIST, null);
assert.strictEqual((h.match(/class="pj/g) || []).length, 3);
assert(h.includes("Ship the &lt;beta&gt;") && h.includes("Pills &amp; zoom") && h.includes("2 min ago"), "focus + last commit, escaped");
assert(/data-ws="acme"><div class="pn2">Acme<em>current<\/em>/.test(h));
const lh = h.split('data-ws="vault"')[1];
assert(lh.includes("Walled: counts only") && lh.includes("<b>1</b> running") && lh.includes("<b>4</b> ready"));
assert(h.includes("scaleX(0.750)") && h.includes("left:60%") && h.includes("75% of recent work"), "usage bar + budget marker");
// a walled workspace that IS current shows its own details
const cur = LIST.map((w) => w.id === "vault" ? Object.assign({}, w, { focus: "Own focus" }) : w);
assert(L.projectsHtml(cur, "vault").split('data-ws="vault"')[1].includes("Own focus"));
assert(L.projectsHtml([], null).includes("No workspaces yet"));

// ---- the activity stream follows the workspace
const Q = { id: "acme", walled: false }, LH = { id: "vault", walled: true };
assert(L.workerVisible({ workspace: "acme" }, Q) && !L.workerVisible({ workspace: "vault" }, Q));
assert(L.workerVisible({}, Q), "untagged: everywhere ...");
assert(!L.workerVisible({}, LH), "... except in a walled workspace");
assert(L.workerVisible({ workspace: "vault" }, LH));
assert(L.workerVisible({ workspace: "x" }, null), "no workspaces: show all");

// ---- voice phrases
assert.deepStrictEqual(command("next project"), { cmd: "ws", dir: 1 });
assert.deepStrictEqual(command("Walter, previous project."), { cmd: "ws", dir: -1 });
assert.deepStrictEqual(command("go to the next workspace"), { cmd: "ws", dir: 1 });
assert.deepStrictEqual(command("show all projects"), { cmd: "zoom", panel: "projects" });
assert.deepStrictEqual(command("show me all the projects"), { cmd: "zoom", panel: "projects" });
assert.deepStrictEqual(command("all projects"), { cmd: "zoom", panel: "projects" });
assert.deepStrictEqual(command("zoom in on projects"), { cmd: "zoom", panel: "projects" });
assert.deepStrictEqual(command("back"), { cmd: "back" });
assert.strictEqual(command("the next project we take on is big"), null, "not mid-sentence");
assert.strictEqual(command("switch to acme"), null, "the server's fast path handles names");
assert.deepStrictEqual(command("zoom in on tests"), { cmd: "zoom", panel: "tests" }, "old zooms unchanged");

// ---- zoom state
const g = create();
g.text(10, "show all projects", true);
assert.strictEqual(g.g.zoom, "projects");
g.text(11, "back", true);
assert.strictEqual(g.g.zoom, "");
const c = g.text(12, "next project", true);
assert.deepStrictEqual(c, { cmd: "ws", dir: 1 });
assert.deepStrictEqual(g.g.wsStep, { dir: 1, t: 12 });
g.text(13, "previous project", false); // Walter's own caption: never a command
assert.deepStrictEqual(g.g.wsStep, { dir: 1, t: 12 });

// ---- the cheat sheet + Say bar list the new phrases
const cheat = JSON.stringify(L.CHEAT);
assert(cheat.includes("next project / previous project") && cheat.includes("show all projects"));
assert(L.SAY.includes("next project") && L.SAY.includes("show all projects"));

console.log("workspaces-view ok");
