// Walter companion app: pairing, chat, push-to-talk, panels, notifications, updates.
// Everything goes to Walter's own server (same origin). See app/README.md.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const VERSION = document.body.dataset.version;
  const LS = {
    get(k, d) { try { const v = localStorage.getItem("walter." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("walter." + k, JSON.stringify(v)); } catch (e) { /* full or private mode */ } },
    del(k) { try { localStorage.removeItem("walter." + k); } catch (e) { /* ignore */ } },
  };
  const HISTORY_MAX = 200;
  const PCM_SR = 16000;

  let token = LS.get("token", "");
  let device = LS.get("device", null);
  let ws = null, wsOpen = false, retry = 0, loadedBuild = null;
  let face = null;
  let wall = { state: "idle", muted: false, sleeping: false };
  let chat = LS.get("history", []);
  const turns = {};          // turn -> {el, text, audio, mood, saidCount}
  let busyTurns = new Set();
  let swReg = null;
  let audioCount = 0;
  let attached = [];        // chat photos waiting to send: {blob, thumb}
  let imagesOK = false, uploading = false;

  // ------------------------------------------------------------ helpers
  function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function fmt(s) {   // tiny, safe markdown: escape first, then **bold**, `code`, links
    let h = esc(s);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    h = h.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    return h;
  }
  function timeStr(t) { return new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
  function toast(title, text, kind = "", secs = 7) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = `<div class="tt">${esc(title)}</div>${text ? `<div>${esc(text)}</div>` : ""}`;
    el.onclick = () => el.remove();
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), secs * 1000);
  }
  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (token) headers.Authorization = "Bearer " + token;
    if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
    const r = await fetch(path, { method: opts.method || (opts.json !== undefined ? "POST" : "GET"), headers, body: opts.body, cache: "no-store" });
    if (r.status === 401 && token) { unpaired("This device isn't paired any more."); throw new Error("unpaired"); }
    let data = null;
    try { data = await r.json(); } catch (e) { data = { error: await r.text().catch(() => "") }; }
    if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
    return data;
  }

  // ------------------------------------------------------------ pairing
  function showPair(msg) {
    $("main").classList.add("hidden");
    $("pair").classList.remove("hidden");
    $("pairMsg").textContent = msg || "";
    if (!face) face = new MiniFace($("pairFace"));
    face.connected = true; face.setMood("curious", 600);
    const m = location.hash.match(/pair=(\d{6})/);
    if (m) $("pairCode").value = m[1];
    if (!$("pairName").value) $("pairName").value = /iPhone|Android|Mobile/i.test(navigator.userAgent) ? "Phone" : "Computer";
  }
  function unpaired(msg) {
    token = ""; device = null;
    LS.del("token"); LS.del("device");
    if (ws) try { ws.close(); } catch (e) { /* ignore */ }
    showPair(msg);
  }
  $("pairForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("pairMsg").textContent = "";
    try {
      const r = await api("/api/app/pair", { json: { code: $("pairCode").value, name: $("pairName").value, owner: $("pairOwner").value } });
      token = r.token; device = r.device;
      LS.set("token", token); LS.set("device", device);
      chat.push({ who: "sys", text: `Paired as "${device.name}".`, t: Date.now() });
      saveHistory();
      if (location.hash) window.history.replaceState(null, "", location.pathname);
      start();
    } catch (err) {
      $("pairMsg").textContent = err.message;
    }
  });

  // ------------------------------------------------------------ chat log
  function saveHistory() {
    if (chat.length > HISTORY_MAX) chat = chat.slice(-HISTORY_MAX);
    // thumbnails are small data URLs, but localStorage is small too: keep the newest 30
    let withPics = 0;
    for (let i = chat.length - 1; i >= 0; i--) {
      const imgs = chat[i].images;
      if (imgs && imgs.length && ++withPics > 30) imgs.forEach((im) => { delete im.thumb; });
    }
    LS.set("history", chat);
  }
  function makeBubble(entry, pending) {
    const el = document.createElement("div");
    el.className = "m " + (entry.who === "me" ? "me" : entry.who === "sys" ? "sys" : "w");
    if (entry.voice) el.classList.add("voice");
    if (entry.wall) el.classList.add("wall");
    if (pending) el.classList.add("pending");
    fillBubble(el, entry);
    return el;
  }
  function bubble(entry, pending) {
    const el = makeBubble(entry, pending);
    const t = entry.t || Date.now();
    if (dayKey(t) !== lastDay) { $("log").appendChild(daySep(t)); lastDay = dayKey(t); }
    $("log").appendChild(el);
    scrollLog();
    return el;
  }
  function fillBubble(el, entry) {
    let html = entry.who === "w" ? fmt(entry.text) : esc(entry.text);
    if (entry.who === "w") {
      const c = MiniFace.color(entry.mood || "neutral");
      html = `<span class="mood" title="${esc(entry.mood || "neutral")}" style="background:rgb(${c});color:rgb(${c})"></span>` + html;
    }
    if (entry.images && entry.images.length) {
      const pics = entry.images.map((im, i) => `<img src="${esc(im.thumb || "")}" data-i="${i}" alt="Photo" loading="lazy">`).join("");
      html = `<div class="pics">${pics}</div>` + html;
    }
    if (entry.t && entry.who !== "sys") html += `<span class="meta">${timeStr(entry.t)}${srcLabel(entry.src)}</span>`;
    el.innerHTML = html;
    el._text = entry.text; el.dataset.k = entryKey(entry); el.dataset.t = entry.t || 0;
    if (entry.sid) el.dataset.sid = entry.sid;
    if (entry.images) el.querySelectorAll(".pics img").forEach((img) => {
      img.onclick = () => openLightbox(entry.images[+img.dataset.i]);
    });
  }
  function scrollLog() { const l = $("log"); requestAnimationFrame(() => { l.scrollTop = l.scrollHeight; }); }

  // ------------------------------------------------------------ full chat history (server)
  // The Chat tab is the whole history (the owner, 10 Nov): the server log (/api/app/history,
  // walter/chatlog.py: wall, app and other faces) merged with this device's own recent bubbles
  // (localStorage, which keep photos and moods). Scroll up = older pages; day separators;
  // search icon in the header; tap a message to copy. The newest page is cached for offline.
  const HIST_PAGE = 50, DUP_MS = 10 * 60 * 1000;
  const srv = new Map();          // server id -> {id, ts, role, text, source}
  (LS.get("histPage", []) || []).forEach((r) => { if (r && r.id) srv.set(r.id, r); });
  let histMore = true, histLoading = false, lastDay = null;
  function dayKey(t) { const d = new Date(t); return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(); }
  function dayLabel(t) {
    const d = new Date(t), now = new Date(), y = new Date(now.getTime() - 864e5);
    if (dayKey(t) === dayKey(now)) return "Today";
    if (dayKey(t) === dayKey(y)) return "Yesterday";
    const o = { weekday: "short", month: "short", day: "numeric" };
    if (d.getFullYear() !== now.getFullYear()) o.year = "numeric";
    return d.toLocaleDateString([], o);
  }
  function daySep(t) { const el = document.createElement("div"); el.className = "daysep"; el.textContent = dayLabel(t); return el; }
  function textKey(s) { return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 40); }
  function entryKey(e) { return (e.who === "me" ? "p" : e.who === "w" ? "w" : "s") + textKey(e.text); }
  function srcLabel(src) {
    if (!src || src === "app") return "";
    return " · " + (src === "wall" ? "on the wall" : src.startsWith("face:") ? esc(src.slice(5)) : esc(src));
  }
  function srvEntry(r) { return { who: r.role === "person" ? "me" : "w", text: r.text, t: Math.round(r.ts * 1000), sid: r.id, src: r.source }; }
  function mergedEntries() {
    const out = chat.slice();
    const seen = out.filter((e) => e.who !== "sys").map((e) => ({ k: entryKey(e), t: e.t || 0 }));
    for (const r of srv.values()) {
      const e = srvEntry(r), k = entryKey(e);
      if (seen.some((x) => x.k === k && Math.abs(x.t - e.t) < DUP_MS)) continue;   // already a local bubble
      out.push(e);
    }
    return out.sort((a, b) => (a.t || 0) - (b.t || 0));
  }
  function canRerender() {   // never while a bubble of mine or Walter's is still live (it would be detached)
    return busyTurns.size === 0 && !talkPending && pendingMine.every((p) => Date.now() - (p.entry.t || 0) > 60000);
  }
  function renderHistory(keep) {
    const log = $("log");
    const fromBottom = log.scrollHeight - log.scrollTop, atBottom = fromBottom - log.clientHeight < 80;
    log.innerHTML = ""; lastDay = null;
    const frag = document.createDocumentFragment();
    for (const e of mergedEntries()) {
      const t = e.t || Date.now();
      if (dayKey(t) !== lastDay) { frag.appendChild(daySep(t)); lastDay = dayKey(t); }
      frag.appendChild(makeBubble(e));
    }
    log.appendChild(frag);
    if (!keep || atBottom) scrollLog();
    else log.scrollTop = log.scrollHeight - fromBottom;   // older pages went on top: stay put
    markHits();
  }
  function cacheNewest() {
    const ids = Array.from(srv.keys()).sort((a, b) => b - a).slice(0, HIST_PAGE);
    LS.set("histPage", ids.map((id) => srv.get(id)));
  }
  function histOK() { return !!(token || isLocal()); }
  async function histRefresh() {   // the newest page: wall turns since we last looked
    if (!histOK()) return;
    let r;
    try { r = await api("/api/app/history?limit=" + HIST_PAGE); } catch (e) { return; }
    const known = srv.size ? Math.max(...srv.keys()) : 0;
    const items = r.items || [];
    const oldest = items.length ? items[items.length - 1].id : 0;
    let fresh = srv.size === 0;
    if (r.has_more && known && oldest > known + 1) { srv.clear(); fresh = true; }   // a gap: start over
    let added = 0;
    items.forEach((x) => { if (!srv.has(x.id)) added++; srv.set(x.id, x); });
    if (fresh) histMore = !!r.has_more;
    cacheNewest();
    if (added && canRerender()) renderHistory(true);
    fillScreen();
  }
  async function loadOlder() {
    if (histLoading || !histMore || !histOK() || !canRerender()) return false;
    histLoading = true; $("histMore").classList.remove("hidden");
    try {
      const before = srv.size ? Math.min(...srv.keys()) : 0;
      const r = await api("/api/app/history?limit=" + HIST_PAGE + (before ? "&before=" + before : ""));
      (r.items || []).forEach((x) => srv.set(x.id, x));
      histMore = !!r.has_more;
      if (canRerender()) renderHistory(true);
      return true;
    } catch (e) {
      return false;
    } finally {
      histLoading = false; $("histMore").classList.add("hidden");
    }
  }
  function fillScreen() {   // not enough to scroll yet: fetch one more page
    const log = $("log");
    if (histMore && log.scrollHeight <= log.clientHeight + 40) loadOlder();
  }
  $("log").addEventListener("scroll", () => { if ($("log").scrollTop < 300) loadOlder(); }, { passive: true });

  // tap a message to copy it (not links, photos or a text selection)
  function copyText(t) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(t);
    const ta = document.createElement("textarea");
    ta.value = t; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
    return Promise.resolve();
  }
  $("log").addEventListener("click", (e) => {
    if (e.target.closest("a, img")) return;
    const el = e.target.closest(".m");
    if (!el || el.classList.contains("sys") || !el._text || el._text === "...") return;
    if (String(window.getSelection && window.getSelection()) !== "") return;
    copyText(el._text).then(() => {
      el.classList.add("copied"); setTimeout(() => el.classList.remove("copied"), 700);
      toast("Copied", "", "", 2);
    }).catch(() => {});
  });

  // search: header icon -> bar at the top of the chat; results list; tap one to jump to it
  let searchQ = "", searchTimer = null, searchSeq = 0;
  function openSearch(on) {
    $("histSearch").classList.toggle("hidden", !on);
    $("histResults").classList.add("hidden");
    $("histSearchBtn").setAttribute("aria-pressed", on ? "true" : "false");
    if (on) { $("histQ").focus(); $("histQ").select(); }
    else { searchQ = ""; $("histQ").value = ""; $("histCount").textContent = ""; markHits(); }
  }
  $("histSearchBtn").addEventListener("click", () => openSearch($("histSearch").classList.contains("hidden")));
  $("histClose").addEventListener("click", () => openSearch(false));
  $("histQ").addEventListener("keydown", (e) => {
    if (e.key === "Escape") openSearch(false);
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); runSearch(); }
  });
  $("histQ").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 350); });
  $("histQ").addEventListener("focus", () => { if (searchQ && $("histResults").children.length) $("histResults").classList.remove("hidden"); });
  function hiliteHtml(text, q) {
    const t = String(text), i = t.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return esc(t.slice(0, 160));
    const a = Math.max(0, i - 50), b = Math.min(t.length, i + q.length + 90);
    return (a ? "…" : "") + esc(t.slice(a, i)) + "<mark>" + esc(t.slice(i, i + q.length)) + "</mark>" + esc(t.slice(i + q.length, b)) + (b < t.length ? "…" : "");
  }
  async function runSearch() {
    const q = $("histQ").value.replace(/\s+/g, " ").trim().slice(0, 100);
    const box = $("histResults");
    searchQ = q; markHits();
    if (q.length < 2) { box.classList.add("hidden"); box.innerHTML = ""; $("histCount").textContent = ""; return; }
    const seq = ++searchSeq;
    let r;
    try { r = await api("/api/app/history?limit=50&q=" + encodeURIComponent(q)); }
    catch (e) { $("histCount").textContent = navigator.onLine ? "Search failed" : "Offline"; return; }
    if (seq !== searchSeq) return;
    const items = r.items || [];
    $("histCount").textContent = items.length ? items.length + (r.has_more ? "+" : "") + " found" : "Nothing found";
    box.innerHTML = "";
    items.forEach((x) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "hit " + (x.role === "person" ? "me" : "w");
      const t = x.ts * 1000;
      b.innerHTML = `<span class="when">${esc(dayLabel(t))}, ${esc(timeStr(t))} · ${x.role === "person" ? "You" : "Walter"}</span>${hiliteHtml(x.text, q)}`;
      b.onclick = () => jumpTo(x);
      box.appendChild(b);
    });
    box.classList.toggle("hidden", !items.length);
  }
  function findBubble(x) {
    const byId = $("log").querySelector(`.m[data-sid="${+x.id}"]`);
    if (byId) return byId;
    const k = (x.role === "person" ? "p" : "w") + textKey(x.text), t = x.ts * 1000;
    let best = null, bestD = DUP_MS;
    $("log").querySelectorAll(".m").forEach((el) => {
      const d = Math.abs(+el.dataset.t - t);
      if (el.dataset.k === k && d < bestD) { best = el; bestD = d; }
    });
    return best;
  }
  async function jumpTo(x) {
    $("histResults").classList.add("hidden");
    $("histQ").blur();
    for (let i = 0; i < 60 && !srv.has(x.id) && histMore; i++) { if (!(await loadOlder())) break; }
    if (!srv.has(x.id)) { srv.set(x.id, x); if (canRerender()) renderHistory(true); }
    const el = findBubble(x);
    if (!el) { toast("Couldn't show that one", "It may be older than this chat can reach.", "err", 4); return; }
    el.scrollIntoView({ block: "center" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }
  function markHits() {   // highlight every loaded message that matches the search
    const q = searchQ.toLowerCase();
    $("log").querySelectorAll(".m").forEach((el) => el.classList.toggle("hit", q.length >= 2 && String(el._text || "").toLowerCase().includes(q)));
  }
  function updateTyping() {
    $("typing").classList.toggle("hidden", busyTurns.size === 0);
    $("stopBtn").classList.toggle("hidden", busyTurns.size === 0);
    if (busyTurns.size) scrollLog();
  }

  // ------------------------------------------------------------ websocket
  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/app/ws`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { ws.send(JSON.stringify({ t: "auth", token })); };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return onBinary(e.data);
      let m; try { m = JSON.parse(e.data); } catch (err) { return; }
      onMessage(m);
    };
    ws.onclose = (e) => {
      wsOpen = false; face && (face.connected = false);
      setStateLine();
      if (e.code === 4401 && token) { unpaired("Walter didn't recognise this device. Pair it again."); return; }
      retry = Math.min(retry + 1, 6);
      setTimeout(() => { if (token || isLocal()) connect(); }, 600 * 2 ** retry);
    };
  }
  function send(m) { if (ws && wsOpen) { ws.send(JSON.stringify(m)); return true; } return false; }
  function isLocal() { return ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname); }

  function onMessage(m) {
    switch (m.t) {
      case "auth":
        if (!m.ok) { if (m.error === "too many tries") toast("Too many tries", "Wait a few minutes.", "err"); return; }
        wsOpen = true; retry = 0; face.connected = true;
        device = m.device; if (device.id !== "kiosk") LS.set("device", device);
        $("devLine").textContent = device.id === "kiosk" ? "This is Walter's own computer (no pairing needed)." : `Paired as "${device.name}"${device.owner ? " for " + device.owner : ""}.`;
        $("unpairBtn").classList.toggle("hidden", device.id === "kiosk");
        wall.state = m.state; wall.muted = m.muted; wall.sleeping = m.sleeping;
        face.setState(m.state);
        if (m.cloud) showCloud(m.cloud);
        if (loadedBuild && loadedBuild !== m.build) updateFound();
        loadedBuild = loadedBuild || m.build;
        setStateLine();
        if (m.away_unseen != null) setAwayBadge(m.away_unseen); else loadAway(true);
        refreshPanels();
        histRefresh();
        setImagesOK((m.features || []).includes("images"));
        break;
      case "state": wall.state = m.state; face.setState(m.state); setStateLine(); break;
      case "mute": wall.muted = m.on; setStateLine(); break;
      case "cloud": showCloud(m); break;
      case "emotion": face.setMood(m.name, Math.min(m.hold || 20, 30)); break;
      case "jobs": if ($("v-walter").classList.contains("active")) loadTasks(); break;
      case "queued": onQueued(m); break;
      case "typing": if (m.on) busyTurns.add(m.turn); else busyTurns.delete(m.turn); updateTyping(); break;
      case "mood": turnOf(m.turn).mood = m.name; face.setMood(m.name); break;
      case "say": onSay(m); break;
      case "reply": onReply(m); break;
      case "noaudio": speakFallback(m.text); break;
      case "transcribing": $("recText").textContent = "Working out what you said..."; break;
      case "heard": onHeard(m); break;
      case "away": setAwayBadge(m.unseen); if ($("v-today").classList.contains("active")) loadAway(); break;
      case "notify":
        toast(m.title || (m.kind === "reminder" ? "Reminder" : "Walter"), m.text, "", m.kind === "reminder" ? 20 : 10);
        if (m.kind === "task") loadTasks();
        break;
      case "app_version": if (m.build !== loadedBuild) updateFound(); break;
      case "revoked": unpaired("This device was unpaired."); break;
    }
  }
  function setStateLine() {
    const names = { idle: "Here", listening: "Listening on the wall", thinking: "Thinking...", working: "Working...", speaking: "Talking on the wall", sleeping: "Asleep" };
    let s = wsOpen ? (names[wall.state] || wall.state) : (navigator.onLine ? "Connecting..." : "Offline");
    if (wsOpen && wall.muted) s += " - mic muted";
    $("stateLine").textContent = s;
    setTiles();
  }
  function showCloud(c) {
    const on = c.tts || c.stt || c.rt, idle = c.idle_tts || c.idle_stt;
    const el = $("cloud");
    el.classList.toggle("hidden", !on && !idle);
    el.classList.toggle("dim", !on);
    el.title = on ? "Using OpenAI for " + [c.tts && "voice", c.stt && "ears", c.rt && "realtime"].filter(Boolean).join(" and ") : "Cloud speech chosen but not available (using local)";
  }

  // ------------------------------------------------------------ turns
  let pendingMine = [];   // my bubbles waiting for their turn number
  function turnOf(n) { return (turns[n] = turns[n] || { el: null, text: "", mood: "neutral", audio: false }); }
  function onQueued(m) {
    const mine = pendingMine.find((p) => p.id === m.id) || pendingMine[0];
    if (mine) {
      pendingMine = pendingMine.filter((p) => p !== mine);
      mine.el.classList.remove("pending");
      if (m.wall) { mine.el.classList.add("wall"); mine.entry.wall = true; saveHistory(); }
      turnOf(m.turn).audio = mine.audio;
    }
    busyTurns.add(m.turn); updateTyping();
  }
  function onSay(m) {
    const t = turnOf(m.turn);
    t.text += (t.text ? " " : "") + m.text;
    if (!t.el) t.el = bubble({ who: "w", text: t.text, mood: t.mood, t: Date.now() });
    else fillBubble(t.el, { who: "w", text: t.text, mood: t.mood, t: Date.now() });
    scrollLog();
  }
  function onReply(m) {
    const t = turnOf(m.turn);
    busyTurns.delete(m.turn); updateTyping();
    const text = m.text || t.text;
    if (!text) { if (t.el) t.el.remove(); if (m.cancelled) bubble({ who: "sys", text: "Stopped." }); return; }
    const entry = { who: "w", text, mood: t.mood, t: Date.now() };
    if (!t.el) t.el = bubble(entry); else fillBubble(t.el, entry);
    chat.push(entry); saveHistory();
    scrollLog();
  }
  function sendChat(text, images) {
    const audio = LS.get("speakTyped", false);
    const id = "m" + Date.now();
    const entry = { who: "me", text, t: Date.now(), wall: wallOn };
    if (images && images.length) entry.images = images.map((im) => ({ id: im.id, thumb: im.thumb }));
    const el = bubble(entry, true);
    chat.push(entry); saveHistory();
    pendingMine.push({ id, el, entry, audio });
    const msg = { t: "chat", text, id, wall: wallOn, audio };
    if (entry.images) msg.images = entry.images.map((im) => im.id);
    if (!send(msg)) toast("Not connected", "I'll need a connection to Walter to send that.", "err");
    setWall(false);
    unlockAudio();
  }
  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("chatInput").value.trim();
    if (attached.length) { sendWithImages(v); return; }
    if (!v) return;
    $("chatInput").value = ""; autosize();
    sendChat(v);
  });
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("chatForm").requestSubmit(); }
  });
  function autosize() { const t = $("chatInput"); t.style.height = "auto"; t.style.height = Math.min(140, t.scrollHeight) + "px"; t.style.overflowY = t.scrollHeight > 140 ? "auto" : "hidden"; $("chatForm").classList.toggle("empty", !t.value.trim() && !attached.length); }
  $("chatInput").addEventListener("input", autosize);
  $("stopBtn").onclick = () => { send({ t: "stop" }); stopAudio(); };

  // ------------------------------------------------------------ chat images
  // Picked/pasted/dropped photos are downscaled here (1600px long edge, JPEG 0.85)
  // then uploaded to /api/app/upload; the chat message names them by id. History
  // keeps a small thumbnail; tapping one fetches the full image (with the token).
  const MAX_IMAGES = 4, MAX_EDGE = 1600, MAX_BYTES = 8 * 1024 * 1024;
  function setImagesOK(ok) {
    imagesOK = ok;
    const b = $("attachBtn");
    b.classList.toggle("off", !ok);
    b.title = ok ? "Attach photos (up to 4)" : "Needs one restart to enable";
  }
  $("attachBtn").onclick = () => {
    if (!imagesOK) { toast("Photos", "Needs one restart to enable.", "", 5); return; }
    if (uploading) return;
    $("attachInput").click();
  };
  $("attachInput").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };
  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => resolve({ img, url });
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode")); };
      img.src = url;
    });
  }
  function drawTo(img, edge, type, q) {
    const w = img.naturalWidth, h = img.naturalHeight;
    const k = Math.min(1, edge / Math.max(w, h));
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * k)); c.height = Math.max(1, Math.round(h * k));
    const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);   // transparent PNGs: white, not black
    g.drawImage(img, 0, 0, c.width, c.height);
    return c;
  }
  function canvasBlob(c, q) { return new Promise((res) => c.toBlob((b) => res(b), "image/jpeg", q)); }
  async function prepare(file) {
    // The browser decodes what it can (Safari decodes HEIC photos); everything becomes JPEG,
    // except small GIFs, which keep their animation.
    const { img, url } = await loadImage(file);
    try {
      const thumb = drawTo(img, 240).toDataURL("image/jpeg", 0.7);
      let blob;
      if (file.type === "image/gif" && file.size <= MAX_BYTES) blob = file;
      else blob = await canvasBlob(drawTo(img, MAX_EDGE), 0.85);
      if (!blob) throw new Error("encode");
      if (blob.size > MAX_BYTES) blob = await canvasBlob(drawTo(img, 1200), 0.7);
      return { blob, thumb };
    } finally { URL.revokeObjectURL(url); }
  }
  async function addFiles(list) {
    if (!imagesOK) { toast("Photos", "Needs one restart to enable.", "", 5); return; }
    const files = Array.from(list || []).filter((f) => /^image\//.test(f.type) || /\.(heic|heif)$/i.test(f.name || ""));
    if (!files.length) return;
    for (const f of files) {
      if (attached.length >= MAX_IMAGES) { toast("Photos", `Up to ${MAX_IMAGES} per message.`, "", 4); break; }
      try { attached.push(await prepare(f)); }
      catch (e) { toast("Can't read that photo", "This browser can't open that format. Try a screenshot or JPEG.", "err"); }
      renderTray();
    }
  }
  function renderTray(progress) {
    const tray = $("attachTray");
    tray.classList.toggle("hidden", !attached.length);
    tray.classList.toggle("busy", uploading);
    tray.innerHTML = attached.map((a, i) => `<div class="ti"><img src="${a.thumb}" alt="Photo to send">${uploading ? "" : `<button type="button" data-i="${i}" aria-label="Remove">&times;</button>`}</div>`).join("")
      + (uploading ? `<span class="prog">Uploading${progress != null ? " " + progress + "%" : "..."}</span>` : "");
    tray.querySelectorAll("button").forEach((b) => { b.onclick = () => { attached.splice(+b.dataset.i, 1); renderTray(); }; });
    autosize();
  }
  function upload(items) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      items.forEach((a, i) => fd.append("image", a.blob, `photo${i}.${a.blob.type === "image/gif" ? "gif" : "jpg"}`));
      const x = new XMLHttpRequest();
      x.open("POST", "/api/app/upload");
      if (token) x.setRequestHeader("Authorization", "Bearer " + token);
      x.upload.onprogress = (e) => { if (e.lengthComputable) renderTray(Math.round(100 * e.loaded / e.total)); };
      x.onload = () => {
        let d = {}; try { d = JSON.parse(x.responseText); } catch (e) { /* not JSON */ }
        if (x.status === 401 && token) { unpaired("This device isn't paired any more."); return reject(new Error("unpaired")); }
        if (x.status === 404) { setImagesOK(false); return reject(new Error("Needs one restart to enable.")); }
        if (x.status < 200 || x.status >= 300 || !d.ok) return reject(new Error(d.error || `HTTP ${x.status}`));
        resolve(d);
      };
      x.onerror = () => reject(new Error("Couldn't reach Walter."));
      x.send(fd);
    });
  }
  async function sendWithImages(text) {
    if (uploading) return;
    uploading = true; renderTray(0);
    try {
      const items = attached.slice();
      const d = await upload(items);
      const imgs = d.images.map((im, i) => ({ id: im.id, thumb: (items[i] || {}).thumb }));
      if (d.errors && d.errors.length) toast("Some photos didn't go", d.errors[0], "err");
      attached = []; uploading = false; renderTray();
      $("chatInput").value = ""; autosize();
      sendChat(text, imgs);
    } catch (e) {
      uploading = false; renderTray();
      if (e.message !== "unpaired") toast("Couldn't send the photos", e.message, "err");
    }
  }
  $("chatInput").addEventListener("paste", (e) => {
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
    if (files.some((f) => /^image\//.test(f.type))) { e.preventDefault(); addFiles(files); }
  });
  const chatView = $("v-chat");
  chatView.addEventListener("dragover", (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) { e.preventDefault(); chatView.classList.add("drop"); }
  });
  chatView.addEventListener("dragleave", (e) => { if (e.target === chatView || !chatView.contains(e.relatedTarget)) chatView.classList.remove("drop"); });
  chatView.addEventListener("drop", (e) => {
    chatView.classList.remove("drop");
    if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); }
  });
  let lightUrl = null;
  async function openLightbox(im) {
    if (!im) return;
    $("lightImg").src = im.thumb || "";
    $("lightbox").classList.remove("hidden");
    try {
      const headers = token ? { Authorization: "Bearer " + token } : {};
      const r = await fetch("/api/app/upload/" + encodeURIComponent(im.id), { headers, cache: "no-store" });
      if (!r.ok) { if (!im.thumb) toast("Photo", "That photo has expired (kept 7 days).", "", 4); return; }
      const b = await r.blob();
      if ($("lightbox").classList.contains("hidden")) return;
      if (lightUrl) URL.revokeObjectURL(lightUrl);
      lightUrl = URL.createObjectURL(b);
      $("lightImg").src = lightUrl;
    } catch (e) { /* offline: the thumbnail will do */ }
  }
  $("lightbox").onclick = () => {
    $("lightbox").classList.add("hidden");
    if (lightUrl) { URL.revokeObjectURL(lightUrl); lightUrl = null; }
  };
  let wallOn = false;
  function setWall(on) { wallOn = on; $("wallBtn").setAttribute("aria-pressed", String(on)); }
  $("wallBtn").onclick = () => setWall(!wallOn);

  // ------------------------------------------------------------ audio out
  let actx = null, playing = [], playHead = 0, levelTimer = null;
  function unlockAudio() {
    try {
      if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
    } catch (e) { /* no web audio */ }
  }
  document.addEventListener("pointerdown", unlockAudio, { passive: true });
  function onBinary(buf) {
    const b = new Uint8Array(buf);
    if (b[0] !== 0x57 || b[1] !== 0x41) return;   // "WA"
    const n = new DataView(buf).getUint32(2);
    const hdr = JSON.parse(new TextDecoder().decode(b.subarray(6, 6 + n)));
    if (hdr.t !== "audio") return;
    audioCount++;
    playWav(buf.slice(6 + n), hdr.text);
  }
  async function playWav(wav, text) {
    unlockAudio();
    if (!actx) return speakFallback(text);
    let ab;
    try { ab = await actx.decodeAudioData(wav); } catch (e) { return speakFallback(text); }
    const src = actx.createBufferSource();
    src.buffer = ab;
    const an = actx.createAnalyser(); an.fftSize = 256;
    src.connect(an); an.connect(actx.destination);
    const startAt = Math.max(actx.currentTime + 0.05, playHead);
    playHead = startAt + ab.duration + 0.12;
    src.start(startAt);
    playing.push(src);
    const showAt = (startAt - actx.currentTime) * 1000;
    setTimeout(() => { caption(text); meter(an); }, showAt);
    src.onended = () => {
      playing = playing.filter((p) => p !== src);
      if (!playing.length) { caption(""); face.setLevel(0); }
    };
  }
  function meter(an) {
    clearInterval(levelTimer);
    const d = new Uint8Array(an.fftSize);
    levelTimer = setInterval(() => {
      if (!playing.length) { clearInterval(levelTimer); face.setLevel(0); return; }
      an.getByteTimeDomainData(d);
      let s = 0; for (let i = 0; i < d.length; i++) s = Math.max(s, Math.abs(d[i] - 128));
      face.setLevel(Math.min(1, s / 70));
    }, 50);
  }
  function stopAudio() {
    playing.forEach((p) => { try { p.stop(); } catch (e) { /* ended */ } });
    playing = []; playHead = 0; caption(""); face.setLevel(0);
    if (window.speechSynthesis) speechSynthesis.cancel();
  }
  function speakFallback(text) {
    if (!window.speechSynthesis || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    u.onstart = () => caption(text);
    u.onend = () => caption("");
    speechSynthesis.speak(u);
  }
  function caption(text) { const c = $("caption"); c.textContent = text; c.classList.toggle("hidden", !text); }

  // ------------------------------------------------------------ talk (mic)
  // Hold the mic button to talk (release sends), or tap once to start and tap again to send.
  let rec = null;   // {stream, ctx, node, src, t0, mode}
  const micBtn = $("micBtn");
  function micAvailable() { return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia); }
  if (!micAvailable()) {
    micBtn.classList.add("off");
    micBtn.title = "The microphone needs a secure (https) address. Walter can explain how to set one up; until then, type.";
  }
  const talkFab = $("talkFab");
  const micBtns = [micBtn, talkFab];
  let downAt = 0;
  if (!micAvailable()) { talkFab.classList.add("off"); talkFab.title = micBtn.title; }
  micBtns.forEach((btn) => btn.addEventListener("pointerdown", async (e) => {
    e.preventDefault();
    unlockAudio();
    if (!micAvailable()) {
      toast("Microphone unavailable here", "Phones only allow the mic on a secure (https) address. Walter's setup notes explain how to get one; until then, type.", "err", 10);
      return;
    }
    if (rec && rec.mode === "tap") { finishTalk(); return; }
    if (rec) return;
    downAt = performance.now();
    haptic(15);
    await startTalk();
  }));
  const release = () => {
    if (!rec || rec.mode === "tap") return;
    if (performance.now() - downAt < 350) { rec.mode = "tap"; $("recText").textContent = "Listening... tap the mic to send"; return; }
    finishTalk();
  };
  micBtns.forEach((btn) => {
    btn.addEventListener("pointerup", release);
    btn.addEventListener("pointerleave", (e) => { if (e.pointerType !== "mouse") release(); });
    btn.addEventListener("pointercancel", release);
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
  });
  $("recCancel").onclick = () => cancelTalk();

  async function startTalk() {
    stopAudio();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (err) {
      toast("No microphone", err.name === "NotAllowedError" ? "Allow the microphone for this page in your browser settings." : err.message, "err", 8);
      return;
    }
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    rec = { stream, ctx, src, node: null, t0: performance.now(), mode: "hold", bytes: 0 };
    send({ t: "talk_start", sr: PCM_SR });
    const onPcm = (ab) => {
      if (!rec) return;
      const out = new Uint8Array(1 + ab.byteLength);
      out[0] = 1; out.set(new Uint8Array(ab), 1);
      rec.bytes += ab.byteLength;
      if (ws && wsOpen) ws.send(out);
    };
    try {
      await ctx.audioWorklet.addModule("pcm-worklet.js");
      const node = new AudioWorkletNode(ctx, "pcm-down");
      node.port.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) onPcm(e.data);
        else if (e.data && typeof e.data.level === "number") face.setLevel(Math.min(1, e.data.level * 2));
      };
      src.connect(node);
      rec.node = node;
    } catch (err) {
      // older browsers: ScriptProcessor fallback, same 16 kHz int16 output
      const node = ctx.createScriptProcessor(4096, 1, 1);
      const ratio = ctx.sampleRate / PCM_SR;
      node.onaudioprocess = (e) => {
        const x = e.inputBuffer.getChannelData(0);
        const n = Math.floor(x.length / ratio), o = new Int16Array(n);
        for (let i = 0; i < n; i++) o[i] = Math.max(-32768, Math.min(32767, x[Math.floor(i * ratio)] * 32767));
        onPcm(o.buffer);
      };
      src.connect(node); node.connect(ctx.destination);
      rec.node = node;
    }
    micBtns.forEach((b) => b.classList.add("rec"));
    $("main").classList.add("recording");
    $("recBar").classList.remove("hidden");
    $("recText").textContent = "Listening... release to send";
    rec.timer = setInterval(() => {
      if (!rec) return;
      const s = (performance.now() - rec.t0) / 1000;
      if (s > 59) finishTalk();
    }, 250);
  }
  function stopMic() {
    if (!rec) return null;
    const r = rec; rec = null;
    clearInterval(r.timer);
    try { r.node && r.node.port && r.node.port.postMessage("flush"); } catch (e) { /* ignore */ }
    return new Promise((res) => setTimeout(() => {
      try { r.src.disconnect(); r.node && r.node.disconnect(); } catch (e) { /* ignore */ }
      r.stream.getTracks().forEach((t) => t.stop());
      r.ctx.close().catch(() => {});
      micBtns.forEach((b) => b.classList.remove("rec"));
      $("main").classList.remove("recording");
      face.setLevel(0);
      res(r);
    }, 120));
  }
  let talkPending = null;
  async function finishTalk() {
    const r = await stopMic();
    if (!r) return;
    $("recText").textContent = "Sending...";
    if (curView !== "chat") go("chat");   // talked from another tab: show the answer
    const id = "v" + Date.now();
    const audio = LS.get("speakTalk", true);
    const entry = { who: "me", text: "...", t: Date.now(), voice: true, wall: wallOn };
    const el = bubble(entry, true);
    talkPending = { id, el, entry, audio };
    send({ t: "talk_end", id, wall: wallOn, audio });
    setWall(false);
  }
  async function cancelTalk() {
    await stopMic();
    send({ t: "talk_cancel" });
    $("recBar").classList.add("hidden");
  }
  function onHeard(m) {
    $("recBar").classList.add("hidden");
    const p = talkPending; talkPending = null;
    if (!m.text) {
      if (p) p.el.remove();
      toast("Didn't catch that", m.error || "Try again a bit closer to the mic.", "err");
      return;
    }
    if (p) {
      p.entry.text = m.text;
      fillBubble(p.el, p.entry);
      chat.push(p.entry); saveHistory();
      pendingMine.push({ id: p.id, el: p.el, entry: p.entry, audio: p.audio });
    }
  }

  // ------------------------------------------------------------ panels
  // Navigation. Four tabs (chat is home) plus two sub-screens of the Walter tab (remote,
  // settings). The phone's back gesture works: history depth 0 = chat, 1 = another tab
  // (or a sub-screen opened straight from chat), 2 = a sub-screen opened from a tab.
  // Back from a tab goes to chat; back from chat leaves the app as usual.
  const TABS = ["chat", "today", "list", "walter"];
  const PARENT = { remote: "walter", settings: "walter", addons: "walter" };
  const reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let curView = "chat", navPending = null;
  function showView(view) {
    if (!$("v-" + view)) view = "chat";
    const changed = view !== curView;
    curView = view;
    const tab = PARENT[view] || view;
    document.querySelectorAll("#tabs button").forEach((x) => {
      const on = x.dataset.view === tab;
      x.classList.toggle("active", on);
      if (on) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
    });
    document.querySelectorAll(".view").forEach((v) => {
      const on = v.id === "v-" + view;
      v.classList.toggle("active", on);
      if (on && changed && !reduceMotion) { v.classList.remove("enter"); void v.offsetWidth; v.classList.add("enter"); }
    });
    $("main").dataset.view = view;
    if (TABS.includes(view)) LS.set("tab", view);
    if (view === "settings") { checkRestart(); checkTextScale(); checkWorkers(); loadFeedback(); checkPresent(); checkCloudLink(); }
    if (view === "settings" || view === "walter") checkLive();
    refreshPanels();
  }
  function navDepth() { return (window.history.state && window.history.state.d) || 0; }
  function go(view) {
    if (view === curView) {   // tapping the current tab scrolls it back to the top
      const v = $("v-" + view); if (v) v.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
      if (view === "chat") scrollLog();
      return;
    }
    const d = navDepth();
    if (PARENT[view]) { window.history.pushState({ v: view, d: d + 1 }, ""); showView(view); return; }
    const want = view === "chat" ? 0 : 1;
    if (d > want) { navPending = view; window.history.go(want - d); return; }   // popstate finishes
    if (d === want) window.history.replaceState({ v: view, d }, "");
    else window.history.pushState({ v: view, d: 1 }, "");
    showView(view);
  }
  window.addEventListener("popstate", (e) => {
    const s = e.state || { v: "chat", d: 0 };
    if (navPending) {
      const v = navPending; navPending = null;
      if (v !== s.v) window.history.replaceState({ v, d: s.d }, "");
      showView(v); return;
    }
    showView(s.v || "chat");
  });
  function haptic(ms) { try { if (navigator.vibrate) navigator.vibrate(ms || 8); } catch (e) { /* not allowed */ } }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => { haptic(); go(b.dataset.view); }));
  document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => { haptic(); go(b.dataset.go); }));
  document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => {
    if (navDepth() > 0) window.history.back(); else go(PARENT[curView] || "chat");
  }));
  $("rmPill").addEventListener("click", () => { haptic(); go("remote"); });

  // Is the wall browser open? Shows the "Wall browser" pill in the header (a shortcut to
  // the remote) and the state on the Walter tab's remote row. Cheap: no peek, no frames.
  let wallBrowserOpen = false, rmTimer = 0;
  async function checkWallBrowser() {
    if (!wsOpen || document.hidden) return;
    let d; try { d = await api("/api/app/remote/state"); } catch (e) { return; }
    wallBrowserOpen = !!d.open;
    $("rmPill").classList.toggle("hidden", !wallBrowserOpen || curView === "remote");
    $("rmRowSub").textContent = wallBrowserOpen ? "Open now" + (d.title ? ": " + d.title : "") : "Browse on the wall from here";
  }
  function watchWallBrowser() { clearInterval(rmTimer); checkWallBrowser(); rmTimer = setInterval(checkWallBrowser, 15000); }

  // the talk button floats over Today / List / Walter; hide it while the keyboard is up
  document.addEventListener("focusin", (e) => { if (e.target.matches("input, textarea, select")) $("main").classList.add("kb"); });
  document.addEventListener("focusout", () => setTimeout(() => {
    const a = document.activeElement;
    if (!a || !a.matches("input, textarea, select")) $("main").classList.remove("kb");
  }, 50));

  function refreshPanels() {
    const v = (document.querySelector(".view.active") || {}).id;
    if (v === "v-list") loadList();
    else if (v === "v-today") { $("todayDate").textContent = new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }); loadAway(); loadEvents(); }
    else if (v === "v-walter") { loadTasks(); loadBacklog(); setTiles(); }
    else if (v === "v-settings") pushState();
    else if (v === "v-chat") scrollLog();
    if (window.walterRemote) window.walterRemote.active(v === "v-remote");   // remote.js
    if (v === "v-remote") $("rmPill").classList.add("hidden"); else checkWallBrowser();
  }

  async function loadList() {
    try { renderList(await api("/api/app/list")); } catch (e) { /* offline */ }
  }
  function renderList(d) {
    $("listTitle").textContent = d.title || "My list";
    const body = $("listBody"); body.innerHTML = "";
    const names = { tasks: "Tasks", inbox: "Inbox", shopping: "Shopping", notes: "Notes", ideas: "Parked ideas" };
    for (const sec of d.sections || ["tasks", "shopping", "notes", "ideas"]) {
      const items = (d.items || []).filter((i) => i.section === sec);
      if (!items.length) continue;
      const h = document.createElement("div"); h.className = "sec"; h.textContent = names[sec] || sec; body.appendChild(h);
      for (const it of items) {
        const row = document.createElement("div");
        row.className = "item" + (it.done_at ? " done" : "");
        row.innerHTML = `<input type="checkbox" ${it.done_at ? "checked" : ""} aria-label="done"><span class="tx">${esc(it.text)}${it.due ? `<span class="due">${esc(it.due)}</span>` : ""}</span><button class="del" aria-label="Delete">&times;</button>`;
        row.querySelector("input").onchange = (e) => listChange({ action: e.target.checked ? "done" : "undone", id: it.id });
        row.querySelector(".del").onclick = () => listChange({ action: "remove", id: it.id });
        row.querySelector(".tx").onclick = () => row.querySelector("input").click();
        body.appendChild(row);
      }
    }
    if (!body.children.length) body.innerHTML = `<p class="muted">Nothing on the list.</p>`;
  }
  async function listChange(d) {
    try {
      const r = await api("/api/app/list", { json: d });
      if (r.command) toast("Done", { wake: "Waking Walter up", sleep: "Walter's going to sleep", mute: "Wall mic off", unmute: "Wall mic on" }[r.command] || r.command);
      renderList(r);
    } catch (e) { toast("Couldn't change the list", e.message, "err"); }
  }
  document.querySelectorAll("[data-sec]").forEach((b) => b.addEventListener("click", () => {
    $("listSection").value = b.dataset.sec;
    document.querySelectorAll("[data-sec]").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-checked", String(x === b)); });
    $("listText").placeholder = { tasks: "Add a task...", shopping: "Add to shopping...", notes: "Add a note...", ideas: "Park an idea...", inbox: "Add to the inbox..." }[b.dataset.sec];
  }));
  $("listForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("listText").value.trim();
    if (!text) return;
    $("listText").value = "";
    listChange({ action: "add", text, section: $("listSection").value });
  });

  // ------------------------------------------------------------ while you were away
  function setAwayBadge(n) {
    const b = $("awayBadge");
    b.textContent = n > 9 ? "9+" : String(n || 0);
    b.classList.toggle("hidden", !n);
    if (navigator.setAppBadge) { try { n ? navigator.setAppBadge(n) : navigator.clearAppBadge(); } catch (e) {} }
  }
  async function loadAway(badgeOnly) {
    let d; try { d = await api("/api/app/away"); } catch (e) { return; }
    setAwayBadge(d.unseen);
    if (badgeOnly) return;
    const items = (d.items || []).slice(0, 12), el = $("awayList");
    $("awayBox").classList.toggle("hidden", !items.length);
    el.innerHTML = "";
    for (const it of items) {
      const c = document.createElement("div"); c.className = "card" + (it.seen ? "" : " unseen");
      const when = new Date(it.t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
      c.innerHTML = `<div class="t"><span class="badge ${it.seen ? "" : "failed"}">${esc(when)}</span>${esc(it.title || "")}</div>` +
        (it.body ? `<div class="d">${esc(it.body)}</div>` : "");
      el.appendChild(c);
    }
    if (d.unseen && document.visibilityState === "visible") {   // they've seen them now
      try { setAwayBadge((await api("/api/app/away", { json: { action: "seen" } })).unseen); } catch (e) {}
    }
  }
  $("awayClear").onclick = async () => { try { await api("/api/app/away", { json: { action: "clear" } }); loadAway(); } catch (e) {} };

  // ------------------------------------------------------------ Web Push (https only)
  function pushSupported() { return window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window; }
  function b64ToBytes(s) {
    const b = atob((s + "===".slice((s.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(b, (c) => c.charCodeAt(0));
  }
  async function pushState() {
    const btn = $("pushBtn"), line = $("pushLine"), test = $("pushTest");
    if (!device || device.id === "kiosk") { btn.disabled = true; line.textContent = "Notifications are for paired phones."; return; }
    if (!pushSupported()) {
      btn.disabled = true;
      line.textContent = /iPhone|iPad/.test(navigator.userAgent) && !navigator.standalone
        ? "On iPhone: Share, Add to Home Screen, then open Walter from there to turn on notifications."
        : "Notifications need Walter's secure (https) address. You'll still see things here when the app is open.";
      return;
    }
    let info; try { info = await api("/api/app/push"); } catch (e) { return; }
    if (!info.available) { btn.disabled = true; line.textContent = "Walter can't send notifications yet (missing pywebpush)."; return; }
    const sub = swReg && await swReg.pushManager.getSubscription();
    const on = !!sub && info.subscribed > 0;
    btn.disabled = false;
    btn.textContent = on ? "Turn off notifications" : "Turn on notifications";
    test.classList.toggle("hidden", !on);
    line.textContent = on ? "On: reminders and things you missed come to this device, even with the app closed."
      : Notification.permission === "denied" ? "Notifications are blocked for this site in the browser settings."
      : "Reminders and things you missed, even with the app closed.";
  }
  $("pushBtn").onclick = async () => {
    try {
      const reg = swReg || await navigator.serviceWorker.ready;
      const old = await reg.pushManager.getSubscription();
      if (old && $("pushBtn").textContent.startsWith("Turn off")) {
        await api("/api/app/push", { json: { action: "unsubscribe", endpoint: old.endpoint } });
        await old.unsubscribe();
      } else {
        if ((await Notification.requestPermission()) !== "granted") { pushState(); return; }
        const info = await api("/api/app/push");
        const sub = old || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(info.key) });
        await api("/api/app/push", { json: { action: "subscribe", subscription: sub.toJSON() } });
        toast("Notifications on", "Reminders will reach this device even with the app closed.");
      }
    } catch (e) { toast("Couldn't change notifications", e.message, "err"); }
    pushState();
  };
  $("pushTest").onclick = async () => {
    try { const r = await api("/api/app/push", { json: { action: "test" } }); if (!r.ok) toast("Test not delivered", "Try turning notifications off and on.", "err"); }
    catch (e) { toast("Couldn't send a test", e.message, "err"); }
  };

  async function loadEvents() {
    let d; try { d = await api("/api/app/events"); } catch (e) { return; }
    const now = new Date(), day = (dd) => dd.toISOString ? `${dd.getFullYear()}-${String(dd.getMonth() + 1).padStart(2, "0")}-${String(dd.getDate()).padStart(2, "0")}` : "";
    const today = day(now), tm = new Date(now); tm.setDate(tm.getDate() + 1); const tomorrow = day(tm);
    const fill = (el, list) => {
      el.innerHTML = list.length ? "" : `<p class="muted">Nothing on the calendar.</p>`;
      for (const ev of list) {
        const s = new Date(ev.start), e2 = ev.end ? new Date(ev.end) : null;
        const when = ev.all_day ? "All day" : timeStr(s) + (e2 ? "" : "");
        const row = document.createElement("div"); row.className = "ev";
        row.innerHTML = `<div class="when">${esc(when)}</div><div><div>${esc(ev.title || "")}</div><div class="cal">${esc(ev.calendar || "")}${ev.location ? " - " + esc(ev.location) : ""}</div></div>`;
        el.appendChild(row);
      }
    };
    const evs = d.events || [];
    fill($("evToday"), evs.filter((e) => String(e.start).slice(0, 10) === today || (e.all_day && String(e.start).slice(0, 10) <= today && String(e.end || "").slice(0, 10) > today)));
    fill($("evTomorrow"), evs.filter((e) => String(e.start).slice(0, 10) === tomorrow));
    $("evAge").textContent = d.updated ? "Walter last updated the calendar at " + timeStr(d.updated * 1000) + "." : "Walter hasn't loaded the calendar yet.";
  }

  async function loadTasks() {
    let d; try { d = await api("/api/app/tasks"); } catch (e) { return; }
    const tl = $("tasks"); tl.innerHTML = "";
    const show = d.tasks.filter((t) => t.status === "running").concat(d.tasks.filter((t) => t.status !== "running").slice(0, 5));
    for (const t of show) {
      const c = document.createElement("div"); c.className = "card";
      c.innerHTML = `<div class="t"><span class="badge ${esc(t.status)}">${esc(t.status)}</span>#${t.id} ${esc(t.title || "")}</div>` +
        (t.result ? `<div class="d">${esc(t.result.slice(0, 280))}${t.result.length > 280 ? "..." : ""}</div>` : "");
      tl.appendChild(c);
    }
    if (!show.length) tl.innerHTML = `<p class="muted">No workers yet.</p>`;
    const rl = $("reminders"); rl.innerHTML = "";
    for (const r of d.reminders) {
      const c = document.createElement("div"); c.className = "card";
      const when = r.repeat ? `every ${(r.repeat.days || []).length === 7 ? "day" : (r.repeat.days || []).map((x) => "MTWTFSS"[x]).join("")} at ${r.repeat.time}` : new Date(r.due * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
      c.innerHTML = `<div class="t"><span class="badge">${r.kind === "do" ? "routine" : "reminder"}</span>${esc(when)}</div><div class="d">${esc((r.text || "").slice(0, 200))}</div>`;
      rl.appendChild(c);
    }
    if (!d.reminders.length) rl.innerHTML = `<p class="muted">No reminders.</p>`;
  }
  async function loadBacklog() {
    let d; try { d = await api("/api/app/backlog"); } catch (e) { return; }
    const el = $("backlog"); el.innerHTML = "";
    const rank = (s) => (s.startsWith("in progress") ? 0 : s === "ready" ? 1 : s === "ask" ? 2 : s.startsWith("done") ? 4 : 3);
    const items = d.items.slice().sort((a, b) => rank(a.status) - rank(b.status));
    for (const it of items.filter((i) => !i.status.startsWith("done")).concat(items.filter((i) => i.status.startsWith("done")).slice(0, 3))) {
      const cls = it.status.startsWith("in progress") ? "progress" : it.status.startsWith("done") ? "done" : it.status === "ask" ? "ask" : "";
      const c = document.createElement("div"); c.className = "card";
      c.innerHTML = `<div class="t"><span class="badge ${cls}">${esc(it.status)}</span>${esc(it.title)}</div>${it.details ? `<div class="d">${esc(it.details.slice(0, 220))}</div>` : ""}`;
      el.appendChild(c);
    }
    if (!el.children.length) el.innerHTML = `<p class="muted">Nothing on Walter's to-do list.</p>`;
  }

  const CTL_DONE = { mute: "Wall mic off", unmute: "Wall mic on", sleep: "Walter's going to sleep", wake: "Waking Walter up", stop: "Stopped talking" };
  async function control(action) {
    haptic();
    try { await api("/api/app/control", { json: { action } }); toast("Done", CTL_DONE[action] || action, "", 3); }
    catch (e) { toast("Couldn't do that", e.message, "err"); }
  }
  function setTiles() {   // the Walter tab's toggles follow the wall's real state
    const asleep = wall.state === "sleeping";
    $("micTile").setAttribute("aria-pressed", String(!!wall.muted));
    $("micTileState").textContent = wall.muted ? "Muted - tap to unmute" : "On - tap to mute";
    $("sleepTile").setAttribute("aria-pressed", String(asleep));
    $("sleepTileState").textContent = asleep ? "Asleep - tap to wake" : "Awake - tap to sleep";
  }
  $("micTile").onclick = () => control(wall.muted ? "unmute" : "mute");
  $("sleepTile").onclick = () => control(wall.state === "sleeping" ? "wake" : "sleep");
  document.querySelectorAll("[data-ctl]").forEach((b) => b.addEventListener("click", () => control(b.dataset.ctl)));
  document.querySelectorAll("[data-vol]").forEach((b) => b.addEventListener("click", async () => {
    try { const r = await api("/api/app/control", { json: { action: "volume", step: b.dataset.vol } }); $("volLine").textContent = r.volume || "?"; } catch (e) { toast("Couldn't change the volume", e.message, "err"); }
  }));
  $("showForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = $("showBody").value.trim();
    if (!body) return;
    try {
      await api("/api/app/show", { json: { title: $("showTitle").value.trim(), body, seconds: 180 } });
      $("showBody").value = ""; $("showTitle").value = "";
      toast("On the wall", "Walter is showing it for 3 minutes.", "", 4);
    } catch (err) { toast("Couldn't show that", err.message, "err"); }
  });
  $("setSpeakTyped").checked = LS.get("speakTyped", false);
  $("setSpeakTalk").checked = LS.get("speakTalk", true);
  $("setSpeakTyped").onchange = (e) => LS.set("speakTyped", e.target.checked);
  $("setSpeakTalk").onchange = (e) => LS.set("speakTalk", e.target.checked);
  $("unpairBtn").onclick = async () => {
    if (!confirm("Unpair this device? You'll need a new code from Walter to pair again.")) return;
    try { await api("/api/app/unpair", { json: {} }); } catch (e) { /* already gone */ }
    unpaired("Unpaired.");
  };
  $("checkUpd").onclick = async () => {
    if (swReg) { await swReg.update().catch(() => {}); }
    try {
      const v = await api("/api/app/version");
      if (v.build !== loadedBuild) updateFound(); else toast("Up to date", "Version " + v.version, "", 3);
    } catch (e) { toast("Couldn't check", e.message, "err"); }
  };

  // ------------------------------------------------------------ restart Walter (Settings > Advanced)
  // GET/POST /api/app/restart (walter/app_restart.py). A Walter from before the restart
  // that added it answers 404: then the button says it needs one restart by hand.
  let restartBusy = false;
  async function rawApi(path, json, ms = 4000) {
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), ms);
    const headers = {}; if (token) headers.Authorization = "Bearer " + token;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    try {
      const r = await fetch(path, { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store", signal: ctl.signal,
        body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  // ------------------------------------------------------------ Cloud Link (Settings)
  // GET/POST /api/app/cloud-link (walter/app_cloud_link.py): on with an invite code, off, rotate.
  // Turning it off from outside the home loses this connection, so it always asks first.
  let clBusy = false;
  function clUi(line, on, off) {
    $("clLine").textContent = line;
    $("clOff").classList.toggle("hidden", !off);
    $("clOnRow").classList.toggle("hidden", !on);
  }
  async function checkCloudLink(refresh) {
    if (clBusy) return;
    try {
      const r = await rawApi("/api/app/cloud-link" + (refresh ? "?refresh=1" : ""), undefined, refresh ? 15000 : 6000);
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) { clUi("Needs one restart of Walter before this works.", false, false); return; }
      if (r.status !== 200) throw new Error("HTTP " + r.status);
      const d = r.data;
      if (d.enabled) {
        let line = `On: ${d.url}/app. Connector ${d.connector}.`;
        if (d.remote_status === "revoked") line += " The Walter team switched this link off; contact them.";
        if (d.via_link) line += " You're connected through it right now.";
        clUi(line, true, false);
      } else {
        let line = "Off. The app works on your home network only.";
        if ((d.existing_tunnel || []).length) line += " This Walter already has its own tunnel; turning Cloud Link on replaces its address.";
        if (!d.cloudflared) line += " (cloudflared isn't installed yet.)";
        clUi(line, false, true);
      }
    } catch (e) { clUi("Can't reach Walter right now.", false, false); }
  }
  async function clAction(body, busyLine, ms) {
    clBusy = true; $("clLine").textContent = busyLine;
    ["clOn", "clOffBtn", "clRotate"].forEach((id) => $(id).disabled = true);
    try { return await rawApi("/api/app/cloud-link", body, ms); }
    finally { clBusy = false; ["clOn", "clOffBtn", "clRotate"].forEach((id) => $(id).disabled = false); }
  }
  $("clOn").onclick = async () => {
    const code = $("clCode").value.trim();
    if (!code) { toast("Invite code", "Type the invite code first.", "err", 4); return; }
    let r = await clAction({ action: "enable", code }, "Setting up your private link... (up to a minute)", 90000).catch(() => null);
    if (r && r.status === 409 && r.data.code === "existing_tunnel") {
      if (!confirm(r.data.error + "\n\nGo ahead anyway?")) { checkCloudLink(); return; }
      r = await clAction({ action: "enable", code, replace: true }, "Setting up your private link...", 90000).catch(() => null);
    }
    if (r && r.status === 200) {
      $("clCode").value = "";
      toast("Cloud Link is on", `${r.data.url}/app` + (r.data.healthy ? "" : " (first connection can take a minute)"), "", 8);
    } else toast("Cloud Link", (r && r.data.error) || "Couldn't reach Walter.", "err", 8);
    checkCloudLink();
  };
  $("clOffBtn").onclick = async () => {
    let r = await clAction({ action: "disable" }, "Checking...", 8000).catch(() => null);
    if (r && r.status === 409 && r.data.needs_confirm) {
      if (!confirm(r.data.warning + "\n\nTurn Cloud Link off?")) { checkCloudLink(); return; }
      r = await clAction({ action: "disable", confirm: true }, "Turning it off...", 30000).catch(() => null);
    }
    if (r && r.status === 200) toast("Cloud Link is off", r.data.warning || "The app works on your home network only.", "ok", 6);
    else if (r) toast("Cloud Link", r.data.error || "Couldn't turn it off.", "err", 6);
    checkCloudLink();
  };
  $("clRotate").onclick = async () => {
    const r = await clAction({ action: "rotate" }, "Getting a fresh token...", 30000).catch(() => null);
    if (r && r.status === 200) toast("Token rotated", "Same address; the link reconnects in a few seconds.", "", 5);
    else toast("Cloud Link", (r && r.data.error) || "Couldn't rotate (you may have been connected through it; try again in a moment).", "err", 6);
    checkCloudLink(true);
  };
  function restartLine(text) { $("restartLine").textContent = text; }
  async function checkRestart() {
    if (restartBusy) return;
    const b = $("restartBtn");
    try {
      const r = await rawApi("/api/app/restart");
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) {
        b.disabled = true; b.textContent = "Needs one restart to enable";
        restartLine("Walter needs to be restarted once by hand (or by asking him) before this button works.");
        return;
      }
      if (r.status !== 200) throw new Error("HTTP " + r.status);
      b.disabled = false; b.textContent = "Restart Walter";
      const n = (r.data.workers || []).length;
      restartLine("Checks his code first; he's offline for about 10 seconds." + (n ? ` ${n} background worker${n > 1 ? "s" : ""} running.` : ""));
    } catch (e) {
      b.disabled = true; b.textContent = "Restart Walter";
      restartLine("Can't reach Walter right now.");
    }
  }
  // ------------------------------------------------------------ wall text size (Settings > Wall)
  // GET/POST /api/app/display {text_scale 0.5..1.5} (walter/display_settings.py, the same
  // code path as `walterctl display text-scale`). The label follows the finger; the value is
  // sent once the finger lifts ("change"), debounced so arrow-key nudges coalesce.
  // A Walter from before the restart that added the route answers 404: "Needs one restart".
  let tsDefault = 80, tsTimer = 0, tsSent = null;
  const tsPct = (v) => Math.round(Number(v) * 100);
  function tsUi(pct, enabled, line) {
    const r = $("tsRange");
    if (pct != null) { r.value = String(pct); $("tsVal").textContent = pct + "%"; }
    if (enabled != null) { r.disabled = !enabled; $("tsReset").disabled = !enabled; }
    if (line != null) $("tsLine").textContent = line;
  }
  const TS_LINE = "Size of all the text on the wall; his eyes and buttons stay the same.";
  async function checkTextScale() {
    if (tsTimer) return;   // a change is on its way; don't jump the slider back
    try {
      const r = await rawApi("/api/app/display");
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) {
        tsUi(null, false, "Walter needs to be restarted once (Advanced below, or ask him) before this works.");
        $("tsVal").textContent = "Needs one restart to enable";
        return;
      }
      if (r.status !== 200) throw new Error("HTTP " + r.status);
      tsDefault = tsPct(r.data.default || 0.8);
      $("tsReset").textContent = `Reset to ${tsDefault}%`;
      tsSent = tsPct(r.data.text_scale);
      tsUi(tsSent, true, TS_LINE);
    } catch (e) {
      tsUi(null, false, "Can't reach Walter right now.");
    }
  }
  async function sendTextScale(pct) {
    tsTimer = 0;
    if (pct === tsSent) return;
    try {
      const r = await rawApi("/api/app/display", { text_scale: pct / 100 });
      if (r.status === 404 || r.status === 405) { checkTextScale(); return; }
      if (r.status !== 200) throw new Error(r.data.error || "HTTP " + r.status);
      tsSent = tsPct(r.data.text_scale);
      tsUi(tsSent, true, `Set to ${tsSent}%; the wall changes in a couple of seconds.`);
    } catch (e) {
      toast("Couldn't change the text size", e.message, "err");
      checkTextScale();
    }
  }
  function queueTextScale(pct) {
    clearTimeout(tsTimer);
    tsTimer = setTimeout(() => sendTextScale(pct), 350);
  }
  $("tsRange").addEventListener("input", (e) => { $("tsVal").textContent = e.target.value + "%"; });
  $("tsRange").addEventListener("change", (e) => queueTextScale(Number(e.target.value)));
  $("tsReset").onclick = () => { haptic(); tsUi(tsDefault); queueTextScale(tsDefault); };
  // ------------------------------------------------------------ beta feedback (Settings > Feedback)
  // walter/feedback.py: POST /api/app/feedback saves it on the box (~/.walter/feedback);
  // "See exactly what's included" shows the redacted diagnostics bundle (walter/diagnostics.py).
  // Nothing leaves the house until the owner taps Send on an item; with no [feedback] endpoint
  // set, Send offers a file download to email instead. The photo reuses /api/app/upload.
  let fbPhoto = null, fbBusy = false;
  const FB_STATUS = { saved: "Saved here", sent: "Sent to the Walter team", "send failed": "Couldn't send" };
  $("fbText").addEventListener("input", () => { $("fbCount").textContent = String($("fbText").value.length); });
  $("fbPreview").onclick = async () => {
    const v = $("fbDiagView");
    if (!v.classList.contains("hidden")) { v.classList.add("hidden"); return; }
    $("fbPreview").disabled = true;
    try {
      const r = await api("/api/app/feedback/diagnostics");
      $("fbDiagSum").innerHTML = (r.summary || []).map((x) => `<li>${esc(x)}</li>`).join("");
      $("fbDiagRaw").textContent = JSON.stringify(r.bundle, null, 1);
      v.classList.remove("hidden");
    } catch (e) { if (e.message !== "unpaired") toast("Couldn't build the preview", e.message, "err"); }
    $("fbPreview").disabled = false;
  };
  $("fbPhoto").onclick = () => $("fbPhotoInput").click();
  $("fbPhotoInput").onchange = async (e) => {
    const f = (e.target.files || [])[0];
    e.target.value = "";
    if (!f) return;
    try { fbPhoto = await prepare(f); $("fbPhotoName").textContent = "Photo attached"; $("fbPhoto").textContent = "Change photo"; }
    catch (err) { toast("Can't read that photo", "Try a screenshot or JPEG.", "err"); }
  };
  async function fbDownload(id) {
    try {
      const r = await fetch("/api/app/feedback/" + encodeURIComponent(id) + "/download",
        { headers: token ? { Authorization: "Bearer " + token } : {}, cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url; a.download = `walter-feedback-${id}.json`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast("Saved the file", "Email it to the Walter team whenever you like.", "", 6);
    } catch (e) { toast("Couldn't download it", e.message, "err"); }
  }
  async function fbSend(id, btn) {
    btn.disabled = true;
    try {
      const r = await rawApi("/api/app/feedback/" + encodeURIComponent(id) + "/send", {}, 20000);
      if (r.status === 409) { toast("No send address yet", "Downloading a file you can email instead.", "", 6); fbDownload(id); }
      else if (r.status !== 200) toast("Couldn't send it", (r.data && r.data.error) || "HTTP " + r.status, "err");
      else toast("Sent, thank you!", "The Walter team will read it.", "", 5);
    } catch (e) { toast("Couldn't send it", e.message, "err"); }
    btn.disabled = false;
    loadFeedback();
  }
  async function loadFeedback() {
    let r;
    try { r = await rawApi("/api/app/feedback"); } catch (e) { return; }
    if (r.status === 404 || r.status === 405) {
      $("fbSubmit").disabled = true;
      $("fbList").innerHTML = `<p class="muted small">Needs one restart to enable.</p>`;
      return;
    }
    if (r.status !== 200) return;
    $("fbSubmit").disabled = false;
    const items = r.data.items || [];
    const sendLabel = r.data.can_send ? "Send to the Walter team" : "Download to email";
    $("fbList").innerHTML = items.length ? items.map((it) => `<div class="fbItem">
        <div class="fbMeta">${esc(it.type)} &middot; ${esc(new Date(it.created * 1000).toLocaleString())} &middot; <b>${esc(FB_STATUS[it.status] || it.status)}</b>${it.has_diagnostics ? " &middot; diagnostics" : ""}${it.photo ? " &middot; photo" : ""}</div>
        <div class="fbTxt">${esc(it.text)}</div>
        ${it.status === "sent" ? "" : `<div class="row"><button class="chip" data-fbsend="${esc(it.id)}">${sendLabel}</button>
        ${r.data.can_send ? `<button class="chip" data-fbdl="${esc(it.id)}">Download</button>` : ""}</div>`}
      </div>`).join("") : `<p class="muted small">Nothing yet.</p>`;
    $("fbList").querySelectorAll("[data-fbsend]").forEach((b) => {
      b.onclick = () => (r.data.can_send ? fbSend(b.dataset.fbsend, b) : fbDownload(b.dataset.fbsend));
    });
    $("fbList").querySelectorAll("[data-fbdl]").forEach((b) => { b.onclick = () => fbDownload(b.dataset.fbdl); });
  }
  $("fbSubmit").onclick = async () => {
    const text = $("fbText").value.trim();
    if (!text) { toast("Feedback", "Write a few words first.", "", 4); return; }
    if (fbBusy) return;
    fbBusy = true; $("fbSubmit").disabled = true;
    try {
      const body = { type: $("fbType").value, text, diagnostics: $("fbDiag").checked };
      if (fbPhoto) { const d = await upload([fbPhoto]); body.image = d.images[0].id; }
      await api("/api/app/feedback", { json: body });
      $("fbText").value = ""; $("fbCount").textContent = "0"; $("fbDiag").checked = false;
      fbPhoto = null; $("fbPhotoName").textContent = ""; $("fbPhoto").textContent = "Attach a photo";
      $("fbDiagView").classList.add("hidden");
      toast("Saved, thank you!", "It's in Your feedback below. Tap Send when you're ready.", "", 6);
      loadFeedback();
    } catch (e) { if (e.message !== "unpaired") toast("Couldn't save it", e.message, "err"); }
    fbBusy = false; $("fbSubmit").disabled = false;
  };
  // ------------------------------------------------------------ workers at once (Settings > Workers)
  // GET/POST /api/app/workers {max_tasks 1..8}: [brain] max_tasks, written to config.toml and
  // applied live (walter/live.py, same as `walterctl config set brain.max_tasks N`).
  // A Walter from before the restart that added the route answers 404: "Needs one restart".
  let wk = null, wkTimer = 0, wkWant = null;
  function wkUi(enabled) {
    $("wkVal").textContent = wkWant != null ? String(wkWant) : (wk ? String(wk.max_tasks) : "\u2013");
    $("wkDown").disabled = !enabled || !wk || wkWant <= wk.min;
    $("wkUp").disabled = !enabled || !wk || wkWant >= wk.max;
    if (!wk) return;
    const q = wk.queued ? `, ${wk.queued} queued` : "";
    $("wkNow").textContent = `${wk.running} running${q}.`;
    const load = wk.load != null ? `, load now ${Number(wk.load).toFixed(1)}` : "";
    $("wkLine").textContent = `More workers use more CPU: this computer has ${wk.threads} thread${wk.threads === 1 ? "" : "s"}${load}.`;
  }
  async function checkWorkers() {
    if (wkTimer) return;   // a change is on its way
    try {
      const r = await rawApi("/api/app/workers");
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) {
        wk = null; wkWant = null; wkUi(false);
        $("wkVal").textContent = "Needs one restart to enable";
        $("wkNow").textContent = "";
        $("wkLine").textContent = "Walter needs to be restarted once (Advanced below, or ask him) before this works.";
        return;
      }
      if (r.status !== 200) throw new Error("HTTP " + r.status);
      wk = r.data; wkWant = wk.max_tasks; wkUi(true);
    } catch (e) {
      wkUi(false); $("wkLine").textContent = "Can't reach Walter right now.";
    }
  }
  // ------------------------------------------------------------ Settings > Presentation
  // GET/POST /api/app/present {action: start|rehearse|stop} (walter/present.py): a ~60 s self-demo on the wall
  // with demo data only; the mic is off while it runs. Polled every 1.5 s while it runs and Settings is open.
  // 404 = Walter from before the restart that added the route: "Needs one restart to enable".
  let prTimer = null;
  function prUi(st) {
    const run = !!(st && st.running);
    $("prStart").disabled = !st || run; $("prRehearse").disabled = !st || run; $("prStop").disabled = !st || !run;
    let line = "A one-minute show of what Walter can do, with pretend data on the wall. His mic is off while it runs.";
    if (run) line = `${st.rehearse ? "Rehearsing" : "Presenting"}: step ${st.step} of ${st.steps}${st.name ? " (" + st.name + ")" : ""}, ${Math.round(st.elapsed || 0)} s in.`;
    else if (st && st.last) line = st.last.result === "done" ? "Last show finished. Everything is back to normal."
      : st.last.result === "stopped" ? "Stopped. Everything is back to normal." : "The last show hit a problem; the wall was put back.";
    $("prLine").textContent = line;
    clearTimeout(prTimer); prTimer = null;
    if (run && curView === "settings") prTimer = setTimeout(checkPresent, 1500);
  }
  async function checkPresent() {
    try {
      const r = await rawApi("/api/app/present");
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) {
        prUi(null); $("prLine").textContent = "Needs one restart to enable. Restart Walter once (Advanced below, or ask him).";
        return;
      }
      if (r.status !== 200) throw new Error("HTTP " + r.status);
      prUi(r.data);
    } catch (e) { prUi(null); $("prLine").textContent = "Can't reach Walter right now."; }
  }
  async function present(action) {
    try {
      const r = await rawApi("/api/app/present", { action }, 15000);
      if (r.status === 409) toast("Already running", "A presentation is on the wall right now.", "err", 4);
      else if (r.status !== 200) toast("Couldn't " + action, (r.data && r.data.error) || "HTTP " + r.status, "err", 5);
      if (r.data && "running" in r.data) prUi(r.data); else checkPresent();
    } catch (e) { toast("Can't reach Walter", "", "err", 4); checkPresent(); }
  }
  $("prStart").addEventListener("click", () => present("start"));
  $("prRehearse").addEventListener("click", () => present("rehearse"));
  $("prStop").addEventListener("click", () => present("stop"));
  // ------------------------------------------------------------ live worker status (Walter tab + Settings > Workers)
  // GET /api/app/workers/live: each worker's title, elapsed time and what it's doing right now
  // (walter/workerlive.py). Polled every 3 s while one of those screens is visible.
  // 404 = Walter from before the restart that added the route: "Needs one restart to enable".
  const wlDur = (s) => { s = Math.max(0, Math.round(s)); if (s < 60) return s + "s"; const m = Math.floor(s / 60); return m < 60 ? m + " min" : Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0"); };
  async function checkLive() {
    const boxes = [$("wkLive"), $("wkLiveW")].filter(Boolean);
    let html;
    try {
      const r = await rawApi("/api/app/workers/live");
      if (r.status === 401 && token) { unpaired("This device isn't paired any more."); return; }
      if (r.status === 404 || r.status === 405) {
        html = `<p class="muted small">Live worker status: needs one restart to enable.</p>`;
      } else if (r.status !== 200) { throw new Error("HTTP " + r.status); }
      else {
        const now = Date.now() / 1000;
        const ws = (r.data.workers || []).filter((w) => w.status === "running" || w.status === "queued" || (w.finished && now - w.finished < 70));
        html = ws.map((w) => {
          const el = w.status === "running" ? wlDur(now - (w.started || now)) : w.status === "queued" ? "waiting" : wlDur(w.elapsed || 0);
          const files = w.status === "running" && w.files ? ` \u00b7 ${w.files} file${w.files === 1 ? "" : "s"}` : "";
          const badge = w.status === "queued" ? "waiting" : w.status;
          // cloud jobs from an add-on ([workers], walter/cloudworkers.py): a small "cloud" marker + the add-on's name
          const cloud = w.cloud ? `<span class="badge cloud">cloud \u00b7 ${esc(w.addon_name || w.addon || "")}</span>` : "";
          return `<div class="card"><div class="t"><span class="badge ${esc(w.status)}">${esc(badge)}</span>${cloud}${w.id && !w.cloud ? "#" + w.id + " " : ""}${esc(w.title || "")} <span class="muted small">${esc(el)}</span></div>` +
            `<div class="d">${esc((w.activity || "") + files)}</div></div>`;
        }).join("") || `<p class="muted small">No workers running right now.</p>`;
      }
    } catch (e) { return; }
    for (const b of boxes) b.innerHTML = html;
  }
  setInterval(() => {
    if (document.visibilityState === "visible" && (curView === "walter" || curView === "settings")) checkLive();
  }, 3000);
  async function sendWorkers() {
    wkTimer = 0;
    if (!wk || wkWant === wk.max_tasks) return;
    try {
      const r = await rawApi("/api/app/workers", { max_tasks: wkWant });
      if (r.status === 404 || r.status === 405) { checkWorkers(); return; }
      if (r.status !== 200) throw new Error((r.data && r.data.error) || "HTTP " + r.status);
      wk = r.data; wkWant = wk.max_tasks; wkUi(true);
      toast(`Up to ${wk.max_tasks} worker${wk.max_tasks > 1 ? "s" : ""} at once`, "Applied now; saved for next time too.");
    } catch (e) {
      toast("Couldn't change the workers", e.message, "err");
      checkWorkers();
    }
  }
  function stepWorkers(d) {
    if (!wk) return;
    haptic();
    wkWant = Math.max(wk.min, Math.min(wk.max, wkWant + d));
    wkUi(true);
    clearTimeout(wkTimer);
    wkTimer = setTimeout(sendWorkers, 600);
  }
  $("wkDown").onclick = () => stepWorkers(-1);
  $("wkUp").onclick = () => stepWorkers(1);
  function sheet(title, bodyHtml, buttons) {
    $("sheetTitle").textContent = title;
    $("sheetBody").innerHTML = bodyHtml;
    const box = $("sheetBtns"); box.innerHTML = "";
    for (const [label, cls, fn] of buttons) {
      const b = document.createElement("button");
      b.className = "btn " + (cls || ""); b.textContent = label; b.onclick = fn;
      box.appendChild(b);
    }
    $("sheet").classList.remove("hidden");
    const last = box.lastElementChild; if (last) last.focus();
  }
  function closeSheet() { $("sheet").classList.add("hidden"); }
  $("sheet").addEventListener("click", (e) => { if (e.target.id === "sheet" && !restartBusy) closeSheet(); });
  $("restartBtn").onclick = () => {
    haptic();
    sheet("Restart Walter?", "He'll be offline for about 10 seconds.",
      [["Cancel", "", closeSheet], ["Restart", "warnfill", () => doRestart(false)]]);
  };
  async function doRestart(force) {
    if (restartBusy) return;
    restartBusy = true;
    let before = null;
    try { before = (await rawApi("/api/app/version")).data.started || null; } catch (e) { /* fine */ }
    sheet("Restart Walter?", '<span class="spin"></span>Checking his code first...', []);
    let r;
    try { r = await rawApi("/api/app/restart", { force }, 90000); }
    catch (e) { r = { status: 0, data: {} }; }
    const d = r.data || {};
    if (r.status === 200 && d.restarting) { restartBusy = false; return waitForWalter(before); }
    restartBusy = false;
    if (r.status === 409 && d.reason === "workers") {
      const list = (d.workers || []).map((w) => `<li>#${esc(String(w.id))} ${esc(w.title || "untitled")}</li>`).join("");
      sheet("Workers are running", `They should carry on through the restart, but just so you know:<ul>${list}</ul>`,
        [["Cancel", "", closeSheet], ["Restart anyway", "warnfill", () => doRestart(true)]]);
    } else if (r.status === 409 && d.reason === "compile") {
      sheet("Not restarting", `Walter's code doesn't compile right now, so a restart would take him down. Nothing was changed.<pre>${esc(d.error || "")}</pre>`,
        [["OK", "", closeSheet]]);
    } else if (r.status === 429) {
      sheet("Just restarted", esc(d.error || "Give it half a minute, then try again."), [["OK", "", closeSheet]]);
    } else if (r.status === 404) {
      closeSheet(); checkRestart();
    } else {
      sheet("Couldn't restart", esc(d.error || "Walter didn't answer. Check the wall, then try again."), [["OK", "", closeSheet]]);
    }
  }
  async function waitForWalter(before) {
    restartBusy = true;
    const t0 = Date.now();
    $("restartBtn").disabled = true;
    restartLine("Restarting...");
    const show = (slow) => sheet("Restarting...",
      `<span class="spin"></span>${slow ? "Taking a little longer than usual. I'll keep checking; you can close this." : "Walter will be back in a few seconds."}`,
      slow ? [["Close", "", closeSheet]] : []);
    show(false);
    let slow = false;
    while (true) {
      await new Promise((ok) => setTimeout(ok, 1500));
      let back = false;
      try {
        const v = await rawApi("/api/app/version", undefined, 2500);
        back = v.status === 200 && v.data.started && v.data.started !== before;
      } catch (e) { /* offline gap: expected */ }
      if (back) break;
      if (!slow && Date.now() - t0 > 45000) { slow = true; if (!$("sheet").classList.contains("hidden")) show(true); }
      if (Date.now() - t0 > 300000) {
        restartBusy = false; restartLine("Walter hasn't come back yet. Check the wall.");
        $("restartBtn").disabled = false; return;
      }
    }
    restartBusy = false;
    if (!wsOpen && (!ws || ws.readyState > 1)) { retry = 0; connect(); }   // skip the long backoff
    if (!$("sheet").classList.contains("hidden")) {
      sheet("Back online", '<span class="okmark">&#10003;</span> Walter is up and running again.', [["OK", "primary", closeSheet]]);
      setTimeout(() => { if ($("sheetTitle").textContent === "Back online") closeSheet(); }, 4000);
    } else toast("Back online", "Walter is up and running again.", "", 4);
    checkRestart();
  }

  // ------------------------------------------------------------ updates
  function updateFound() { $("update").classList.remove("hidden"); }
  $("updateBtn").onclick = () => {
    if (swReg && swReg.waiting) { swReg.waiting.postMessage("skip"); return; }   // reload on controllerchange
    if (swReg) swReg.update().finally(() => location.reload()); else location.reload();
  };
  function setupSW() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;   // plain http: no worker, still works
    navigator.serviceWorker.register("sw.js", { scope: "./" }).then((reg) => {
      swReg = reg;
      if (reg.waiting && navigator.serviceWorker.controller) updateFound();
      reg.addEventListener("updatefound", () => {
        const nw = reg.installing;
        nw && nw.addEventListener("statechange", () => {
          if (nw.state === "installed" && navigator.serviceWorker.controller) updateFound();
        });
      });
      setInterval(() => reg.update().catch(() => {}), 30 * 60 * 1000);
    }).catch(() => {});
    let reloading = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => { if (!reloading) { reloading = true; location.reload(); } });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (swReg) swReg.update().catch(() => {});
      if (!wsOpen && ws && ws.readyState > 1) { retry = 0; connect(); }
      refreshPanels();
      if (wsOpen) histRefresh();
    }
  });
  window.addEventListener("online", () => { if (!wsOpen) { retry = 0; connect(); } });

  // ------------------------------------------------------------ start
  function start() {
    $("pair").classList.add("hidden");
    $("main").classList.remove("hidden");
    face = new MiniFace($("miniFace"));
    renderHistory();
    window.history.replaceState({ v: "chat", d: 0 }, "");
    let tab = LS.get("tab", "chat");
    if (tab === "remote") tab = "walter";   // Remote used to be a tab
    if (TABS.includes(tab) && tab !== "chat") go(tab);
    connect();
    watchWallBrowser();
  }
  setupSW();
  // the expose for tests/debugging (headless checks drive the mic logic through this)
  window.walterApp = { api, toast, startTalk, finishTalk, cancelTalk, token: () => token, state: () => ({ wsOpen, rec: !!rec, device, wall, audioCount, sw: !!swReg, build: loadedBuild }) };
  if (token || isLocal()) start(); else showPair();
})();

// iOS Safari ignores user-scalable=no: block pinch gestures explicitly.
["gesturestart", "gesturechange", "gestureend"].forEach(function (ev) {
  document.addEventListener(ev, function (e) { e.preventDefault(); }, { passive: false });
});
