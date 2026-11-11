/* Conversation mode (owner, 9 Nov 13:11): "when you and I are interacting, it's just you and I,
 * not the calendar, not...".
 *
 * While we talk (listening, thinking, working or speaking: wake word, a spoken turn, or a typed /
 * app turn answered on the wall) the portrait board (calendar, list, weather, podcast card, loop
 * chip, workers, forecast, verse, me row) fades out in ~0.5 s, I grow back to my full centred size
 * (walter.js reads window.WALTER_CONVO) and the captions get big and centred under me, with a soft
 * glow ring showing listening / thinking / speaking. Only after a real back-and-forth: conversation_turns
 * (3) answered exchanges, each within conversation_window_s (45) of my last reply (convgate.js); a single
 * quick Q&A leaves the board alone. The board fades back conversation_hold_s (8) s after the last
 * speech / state change. Landscape: the corner furniture (clock, workers,
 * usage, to-do list, loop chip) fades out too and captions grow a little.
 *
 * Sticky build mode (the owner, 9 Nov 13:25; buildgate.js): "build mode" or `walterctl display build on`
 * brings it up at once and holds it until the owner exits ("done building", Escape, display build off|auto).
 *
 * Muting the mic (Space, walterctl mute, the app, the USB mic's button, auto-mute at sleep, or
 * already muted at start-up) leaves it IMMEDIATELY, sticky build mode included (the owner, 9 Nov 13:33).
 * display.json is not changed; unmuting does not bring it back, a new back-and-forth or "build
 * mode" does (with build_mode "on", only the next explicit "build mode" phrase; buildgate.js).
 *
 * Never kicks in: asleep (sleeping eyes over the dashboard), first-time setup / QR, while the canvas
 * or live browser is open (they keep me small, as before), or during a focus box (the ring stays as
 * it is). Background audio ducking is NOT a conversation: only my own states count.
 * Urgent things stay: restart pill, error pills, mic-off, cards, typebox.
 *
 * Setting: face/display.json {"conversation_mode": "on|off", "conversation_turns": 3,
 * "conversation_window_s": 45, "conversation_hold_s": 8} (walterctl display conversation on|off|turns N|hold N|window N), read every 3 s like the text scale.
 * Opacity/transform only (weak GPU); prefers-reduced-motion = no fades, no pulse.
 */
(() => {
  "use strict";
  const ENGAGED = new Set(["listening", "thinking", "working", "speaking"]);
  let enabled = true, on = false;
  // Only after a real back-and-forth (owner, 9 Nov 13:12): convgate.js counts answered exchanges
  // ([display] conversation_turns, default 3, each turn within conversation_window_s = 45 s of my
  // last reply) and ends it after conversation_hold_s (8) of quiet. One quick Q&A: board untouched.
  const gate = window.WalterConvGate.create({});
  const nowS = () => Date.now() / 1000;
  let micMuted = null; // null = not told yet (the first "mute on" at start-up counts)
  window.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    if (m.t === "mute" || m.t === "hwmute") {
      // Mute exits at once (the owner, 9 Nov 13:33): no hold timer, counter back to 0, settings untouched;
      // unmuting does not re-enter. Also when the face starts muted. buildview.js clears the sticky
      // build flag in the same event, so tick right after this dispatch finishes.
      const was = micMuted;
      if (m.t === "mute") micMuted = !!m.on;
      if (!m.on || was === true || (m.t === "hwmute" && micMuted)) return;
      gate.mute(nowS());
      setTimeout(tick, 0);
    } else if (m.t === "convo") {
      // the companion app's mode switcher (walter/app_mode.py): same as saying "conversation mode" /
      // "close the chat"
      if (m.open) gate.open(nowS()); else gate.close(nowS());
      setTimeout(tick, 0);
    } else if (m.t === "state") gate.state(nowS(), m.state);
    else if (m.t === "caption") {
      if (m.who === "you") {
        if (m.muted) return;
        const partial = !!(m.partial || m.live);
        gate.heard(nowS(), partial);
        // "conversation mode" / "show the chat" opens it now; "close the chat" ends a voice-opened one
        // (the owner, 9 Nov 19:56; chat.js switches to the chat view on the same words)
        const c = !partial && window.WalterChatLib ? window.WalterChatLib.command(m.text) : null;
        if (c && c.cmd === "open") { gate.open(nowS()); setTimeout(tick, 0); }
        else if (c && c.cmd === "close" && gate.close(nowS())) setTimeout(tick, 0);
      }
      else gate.spoke(nowS());
    }
  });

  const css = document.createElement("style");
  css.dataset.portrait = "convo"; // sizes below are already per-orientation: portrait.js leaves it alone
  css.textContent = `
  /* the board and the corner furniture step back */
  body.convo #clock, body.convo #usage, body.convo #workers, body.convo #checklist,
  body.convo #loopchip, body.convo #focusstrip, body.convo #sayname {
    opacity: 0 !important; visibility: hidden; transition: opacity 0.5s ease, visibility 0s 0.5s !important; }
  html.big body.board-on.convo #dash { opacity: 0 !important; visibility: hidden;
    transition: opacity 0.5s ease, visibility 0s 0.5s !important; }
  html.big body.board-on #dash { will-change: opacity; }
  /* captions: big and centred under me */
  body.convo #caption { font-size: 3.4vh; max-width: 80vw; color: rgba(255,255,255,0.8); }
  @media (orientation: portrait) {
    html body.convo #caption, html.big body.convo #caption { top: 72vh; bottom: auto; font-size: 4.6vw;
      max-width: 88vw; line-height: 1.3; padding: 1.2vw 3vw; border-radius: 3vw; background: rgba(5,6,10,0.55); }
  }
  body.convo #caption.karaoke .w.said { color: rgba(255,255,255,0.85); }
  body.convo #caption.karaoke .w.now { color: #fff; }
  /* the glow ring round me: one composited layer, moved with transform, faded with opacity */
  #convoring { position: fixed; left: 0; top: 0; width: 400px; height: 400px; margin: -200px 0 0 -200px;
    border-radius: 50%; pointer-events: none; z-index: 1; opacity: 0; will-change: transform, opacity;
    border: 1.5px solid currentColor; color: rgba(150,190,255,0.9);
    box-shadow: 0 0 18px 2px currentColor, inset 0 0 18px 2px currentColor; transition: opacity 0.5s ease, color 0.5s ease; }
  #convoring.on { opacity: 0.35; }
  #convoring.listening { color: rgba(150,200,255,0.9); animation: cvbreath 2.2s ease-in-out infinite; }
  #convoring.thinking, #convoring.working { color: rgba(255,214,140,0.85); animation: cvbreath 1.3s ease-in-out infinite; }
  #convoring.speaking { color: rgba(160,255,210,0.85); }
  @keyframes cvbreath { 50% { opacity: 0.12; } }
  @media (prefers-reduced-motion: reduce) {
    body.convo #clock, body.convo #usage, body.convo #workers, body.convo #checklist, body.convo #loopchip,
    body.convo #focusstrip, body.convo #sayname, html.big body.board-on.convo #dash,
    html.big body.board-on #dash, #convoring { transition: none !important; animation: none !important; }
  }`;
  document.head.appendChild(css);

  const ring = document.createElement("div");
  ring.id = "convoring";
  document.body.appendChild(ring);

  const pollSettings = async () => {
    try {
      const res = await fetch("display.json", { cache: "no-store" });
      if (!res.ok) return;
      const d = await res.json();
      gate.configure(d);
      enabled = gate.cfg.mode !== "off";
    } catch (e) {}
  };
  pollSettings(); setInterval(pollSettings, 3000);

  function blocked(S) {
    const b = document.body.classList;
    const FB = window.WALTER_FOCUS;
    return S.state === "sleeping" || S.dash || S.setup || S.canvas || S.browser ||
      b.contains("canvas-on") || b.contains("browser-on") || b.contains("dash-on") || !!(FB && FB.on);
  }

  function tick() {
    const S = window.WALTER_STATE;
    if (!S) return;
    // sticky build mode (buildview.js sets WALTER_BUILD_STICKY): stays up until the owner exits it; sleep,
    // setup, canvas / browser and focus boxes still win, and it comes back when they end.
    gate.setSticky(!!window.WALTER_BUILD_STICKY, nowS());
    const want = gate.tick(nowS()) && (enabled || gate.g.sticky || gate.g.opened) && !blocked(S);
    if (want !== on) {
      on = want;
      window.WALTER_CONVO = on;
      document.body.classList.toggle("convo", on);
      if (on) requestAnimationFrame(follow);
    }
    ring.className = on && ENGAGED.has(S.state) ? "on " + S.state : "";
  }
  setInterval(tick, 120);

  // keep the ring on me while I grow and move (only runs while a conversation is on)
  function follow() {
    const S = window.WALTER_STATE || {}, p = S.pos;
    if (p && p.R) {
      const k = (p.R * 2.5) / 400;
      ring.style.transform = "translate(" + p.cx.toFixed(1) + "px," + p.cy.toFixed(1) + "px) scale(" + k.toFixed(3) + ")";
    }
    if (on) requestAnimationFrame(follow);
  }
  window.walterConvo = () => ({ on, enabled, cfg: gate.cfg, gate: gate.g }); // for the console (and chat.js)
})();
