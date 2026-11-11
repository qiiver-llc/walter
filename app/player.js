// Walter companion app: the Player (podcast on the wall speakers / YouTube in the wall browser).
// GET /api/app/player + POST /api/app/player/<action> (walter/app_player.py). Polls every
// 1500 ms only while the app is in front and the player is visible (mini bar, sheet or the
// Walter tab); otherwise a slow check every 15 s while in front, nothing while hidden.
// A Walter from before the restart that added the routes answers 404: "Needs one restart to enable".
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("miniPlayer")) return;
  const FAST = 1500, SLOW = 15000;
  const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];

  let st = { kind: "none", upnext: [] }, ok = null, timer = 0, sheetOpen = false;
  let dragging = false, volDragging = false, busy = false;
  const artCache = {};     // "/api/app/player/art/x.jpg" -> blob: URL

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function clock(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  }
  async function raw(path, json) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), json !== undefined ? 40000 : 6000);
    try {
      const r = await fetch(path, { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store", signal: ctl.signal,
        body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  async function art(img, url) {
    if (!url) { img.removeAttribute("src"); img.classList.add("none"); return; }
    if (!artCache[url]) {
      artCache[url] = "pending";
      try {
        const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
        const r = await fetch(url, { headers });
        artCache[url] = r.ok ? URL.createObjectURL(await r.blob()) : "";
      } catch (e) { artCache[url] = ""; }
    }
    const u = artCache[url];
    if (u && u !== "pending") { if (img.src !== u) img.src = u; img.classList.remove("none"); }
    else if (u === "") { img.removeAttribute("src"); img.classList.add("none"); }
  }

  // ---------------------------------------------------------------- render
  function playing() { return st.kind === "podcast" || st.kind === "youtube"; }
  function render() {
    const on = playing();
    $("miniPlayer").classList.toggle("hidden", !on || ok === false);
    $("main").classList.toggle("has-mini", on && ok !== false);
    const sub = ok === false ? "Needs one restart to enable"
      : on ? (st.paused ? "Paused: " : "Playing: ") + (st.title || "")
      : "Nothing playing";
    $("plRowSub").textContent = sub;
    if (on) {
      $("mpTitle").textContent = st.title || "";
      $("mpShow").textContent = st.kind === "youtube" ? "YouTube on the wall" : (st.show || "Podcast");
      $("mpPlay").innerHTML = st.paused ? ICON.play : ICON.pause;
      $("mpPlay").setAttribute("aria-label", st.paused ? "Play" : "Pause");
      $("mpBar").style.width = st.dur ? Math.min(100, 100 * st.pos / st.dur) + "%" : "0%";
      art($("mpArt"), st.art);
    }
    if (sheetOpen) renderSheet();
  }
  function renderSheet() {
    const on = playing();
    $("psNeeds").classList.toggle("hidden", ok !== false);
    $("psNow").classList.toggle("hidden", !on || ok === false);
    $("psIdle").classList.toggle("hidden", on || ok === false);
    if (on) {
      art($("psArt"), st.art);
      $("psArt").classList.toggle("wide", st.kind === "youtube");
      $("psShow").textContent = st.kind === "youtube" ? (st.show || "YouTube") + " (on the wall)" : st.show || "";
      $("psTitle").textContent = st.title || "";
      $("psDuck").textContent = st.ad ? "An ad is on" : st.ducked ? "Quieter while Walter listens" : "";
      if (!dragging) {
        const r = $("psSeek");
        r.max = Math.max(1, Math.round(st.dur || 0));
        r.value = Math.round(st.pos || 0);
        r.disabled = !st.dur;
        $("psPos").textContent = clock(st.pos);
        $("psLeft").textContent = st.dur ? "-" + clock(st.dur - st.pos) : "live";
      }
      $("psPlay").innerHTML = st.paused ? ICON.play : ICON.pause;
      $("psPlay").setAttribute("aria-label", st.paused ? "Play" : "Pause");
      $("psNext").classList.toggle("hidden", !st.can_next);
      document.querySelectorAll("#psSpeeds .chip").forEach((c) =>
        c.classList.toggle("on", Math.abs(parseFloat(c.dataset.sp) - (st.speed || 1)) < 0.01));
      if (!volDragging) { $("psVol").value = st.volume == null ? 60 : st.volume; $("psVolVal").textContent = $("psVol").value; }
    } else if (ok !== false) {
      const r = st.resume;
      $("psResume").classList.toggle("hidden", !r);
      if (r) $("psResumeLine").textContent = `${r.show}: ${r.title} (at ${clock(r.pos)}${r.dur ? " of " + clock(r.dur) : ""})`;
    }
    const list = st.upnext || [];
    $("psUpHead").classList.toggle("hidden", !list.length || ok === false);
    $("psUpnext").innerHTML = ok === false ? "" : list.map((i) => `
      <button class="pl-item" data-guid="${esc(i.guid)}">
        <img alt="" class="none" data-art="${esc(i.art)}">
        <span class="pl-it"><span class="pl-t">${esc(i.title)}</span>
        <span class="pl-s">${esc(i.show)}${i.resume ? " &middot; resume at " + clock(i.pos) : i.new ? " &middot; new" : ""}${i.dur ? " &middot; " + clock(i.dur) : ""}</span></span>
        <span class="pl-go">${ICON.play}</span></button>`).join("");
    $("psUpnext").querySelectorAll("img[data-art]").forEach((im) => art(im, im.dataset.art));
  }

  // ---------------------------------------------------------------- data
  async function load() {
    try {
      const r = await raw("/api/app/player");
      if (r.status === 404) { ok = false; st = { kind: "none", upnext: [] }; }
      else if (r.status === 200) { ok = true; st = r.data; }
    } catch (e) { /* offline: keep the last state */ }
    render();
  }
  async function act(action, extra) {
    if (ok === false) { A.toast("Player", "Needs one restart to enable.", "", 5); return; }
    if (busy && action !== "seek" && action !== "volume") return;
    busy = true;
    try {
      const r = await raw("/api/app/player/" + action, { kind: playing() ? st.kind : undefined, ...(extra || {}) });
      if (r.status === 404) { ok = false; }
      else if (r.data && r.data.kind) st = r.data;
      if (r.status !== 200) A.toast("Player", (r.data && r.data.error) || "That didn't work.", "err", 5);
    } catch (e) { A.toast("Player", "Can't reach Walter right now.", "err", 5); }
    finally { busy = false; }
    render();
  }

  // ---------------------------------------------------------------- polling
  function visible() {
    return sheetOpen || playing() || ($("v-walter") && $("v-walter").classList.contains("active"));
  }
  function schedule() {
    clearTimeout(timer);
    if (document.visibilityState !== "visible") return;
    timer = setTimeout(async () => { await load(); schedule(); }, visible() && ok !== false ? FAST : SLOW);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { load().then(schedule); } else clearTimeout(timer);
  });
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => setTimeout(() => { load(); schedule(); }, 50)));

  // ---------------------------------------------------------------- sheet
  function openSheet() {
    sheetOpen = true;
    $("playerSheet").classList.remove("hidden");
    renderSheet(); load(); schedule();
  }
  function closeSheet() { sheetOpen = false; $("playerSheet").classList.add("hidden"); schedule(); }
  $("mpOpen").onclick = openSheet;
  $("plRow").onclick = openSheet;
  $("psClose").onclick = closeSheet;
  $("playerSheet").addEventListener("click", (e) => { if (e.target.id === "playerSheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && sheetOpen) closeSheet(); });

  $("mpPlay").onclick = (e) => { e.stopPropagation(); act("toggle"); };
  $("psPlay").onclick = () => act("toggle");
  $("psBack").onclick = () => act("back");
  $("psFwd").onclick = () => act("forward");
  $("psNext").onclick = () => act("next");
  $("psStop").onclick = () => act("stop");
  $("psResumeBtn").onclick = () => act("play");
  $("psUpnext").addEventListener("click", (e) => {
    const b = e.target.closest(".pl-item");
    if (b) act("play", { guid: b.dataset.guid, kind: "podcast" });
  });
  $("psSpeeds").innerHTML = SPEEDS.map((s) => `<button class="chip" data-sp="${s}">${s}x</button>`).join("");
  $("psSpeeds").addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (c) act("speed", { value: parseFloat(c.dataset.sp) });
  });
  const seek = $("psSeek");
  seek.addEventListener("input", () => {
    dragging = true;
    $("psPos").textContent = clock(+seek.value);
    $("psLeft").textContent = "-" + clock((st.dur || 0) - seek.value);
  });
  seek.addEventListener("change", async () => { await act("seek", { value: +seek.value }); dragging = false; });
  const vol = $("psVol");
  vol.addEventListener("input", () => { volDragging = true; $("psVolVal").textContent = vol.value; });
  vol.addEventListener("change", async () => { await act("volume", { value: +vol.value }); volDragging = false; });

  const ICON = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="5" width="4" height="14" rx="1"/><rect x="13.5" y="5" width="4" height="14" rx="1"/></svg>',
  };

  load().then(schedule);
  window.walterPlayer = { open: openSheet, close: closeSheet, state: () => st };
})();
