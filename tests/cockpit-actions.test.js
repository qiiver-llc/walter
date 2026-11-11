// node tests/cockpit-actions.test.js : GitHub Actions lines in the cockpit's Tests card (face/cockpitlib.js)
const assert = require("assert");
const L = require("../face/cockpitlib.js");

const now = 1_000_000;
// hidden when there is nothing
assert.strictEqual(L.actionsHtml(null, now), "");
assert.strictEqual(L.actionsHtml({ items: [], noaccess: [] }, now), "");
assert.strictEqual(L.actionsHtml({}, now), "");

// dots per state, ages, no repo label for a single repo
const one = L.actionsHtml({ multi: false, items: [
  { name: "USB installer", state: "bad", repo: "org", ts: now - 120 },
  { name: "Tests", state: "ok", repo: "org", ts: now - 30 },
  { name: "Deploy", state: "run", repo: "org", ts: now - 7200 },
  { name: "Docs", state: "off", repo: "org", ts: now - 3 * 86400 }] }, now);
assert(one.includes('class="ac bad"') && one.includes('class="ac ok"') && one.includes('class="ac run"') && one.includes('class="ac off"'), one);
assert(one.includes("2m ago") && one.includes(">now<") && one.includes("2h ago") && one.includes("3d ago"), one);
assert(!one.includes('class="ar"'), "no repo label with one repo");
assert(!one.includes("more"), one);
assert(one.includes(">Actions<"));

// several repos: labels; more than 4: +N
const many = L.actionsHtml({ multi: true, items: [1, 2, 3, 4, 5, 6].map((i) => (
  { name: "wf" + i, state: i % 2 ? "ok" : "bad", repo: i < 4 ? "org" : "dev", ts: now - 60 * i })) }, now);
assert.strictEqual((many.match(/class="ac (ok|bad)"/g) || []).length, 4, many);
assert(many.includes("+2 more"), many);
assert(many.includes('<span class="ar">org</span>'), many);

// unknown state -> grey; HTML escaped
const odd = L.actionsHtml({ items: [{ name: "<img src=x onerror=1>", state: "weird", ts: now }] }, now);
assert(odd.includes('class="ac off"') && !odd.includes("<img"), odd);

// offline + no access notes
const off = L.actionsHtml({ items: [{ name: "Tests", state: "ok", ts: now - 600 }], offline_since: now - 900, noaccess: ["dev"] }, now);
assert(off.includes("offline · last seen 15m ago") && off.includes("dev: no Actions access"), off);
// only "no access" (the repo has workflows but the token can't read them): still a short note
assert(L.actionsHtml({ items: [], noaccess: ["dev"] }, now).includes("no Actions access"));

console.log("cockpit-actions ok");
