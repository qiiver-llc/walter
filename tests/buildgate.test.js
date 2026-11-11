// node tests/buildgate.test.js : build-context gate (face/buildgate.js)
const assert = require("assert");
const { create, command } = require("../face/buildgate.js");
const run = (t) => ({ workers: [{ status: "running", started: t - 60 }] });
const none = { workers: [] };

// plain chat, no workers: never
let g = create(); g.workers(0, none); g.text(0, "what's the weather like", true);
assert.strictEqual(g.tick(1), false);
// build words but no workers: still no
g.text(2, "did the commit go in", true); assert.strictEqual(g.tick(3), false);
// workers running + build words: yes
g.workers(4, run(4)); assert.strictEqual(g.tick(5), true);
// workers running but recent talk is not about building: no
g = create(); g.workers(0, run(0));
g.text(1, "how's Sam doing", true); assert.strictEqual(g.tick(2), false);
// worker finished 2 min ago counts; 4 min ago does not
g = create(); g.workers(500, { workers: [{ status: "done", started: 100, finished: 400 }] });
g.text(500, "is the fix in", true); assert.strictEqual(g.tick(501), true);
g = create(); g.workers(700, { workers: [{ status: "done", started: 100, finished: 400 }] });
g.text(700, "is the fix in", true); assert.strictEqual(g.tick(701), false);
// explicit "let's build": on without workers, off on "done building"
g = create(); g.workers(0, none); g.text(0, "OK let's build", true);
assert.strictEqual(g.tick(1), true);
g.text(10, "right, done building", true); assert.strictEqual(g.tick(11), false);
// sticky (the owner, 9 Nov 13:25): NO timeout, survives quiet, chat and workers finishing
g = create(); g.workers(0, run(0)); g.text(0, "show me the build", true);
assert.strictEqual(g.sticky(), true);
g.workers(100, { workers: [{ status: "done", started: 0, finished: 90 }] });
g.text(200, "how's Sam doing", true); g.text(300, "what's the weather", true); g.text(400, "tell me a joke", true);
assert.strictEqual(g.tick(5000), true); assert.strictEqual(g.tick(86400), true);
// exit phrases, robust to case / punctuation / longer sentences
for (const p of ["Exit build mode.", "OK, leave build mode please", "we're done building for now", "Stop building!",
  "back to the board", "Alright Walter, let's go back to the board.", "turn off build mode", "BUILD MODE OFF"]) {
  g = create(); g.text(0, "build mode", true); assert.strictEqual(g.tick(1), true, p);
  assert.deepStrictEqual(g.text(2, p, true), { cmd: "off" }, p); assert.strictEqual(g.tick(3), false, p);
  assert.strictEqual(g.sticky(), false, p);
}
for (const p of ["Build mode", "Walter, BUILD MODE.", "okay let's build something", "lets build", "Show me the build!",
  "we're building the site today", "we\u2019re building", "go into build mode please"]) {
  assert.deepStrictEqual(command(p), { cmd: "on" }, p);
  g = create(); g.workers(0, none); g.text(0, p, true); assert.strictEqual(g.tick(0.1), true, p);
}
// display.json "on" = sticky; voice exit wins until the mode changes; CLI re-run clears it
g = create(); g.setMode("on"); assert.strictEqual(g.sticky(), true);
g.text(1, "done building", true); assert.strictEqual(g.tick(2), false); assert.strictEqual(g.sticky(), false);
g.setMode("on"); assert.strictEqual(g.tick(3), false);          // same value from the poll: still exited
g.cliMode("on"); assert.strictEqual(g.tick(4), true);           // walterctl display build on again
g.text(5, "build mode", true); g.setMode("auto"); assert.strictEqual(g.sticky(), false); // walterctl ... auto
g = create(); g.text(0, "build mode", true); g.setMode("auto"); g.cliMode("auto"); assert.strictEqual(g.tick(1), false);
// off = never, even if the owner says "build mode"
g = create(); g.setMode("off"); g.text(0, "build mode", true); assert.strictEqual(g.tick(1), false);
// Escape (buildview.js calls exit)
g = create(); g.text(0, "build mode", true); g.exit(1); assert.strictEqual(g.tick(2), false);
// persisted across a face reload
g = create(); g.text(0, "build mode", true); const saved = g.save();
let g2 = create(); g2.restore(saved); g2.setMode("auto"); assert.strictEqual(g2.tick(1), true);
g = create(); g.setMode("on"); g.text(0, "done building", true);
g2 = create(); g2.restore(g.save()); g2.setMode("on"); assert.strictEqual(g2.tick(1), false);
// exit stays off while workers run, until new build talk
g = create(); g.workers(0, run(0)); g.text(0, "run the tests", true); assert.strictEqual(g.tick(1), true);
g.text(2, "exit build mode", true); assert.strictEqual(g.tick(3), false);
g.text(5, "how's the installer", true); assert.strictEqual(g.tick(6), true);
// modes
g = create(); g.setMode("on"); assert.strictEqual(g.tick(0), true);
g.setMode("off"); g.text(0, "let's build", true); assert.strictEqual(g.tick(1), false);
// commands
assert.deepStrictEqual(command("Focus worker 2."), { cmd: "focus", n: 2 });
assert.deepStrictEqual(command("zoom in on worker two"), { cmd: "focus", n: 2 });
assert.deepStrictEqual(command("show the plan"), { cmd: "pane", pane: "plan" });
assert.deepStrictEqual(command("what changed?"), { cmd: "pane", pane: "diff" });
assert.deepStrictEqual(command("scroll down"), { cmd: "scroll", dir: -1 });
assert.strictEqual(command("tell me a joke"), null);
g = create(); g.text(0, "scroll up", true); g.text(1, "scroll up", true); g.text(2, "scroll down", true); g.text(3, "scroll down", true); g.text(4, "scroll down", true);
assert.strictEqual(g.g.scroll, 0);
// Walter's own words never carry commands
g = create(); assert.strictEqual(g.text(0, "say exit build mode to leave", false), null);
console.log("buildgate ok");

// MUTE EXITS (the owner, 9 Nov 13:33)
{
  const B = require("../face/buildgate.js");
  // auto mode: sticky by voice, mute exits at once, unmute (nothing) does not re-enter, phrase re-enters
  let m = B.create(); m.setMode("auto"); m.workers(0, { workers: [{ status: "running", started: 0 }] });
  m.text(0, "build mode", true); assert.strictEqual(m.tick(1), true);
  m.mute(2); assert.strictEqual(m.sticky(), false); assert.strictEqual(m.tick(2.1), false);
  assert.strictEqual(m.tick(100), false); // still off later (workers running, old build talk cleared)
  m.text(101, "ok build mode again", true); assert.strictEqual(m.tick(102), true);
  // display.json "on": mute exits; stays off (display.json untouched) until the next explicit phrase
  m = B.create(); m.setMode("on"); assert.strictEqual(m.tick(0), true);
  m.mute(1); assert.strictEqual(m.tick(1.1), false); assert.strictEqual(m.g.mode, "on");
  m.setMode("on"); assert.strictEqual(m.tick(50), false); // same display.json value re-read: still off
  m.text(60, "let's build", true); assert.strictEqual(m.tick(61), true);
  // ...or until display.json build_mode changes
  m.mute(70); assert.strictEqual(m.tick(71), false);
  m.setMode("auto"); m.setMode("on"); assert.strictEqual(m.tick(72), true);
  // survives a face reload
  m.mute(80); const s = m.save(); const r = B.create(); r.setMode("on"); r.restore(s);
  assert.strictEqual(r.tick(81), false);
  console.log("buildgate mute: ok");
}

// T47: "dev mode" is build mode (on), "exit / leave / done with dev mode" is off
for (const p of ["dev mode", "Developer mode", "go to dev mode", "go into dev mode", "switch to dev mode", "start dev mode"])
  assert.deepStrictEqual(command(p), { cmd: "on" }, p);
for (const p of ["exit dev mode", "Leave dev mode.", "done with dev mode", "I'm done with developer mode", "turn off dev mode"])
  assert.deepStrictEqual(command(p), { cmd: "off" }, p);
g = create(); g.text(0, "go into dev mode", true); assert.strictEqual(g.tick(1), true);
g.text(2, "exit dev mode", true); assert.strictEqual(g.tick(3), false);
console.log("buildgate dev mode ok");
