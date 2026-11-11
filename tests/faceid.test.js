// node tests/faceid.test.js : face id shim (face/faceid.js): own display/card files per face
const assert = require("assert");
const F = require("../face/faceid.js");

assert.strictEqual(F.faceId("?face=kitchen"), "kitchen");
assert.strictEqual(F.faceId("?lite&face=garage-2&x=1"), "garage-2");
assert.strictEqual(F.faceId("?face=wall"), "");          // the wall keeps display.json
assert.strictEqual(F.faceId(""), "");
assert.strictEqual(F.faceId("?face=../etc"), "");       // only safe ids
assert.strictEqual(F.faceId("?face=Kitchen"), "");      // ids are lower case
assert.strictEqual(F.faceId("?notface=kitchen"), "");

assert.strictEqual(F.mapUrl("display.json", "kitchen"), "display-kitchen.json");
assert.strictEqual(F.mapUrl("./card.json", "kitchen"), "card-kitchen.json");
assert.strictEqual(F.mapUrl("display.json?x=1", "kitchen"), "display-kitchen.json?x=1");
assert.strictEqual(F.mapUrl("display.json", ""), "display.json");
assert.strictEqual(F.mapUrl("/api/tasks", "kitchen"), "/api/tasks");
assert.strictEqual(F.mapUrl("dashboard-events.json", "kitchen"), "dashboard-events.json");
assert.strictEqual(F.mapUrl("old-display.json", "kitchen"), "old-display.json");

assert.strictEqual(F.basePath("/face/"), "/face/");
assert.strictEqual(F.basePath("/face"), "/face/");
assert.strictEqual(F.basePath("/"), "/");
assert.strictEqual(F.basePath("/faces"), "/");
assert.strictEqual(F.basePath("/rotate.html"), "/");
console.log("faceid ok");
