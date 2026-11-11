// node tests/convgate.test.js : conversation gate (face/convgate.js)
const assert = require("assert");
const { create } = require("../face/convgate.js");

function ex(g, t, replyLen = 3) { // one exchange: the owner speaks at t, Walter replies t+1..t+1+replyLen
  g.heard(t); g.state(t, "thinking"); g.state(t + 1, "speaking"); g.state(t + 1 + replyLen, "idle");
  return t + 1 + replyLen;
}

// single quick Q&A: never active
let g = create({}); let end = ex(g, 0);
assert.strictEqual(g.tick(end + 1), false);
// three quick exchanges -> active; stays during talk; hold 8 s ends it
g = create({}); end = ex(g, 0); end = ex(g, end + 10);
assert.strictEqual(g.tick(end + 1), false);
end = ex(g, end + 10);
assert.strictEqual(g.tick(end + 1), true);
assert.strictEqual(g.tick(end + 7), true);
assert.strictEqual(g.tick(end + 9), false);
assert.strictEqual(g.g.count, 0);
// turns = 2
g = create({ conversation_turns: 2 }); end = ex(g, 0); end = ex(g, end + 5);
assert.strictEqual(g.tick(end + 1), true);
// window passing resets the counter
g = create({}); end = ex(g, 0); end = ex(g, end + 60); end = ex(g, end + 10);
assert.strictEqual(g.tick(end + 1), false);
assert.strictEqual(g.g.count, 2);
// noise / wake word only (heard, no reply) never counts; a later self-initiated reminder doesn't either
g = create({ conversation_turns: 2 }); end = ex(g, 0);
g.heard(end + 2); g.tick(end + 40); g.state(end + 41, "speaking"); g.state(end + 44, "idle");
assert.strictEqual(g.g.count, 1); assert.strictEqual(g.tick(end + 45), false);
// Walter speaking on his own (no heard turn) never counts
g = create({ conversation_turns: 2 }); for (let t = 0; t < 30; t += 5) { g.state(t, "speaking"); g.state(t + 2, "idle"); }
assert.strictEqual(g.g.count, 0);
// two speaking spans for one turn count once
g = create({ conversation_turns: 2 }); g.heard(0); g.state(1, "speaking"); g.state(2, "thinking"); g.state(3, "speaking"); g.state(4, "idle");
assert.strictEqual(g.g.count, 1);
// off
g = create({ conversation_mode: "off", conversation_turns: 2 }); end = ex(g, 0); end = ex(g, end + 2);
assert.strictEqual(g.tick(end + 1), false);
// sticky build mode: up at once, ignores counts / hold / silence, wins over mode off; exit = fresh count
g = create({}); g.setSticky(true, 0);
assert.strictEqual(g.tick(0.1), true); assert.strictEqual(g.tick(10000), true);
end = ex(g, 10001); assert.strictEqual(g.tick(end + 500), true);
g.setSticky(false, end + 501); assert.strictEqual(g.tick(end + 502), false); assert.strictEqual(g.g.count, 0);
g = create({ conversation_mode: "off" }); g.setSticky(true, 0); assert.strictEqual(g.tick(1), true);
g.setSticky(false, 2); assert.strictEqual(g.tick(3), false);
// asleep: converse.js blocks it, but the sticky flag is untouched, so it returns on wake
g = create({}); g.setSticky(true, 0); g.state(1, "sleeping"); assert.strictEqual(g.tick(5000), true);
console.log("convgate: all ok");

// MUTE EXITS (the owner, 9 Nov 13:33): at once, counter to 0, unmute does not re-enter
{
  const C = require("../face/convgate.js");
  const say = (c, t) => { c.heard(t, false); c.state(t + 1, "speaking"); c.state(t + 3, "idle"); };
  let c = C.create({}); say(c, 0); say(c, 5); say(c, 10);
  assert.strictEqual(c.tick(14), true);
  c.mute(14.1); assert.strictEqual(c.tick(14.2), false); assert.strictEqual(c.g.count, 0);
  assert.strictEqual(c.tick(15), false); assert.strictEqual(c.tick(100), false);
  // after unmute a fresh back-and-forth works normally
  say(c, 200); assert.strictEqual(c.tick(204), false); say(c, 205); say(c, 210); assert.strictEqual(c.tick(214), true);
  // sticky build mode is dropped too; settings untouched
  c = C.create({ conversation_mode: "on" }); c.setSticky(true, 0); assert.strictEqual(c.tick(1), true);
  c.mute(2); assert.strictEqual(c.tick(2.1), false); assert.strictEqual(c.cfg.mode, "on");
  console.log("convgate mute: ok");
}
