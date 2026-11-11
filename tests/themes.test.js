// node tests/themes.test.js : wall themes (face/themes.js)
const assert = require("assert");
const T = require("../face/themes.js");

// ten themes, midnight first and the default
assert.deepStrictEqual(T.NAMES, ["midnight", "light", "cream", "fall", "spring", "summer", "winter", "sunset", "ocean", "forest"]);
assert.strictEqual(T.DEFAULT, "midnight");

// midnight = the original look (the values the face's CSS and canvas had before themes)
const M = T.THEMES.midnight;
assert.strictEqual(M.bg, "#05060a"); assert.strictEqual(M.text, "#e8eefc"); assert.strictEqual(M.accent, "#58c4ff");
assert.strictEqual(M.blob, "#58c4ff"); assert.deepStrictEqual(T.hexRgb(M.eye), [16, 20, 36]);
assert.strictEqual(M.fg, "#ffffff");
assert.ok(M.glow === 1 && M.radius === 1 && M.space === 1 && M.border === 1 && M.fwShift === 0);
assert.ok(/^system-ui, -apple-system, "Segoe UI", Roboto, sans-serif$/.test(M.font));

for (const n of T.NAMES) {
  const t = T.THEMES[n], v = T.vars(n);
  // every required variable, none empty
  for (const k of T.VARS) assert.ok(v[k] !== undefined && String(v[k]).trim() !== "", `${n} ${k}`);
  for (const k of T.COLOR_KEYS) assert.ok(/^#[0-9a-f]{6}$/i.test(t[k]), `${n} ${k} is a hex colour`);
  assert.ok(/^\d+,\d+,\d+$/.test(v["--accent-rgb"]), n);
  assert.ok(t.label && typeof t.light === "boolean", n);
  // layout feel within sane bounds (nothing clips, still a recognisable face)
  assert.ok(t.radius >= 0 && t.radius <= 1.7, `${n} radius`);
  assert.ok(t.space >= 0.9 && t.space <= 1.12, `${n} space`);
  assert.ok(t.border >= 0 && t.border <= 2, `${n} border`);
  assert.ok(t.glow >= 0.3 && t.glow <= 1.5, `${n} glow`);
  assert.ok([-100, 0, 100].includes(t.fwShift), `${n} weight`);
  // fonts: local / system only, no web fonts
  for (const f of [t.font, t.mono]) {
    assert.ok(!/url\(|https?:|@import/.test(f), `${n} font is local`);
    assert.ok(/(sans-serif|serif|monospace)$/.test(f), `${n} font stack ends in a generic family`);
  }
  // readable from across the room: body text >= 4.5 on surface and page
  for (const s of ["surface", "surface2", "bg"]) {
    assert.ok(T.contrast(t.text, t[s]) >= 4.5, `${n} text on ${s}: ${T.contrast(t.text, t[s]).toFixed(2)}`);
    assert.ok(T.contrast(t.fg, t[s]) >= 4.5, `${n} fg on ${s}`);
    assert.ok(T.contrast(t.dim, t[s]) >= 4.5, `${n} secondary text on ${s}`);
  }
  assert.ok(T.contrast(t.faint, t.bg) >= 3, `${n} faint text on bg`);
  // accent and status colours visible (used as text too: badges, pills)
  for (const k of ["accent", "ok", "warn", "bad"]) {
    assert.ok(T.contrast(t[k], t.surface) >= 4.5, `${n} ${k} on surface: ${T.contrast(t[k], t.surface).toFixed(2)}`);
    assert.ok(T.contrast(t[k], t.bg) >= 4.5 || (k === "accent" && T.contrast(t[k], t.bg) >= 3), `${n} ${k} on bg`);
  }
  // my eyes stand out on my body, and my body stands out from the page
  assert.ok(T.contrast(t.eye, t.blob) >= 4.5, `${n} eyes on blob`);
  assert.ok(T.contrast(t.blob, t.bg) >= 2, `${n} blob on bg`);
  // light themes really are light (dark ink), dark themes dark
  assert.strictEqual(T.lum(T.hexRgb(t.bg)) > 0.5, t.light, `${n} light flag`);
}
// light themes are different looks, not recolours: rounding / fonts differ somewhere
assert.ok(new Set(T.NAMES.map((n) => T.THEMES[n].radius)).size >= 5);
assert.ok(new Set(T.NAMES.map((n) => T.THEMES[n].font)).size >= 4);
assert.ok(T.THEMES.cream.font.includes("serif") && !T.THEMES.cream.font.includes("sans-serif"));
assert.ok(T.THEMES.cream.radius < 1 && T.THEMES.summer.radius > 1 && T.THEMES.winter.fwShift < 0);

// contrast maths
assert.ok(Math.abs(T.contrast("#000000", "#ffffff") - 21) < 1e-9);
assert.strictEqual(T.contrast("#777777", "#777777"), 1);

// ---- auto: season by month, midnight 10 PM-6 AM
const at = (m, d, h) => T.autoTheme(new Date(2026, m - 1, d, h, 0));
assert.strictEqual(at(12, 1, 12), "winter"); assert.strictEqual(at(1, 15, 9), "winter"); assert.strictEqual(at(2, 28, 21), "winter");
assert.strictEqual(at(3, 1, 12), "spring"); assert.strictEqual(at(5, 31, 12), "spring");
assert.strictEqual(at(6, 1, 12), "summer"); assert.strictEqual(at(8, 31, 12), "summer");
assert.strictEqual(at(9, 1, 12), "fall"); assert.strictEqual(at(11, 10, 9), "fall");
assert.strictEqual(at(11, 10, 22), "midnight"); assert.strictEqual(at(7, 4, 23), "midnight");
assert.strictEqual(at(7, 4, 0), "midnight"); assert.strictEqual(at(7, 4, 5), "midnight");
assert.strictEqual(at(7, 4, 6), "summer"); assert.strictEqual(at(11, 10, 21), "fall");
assert.strictEqual(T.resolve("auto", new Date(2026, 3, 2, 10)), "spring");
assert.strictEqual(T.resolve("ocean"), "ocean"); assert.strictEqual(T.resolve("OCEAN"), "ocean");
assert.strictEqual(T.resolve("nope"), "midnight"); assert.strictEqual(T.resolve(undefined), "midnight");
assert.strictEqual(T.nextName("midnight"), "light"); assert.strictEqual(T.nextName("forest"), "midnight");
assert.strictEqual(T.nextName("bogus"), "light");
for (let i = 0; i < 50; i++) { const r = T.randomName("fall"); assert.ok(T.NAMES.includes(r) && r !== "fall"); }
assert.strictEqual(T.randomName("midnight", () => 0.999999), "forest");

// ---- voice phrases
const C = T.command;
const set = (n) => ({ cmd: "set", name: n });
for (const [p, want] of [
  ["switch to the fall theme", set("fall")], ["Fall theme.", set("fall")], ["theme light", set("light")],
  ["Walter, can you switch to the autumn theme please", set("fall")], ["use the ocean theme", set("ocean")],
  ["set the theme to forest", set("forest")], ["change the theme to cream", set("cream")],
  ["midnight theme", set("midnight")], ["go back to the dark theme", set("midnight")],
  ["Summer theme!", set("summer")], ["winter theme", set("winter")], ["sunset theme", set("sunset")],
  ["spring theme", set("spring")], ["light mode theme", set("light")],
  ["next theme", { cmd: "next" }], ["Next theme please", { cmd: "next" }], ["try another theme", { cmd: "next" }],
  ["random theme", { cmd: "random" }], ["surprise me theme", { cmd: "random" }],
  ["auto theme", { cmd: "auto" }], ["put the theme back to auto", { cmd: "auto" }], ["automatic theme", { cmd: "auto" }],
  ["what themes are there", { cmd: "list" }], ["What themes are there?", { cmd: "list" }],
  ["which themes do you have", { cmd: "list" }], ["show me the themes", { cmd: "list" }], ["list themes", { cmd: "list" }],
]) assert.deepStrictEqual(C(p), want, p);
// everyday talk never switches
for (const p of ["it's going to be a cold winter", "turn the light on", "fall back an hour", "the ocean was nice",
  "what's the theme of the party", "next one", "random question", "forest fire", "", null]) {
  const c = C(p);
  assert.ok(c === null || c.cmd === "list" ? !(c && c.cmd === "set") : false, String(p));
  if (p !== "what's the theme of the party") assert.strictEqual(c, null, String(p));
}
assert.strictEqual(C("what's the theme of the party"), null);

console.log("themes ok");

// ---- the CSS rewrite (applied for every theme but midnight; midnight keeps the original rules)
const R = T.role;
assert.strictEqual(R(255, 255, 255), "fg"); assert.strictEqual(R(232, 238, 252), "fg");
assert.strictEqual(R(159, 176, 208), "text-dim"); assert.strictEqual(R(111, 127, 159), "text-faint");
assert.strictEqual(R(29, 38, 56), "line"); assert.strictEqual(R(13, 17, 27), "surface"); assert.strictEqual(R(5, 6, 10), "bg");
assert.strictEqual(R(0, 0, 0, "box-shadow"), "shadow"); assert.strictEqual(R(0, 0, 0, "text-shadow"), "halo");
assert.strictEqual(R(88, 196, 255), "accent"); assert.strictEqual(R(138, 255, 193), "ok");
assert.strictEqual(R(255, 210, 122), "warn"); assert.strictEqual(R(255, 138, 138), "bad");
assert.strictEqual(R(170, 120, 255), "accent-2");
const th = T.themed;
assert.strictEqual(th("color", "rgba(255, 255, 255, 0.55)"), "rgba(var(--fg-rgb), calc(0.55 * var(--ink, 1)))");
assert.strictEqual(th("background-color", "rgba(255, 255, 255, 0.05)"), "rgba(var(--fg-rgb), calc(0.05 * var(--surface-a, 1)))");
assert.strictEqual(th("color", "rgb(88, 196, 255)"), "rgba(var(--accent-rgb), 1)");
assert.strictEqual(th("border-top-color", "rgb(88 196 255 / 0.35)"), "rgba(var(--accent-rgb), 0.35)");
assert.strictEqual(th("background-image", "linear-gradient(rgba(5, 6, 10, 0.35) 0%, rgba(5, 6, 10, 0) 22%)"),
  "linear-gradient(rgba(var(--bg-rgb), 0.35) 0%, rgba(var(--bg-rgb), 0) 22%)");
assert.strictEqual(th("text-shadow", "rgba(0, 0, 0, 0.9) 0px 1px 6px"), "rgba(var(--halo-rgb), 0.9) 0px 1px 6px");
assert.strictEqual(th("border-top-left-radius", "3vh"), "calc(3vh * var(--radius, 1))");
assert.strictEqual(th("border-top-left-radius", "50%"), null);            // circles stay circles
assert.strictEqual(th("padding-left", "1.6vh"), "calc(1.6vh * var(--space, 1))");
assert.strictEqual(th("padding-left", "0px"), "0px");
assert.strictEqual(th("row-gap", "normal"), null);
assert.strictEqual(th("border-top-width", "1px"), "calc(1px * var(--border-scale, 1))");
assert.strictEqual(th("font-family", "system-ui, sans-serif"), "var(--font)");
assert.strictEqual(th("font-family", "ui-monospace, monospace"), "var(--font-mono)");
assert.strictEqual(th("font-weight", "600"), "calc(600 + var(--fw-shift, 0))");
assert.strictEqual(th("font-weight", "bold"), null);
assert.strictEqual(th("color", "var(--text)"), null);                   // already themed
assert.strictEqual(th("color", "transparent"), null);
assert.strictEqual(th("width", "10vh"), null);                          // sizes never change
console.log("themes css ok");
