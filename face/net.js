/* Wi-Fi: a small icon in the corner while Walter is offline; tap it to pick
   a network and type its password. Nothing shows while he's online. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const icon = $("netIcon"), panel = $("netPanel"), list = $("netList"), form = $("netForm");
  const ssidIn = $("netSsid"), pwIn = $("netPw"), go = $("netGo"), msg = $("netMsg"), sub = $("netSub");
  let online = true, open = false, chosen = null;

  function say(text, cls) { msg.textContent = text || ""; msg.className = "msg " + (cls || ""); }
  function showCursor(on) { document.body.style.cursor = on ? "default" : "none"; }

  function setOnline(v) {
    online = v;
    icon.style.display = online ? "none" : "block";
    if (online && open) { say("Connected!", "ok"); setTimeout(close, 1500); }
  }

  function bars(signal) { return signal > 70 ? "▂▄▆█" : signal > 45 ? "▂▄▆" : signal > 20 ? "▂▄" : "▂"; }

  function render(networks) {
    list.innerHTML = "";
    for (const n of networks) {
      const b = document.createElement("button");
      b.className = "row";
      const name = document.createElement("span"); name.className = "name"; name.textContent = n.ssid;
      const meta = document.createElement("span"); meta.className = "meta";
      meta.textContent = (n.in_use ? "connected · " : "") + (n.secure ? "🔒 " : "") + bars(n.signal);
      b.append(name, meta);
      b.onclick = () => pick(n, b);
      list.append(b);
    }
    const other = document.createElement("button");
    other.className = "row";
    other.innerHTML = '<span class="name">Other network…</span>';
    other.onclick = () => pick(null, other);
    list.append(other);
  }

  function pick(n, el) {
    chosen = n;
    for (const r of list.children) r.classList.remove("sel");
    el.classList.add("sel");
    form.style.display = "block";
    ssidIn.style.display = n ? "none" : "block";
    ssidIn.value = n ? n.ssid : "";
    pwIn.style.display = !n || n.secure ? "block" : "none";
    pwIn.value = "";
    go.style.display = "block";
    say("");
    (n ? (n.secure ? pwIn : go) : ssidIn).focus();
  }

  async function scan() {
    say("Looking for networks…");
    list.innerHTML = ""; form.style.display = "none"; go.style.display = "none";
    try {
      const r = await (await fetch("/api/net/wifi")).json();
      render(r.networks || []);
      say(r.message || "", r.ok ? "" : "err");
    } catch (e) { say("Couldn't scan: " + e.message, "err"); }
  }

  async function connect() {
    const ssid = (chosen ? chosen.ssid : ssidIn.value).trim();
    if (!ssid) return say("Type the network's name.", "err");
    go.disabled = true; say("Connecting to " + ssid + "…");
    try {
      const r = await (await fetch("/api/net/connect", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ssid, password: pwIn.value }),
      })).json();
      say(r.message, r.ok ? "ok" : "err");
      if (r.ok) setTimeout(close, 1800);
    } catch (e) { say("Couldn't connect: " + e.message, "err"); }
    go.disabled = false;
  }

  function show() {
    open = true; panel.style.display = "flex"; showCursor(true);
    sub.textContent = online ? "Walter is online. You can switch networks here." : "Walter isn't connected to the internet. Pick your network.";
    scan();
  }
  function close() { open = false; panel.style.display = "none"; showCursor(false); }

  icon.onclick = show;
  $("netClose").onclick = close;
  $("netRescan").onclick = scan;
  go.onclick = connect;
  for (const el of [ssidIn, pwIn]) {
    el.addEventListener("keydown", (e) => {
      e.stopPropagation(); // don't let Walter's own keyboard shortcuts see these keys
      if (e.key === "Enter") connect();
      if (e.key === "Escape") close();
    });
  }
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); e.stopPropagation(); });

  // Show the mouse pointer when it moves (so the icon can be clicked), hide it again after a while.
  let hideTimer = 0;
  window.addEventListener("mousemove", () => {
    if (open) return;
    showCursor(true);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { if (!open) showCursor(false); }, 4000);
  });
  // "i" (for internet) opens the Wi-Fi panel any time, even when online
  window.addEventListener("keydown", (e) => {
    const typing = document.activeElement && document.activeElement.tagName === "INPUT";
    if (!open && !typing && e.key === "i") { e.preventDefault(); show(); }
  });

  // `walterctl display wifi` (or me, on a spoken "show the wifi") bumps display.json wifi_seq: open the panel
  let seenSeq = null, seenClose = null;
  setInterval(() => {
    fetch("display.json", { cache: "no-store" }).then((r) => r.json()).then((d) => {
      const q = d && d.wifi_seq;
      if (seenSeq === null) { seenSeq = q || 0; return; }
      if (q && q !== seenSeq) { seenSeq = q; if (!open) show(); }
      const c = d && d.wifi_close_seq;
      if (seenClose === null) seenClose = c || 0;
      else if (c && c !== seenClose) { seenClose = c; if (open) close(); }
    }).catch(() => {});
  }, 3000);

  // which Wi-Fi am I on: a small chip in the top-right corner (owner, 10 Nov)
  const chip = document.createElement("div");
  chip.id = "wifichip";
  const cs = document.createElement("style");
  cs.textContent = `#wifichip { position: fixed; top: 0.5vh; right: 1vw; z-index: 30; display: none; align-items: center; gap: 0.45em;
    font: 600 1.25vh system-ui, sans-serif; color: rgba(225,240,255,0.7); background: rgba(5,6,10,0.45); padding: 0.25em 0.7em;
    border-radius: 1em; pointer-events: none; white-space: nowrap; max-width: 30vw; overflow: hidden; text-overflow: ellipsis; }
    #wifichip svg { width: 1.2em; height: 1.2em; flex: none; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; }
    #wifichip.off { color: rgba(255,170,150,0.85); }`;
  document.head.appendChild(cs);
  document.body.appendChild(chip);
  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const wifiSvg = '<svg viewBox="0 0 24 24"><path d="M2 8.5a15 15 0 0 1 20 0"/><path d="M5.5 12a10 10 0 0 1 13 0"/><path d="M9 15.5a5 5 0 0 1 6 0"/><path d="M12 19h0"/></svg>';
  function chipTick() {
    fetch("/api/net/status", { cache: "no-store" }).then((r) => r.json()).then((s) => {
      const label = s && s.ssid ? s.ssid : (s && s.online ? "Wired" : "Offline");
      chip.innerHTML = wifiSvg + "<span>" + esc(label) + "</span>";
      chip.classList.toggle("off", !(s && s.online));
      chip.style.display = "flex";
    }).catch(() => {});
  }
  chipTick();
  setInterval(chipTick, 10000);

  window.addEventListener("walter-msg", (e) => { if (e.detail.t === "net") setOnline(!!e.detail.online); });
  fetch("/api/net/status").then((r) => r.json()).then((s) => setOnline(!!s.online)).catch(() => {});
})();
