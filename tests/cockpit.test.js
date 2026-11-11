// node tests/cockpit.test.js : build mode v2 cockpit helpers (face/cockpitlib.js) + voice phrases (face/buildgate.js)
const assert = require("assert");
const L = require("../face/cockpitlib.js");
const { create, command } = require("../face/buildgate.js");

// ---- plan markdown: safe, minimal
let h = L.md("# Plan\n\n**Goal:** a cockpit\n\n- one\n- [x] done item\n- [ ] open item\n\n1. Which panels?\n2. Sound on?\n\n**Want me to build that now, or do you have more?**");
assert(h.includes("<h2 class=\"md\">Plan</h2>"), h);
assert(h.includes("<b>Goal:</b>"));
assert(h.includes('<li class="cb done">') && h.includes('<li class="cb">'));
assert(h.includes('<ol class="md" start="1">') && h.includes("Which panels?"));
assert(/class="md ask"><b>Want me to build that now/.test(h), "closing question highlighted");
assert.strictEqual((h.match(/ask/g) || []).length, 1, "only the last question");
// no HTML injection
h = L.md("# <script>alert(1)</script>\n- <img src=x onerror=alert(1)>\n[link](javascript:alert(1)) **<b>x</b>**");
assert(!/<script|<img|<b>x/.test(h), h);
assert(h.includes("&lt;script&gt;"));
// checklist for the build phase
const ck = L.checklist("# P\n- [x] **Activity** stream\n- [ ] Diff `viewer`\n1. Tests\nplain line");
assert.deepStrictEqual(ck, [{ text: "Activity stream", done: true }, { text: "Diff viewer", done: false }, { text: "Tests", done: false }]);

// ---- highlighter
assert.strictEqual(L.langOf("walter/cockpit.py"), "py");
assert.strictEqual(L.langOf("face/cockpit.js"), "js");
assert.strictEqual(L.langOf("a/b.css"), "css");
assert.strictEqual(L.langOf("index.html"), "html");
assert.strictEqual(L.langOf("README.md"), "md");
assert.strictEqual(L.langOf("bin/walterctl"), "sh");
let x = L.highlight('def f(x): return "a<b" # note', "py");
assert(x.includes('<span class="k">def</span>') && x.includes('<span class="s">&quot;a&lt;b&quot;</span>') && x.includes('<span class="c"># note</span>'), x);
x = L.highlight("const a = `t` // c", "js");
assert(x.includes('<span class="k">const</span>') && x.includes('<span class="c">// c</span>'), x);
x = L.highlight("color: #fff; /* c */", "css");
assert(x.includes('<span class="d">#fff</span>') && x.includes('<span class="c">/* c */</span>'), x);
x = L.highlight('<div class="a">x</div>', "html");
assert(x.includes('<span class="d">&lt;div</span>') && x.includes('<span class="s">&quot;a&quot;</span>'), x);
x = L.highlight('echo "$HOME" # hi', "sh");
assert(x.includes('<span class="k">echo</span>') && x.includes('<span class="c"># hi</span>'), x);
assert(!L.highlight("<script>", "zz").includes("<script>"));
assert(!L.highlight("x = '<img onerror=1>'", "py").includes("<img"));

// ---- cheat sheet + zoom phrases
for (const p of ["cheat sheet", "Walter, what can I say?", "show commands", "show me the commands", "cheatsheet please"])
  assert.deepStrictEqual(command(p), { cmd: "cheat" }, p);
for (const p of ["close", "Close it.", "close the cheat sheet", "hide the cheat sheet"])
  assert.deepStrictEqual(command(p), { cmd: "close" }, p);
for (const [p, z] of [["zoom in on diff", "diff"], ["zoom in on the tests", "tests"], ["Zoom into the board", "board"],
  ["zoom in on stream", "stream"], ["zoom in on the activity stream", "stream"], ["zoom on graph", "graph"],
  ["zoom in on the git graph", "graph"], ["zoom in on plan", "plan"], ["show the tests", "tests"], ["show me the board", "board"]])
  assert.deepStrictEqual(command(p), { cmd: "zoom", panel: z }, p);
for (const p of ["back", "Go back.", "zoom out", "back to the cockpit"]) assert.deepStrictEqual(command(p), { cmd: "back" }, p);
assert.deepStrictEqual(command("back to the board"), { cmd: "off" }); // still leaves build mode
assert.deepStrictEqual(command("zoom in on worker 2"), { cmd: "focus", n: 2 });
for (const p of ["build it", "OK, build it", "Go!", "let's go", "go ahead", "yeah go for it"]) assert.deepStrictEqual(command(p), { cmd: "go" }, p);
assert.strictEqual(command("go to the store later"), null);
assert.strictEqual(command("I'm going back home"), null);

// ---- zoom / cheat / phase state
let g = create();
g.text(0, "build mode", true);
g.text(1, "zoom in on the tests", true); assert.strictEqual(g.g.zoom, "tests");
g.text(2, "show the diff", true); assert.strictEqual(g.g.zoom, "diff");
g.text(3, "cheat sheet", true); assert.strictEqual(g.cheatOpen(4), true); assert.strictEqual(g.g.zoom, "diff");
g.text(5, "back", true); assert.strictEqual(g.cheatOpen(5), false); assert.strictEqual(g.g.zoom, "diff"); // back closes the sheet first
g.text(6, "back", true); assert.strictEqual(g.g.zoom, "");
g.text(7, "what can I say", true); assert.strictEqual(g.cheatOpen(20), true); assert.strictEqual(g.cheatOpen(28), false); // 20 s auto-close
g.text(30, "show the plan", true); assert.strictEqual(g.g.zoom, "plan");
assert.strictEqual(g.g.phase, "plan");
g.text(31, "build it", true); assert.strictEqual(g.g.phase, "build"); assert.strictEqual(g.g.zoom, "");
assert.strictEqual(g.tick(32), true, "still in build mode");

// ---- sound policy + cues
assert.strictEqual(L.soundAllowed(new Date(2026, 10, 9, 14, 0), false, "on"), true);
assert.strictEqual(L.soundAllowed(new Date(2026, 10, 9, 23, 0), false, "on"), false);
assert.strictEqual(L.soundAllowed(new Date(2026, 10, 9, 7, 59), false, "on"), false);
assert.strictEqual(L.soundAllowed(new Date(2026, 10, 9, 14, 0), true, "on"), false);
assert.strictEqual(L.soundAllowed(new Date(2026, 10, 9, 14, 0), false, "off"), false);
assert.strictEqual(L.testCue(null, { when: 5, ok: true }), null);
assert.strictEqual(L.testCue({ when: 5, running: false }, { when: 5, ok: true }), null);
assert.strictEqual(L.testCue({ when: 5, running: true }, { when: 9, ok: true }), "pass");
assert.strictEqual(L.testCue({ when: 5 }, { when: 9, ok: false }), "fail");
assert.strictEqual(L.testCue({ when: 5 }, { when: 9, running: true }), null);
assert(L.CHEAT.length >= 3 && L.SAY.includes("cheat sheet"));
console.log("cockpit ok");
// T47: the cheat sheet lists dev mode
assert.ok(L.CHEAT.some((g) => g.items.some((it) => /dev mode/.test(it[0]))), "cheat sheet has dev mode");
