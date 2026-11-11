// node tests/wallstate.test.js : the wall face's own report of what it shows (face/wallstate.js)
const assert = require("assert");
const W = require("../face/wallstate.js");
const C = W.classify;

// every mode, alone
assert.strictEqual(C({}), "face");
assert.strictEqual(C({ board: true }), "board");
assert.strictEqual(C({ convo: true }), "conversation");
assert.strictEqual(C({ convo: true, chat: true }), "chat");
assert.strictEqual(C({ convo: true, convoBuild: true }), "build");
assert.strictEqual(C({ focus: true }), "focus");
assert.strictEqual(C({ sleeping: true }), "sleep");
assert.strictEqual(C({ dash: true }), "sleep");
assert.strictEqual(C({ canvas: true }), "canvas");
assert.strictEqual(C({ browser: true }), "browser");
assert.strictEqual(C({ setup: true }), "setup");
assert.strictEqual(C({ demo: true }), "presentation");

// conflicts: what wins on the screen
assert.strictEqual(C({ convo: true, convoBuild: true, chat: true }), "build");      // chat column inside the cockpit
assert.strictEqual(C({ buildSticky: true, convo: true }), "build");                  // sticky, cockpit fading in
assert.strictEqual(C({ buildSticky: true }), "face");                                // sticky but held off (e.g. muted gate)
assert.strictEqual(C({ buildSticky: true, board: true }), "board");
assert.strictEqual(C({ board: true, convo: true }), "conversation");                 // talking over the board
assert.strictEqual(C({ focus: true, convo: true, convoBuild: true }), "focus");      // the ring wins
assert.strictEqual(C({ canvas: true, focus: true }), "canvas");                      // panels hide the ring
assert.strictEqual(C({ canvas: true, browser: true }), "browser");                   // browser sits over the canvas
assert.strictEqual(C({ sleeping: true, browser: true, board: true }), "sleep");
assert.strictEqual(C({ demo: true, sleeping: true, canvas: true }), "presentation"); // the show owns the wall
assert.strictEqual(C({ setup: true, demo: true }), "setup");

// the snapshot: whitelisted words, no text, workspace id only (none in demo mode)
const s = W.snapshot({ convo: true, convoBuild: true, buildSticky: true, buildPhase: "plan", buildZoom: "diff", buildPlan: true,
  theme: "cream", themeAuto: false, board: false, layoutPref: "large", landscape: false, muted: true, workspace: "acme",
  focusStatus: "running" }, 1794412761000);
assert.strictEqual(s.t, 1794412761);
assert.strictEqual(s.mode, "build");
assert.deepStrictEqual(s.build, { on: true, sticky: true, showing: true, phase: "plan", zoom: "diff", plan: true });
assert.deepStrictEqual(s.conversation, { on: true, view: "build" });
assert.strictEqual(s.layout, "compact"); assert.strictEqual(s.layout_pref, "large");
assert.strictEqual(s.orientation, "portrait"); assert.strictEqual(s.muted, true); assert.strictEqual(s.workspace, "acme");
assert.deepStrictEqual(s.focus, { on: false, status: "running" });
const keys = ["t", "mode", "build", "theme", "theme_auto", "layout", "layout_pref", "orientation", "conversation", "focus",
  "demo", "workspace", "asleep", "muted", "canvas", "browser"];
assert.deepStrictEqual(Object.keys(s).sort(), keys.slice().sort());
// junk is cleaned, never passed through
const j = W.snapshot({ buildZoom: "kitchen", buildPhase: "x", theme: "Call the vet", workspace: "Acme Corp", layoutPref: "huge" });
assert.strictEqual(j.build.zoom, ""); assert.strictEqual(j.build.phase, "build"); assert.strictEqual(j.theme, "");
assert.strictEqual(j.workspace, null); assert.strictEqual(j.layout_pref, "auto");
assert.strictEqual(W.snapshot({ demo: true, workspace: "acme" }).workspace, null);
assert.strictEqual(W.snapshot({ landscape: true }).orientation, "landscape");
assert.strictEqual(W.snapshot({ chat: true, convo: true }).conversation.view, "chat");
// only the timestamp differs: same picture, no re-send
assert.ok(W.same(W.snapshot({ board: true }, 1000), W.snapshot({ board: true }, 9000)));
assert.ok(!W.same(W.snapshot({ board: true }, 1000), W.snapshot({}, 1000)));
assert.ok(!W.same(null, W.snapshot({})));
console.log("wallstate ok");
