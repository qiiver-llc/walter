// Walter companion app service worker: offline shell, versioned cache.
// The server fills in BUILD (VERSION + a hash of the app's files), so any edit
// Walter makes produces a new worker; the page then offers "Update available".
const BUILD = "__BUILD__";
const SHELL = __SHELL__;
const CACHE = "walter-app-" + BUILD;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("walter-app-") && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("message", (e) => {
  if (e.data === "skip") self.skipWaiting();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/app/ws" || url.pathname === "/app/sw.js") return;
  if (req.mode === "navigate") {
    // network first (fresh when online), the cached shell when offline
    e.respondWith(fetch(req).catch(() => caches.match("./", { ignoreSearch: true })));
    return;
  }
  if (!url.pathname.startsWith("/app/")) return;
  e.respondWith(caches.open(CACHE).then(async (c) => {
    const hit = await c.match(req, { ignoreSearch: true });
    if (hit) return hit;
    const resp = await fetch(req);
    if (resp.ok) c.put(req, resp.clone());
    return resp;
  }));
});

// Web Push: reminders and "while you were away" notices, even with the app closed.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: "Walter", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Walter", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, icon: "icons/icon-192.png", badge: "icons/icon-192.png",
    data: { url: d.url || "./" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) if (new URL(c.url).pathname.startsWith("/app")) { c.focus(); return; }
    await self.clients.openWindow(new URL("./", self.registration.scope).href);
  })());
});
