// node tests/wallsync.test.js : the app trusts the wall's own report (app/modes.js, top part)
const assert = require("assert");
global.window = {};   // the sheets below it bail out without window.walterApp
const S = require("../app/modes.js");

const wall = (o) => Object.assign({ reported: true, stale: false, age: 1, mode: "face", orientation: "portrait",
  build: { on: false, phase: "build", zoom: "" }, workspace: "home", asleep: false }, o);

// usable / fallback
assert.ok(S.usable(wall()));
assert.ok(!S.usable(wall({ stale: true })) && !S.usable({ stale: true, reported: false }) && !S.usable(null));
assert.strictEqual(S.modeOf(wall({ mode: "chat" }), "board"), "chat");
assert.strictEqual(S.modeOf(wall({ stale: true, mode: "chat" }), "board"), "board");
assert.strictEqual(S.modeOf(null, "board"), "board");

// matches
assert.ok(S.matches("mode", "board", wall({ mode: "board" })));
assert.ok(!S.matches("mode", "board", wall({ mode: "face" })));
assert.ok(S.matches("mode", "sleep", wall({ mode: "presentation", asleep: true })));
assert.ok(S.matches("build", { phase: "plan" }, wall({ mode: "build", build: { phase: "plan", zoom: "" } })));
assert.ok(!S.matches("build", { phase: "plan" }, wall({ mode: "build", build: { phase: "build", zoom: "" } })));
assert.ok(S.matches("build", { zoom: "back" }, wall({ mode: "build", build: { phase: "build", zoom: "" } })));
assert.ok(!S.matches("build", { zoom: "diff" }, wall({ mode: "conversation" })));
assert.ok(S.matches("orientation", "portrait-flipped", wall({ orientation: "portrait" })));
assert.ok(!S.matches("orientation", "landscape", wall({ orientation: "portrait" })));
assert.ok(S.matches("workspace", "acme", wall({ workspace: "acme" })));
assert.ok(!S.matches("mode", "board", wall({ mode: "board", stale: true })));   // a stale report never confirms

// pending until confirmed, else a timeout after 6 s
const p = { kind: "mode", want: "chat", at: 10000 };
assert.strictEqual(S.check(p, wall({ mode: "face" }), 12000), "pending");
assert.strictEqual(S.check(p, wall({ mode: "chat" }), 12000), "confirmed");
assert.strictEqual(S.check(p, wall({ mode: "face" }), 10000 + S.PENDING_MS), "timeout");
assert.strictEqual(S.check(p, null, 11000), "pending");
assert.strictEqual(S.check(p, wall({ stale: true, mode: "chat" }), 17000), "timeout");
assert.strictEqual(S.check(null, null, 0), "confirmed");

// reasons in the toast
assert.ok(/portrait/.test(S.reason("mode", "board", wall({ mode: "face", orientation: "landscape" }))));
assert.ok(/not responding/.test(S.reason("mode", "chat", wall({ stale: true }))));
assert.ok(/isn't reporting/.test(S.reason("mode", "chat", { reported: false, stale: true })));
assert.ok(/focus box/.test(S.reason("mode", "board", wall({ mode: "focus" }))));
assert.ok(/canvas/.test(S.reason("mode", "board", wall({ mode: "canvas" }))));
assert.ok(/presentation/.test(S.reason("mode", "board", wall({ mode: "presentation" }))));
assert.ok(/Face/.test(S.reason("mode", "chat", wall({ mode: "face" }))));
assert.ok(/project/.test(S.reason("workspace", "acme", wall())));
console.log("wallsync ok");
