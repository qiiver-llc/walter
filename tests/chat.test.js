// node tests/chat.test.js : conversation chat view logic (face/chatlib.js) + voice open/close (convgate.js)
const assert = require("assert");
const C = require("../face/chatlib.js");
const G = require("../face/convgate.js");

// ---- open / close phrases
for (const s of ["conversation mode", "Walter, show the chat", "chat mode please", "open the chat",
  "show me the conversation", "Switch on chat mode"]) assert.deepStrictEqual(C.command(s), { cmd: "open" }, s);
for (const s of ["close the chat", "Hide the chat.", "chat mode off", "exit conversation mode", "leave chat mode",
  "turn off the chat", "close chat"]) assert.deepStrictEqual(C.command(s), { cmd: "close" }, s);
for (const s of ["what's the weather", "let's chat later", "build mode", "close", "", null]) assert.strictEqual(C.command(s), null, String(s));

// ---- view setting
assert.strictEqual(C.viewOf("chat"), "chat"); assert.strictEqual(C.viewOf("CHAT"), "chat");
assert.strictEqual(C.viewOf("default"), "default"); assert.strictEqual(C.viewOf(undefined), "default");
assert.strictEqual(C.viewOf("build"), "default");

// ---- markdown: escape first, never HTML from the text
let h = C.md('<img src=x onerror="alert(1)"> <script>bad()</script>');
assert.ok(!/<img|<script/i.test(h), h); assert.ok(h.includes("&lt;img") && h.includes("&lt;script&gt;"));
h = C.md("**Bold** and `x<y` and *soft*");
assert.strictEqual(h, "<p><b>Bold</b> and <code>x&lt;y</code> and <i>soft</i></p>");
h = C.md("Shopping:\n- eggs\n- milk\n\n1. first\n2. second");
assert.strictEqual(h, "<p>Shopping:</p><ul><li>eggs</li><li>milk</li></ul><ol><li>first</li><li>second</li></ol>");
h = C.md("```\nif a < b:\n  go()\n```");
assert.strictEqual(h, "<pre><code>if a &lt; b:\n  go()</code></pre>");
h = C.md("[the site](https://example.com) ![pic](https://example.com/x.png) https://example.com");
assert.ok(!/<a\b|href|<img|src=/i.test(h), h);
assert.ok(h.includes("the site") && h.includes("[image: pic]") && h.includes("https://example.com"), h);
h = C.md('[click](javascript:alert(1)) "quoted" it\'s');
assert.ok(!h.includes("javascript:") && h.includes("&quot;quoted&quot;") && h.includes("it&#39;s"), h);
assert.strictEqual(C.md("[happy] Ooh, nice. [action:grow]"), "<p>Ooh, nice.</p>");
assert.strictEqual(C.md("# Title"), "<p><b>Title</b></p>");
assert.strictEqual(C.md("3 * 4 * 5 = 60"), "<p>3 * 4 * 5 = 60</p>");
// a bubble: the person's words are plain escaped text (no markdown), Walter's get markdown
let b = C.bubble({ id: 1, who: "you", text: "**hi** <b>", partial: false, from: "" });
assert.ok(b.includes("**hi** &lt;b&gt;") && b.includes('class="cb y"'), b);
b = C.bubble({ id: 2, who: "walter", text: "**hi**", partial: false, from: "phone" });
assert.ok(b.includes("<b>hi</b>") && b.includes("replied on phone") && b.includes("cb w ph"), b);
b = C.bubble({ id: 3, who: "you", text: "add eggs", partial: true, from: "phone" });
assert.ok(b.includes("live") && b.includes("from phone") && b.includes("\u2026"), b);

// ---- thread
const day = new Date(2026, 10, 9, 20, 0, 0).getTime();
let T = C.createThread();
T.heard(day, "what's on", true); T.heard(day + 300, "what's on tonight", true);
assert.strictEqual(T.msgs().length, 1); assert.strictEqual(T.msgs()[0].partial, true);
T.heard(day + 900, "What's on tonight?", false);
assert.strictEqual(T.msgs().length, 1); assert.strictEqual(T.msgs()[0].partial, false);
T.said(day + 2000, "[happy] Two things."); T.said(day + 4000, "Volleyball at seven.");
T.said(day + 4200, "Volleyball at seven."); // cue + karaoke caption: shown once
assert.strictEqual(T.msgs().length, 2);
assert.strictEqual(T.msgs()[1].text, "Two things. Volleyball at seven.");
T.said(day + 5000, "- first item"); assert.strictEqual(T.msgs()[1].text, "Two things. Volleyball at seven.\n- first item");
T.endReply(); T.said(day + 9000, "Oh, and one more.");
assert.strictEqual(T.msgs().length, 3); // a new bubble after I stopped talking
T.heard(day + 10000, "thanks", false); T.said(day + 11000, "Any time.");
assert.deepStrictEqual(T.msgs().map((m) => m.who), ["you", "walter", "walter", "you", "walter"]);
// app turns join the thread, marked
T.message(day + 12000, "you", "add milk", "phone"); T.message(day + 13000, "walter", "Added milk.", "phone");
assert.strictEqual(T.msgs()[5].from, "phone"); assert.strictEqual(T.msgs()[6].who, "walter");
// a Walter sentence after an app reply starts its own bubble
T.said(day + 14000, "Hello wall."); assert.strictEqual(T.msgs().length, 8);
// stale partial dropped; a Walter sentence drops a dangling partial too
T.heard(day + 15000, "uh", true); assert.ok(T.dropPartial()); assert.strictEqual(T.msgs().length, 8);
T.heard(day + 16000, "um", true); T.said(day + 17000, "Sorry?"); assert.strictEqual(T.msgs()[T.msgs().length - 1].who, "walter");
assert.ok(!T.msgs().some((m) => m.partial));
// cap at MAX messages
T = C.createThread();
for (let i = 0; i < 30; i++) { T.heard(day + i * 10000, "q" + i, false); T.said(day + i * 10000 + 2000, "a" + i); }
assert.strictEqual(T.msgs().length, C.MAX); assert.strictEqual(T.msgs()[T.msgs().length - 1].text, "a29");
// cleared at midnight (local date change)
const next = new Date(2026, 10, 10, 0, 0, 5).getTime();
T.checkDay(next); assert.strictEqual(T.msgs().length, 0);
T.heard(next + 1000, "morning", false); assert.strictEqual(T.msgs().length, 1);
T.checkDay(next + 3600 * 1000); assert.strictEqual(T.msgs().length, 1);
// empty text ignored
assert.strictEqual(T.heard(next, "  ", false), null); assert.strictEqual(T.said(next, "[happy]"), null);
assert.strictEqual(T.message(next, "you", ""), null);

// ---- convgate voice open / close (conversation mode at once, quiet minute, mute, close)
let g = G.create({ conversation_hold_s: 8 });
assert.strictEqual(g.tick(0), false);
g.open(0); assert.strictEqual(g.tick(1), true); assert.strictEqual(g.tick(50), true);
assert.strictEqual(g.tick(61), false); assert.strictEqual(g.g.opened, false); assert.strictEqual(g.g.count, 0);
// talk keeps it up; quiet minute ends it
g.open(100); g.heard(130, false); g.state(131, "speaking"); g.state(133, "idle");
assert.strictEqual(g.tick(190), true); assert.strictEqual(g.tick(194), false);
// longer hold wins
g = G.create({ conversation_hold_s: 120 }); g.open(0); assert.strictEqual(g.tick(100), true); assert.strictEqual(g.tick(121), false);
// works with conversation mode off (explicit ask), stays opened across settings polls
g = G.create({ conversation_mode: "off" }); g.open(0); g.configure({ conversation_mode: "off" });
assert.strictEqual(g.tick(5), true);
// close ends a voice-opened one, not one that started by itself
assert.strictEqual(g.close(6), true); assert.strictEqual(g.tick(6.1), false);
g = G.create({ conversation_turns: 2 });
const ex = (t) => { g.heard(t); g.state(t + 1, "speaking"); g.state(t + 3, "idle"); };
ex(0); ex(5); assert.strictEqual(g.tick(9), true);
assert.strictEqual(g.close(9.5), false); assert.strictEqual(g.tick(10), true);
// mute drops a voice-opened one
g = G.create({}); g.open(0); g.mute(1); assert.strictEqual(g.tick(1.1), false); assert.strictEqual(g.g.opened, false);

console.log("chat: all ok");
