// Face id (one brain, many faces; walter/faces.py, docs/FACES.md).
//
// The wall loads this page as "/" and reads face/display.json. Another face
// (kitchen tablet, laptop) loads it from the brain as /face/?face=<id> (a paired
// cookie, see walter/face_server.py), or as /?face=<id> on this machine for a
// preview. With a face id, display.json and card.json mean that face's OWN
// files (display-<id>.json, card-<id>.json), and the websocket is /face/ws.
// Loaded first, before walter.js; without ?face it does nothing at all.
(function (root) {
  "use strict";
  const ID_RE = /[?&]face=([a-z0-9][a-z0-9-]{0,31})(?:&|#|$)/;
  const OWN = /^(\.\/)?(display|card)\.json(\?.*)?$/;

  function faceId(search) {
    const m = ID_RE.exec(String(search || ""));
    return m && m[1] !== "wall" ? m[1] : "";
  }

  // "display.json" -> "display-kitchen.json" (relative names only; anything else untouched)
  function mapUrl(url, id) {
    if (!id || typeof url !== "string") return url;
    const m = OWN.exec(url);
    return m ? `${m[2]}-${id}.json${m[3] || ""}` : url;
  }

  function basePath(pathname) {
    return /^\/face(\/|$)/.test(String(pathname || "")) ? "/face/" : "/";
  }

  const api = { faceId, mapUrl, basePath };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (!root || !root.location) return;

  const id = faceId(root.location.search);
  const base = basePath(root.location.pathname);
  root.WALTER_FACE = { id: id || "wall", base, ws: base === "/face/" ? "/face/ws" : "/ws", home: base };
  if (!id || typeof root.fetch !== "function") return;
  const orig = root.fetch.bind(root);
  root.fetch = (input, init) => orig(typeof input === "string" ? mapUrl(input, id) : input, init);
})(typeof window !== "undefined" ? window : null);
