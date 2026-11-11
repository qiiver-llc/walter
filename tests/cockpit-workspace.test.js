// node tests/cockpit-workspace.test.js : every cockpit card follows the current project (face/cockpitlib.js)
const assert = require("assert");
const L = require("../face/cockpitlib.js");

// files per workspace; ids are whitelisted (no paths), no workspace = the old shared files
assert.strictEqual(L.wsFile("plan", "walter-business"), "dashboard-plan-walter-business.md");
assert.strictEqual(L.wsFile("tests", "walter"), "dashboard-tests-walter.json");
assert.strictEqual(L.wsFile("plan", null), "dashboard-plan.md");
assert.strictEqual(L.wsFile("tests", null), "dashboard-tests.json");
assert.strictEqual(L.wsFile("plan", "../secret"), "dashboard-plan.md");
assert.strictEqual(L.wsFile("tests", "A B"), "dashboard-tests.json");

// the plan name: only the current workspace's
const disp = { build_plan: "", build_plans: { walter: "plan-wsview.md", "walter-business": "site-plan.md" } };
assert.strictEqual(L.planFor(disp, "walter"), "plan-wsview.md");
assert.strictEqual(L.planFor(disp, "walter-business"), "site-plan.md");
assert.strictEqual(L.planFor(disp, "home"), "");
assert.strictEqual(L.planFor({ build_plans: {} }, "walter"), "");
// an older display.json (one shared plan): the name stays, but the face only shows it when that
// workspace's own file exists (the shared file was moved to the Walter workspace)
assert.strictEqual(L.planFor({ build_plan: "old.md" }, "walter-business"), "old.md");
assert.strictEqual(L.planFor({ build_plan: "old.md" }, null), "old.md");
assert.strictEqual(L.planFor(null, "walter"), "");

// a test file only counts for its own workspace
const t = { passed: 3, ok: true, workspace: "walter" };
assert.strictEqual(L.testsFor(t, "walter"), t);
assert.strictEqual(L.testsFor(t, "walter-business"), null);
assert.strictEqual(L.testsFor({ passed: 1 }, "walter-business").passed, 1); // older file without a tag
assert.strictEqual(L.testsFor(t, null), t);
assert.strictEqual(L.testsFor(null, "walter"), null);

console.log("cockpit-workspace ok");
