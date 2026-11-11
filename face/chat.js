/* Conversation mode CHAT view (the owner approved 9 Nov 19:56,
 * journal/2026-11-09-walter-product/PLAN-conversation-chat.md). A second view of conversation mode:
 * our conversation as chat bubbles, readable from across the room.
 *
 * KEEP BOTH: the normal conversation layout (converse.js: me big and centred, big captions) stays the
 * default when conversation mode starts by itself (convgate.js). The chat view shows instead when
 *   - the owner says "conversation mode" / "show the chat" / "chat mode" (converse.js opens conversation
 *     mode at once too; it ends after a quiet minute, on mute, or "close the chat"), or
 *   - display.json chat_view is "chat" (`walterctl display conversation view chat|default`): then every
 *     conversation uses it. "close the chat" goes back to the normal layout (or to the board when the
 *     conversation was only opened by voice). A CLI change wins over the voice choice; the voice
 *     choice lasts for that conversation only.
 *
 * Bubbles: the owner right, me left, newest at the bottom, the last ~8 turns (chatlib.js). the owner's words show
 * live while he speaks (partials, at most 2 redraws a second); my reply grows sentence by sentence as I
 * say each one (the same "caption" messages the face already gets). My bubble gets safe minimal
 * markdown (chatlib.md: escaped first; no links, no images). Companion-app turns arrive as
 * {t:"chat", who, text, from:"phone"} (walter/app_server.py) and join the same thread, marked.
 * In memory only, cleared at midnight. While it shows, the normal caption line hides.
 *
 * Build mode (buildview.js / cockpit.js): with the chat view selected the chat is a slim column beside
 * the cockpit (portrait: right of it, under me; landscape: the right edge).
 *
 * Opacity / transform only, one fade-in per new bubble; prefers-reduced-motion = none. Font sizes are
 * absolute (vmin) so textscale.js scales them.
 */
(() => {
  "use strict";
  const L = window.WalterChatLib;
  if (!L) return;
  const thread = L.createThread();
  let cfgView = "default", seenCfg = null, voiceView = null, showing = false, column = false;
  let convoOffSince = 0;

  const css = document.createElement("style");
  css.dataset.portrait = "chat"; // per-orientation sizes below: portrait.js leaves it alone
  css.textContent = `
  #chatview { position: fixed; z-index: 2; pointer-events: none; opacity: 0; visibility: hidden;
    display: flex; flex-direction: column; justify-content: flex-end; gap: 1.4vmin; overflow: hidden;
    transition: opacity 0.5s ease, visibility 0s 0.5s; will-change: opacity; font-size: 3.6vmin; line-height: 1.32; }
  #chatview.on { opacity: 1; visibility: visible; transition: opacity 0.5s ease 0.1s, visibility 0s; }
  #chatview .cb { flex: none; max-width: 84%; padding: 1.2vmin 2vmin; border-radius: 2.4vmin;
    color: rgba(255,255,255,0.93); background: rgba(14,20,34,0.78); border: 1px solid rgba(255,255,255,0.08);
    overflow-wrap: anywhere; font-variant-numeric: tabular-nums; animation: cbin 0.35s ease-out both; }
  #chatview .cb.w { align-self: flex-start; border-bottom-left-radius: 0.6vmin; }
  #chatview .cb.y { align-self: flex-end; border-bottom-right-radius: 0.6vmin; background: rgba(46,78,140,0.72);
    border-color: rgba(150,190,255,0.22); }
  #chatview .cb.y.live { opacity: 0.72; font-style: italic; }
  #chatview .cb .dots { color: rgba(255,255,255,0.55); }
  #chatview .cb .from { font-size: 2.0vmin; letter-spacing: 0.08em; text-transform: uppercase; color: rgba(255,214,140,0.8);
    margin-bottom: 0.4vmin; }
  #chatview .cb p { margin: 0; } #chatview .cb p + p, #chatview .cb p + ul, #chatview .cb p + ol,
  #chatview .cb ul + p, #chatview .cb ol + p, #chatview .cb pre + p, #chatview .cb p + pre { margin-top: 0.8vmin; }
  #chatview .cb ul, #chatview .cb ol { margin: 0.3vmin 0; padding-left: 1.3em; }
  #chatview .cb li + li { margin-top: 0.3vmin; }
  #chatview .cb b { color: #fff; font-weight: 600; }
  #chatview .cb code { font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 0.86em; padding: 0 0.3em;
    border-radius: 0.6vmin; background: rgba(255,255,255,0.1); }
  #chatview .cb pre { margin: 0.6vmin 0; padding: 1vmin 1.4vmin; border-radius: 1.2vmin; background: rgba(4,6,12,0.7);
    white-space: pre-wrap; overflow: hidden; max-height: 40vh; }
  #chatview .cb pre code { background: none; padding: 0; }
  @keyframes cbin { from { opacity: 0; transform: translateY(1.2vmin); } to { opacity: 1; transform: none; } }
  body.chat-on #caption { opacity: 0 !important; }
  @media (orientation: portrait) {
    #chatview { left: 5vw; right: 5vw; top: 44vh; bottom: 3vh; }
  }
  @media (orientation: landscape) {
    #chatview { left: 47vw; right: 3vw; top: 5vh; bottom: 5vh; }
  }
  /* build mode: a slim column beside the cockpit */
  #chatview.col { font-size: 2.4vmin; gap: 1vmin; }
  #chatview.col .cb { max-width: 94%; padding: 0.9vmin 1.3vmin; border-radius: 1.6vmin; }
  #chatview.col .cb .from { font-size: 1.5vmin; }
  @media (orientation: portrait) {
    #chatview.col { left: auto; right: 2.5vw; width: 28vw; top: 41.5vh; bottom: 1.5vh; }
    body.chat-col #buildview.cockpit { right: 31.5vw; }
  }
  @media (orientation: landscape) {
    #chatview.col { left: auto; right: 1.5vw; width: 21vw; top: 3vh; bottom: 3vh; }
    body.chat-col #buildview.cockpit { right: 23.5vw; }
  }
  @media (prefers-reduced-motion: reduce) {
    #chatview, #chatview.on { transition: none !important; }
    #chatview .cb { animation: none !important; }
  }`;
  document.head.appendChild(css);

  const root = document.createElement("div");
  root.id = "chatview";
  root.setAttribute("aria-hidden", "true");
  document.body.appendChild(root);

  // ------------------------------------------------------------ drawing (keyed, only what changed)
  const els = new Map(); // id -> {el, v}
  let dirty = false, lastPartialDraw = 0, partialTimer = 0;
  function draw() {
    dirty = false;
    if (!showing) return;
    const msgs = thread.msgs(), keep = new Set();
    let prev = null;
    for (const m of msgs) {
      keep.add(m.id);
      let e = els.get(m.id);
      if (!e) {
        const wrap = document.createElement("div");
        wrap.innerHTML = L.bubble(m); // chatlib escapes everything first
        e = { el: wrap.firstChild, v: m.v, live: m.partial };
        els.set(m.id, e);
        if (prev) prev.after(e.el); else root.prepend(e.el);
      } else if (e.v !== m.v || e.live !== m.partial) {
        const wrap = document.createElement("div");
        wrap.innerHTML = L.bubble(m);
        e.el.className = wrap.firstChild.className;
        e.el.innerHTML = wrap.firstChild.innerHTML;
        e.v = m.v; e.live = m.partial;
      }
      prev = e.el;
    }
    for (const [id, e] of els) if (!keep.has(id)) { e.el.remove(); els.delete(id); }
  }
  function soon() { if (!dirty) { dirty = true; requestAnimationFrame(draw); } }
  // live partials: at most 2 redraws a second
  function soonPartial() {
    const wait = 500 - (Date.now() - lastPartialDraw);
    if (wait <= 0) { lastPartialDraw = Date.now(); soon(); return; }
    if (!partialTimer) partialTimer = setTimeout(() => { partialTimer = 0; lastPartialDraw = Date.now(); soon(); }, wait);
  }

  // ------------------------------------------------------------ messages
  window.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    const now = Date.now();
    if (m.t === "caption") {
      if (m.who === "you") {
        if (m.muted) return;
        const partial = !!(m.partial || m.live);
        thread.heard(now, m.text, partial);
        if (!partial) {
          const c = L.command(m.text);
          if (c) setVoice(c.cmd === "open" ? "chat" : "default");
        }
        if (partial) soonPartial(); else soon();
      } else {
        if (thread.said(now, m.text)) soon();
      }
    } else if (m.t === "chat") {
      // companion-app turns (walter/app_server.py): the same thread, marked "from phone"
      if (thread.message(now, m.who, m.text, m.from === "phone" ? "phone" : String(m.from || "").slice(0, 20))) soon();
    } else if (m.t === "state") {
      if (m.state === "idle" || m.state === "listening" || m.state === "sleeping") thread.endReply();
    }
  });

  function setVoice(v) { voiceView = v; tick(); }

  // a partial that never became a turn (noise, just my name) fades out after a while
  function stalePartial() {
    const ms = thread.msgs(), last = ms[ms.length - 1];
    if (last && last.who === "you" && last.partial && Date.now() - last.t > 8000) { thread.dropPartial(); soon(); }
  }

  // ------------------------------------------------------------ settings + show / hide
  const pollSettings = async () => {
    try {
      const r = await fetch("display.json", { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const v = L.viewOf(d.chat_view);
      if (seenCfg !== null && v !== seenCfg) voiceView = null; // walterctl changed it: that wins
      seenCfg = cfgView = v;
    } catch (e) {}
  };
  pollSettings(); setInterval(pollSettings, 3000);

  function tick() {
    const b = document.body.classList, convo = b.contains("convo");
    const now = Date.now();
    // the voice choice lasts for one conversation
    if (convo) convoOffSince = 0;
    else if (voiceView !== null) {
      if (!convoOffSince) convoOffSince = now;
      else if (now - convoOffSince > 3000) { voiceView = null; convoOffSince = 0; }
    }
    const want = convo && (voiceView || cfgView) === "chat";
    const col = want && b.contains("convo-build");
    if (want !== showing || col !== column) {
      const was = showing;
      showing = want; column = col;
      window.WALTER_CONVO_CHAT = want;
      b.toggle("chat-on", want);
      b.toggle("chat-col", col);
      root.classList.toggle("on", want);
      root.classList.toggle("col", col);
      if (want && !was) draw();
    }
  }
  setInterval(tick, 250);
  setInterval(() => {
    stalePartial();
    const before = thread.msgs().length;
    thread.checkDay(Date.now()); // cleared at midnight: no history across days
    if (before && !thread.msgs().length) soon();
  }, 2000);

  window.walterChat = () => ({ showing, column, cfgView, voiceView, msgs: thread.msgs() }); // for the console
})();
