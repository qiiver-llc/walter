// node tests/calstyle.test.js : calendar style (face/calstyle.js): count tiles + voice phrases
const assert = require("assert");
const C = require("../face/calstyle.js");

assert.deepStrictEqual(C.STYLES, ["chips", "counts"]);
assert.strictEqual(C.DEFAULT, "chips");
assert.strictEqual(C.resolve("COUNTS"), "counts");
assert.strictEqual(C.resolve("nonsense"), "chips");
assert.strictEqual(C.resolve(undefined), "chips");

const ev = (calendar, extra) => Object.assign({ e: { calendar, title: "x" } }, extra || {});
const ORDER = ["Main", "Main", "Kids", "Activities", "Partner"]; // Main first, then config order (Main repeated is fine)

// counts per calendar, fixed order whatever the event order
let r = C.tiles([ev("Kids"), ev("Partner"), ev("Kids"), ev("Main"), ev("Kids"), ev("Activities")], ORDER);
assert.deepStrictEqual(r.tiles.map((t) => [t.cal, t.n]), [["Main", 1], ["Kids", 3], ["Activities", 1], ["Partner", 1]]);
assert.strictEqual(r.more, 0);

// nothing -> no tiles; multi-day bars don't count (they keep their bar)
assert.deepStrictEqual(C.tiles([], ORDER), { tiles: [], more: 0 });
r = C.tiles([ev("Kids", { multi: true }), ev("Kids")], ORDER);
assert.deepStrictEqual(r.tiles.map((t) => [t.cal, t.n]), [["Kids", 1]]);

// unknown calendars after the known ones, by count then name; missing calendar = "Other"
r = C.tiles([ev("Zed"), ev("Alpha"), ev("Zed"), ev("Kids"), ev(undefined)], ORDER);
assert.deepStrictEqual(r.tiles.map((t) => [t.cal, t.n]), [["Kids", 1], ["Zed", 2], ["Alpha", 1], ["Other", 1]]);

// past: a tile is past only if all its events are
r = C.tiles([ev("Kids", { past: true }), ev("Kids"), ev("Main", { past: true })], ORDER);
assert.deepStrictEqual(r.tiles.map((t) => [t.cal, t.past]), [["Main", true], ["Kids", false]]);

// more than 4 calendars: 3 tiles + "+N" with the leftover events (never more than 4 boxes)
r = C.tiles([ev("Main"), ev("Kids"), ev("Activities"), ev("Partner"), ev("Partner"), ev("Club"), ev("Club"), ev("Club")], ORDER);
assert.deepStrictEqual(r.tiles.map((t) => t.cal), ["Main", "Kids", "Activities"]);
assert.strictEqual(r.more, 5);
// exactly 4: all tiles, no +N
r = C.tiles([ev("Main"), ev("Kids"), ev("Activities"), ev("Partner")], ORDER);
assert.strictEqual(r.tiles.length, 4); assert.strictEqual(r.more, 0);
// custom max
r = C.tiles([ev("Main"), ev("Kids"), ev("Activities")], ORDER, 2);
assert.deepStrictEqual([r.tiles.map((t) => t.cal), r.more], [["Main"], 2]);

// ---- voice
for (const p of ["calendar counts", "Walter, calendar counts please.", "count view", "switch to the count view",
  "show the calendar as counts", "counts view", "count tiles", "calendar count view", "switch the calendar to counts"])
  assert.deepStrictEqual(C.command(p), { cmd: "set", style: "counts" }, p);
for (const p of ["calendar chips", "chip view", "Chips view.", "calendar back to chips", "go back to the chip view"])
  assert.deepStrictEqual(C.command(p), { cmd: "set", style: "chips" }, p);
for (const p of ["toggle the calendar style", "Switch the calendar view", "change the calendar style", "flip calendar style"])
  assert.deepStrictEqual(C.command(p), { cmd: "toggle" }, p);
for (const p of ["count me in", "potato chips", "what's on the calendar", "toggle the lights", "fall theme",
  "the counts were close", "next theme", "", null])
  assert.strictEqual(C.command(p), null, String(p));

console.log("calstyle ok");
