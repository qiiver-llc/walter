/*
 * Walter's face.
 *
 * A soft glowing blob with two eyes. No mouth, (almost) no text: every
 * feeling is shown through body shape, motion, colour and eye shape.
 *
 * The backend drives it over a websocket (see walter/server.py):
 *   {t:"state",   state:"idle|listening|thinking|working|speaking|sleeping"}
 *   {t:"emotion", name:"happy", hold:20}
 *   {t:"action",  name:"bounce"}   (or "look:left", "look:up", ...)
 *   {t:"look",    x:0.4, y:0.1, hold:3}   glance at a spot (-1..1)
 *   {t:"camera",  on:true}         presence camera indicator
 *   {t:"cloud",   tts, stt, rt, idle_tts, idle_stt, idle_rt}  cloud speech indicator
 *                 (OpenAI voice / ears / realtime "live" session streaming the mic)
 *   {t:"level",   v:0..1}           speaking loudness, ~30x per second
 *   {t:"hear",    v:0..1}           someone is talking (mic level)
 *   {t:"caption", who:"you|walter", text:"...", partial:true?}  (partial = live, still talking)
 *     Walter's own: + words:[[word, start_s, end_s], ...], dur:s, muted:bool  (karaoke timing)
 *   {t:"listen",  phase:"open|heard|sent|off"}  "Listening…" / "Got it" / "Thinking…" / hidden
 *   {t:"config",  captions:true|false}
 *   {t:"jobs",    n:2}              background tasks running (orbiting satellites)
 *   {t:"version", v}       face code version, sent first on every (re)connect
 *   {t:"reload", v?, force?}
 *
 * Walter is allowed to edit this file to change his own look. The server
 * reloads the page automatically when anything in face/ changes.
 *
 * Keyboard (handy for testing on the laptop itself):
 *   Enter / t  open the type box to talk to Walter
 *   Space      mute / unmute the microphone
 *   1-9, 0     preview emotions        b w n s j  preview actions
 *   c          toggle captions         z          toggle sleep
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- presets
  // Eye params:  sx, sy  size multipliers
  //              lid     top lid coverage 0..1     tilt  lid angle (+ = inner corner down / angry)
  //              cheek   bottom "smile" cut 0..1   round corner roundness 0..1
  //              heart   0..1 crossfade into heart-shaped eyes
  // Body params: sx, sy  squash/stretch      wob  wobble energy
  //              drop    sink downwards (sad) hop  idle hopping energy  jit  shiver
  const BASE_EYE = { sx: 1, sy: 1, lid: 0, tilt: 0, cheek: 0, round: 1, heart: 0 };
  const BASE_BODY = { sx: 1, sy: 1, wob: 1, drop: 0, hop: 0, jit: 0 };

  const EMOTIONS = {
    neutral:     { color: [88, 196, 255] },
    happy:       { color: [255, 205, 86],  eye: { cheek: 0.55, sy: 0.95 }, body: { sx: 1.03, sy: 0.98, wob: 1.3, hop: 0.25 } },
    excited:     { color: [255, 128, 92],  eye: { sx: 1.15, sy: 1.2, cheek: 0.35 }, body: { wob: 2.0, hop: 1.0 }, fx: "sparkle" },
    love:        { color: [255, 120, 180], eye: { heart: 1, sx: 1.2, sy: 1.05 }, body: { wob: 1.4, hop: 0.2 }, fx: "heart" },
    sad:         { color: [70, 110, 220],  eye: { lid: 0.3, tilt: -0.35, sy: 0.9 }, body: { sx: 1.07, sy: 0.92, wob: 0.5, drop: 0.08 }, fx: "tear" },
    angry:       { color: [255, 72, 72],   eye: { lid: 0.38, tilt: 0.45, sy: 0.9, round: 0.6 }, body: { sx: 1.04, sy: 0.96, wob: 0.6, jit: 0.5 } },
    surprised:   { color: [140, 235, 255], eye: { sx: 1.25, sy: 1.25, round: 1 }, body: { sx: 0.95, sy: 1.08, wob: 0.8 } },
    confused:    { color: [170, 120, 255], eyeL: { sy: 1.15, sx: 1.1 }, eyeR: { sy: 0.7, lid: 0.25, tilt: -0.2 }, body: { wob: 1.1 }, fx: "question" },
    thinking:    { color: [130, 120, 255], eyeL: { lid: 0.2, tilt: -0.1 }, eyeR: { lid: 0.05 }, body: { wob: 0.7 } },
    curious:     { color: [80, 230, 190],  eyeL: { sx: 1.1, sy: 1.15 }, eyeR: { sx: 0.95, sy: 0.95, lid: 0.12 }, body: { sx: 0.98, sy: 1.04 } },
    sleepy:      { color: [60, 90, 170],   eye: { lid: 0.62, tilt: -0.08 }, body: { sx: 1.04, sy: 0.95, wob: 0.4, drop: 0.04 } },
    proud:       { color: [255, 190, 70],  eye: { lid: 0.32, cheek: 0.4, tilt: -0.05 }, body: { sx: 1.06, sy: 1.06, wob: 0.8 }, fx: "sparkle" },
    scared:      { color: [190, 180, 255], eye: { sx: 0.8, sy: 1.2 }, body: { sx: 0.92, sy: 0.95, jit: 1.0, drop: 0.03 }, fx: "sweat" },
    embarrassed: { color: [255, 130, 150], eye: { cheek: 0.5, sy: 0.75, lid: 0.15 }, body: { sx: 1.03, sy: 0.96 }, fx: "sweat" },
    mischievous: { color: [170, 230, 90],  eyeL: { lid: 0.45, tilt: 0.25, cheek: 0.3 }, eyeR: { lid: 0.2, cheek: 0.35 }, body: { wob: 1.2 } },
    wink:        { color: [255, 200, 110], eyeL: { cheek: 0.5 }, eyeR: { sy: 0.08, cheek: 0 }, body: { hop: 0.2 } },
    calm:        { color: [110, 200, 210], eye: { lid: 0.25, cheek: 0.25 }, body: { wob: 0.5 } },
  };
  const EMOTION_KEYS = ["neutral", "happy", "excited", "love", "sad", "angry", "surprised", "confused", "curious", "sleepy"];

  // What each *state* layers on top of the emotion.
  const STATES = {
    idle:      {},
    listening: { eye: { sx: 1.06, sy: 1.1 }, body: { sx: 1.02, sy: 1.04 } },
    thinking:  { mix: "thinking", amount: 0.35, eye: {}, body: {} },
    working:   { mix: "thinking", amount: 0.5 },
    speaking:  {},
    sleeping:  { eye: { sy: 0.07, lid: 0, cheek: 0, heart: 0 }, body: { sx: 1.06, sy: 0.92, wob: 0.3, drop: 0.05, hop: 0 } },
  };

  // ------------------------------------------------------------------ setup
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const captionEl = document.getElementById("caption");
  const typebox = document.getElementById("typebox");
  const typein = document.getElementById("typein");
  const params = new URLSearchParams(location.search);
  const LITE = params.has("lite"); // cheaper rendering for very old GPUs

  let W = 0, H = 0, DPR = 1, PORTRAIT = false;
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, LITE ? 1 : 2);
    W = window.innerWidth; H = window.innerHeight;
    PORTRAIT = window.matchMedia("(orientation: portrait)").matches; // same test as portrait.js
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  // ------------------------------------------------------------------ state
  const S = {
    state: "idle",
    emotion: "neutral",
    emotionUntil: 0,
    level: 0, levelTarget: 0,       // speaking loudness
    hear: 0, hearTarget: 0,          // mic level
    captions: params.has("captions"),
    live: true,                      // show live "what I'm hearing" while you talk
    connected: false,
    // smoothed render params
    color: [88, 196, 255],
    eyeL: { ...BASE_EYE }, eyeR: { ...BASE_EYE },
    body: { ...BASE_BODY },
    gaze: { x: 0, y: 0 }, gazeTarget: { x: 0, y: 0 }, nextSaccade: 0,
    lookOverride: null, lookUntil: 0,
    blink: 0, blinkT: -1, nextBlink: 1.5,
    actions: [],                     // running one-shot body animations
    particles: [],
    ripples: [],
    nextFx: 0,
    dots: 0,                         // thinking dots visibility 0..1
    hud: 0,                          // "working" HUD rings visibility 0..1
    setup: false, setupMix: 0,       // first-time setup: Walter moves aside for the QR code
    browser: false, browserMix: 0,   // live browser panel (browser.js, ~2/3 width): step further left, smaller
    canvas: false, canvasMix: 0,     // canvas panel open on the right (canvas.js): Walter steps left
    dash: false, dashMix: 0,         // dashboard mode (dashboard.js): asleep, small, in the corner
    dashMode: "",                    // "day" | "night" | "dark" (dark: hidden, only a faint clock)
    focusQuiet: false, focusMix: 0,  // focus box running (focus.js): small and calm in the bottom-right corner
    focusCorner: false, focusTalk: false, focusTalkMix: 0, // in the corner / talking there (a bit bigger)
    engagedUntil: 0,                 // talking with someone: a bit bigger in the corner until then (seconds, now())
    frameN: 0,
    jobs: 0, jobsVis: 0,             // background tasks running
    dim: 1,
    muted: false,                    // the person muted the mic (Space bar)
    hopPhase: 0,
  };

  const now = () => performance.now() / 1000;
  const lerp = (a, b, k) => a + (b - a) * k;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const rand = (a, b) => a + Math.random() * (b - a);
  const ease = (dt, speed) => 1 - Math.exp(-dt * speed);

  function setEmotion(name, hold) {
    if (!EMOTIONS[name]) return;
    S.emotion = name;
    S.emotionUntil = name === "neutral" ? 0 : now() + (hold || 25);
    if (name === "surprised") addAction("pop");
    if (name === "excited") addAction("bounce");
  }

  function setState(st) {
    if (!STATES[st]) return;
    const prev = S.state;
    S.state = st;
    if (st === "listening" && prev !== "listening") addAction("perk");
    if (prev === "sleeping" && st !== "sleeping") { addAction("pop"); S.nextBlink = now() + 0.2; }
  }

  // ---------------------------------------------------------------- actions
  // Each action returns {dx, dy, rot, sx, sy} offsets for progress p (0..1).
  const ACTIONS = {
    bounce: { dur: 0.9, f: (p) => { const k = Math.abs(Math.sin(p * Math.PI * 2)) * (1 - p); return { dy: -0.22 * k, sy: 1 + 0.08 * k, sx: 1 - 0.05 * k }; } },
    hop:    { dur: 0.5, f: (p) => { const k = Math.sin(p * Math.PI); return { dy: -0.15 * k, sy: 1 + 0.06 * k, sx: 1 - 0.04 * k }; } },
    pop:    { dur: 0.45, f: (p) => { const k = Math.sin(p * Math.PI) * (1 - p * 0.5); return { sx: 1 + 0.08 * k, sy: 1 + 0.12 * k }; } },
    perk:   { dur: 0.35, f: (p) => { const k = Math.sin(p * Math.PI); return { dy: -0.03 * k, sy: 1 + 0.05 * k }; } },
    wiggle: { dur: 1.0, f: (p) => ({ rot: 0.12 * Math.sin(p * Math.PI * 6) * (1 - p) }) },
    shake:  { dur: 0.8, f: (p) => ({ dx: 0.06 * Math.sin(p * Math.PI * 6) * (1 - p), gx: Math.sin(p * Math.PI * 6) * (1 - p) }) },
    nod:    { dur: 0.9, f: (p) => { const k = Math.sin(p * Math.PI * 4) * (1 - p); return { dy: 0.03 * k, gy: 0.8 * k, sy: 1 - 0.03 * k }; } },
    jump:   { dur: 1.1, f: (p) => { const up = p < 0.15 ? -p / 0.15 * 0.05 : Math.sin((p - 0.15) / 0.85 * Math.PI); const sq = p < 0.15 ? 1 - p / 0.15 * 0.1 : 1; return { dy: p < 0.15 ? 0.04 * (p / 0.15) : -0.35 * up, sy: sq, sx: 2 - sq }; } },
    spin:   { dur: 1.0, f: (p) => { const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2; return { rot: e * Math.PI * 2 }; } },
    shiver: { dur: 1.2, f: (p) => ({ dx: 0.012 * Math.sin(p * 90) * (1 - p) }) },
    melt:   { dur: 2.0, f: (p) => { const k = Math.sin(p * Math.PI); return { sx: 1 + 0.15 * k, sy: 1 - 0.18 * k, dy: 0.08 * k }; } },
    puff:   { dur: 1.2, f: (p) => { const k = Math.sin(p * Math.PI); return { sx: 1 + 0.12 * k, sy: 1 + 0.12 * k }; } },
    // expressive moves Walter may choose himself (owner: "move around, get bigger or smaller, whatever")
    grow:   { dur: 3.0, f: (p) => { const k = Math.sin(p * Math.PI) ** 0.6; return { sx: 1 + 0.35 * k, sy: 1 + 0.35 * k }; } },
    shrink: { dur: 3.0, f: (p) => { const k = Math.sin(p * Math.PI) ** 0.6; return { sx: 1 - 0.35 * k, sy: 1 - 0.35 * k, dy: 0.05 * k }; } },
    "scoot:left":  { dur: 4.0, f: (p) => { const k = Math.sin(p * Math.PI) ** 0.5; return { dx: -0.9 * k, rot: -0.05 * Math.sin(p * Math.PI * 4) * (1 - p) }; } },
    "scoot:right": { dur: 4.0, f: (p) => { const k = Math.sin(p * Math.PI) ** 0.5; return { dx: 0.9 * k, rot: 0.05 * Math.sin(p * Math.PI * 4) * (1 - p) }; } },
    peek:   { dur: 3.0, f: (p) => { const k = Math.sin(p * Math.PI) ** 0.5; return { dy: 0.6 * k, sy: 1 - 0.05 * k }; } },
    // a chuckle: quick little shoulder-shaking hops that fade out, head tipped back a touch
    laugh:  { dur: 1.4, f: (p) => { const k = Math.abs(Math.sin(p * Math.PI * 7)) * (1 - p) ; return { dy: -0.05 * k, sy: 1 + 0.05 * k, sx: 1 - 0.03 * k, rot: 0.04 * Math.sin(p * Math.PI) , gy: -0.4 * (1 - p) }; } },
  };

  // the only body moves allowed while I sit small in the focus corner (tiny, no travel)
  const CALM_ACTIONS = new Set(["perk", "nod", "pop", "shiver"]);
  // Focus corner (owner, 5 Nov): while a time box runs I shrink into the bottom-right corner and
  // keep still; talking to me brings me back to normal for a bit, then I settle back.
  const ENGAGED_STATES = new Set(["listening", "thinking", "working", "speaking"]);
  const SETTLE_AFTER = 6; // seconds back at normal size after a conversation goes quiet
  // Big ring (owner, 5 Nov 11:33): the box's ring now sits big in the middle (focus.js), so while
  // we talk mid-box I stay in the corner and only grow a little (S.focusTalk) instead of coming
  // home over it. Returns true while I'm in the corner, quiet or talking.
  function focusCornerWanted(t) {
    const FB = window.WALTER_FOCUS;
    if (!FB || !FB.on || S.dash || S.setup || S.canvas || S.browser || S.state === "sleeping") {
      S.focusTalk = false; return false;
    }
    if (ENGAGED_STATES.has(S.state)) S.engagedUntil = t + SETTLE_AFTER;
    const followup = (S.followupUntil || 0) > Date.now() / 1000; // he can still answer without my name
    S.focusTalk = t <= S.engagedUntil || followup;
    return true;
  }
  window.walterFocusCorner = focusCornerWanted; // for tests / the console

  function addAction(name) {
    if (name.startsWith("look:")) {
      const dir = name.slice(5);
      const L = { left: [-1, 0], right: [1, 0], up: [0, -1], down: [0, 1], center: [0, 0], "up-left": [-0.8, -0.8], "up-right": [0.8, -0.8], "down-left": [-0.8, 0.8], "down-right": [0.8, 0.8] }[dir];
      if (L) { S.lookOverride = { x: L[0], y: L[1] }; S.lookUntil = now() + 2.5; }
      return;
    }
    if (name === "blink") { S.blinkT = 0; return; }
    if (!ACTIONS[name]) return;
    if (S.focusQuiet && !CALM_ACTIONS.has(name)) return; // focus box: no bounces, scoots or spins in the corner
    if (S.focusCorner && !S.big && name === "scoot:right") return; // talking in the corner: no room to the right
    S.actions.push({ name, t0: now() });
  }

  // -------------------------------------------------------------- particles
  function spawn(type, x, y, R) {
    const p = { type, x, y, vx: rand(-0.15, 0.15) * R, vy: -rand(0.25, 0.45) * R, life: 0, max: rand(2, 3.2), size: rand(0.07, 0.11) * R, rot: rand(-0.4, 0.4) };
    if (type === "tear") { p.vx = 0; p.vy = 0.05 * R; p.max = 2.2; p.size = 0.06 * R; }
    if (type === "sweat") { p.vx = 0.02 * R; p.vy = 0.08 * R; p.max = 1.6; p.size = 0.06 * R; }
    if (type === "z") { p.vx = rand(0.12, 0.2) * R; p.vy = -0.22 * R; p.max = 3.5; p.size = rand(0.1, 0.16) * R; }
    if (type === "sparkle") { p.vx = rand(-0.3, 0.3) * R; p.vy = rand(-0.3, 0.1) * R; p.max = rand(0.8, 1.4); p.size = rand(0.04, 0.08) * R; }
    if (type === "question") { p.vx = 0.05 * R; p.vy = -0.12 * R; p.max = 2.5; p.size = 0.18 * R; }
    S.particles.push(p);
  }

  // ------------------------------------------------------------ the update
  function mixInto(dst, src, amount) {
    if (!src) return;
    for (const k in src) dst[k] = lerp(dst[k], src[k], amount);
  }

  function targetFor() {
    const e = EMOTIONS[S.emotion] || EMOTIONS.neutral;
    const st = STATES[S.state] || {};
    const eyeL = { ...BASE_EYE, ...(e.eye || {}), ...(e.eyeL || {}) };
    const eyeR = { ...BASE_EYE, ...(e.eye || {}), ...(e.eyeR || {}) };
    const body = { ...BASE_BODY, ...(e.body || {}) };
    // themes.js: my neutral body colour follows the theme (other emotions keep their own colours)
    const TH = window.WALTER_THEME;
    let color = (e === EMOTIONS.neutral && TH && TH.blob ? TH.blob : e.color).slice();
    if (st.mix) {
      const m = EMOTIONS[st.mix];
      if (S.emotion === "neutral") {
        mixInto(eyeL, { ...BASE_EYE, ...(m.eye || {}), ...(m.eyeL || {}) }, 1);
        mixInto(eyeR, { ...BASE_EYE, ...(m.eye || {}), ...(m.eyeR || {}) }, 1);
      }
      color = color.map((c, i) => lerp(c, m.color[i], st.amount));
    }
    if (st.eye) { mixInto(eyeL, st.eye, 1); mixInto(eyeR, st.eye, 1); }
    // multiplicative bits of the state (size) rather than override
    if (S.state === "listening") { eyeL.sx *= 1.06; eyeR.sx *= 1.06; }
    if (S.state === "sleeping") { color = color.map((c, i) => lerp(c, [50, 70, 140][i], 0.7)); }
    if (st.body) mixInto(body, st.body, 1);
    if (!S.connected) { color = color.map((c) => lerp(c, 120, 0.75)); eyeL.lid = 0.3; eyeR.lid = 0.3; }
    return { eyeL, eyeR, body, color };
  }

  function update(dt, t, R) {
    if (S.emotionUntil && t > S.emotionUntil && S.state !== "speaking") { S.emotion = "neutral"; S.emotionUntil = 0; }

    const tg = targetFor();
    const k = ease(dt, 7);
    mixInto(S.eyeL, tg.eyeL, k);
    mixInto(S.eyeR, tg.eyeR, k);
    mixInto(S.body, tg.body, ease(dt, 4));
    S.color = S.color.map((c, i) => lerp(c, tg.color[i], ease(dt, 2.5)));
    S.dim = lerp(S.dim, S.state === "sleeping" ? (S.dash && S.dashMode !== "day" ? 0.28 : 0.45) : S.muted ? 0.72 : 1, ease(dt, 1.5));

    S.level = lerp(S.level, S.levelTarget, ease(dt, S.levelTarget > S.level ? 30 : 12));
    S.hear = lerp(S.hear, S.hearTarget, ease(dt, 10));
    S.hearTarget *= Math.exp(-dt * 3);
    if (S.state !== "speaking") S.levelTarget *= Math.exp(-dt * 8);

    // thinking dots
    S.dots = lerp(S.dots, S.state === "thinking" ? 1 : 0, ease(dt, 5));
    S.hud = lerp(S.hud, S.state === "working" ? 1 : 0, ease(dt, 4));
    S.jobsVis = lerp(S.jobsVis, S.jobs > 0 ? 1 : 0, ease(dt, 3));

    // --- gaze
    if (S.lookOverride && t < S.lookUntil) {
      S.gazeTarget = { ...S.lookOverride };
    } else if (S.state === "thinking" || S.state === "working") {
      const sp = S.state === "working" ? 1.6 : 0.7;
      S.gazeTarget = { x: 0.55 + 0.25 * Math.cos(t * sp), y: -0.65 + 0.15 * Math.sin(t * sp * 1.3) };
    } else if (S.state === "sleeping") {
      S.gazeTarget = { x: 0, y: 0.3 };
    } else if (t > S.nextSaccade) {
      const focused = S.state === "listening" || S.state === "speaking";
      const spread = focused ? 0.25 : S.focusQuiet ? 0.35 : 0.9;
      S.gazeTarget = Math.random() < (focused ? 0.6 : 0.35) ? { x: rand(-0.1, 0.1), y: rand(-0.05, 0.1) } : { x: rand(-spread, spread), y: rand(-spread * 0.6, spread * 0.6) };
      S.nextSaccade = t + (focused ? rand(0.8, 2.2) : S.focusQuiet ? rand(4, 9) : rand(1.0, 4.5));
    }
    S.gaze.x = lerp(S.gaze.x, S.gazeTarget.x, ease(dt, 14));
    S.gaze.y = lerp(S.gaze.y, S.gazeTarget.y, ease(dt, 14));

    // --- idle fidgets: while idle, now and then do something with my body
    // (owner: "be more animated, move around the screen more"). Not while
    // talking, listening, asleep, muted, or while the canvas is open.
    if (S.nextFidget === undefined) S.nextFidget = t + rand(20, 45);
    if (t > S.nextFidget) {
      const calm = S.state === "idle" && !S.muted && !S.focusQuiet && !document.body.classList.contains("canvas-on") && !document.body.classList.contains("browser-on") && !S.actions.length;
      if (calm) {
        const r = Math.random();
        const pick = r < 0.18 ? ["scoot:left", "look:left"] : r < 0.36 ? ["scoot:right", "look:right"]
          : r < 0.48 ? ["peek"] : r < 0.58 ? ["wiggle"] : r < 0.68 ? ["hop"] : r < 0.76 ? ["grow"]
          : r < 0.84 ? ["look:up"] : r < 0.92 ? ["puff"] : ["spin"];
        pick.forEach((a, i) => setTimeout(() => addAction(a), i * 600));
      }
      S.nextFidget = t + rand(35, 90);
    }

    // --- blink
    if (S.blinkT < 0 && t > S.nextBlink && S.state !== "sleeping") {
      S.blinkT = 0;
      S.nextBlink = t + (Math.random() < 0.15 ? 0.3 : rand(2.0, 6.0)); // occasional double blink
    }
    if (S.blinkT >= 0) {
      S.blinkT += dt;
      const d = 0.16;
      S.blink = S.blinkT < d / 2 ? S.blinkT / (d / 2) : 1 - (S.blinkT - d / 2) / (d / 2);
      if (S.blinkT >= d) { S.blinkT = -1; S.blink = 0; }
      S.blink = clamp(S.blink, 0, 1);
    }

    // --- actions
    S.actions = S.actions.filter((a) => t - a.t0 < ACTIONS[a.name].dur);

    // --- particles / fx
    const fx = (EMOTIONS[S.emotion] || {}).fx;
    if (S.state === "sleeping") {
      if (t > S.nextFx) { spawn("z", 0, 0, R); S.nextFx = t + 1.6; }
    } else if (fx && !S.focusQuiet && t > S.nextFx) { // no sparkles/hearts in the focus corner
      if (fx === "heart") { spawn("heart", rand(-0.6, 0.6) * R, -0.7 * R, R); S.nextFx = t + rand(0.6, 1.2); }
      else if (fx === "sparkle") { spawn("sparkle", rand(-1.1, 1.1) * R, rand(-1.0, 0.4) * R, R); S.nextFx = t + rand(0.15, 0.4); }
      else if (fx === "tear") { spawn("tear", 0, 0, R); S.nextFx = t + rand(3, 5); }
      else if (fx === "sweat") { spawn("sweat", 0, 0, R); S.nextFx = t + rand(2.5, 4); }
      else if (fx === "question") { spawn("question", 0, 0, R); S.nextFx = t + 2.6; }
    }
    for (const p of S.particles) {
      p.life += dt;
      p.x += p.vx * dt; p.y += p.vy * dt;
      if (p.type === "tear" || p.type === "sweat") p.vy += 0.25 * R * dt;
    }
    S.particles = S.particles.filter((p) => p.life < p.max);

    // --- listening ripples driven by mic level
    if (S.hear > 0.15 && (S.ripples.length === 0 || t - S.ripples[S.ripples.length - 1].t0 > 0.45)) S.ripples.push({ t0: t, a: clamp(S.hear, 0.2, 1) });
    S.ripples = S.ripples.filter((r) => t - r.t0 < 1.6);
  }

  // ------------------------------------------------------------- rendering
  const rgb = (c, a = 1) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
  const shade = (c, f) => c.map((v) => clamp(f > 0 ? v + (255 - v) * f : v * (1 + f), 0, 255));

  function blobPoints(R, sx, sy, t, wob, level) {
    const N = LITE ? 64 : 120, pts = [];
    for (let i = 0; i < N; i++) {
      const a = (i / N) * Math.PI * 2;
      let r = 1
        + wob * (0.020 * Math.sin(2 * a + t * 0.9) + 0.013 * Math.sin(3 * a - t * 1.3 + 1) + 0.008 * Math.sin(5 * a + t * 2.1 + 2))
        + level * (0.025 * Math.sin(4 * a + t * 11) + 0.014 * Math.sin(6 * a - t * 15));
      const s = Math.sin(a);
      if (s > 0) r *= 1 - 0.07 * Math.pow(s, 4); // slightly flatter bottom: he "sits"
      pts.push([Math.cos(a) * r * R * sx, s * r * R * sy]);
    }
    return pts;
  }

  function tracePath(pts) {
    const n = pts.length;
    ctx.beginPath();
    const mid = (p, q) => [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
    let m = mid(pts[n - 1], pts[0]);
    ctx.moveTo(m[0], m[1]);
    for (let i = 0; i < n; i++) {
      const p = pts[i], q = pts[(i + 1) % n];
      m = mid(p, q);
      ctx.quadraticCurveTo(p[0], p[1], m[0], m[1]);
    }
    ctx.closePath();
  }

  function roundRect(x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function heartPath(cx, cy, s) {
    ctx.beginPath();
    ctx.moveTo(cx, cy + s * 0.35);
    ctx.bezierCurveTo(cx - s * 0.05, cy + s * 0.3, cx - s * 0.55, cy + s * 0.05, cx - s * 0.5, cy - s * 0.2);
    ctx.bezierCurveTo(cx - s * 0.45, cy - s * 0.5, cx - s * 0.05, cy - s * 0.5, cx, cy - s * 0.22);
    ctx.bezierCurveTo(cx + s * 0.05, cy - s * 0.5, cx + s * 0.45, cy - s * 0.5, cx + s * 0.5, cy - s * 0.2);
    ctx.bezierCurveTo(cx + s * 0.55, cy + s * 0.05, cx + s * 0.05, cy + s * 0.3, cx, cy + s * 0.35);
    ctx.closePath();
  }

  // side: -1 left eye, +1 right eye (tilt is mirrored so "inner corner" means the same on both)
  function drawEye(ex, ey, R, e, side, speakSquint) {
    const w = R * 0.29 * e.sx;
    const hFull = R * 0.45 * e.sy * (1 - 0.08 * speakSquint);
    const h = Math.max(hFull * (1 - S.blink * 0.94), R * 0.025);
    const x = ex - w / 2, y = ey - h / 2;
    const eyeCol = (window.WALTER_THEME && window.WALTER_THEME.eye) || [16, 20, 36]; // themes.js

    ctx.save();
    // top lid: keep everything below a tilted line
    if (e.lid > 0.01) {
      const ly = y + e.lid * h;
      const slope = Math.tan(e.tilt) * -side; // inner corner down when tilt > 0
      const big = R * 3;
      ctx.beginPath();
      ctx.moveTo(ex - big, ly - slope * big);
      ctx.lineTo(ex + big, ly + slope * big);
      ctx.lineTo(ex + big, ly + big * 2);
      ctx.lineTo(ex - big, ly + big * 2);
      ctx.closePath();
      ctx.clip();
    }
    // bottom "smile" lid: cut away a circle from below => ^ ^ eyes
    if (e.cheek > 0.01) {
      const cr = w * 1.1;
      const cy = y + h + cr - e.cheek * h * 0.85;
      const p = new Path2D();
      p.rect(ex - R * 3, ey - R * 3, R * 6, R * 6);
      p.arc(ex, cy, cr, 0, Math.PI * 2);
      ctx.clip(p, "evenodd");
    }

    ctx.fillStyle = rgb(eyeCol);
    const heartA = clamp(e.heart, 0, 1);
    if (heartA < 0.99) {
      ctx.globalAlpha = 1 - heartA;
      roundRect(x, y, w, h, Math.min(w, h) * 0.5 * clamp(e.round, 0.2, 1) + 0.001);
      ctx.fill();
    }
    if (heartA > 0.01) {
      ctx.globalAlpha = heartA;
      ctx.fillStyle = "rgb(200,30,80)";
      heartPath(ex, ey, Math.max(w, hFull) * 1.15 * (1 - S.blink * 0.9));
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    // sparkle highlights (only when the eye is open enough)
    if (h > R * 0.12 && heartA < 0.5) {
      ctx.fillStyle = "rgba(255,255,255,0.9)";
      ctx.beginPath(); ctx.arc(ex - w * 0.18, ey - h * 0.22, w * 0.17, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.5)";
      ctx.beginPath(); ctx.arc(ex + w * 0.16, ey + h * 0.18, w * 0.08, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }

  function drawParticle(p, R, col) {
    const a = clamp(Math.min(p.life / 0.3, (p.max - p.life) / 0.6), 0, 1);
    ctx.save();
    ctx.translate(p.x, p.y);
    ctx.globalAlpha = a;
    if (p.type === "heart") {
      ctx.rotate(p.rot);
      ctx.fillStyle = "rgba(255,110,170,0.95)";
      heartPath(0, 0, p.size * 2);
      ctx.fill();
    } else if (p.type === "sparkle") {
      ctx.fillStyle = "rgba(255,250,220,0.95)";
      const s = p.size * (1 - Math.abs(p.life / p.max - 0.5));
      ctx.beginPath();
      for (let i = 0; i < 8; i++) {
        const r = i % 2 ? s * 0.3 : s * 1.4, ang = (i / 8) * Math.PI * 2;
        ctx.lineTo(Math.cos(ang) * r, Math.sin(ang) * r);
      }
      ctx.closePath(); ctx.fill();
    } else if (p.type === "tear" || p.type === "sweat") {
      ctx.fillStyle = p.type === "tear" ? "rgba(150,210,255,0.9)" : "rgba(200,235,255,0.85)";
      const s = p.size;
      ctx.beginPath();
      ctx.moveTo(0, -s * 1.4);
      ctx.bezierCurveTo(s * 0.9, -s * 0.2, s, s, 0, s);
      ctx.bezierCurveTo(-s, s, -s * 0.9, -s * 0.2, 0, -s * 1.4);
      ctx.fill();
    } else if (p.type === "z" || p.type === "question") {
      ctx.fillStyle = p.type === "z" ? "rgba(200,215,255,0.75)" : rgb(shade(col, 0.6), 0.9);
      ctx.font = `700 ${p.size * 2}px system-ui, sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.rotate(p.rot * 0.5);
      ctx.fillText(p.type === "z" ? "z" : "?", 0, 0);
    }
    ctx.restore();
  }

  // One satellite per background task, on a faint tilted orbit. Drawn in two
  // passes (back half behind the body, front half in front) so it looks 3D.
  function drawOrbit(cx, cy, R, col, t, front) {
    if (S.jobsVis < 0.01) return;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-0.25);
    const ox = R * 1.75, oy = R * 0.4;
    ctx.strokeStyle = rgb(shade(col, 0.4), 0.16 * S.jobsVis);
    ctx.lineWidth = Math.max(1, R * 0.006);
    ctx.beginPath();
    if (front) ctx.ellipse(0, 0, ox, oy, 0, 0, Math.PI);
    else ctx.ellipse(0, 0, ox, oy, 0, Math.PI, Math.PI * 2);
    ctx.stroke();
    const n = Math.max(1, S.jobs);
    for (let i = 0; i < n; i++) {
      const a = (t * 0.7 + (i * Math.PI * 2) / n) % (Math.PI * 2);
      if ((Math.sin(a) > 0) !== front) continue;
      const px = Math.cos(a) * ox, py = Math.sin(a) * oy;
      const pulse = 0.75 + 0.25 * Math.sin(t * 4 + i);
      ctx.fillStyle = rgb(shade(col, 0.7), 0.25 * S.jobsVis);
      ctx.beginPath(); ctx.arc(px, py, R * 0.07 * pulse, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = rgb(shade(col, 0.8), 0.9 * S.jobsVis);
      ctx.beginPath(); ctx.arc(px, py, R * 0.035, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }

  let last = now();
  window.WALTER_STATE = S; // read-only peek for small add-ons (say-name hint)
  function frame() {
    const t = now();
    const dt = Math.min(0.05, t - last);
    last = t;

    S.setupMix = lerp(S.setupMix, S.setup ? 1 : 0, ease(dt, 3));
    S.canvasMix = lerp(S.canvasMix, S.canvas ? 1 : 0, ease(dt, 3));
    S.browserMix = lerp(S.browserMix, S.browser ? 1 : 0, ease(dt, 3));
    const cvMix = S.canvasMix * (1 - S.browserMix); // the browser panel sits over the canvas
    S.dashMix = lerp(S.dashMix, S.dash ? 1 : 0, ease(dt, 1.4));
    if (S.dashMix > 0.9999) S.dashMix = 1; else if (S.dashMix < 0.0001) S.dashMix = 0;
    // smoothstep so the trip to the corner (and back) eases in and out
    const dm = S.dashMix * S.dashMix * (3 - 2 * S.dashMix);
    S.focusCorner = focusCornerWanted(t);
    S.focusQuiet = S.focusCorner && !S.focusTalk;
    // grow back a little quicker than I shrink away (box over: show up promptly)
    S.focusMix = lerp(S.focusMix, S.focusCorner ? 1 : 0, ease(dt, S.focusCorner ? 1.1 : 2.2));
    if (S.focusMix > 0.9999) S.focusMix = 1; else if (S.focusMix < 0.0001) S.focusMix = 0;
    S.focusTalkMix = lerp(S.focusTalkMix || 0, S.focusTalk ? 1 : 0, ease(dt, S.focusTalk ? 3 : 1.2));
    const fm = S.focusMix * S.focusMix * (3 - 2 * S.focusMix) * (1 - dm);
    // in the corner: about a third of my size, a bit over half while we talk
    // portrait (portrait.js): panels come up from the bottom, so I shrink a bit more and move up instead of left
    const PT = PORTRAIT;
    S.portrait = PT;
    // big-screen portrait layout (portrait.js html.big): I sit small at the top over the info
    // board; panels and the focus ring don't need me to shrink or move any further
    // conversation mode (converse.js, owner 9 Nov): while we talk the board fades out and I come
    // back to my full, centred portrait size; the board fades back after the hold
    S.big = PT && !S.setup && !S.dash && !window.WALTER_CONVO && document.documentElement.classList.contains("big");
    S.bigMix = REDUCED_MOTION.matches ? (S.big ? 1 : 0) : lerp(S.bigMix || 0, S.big ? 1 : 0, ease(dt, S.big ? 2 : 3));
    if (S.bigMix > 0.9999) S.bigMix = 1; else if (S.bigMix < 0.0001) S.bigMix = 0;
    const bm = S.bigMix * S.bigMix * (3 - 2 * S.bigMix), nb = 1 - bm;
    // build view (buildview.js, owner 9 Nov 13:14): in conversation mode I sit large at the top
    // (portrait, inside the top ~40%) or on the left (landscape) with the live build view beside me
    // the chat view (chat.js, the owner 9 Nov 19:56) puts me in the same spot, the bubbles beside / below me
    const sideOn = window.WALTER_CONVO_BUILD || window.WALTER_CONVO_CHAT;
    S.buildMix = REDUCED_MOTION.matches ? (sideOn ? 1 : 0)
      : lerp(S.buildMix || 0, sideOn ? 1 : 0, ease(dt, 3));
    const bv = S.buildMix * S.buildMix * (3 - 2 * S.buildMix);
    const R = Math.min(W, H) * 0.26 * (1 - 0.3 * S.setupMix) * (1 - (PT ? 0.3 : 0.22) * cvMix * nb) * (1 - (PT ? 0.3 : 0.42) * S.browserMix * nb) * (1 - 0.7 * dm) * (1 - (0.66 - 0.2 * S.focusTalkMix) * fm * nb) * (1 - 0.54 * bm) * (1 - (PT ? 0.45 : 0.4) * bv);
    update(dt, t, R);
    // Dashboard: dark hours hide me completely; otherwise ~30 fps is plenty for a sleepy corner blob
    // (the old laptop CPU also has the dashboard to draw).
    S.frameN++;
    if (S.dash && S.dashMode === "dark" && S.dashMix === 1) {
      if (!S.darkCleared) { ctx.clearRect(0, 0, W, H); S.darkCleared = true; }
      requestAnimationFrame(frame); return;
    }
    S.darkCleared = false;
    if (dm === 1 && (S.frameN & 1)) { requestAnimationFrame(frame); return; }

    // combine one-shot actions
    let ax = 0, ay = 0, arot = 0, asx = 1, asy = 1, agx = 0, agy = 0;
    for (const a of S.actions) {
      const A = ACTIONS[a.name];
      const o = A.f(clamp((t - a.t0) / A.dur, 0, 1));
      ax += o.dx || 0; ay += o.dy || 0; arot += o.rot || 0;
      asx *= o.sx || 1; asy *= o.sy || 1; agx += o.gx || 0; agy += o.gy || 0;
    }

    const B = S.body;
    // breathing + idle hop + speaking pulse
    const breathSpeed = S.state === "sleeping" ? 0.9 : 1.6;
    const breath = Math.sin(t * breathSpeed * (1 - 0.35 * fm)) * (S.state === "sleeping" ? 0.03 : 0.015);
    S.hopPhase += dt * (5 + B.hop * 3);
    const hop = B.hop > 0.02 ? Math.max(0, Math.sin(S.hopPhase)) * B.hop * 0.06 * (1 - fm) : 0;
    const lvl = S.level;
    const sx = B.sx * asx * (1 + breath * 0.6 + lvl * 0.05 - hop * 0.3);
    const sy = B.sy * asy * (1 + breath + lvl * 0.07 + hop * 0.4);
    const jit = B.jit * 0.006 * Math.sin(t * 70);

    // The owner wants Walter always centred: no wandering drift (this is an LCD laptop
    // panel, so burn-in isn't a concern; the idle breathing/blinking keeps him alive).
    const driftX = 0;
    const driftY = 0;
    // dashboard corner: bottom right, clear of the screen edge
    const homeX = PT ? W * 0.5 + driftX
      : W * (0.5 - 0.22 * S.setupMix - 0.24 * cvMix - 0.33 * S.browserMix) + driftX * (1 - S.setupMix) * (1 - 0.5 * S.canvasMix) - W * 0.265 * bv;
    // portrait: a little above the middle; above the setup QR (bottom half), the canvas (lower 60%)
    // or the browser (lower 60% too)
    const homeY = PT ? lerp(H * (0.45 - 0.2 * S.setupMix - 0.25 * cvMix - 0.25 * S.browserMix), H * 0.14, bm) - H * 0.33 * bv : H * 0.5 - H * 0.04 * bv;
    // focus corner: bottom right, ring clear of the screen edge and above the restart pill
    // (big layout: I just stay put at the top, already small; the ring sits over the board)
    const fp = fm * nb;
    const fX = lerp(homeX, W - R * 1.15 - H * 0.022, fp); // between the old gap and half of it (owner, 5 Nov)
    const fY = lerp(homeY + driftY, H - R * 1.08 - H * 0.02, fp); // tight to the bottom (owner, 5 Nov): rings may spill off-screen
    const baseX = lerp(fX, W - R * 1.9 - H * 0.03, dm);
    const baseY = lerp(fY, H - R * 1.35 - H * 0.04, dm);
    const cx = baseX + (ax + jit) * R;
    const cy = baseY + (ay + B.drop - hop) * R - 0.02 * R;
    S.pos = { cx: baseX, cy: baseY, R }; // for add-ons placed under me (focus.js strip)

    // ---- background
    ctx.clearRect(0, 0, W, H);
    const col = S.color.map((c) => c * S.dim * (1 - 0.22 * fm * (1 - S.focusTalkMix))); // a little dimmer in the focus corner
    const bg = ctx.createRadialGradient(cx, cy, R * 0.3, cx, cy, Math.max(W, H) * 0.75);
    bg.addColorStop(0, rgb(col, 0.16));
    bg.addColorStop(0.5, rgb(col, 0.04));
    bg.addColorStop(1, "rgba(0,0,0,0)");
    // with a scenic background (background.js) the canvas stays transparent behind me
    if (!document.body.classList.contains("bg-on")) { ctx.fillStyle = (window.WALTER_THEME && window.WALTER_THEME.bg) || "#05060a"; ctx.fillRect(0, 0, W, H); }
    ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);

    // floor shadow (shrinks when he's in the air)
    const air = clamp(-(ay - hop) * 3, 0, 1);
    ctx.fillStyle = `rgba(0,0,0,${0.35 * (1 - air * 0.6)})`;
    ctx.beginPath();
    ctx.ellipse(cx, baseY + R * (0.98 + B.drop), R * 0.75 * (1 - air * 0.35) * B.sx, R * 0.09, 0, 0, Math.PI * 2);
    ctx.fill();

    // ripples (he hears you)
    for (const r of S.ripples) {
      const p = (t - r.t0) / 1.6;
      ctx.strokeStyle = rgb(shade(col, 0.4), (1 - p) * 0.35 * r.a);
      ctx.lineWidth = R * 0.02 * (1 - p);
      ctx.beginPath();
      ctx.ellipse(cx, cy, R * (1.08 + p * 0.6) * sx, R * (1.08 + p * 0.6) * sy, 0, 0, Math.PI * 2);
      ctx.stroke();
    }

    drawOrbit(cx, cy, R, col, t, false);

    // ---- body
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(arot + Math.sin(t * 0.5) * 0.015 * B.wob);
    const pts = blobPoints(R, sx, sy, t, B.wob, lvl);
    tracePath(pts);
    if (!LITE) { ctx.shadowColor = rgb(col, 0.55); ctx.shadowBlur = R * (0.35 + lvl * 0.25) * ((window.WALTER_THEME && window.WALTER_THEME.glow) || 1); }
    const g = ctx.createRadialGradient(-R * 0.35, -R * 0.45, R * 0.05, 0, 0, R * 1.15);
    g.addColorStop(0, rgb(shade(col, 0.55)));
    g.addColorStop(0.45, rgb(col));
    g.addColorStop(1, rgb(shade(col, -0.35)));
    ctx.fillStyle = g;
    ctx.fill();
    ctx.shadowBlur = 0;
    // glossy highlight
    ctx.fillStyle = "rgba(255,255,255,0.18)";
    ctx.beginPath();
    ctx.ellipse(-R * 0.38 * sx, -R * 0.55 * sy, R * 0.22, R * 0.11, -0.5, 0, Math.PI * 2);
    ctx.fill();

    // cheeks blush for happy/love/embarrassed
    const blush = { happy: 0.35, love: 0.6, embarrassed: 0.8, excited: 0.3, wink: 0.35 }[S.emotion] || 0;
    if (blush) {
      ctx.fillStyle = `rgba(255,90,120,${0.28 * blush})`;
      for (const sd of [-1, 1]) {
        ctx.beginPath();
        ctx.ellipse(sd * R * 0.48 * sx + S.gaze.x * R * 0.08, R * 0.18 * sy + S.gaze.y * R * 0.05, R * 0.13, R * 0.07, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // ---- eyes
    const gx = clamp(S.gaze.x + agx, -1.2, 1.2), gy = clamp(S.gaze.y + agy, -1.2, 1.2);
    const spacing = R * 0.36 * sx;
    const eyeY = -R * 0.1 * sy + gy * R * 0.16;
    const eyeX = gx * R * 0.22;
    // eyes on the far side of where he looks get a touch smaller (fake 3D turn)
    const turn = gx * 0.12;
    const squint = lvl;
    drawEye(eyeX - spacing * (1 - Math.abs(turn) * 0.4), eyeY, R, { ...S.eyeL, sx: S.eyeL.sx * (1 - turn), sy: S.eyeL.sy * (1 - turn * 0.5) }, -1, squint);
    drawEye(eyeX + spacing * (1 - Math.abs(turn) * 0.4), eyeY, R, { ...S.eyeR, sx: S.eyeR.sx * (1 + turn), sy: S.eyeR.sy * (1 + turn * 0.5) }, 1, squint);
    ctx.restore();

    // ---- thinking dots orbiting
    if (S.dots > 0.01) {
      const sp = S.state === "working" ? 3.2 : 1.8;
      for (let i = 0; i < 3; i++) {
        const a = t * sp + i * 0.55;
        const px = cx + Math.cos(a) * R * 1.32 * sx;
        const py = cy + Math.sin(a) * R * 1.32 * sy * 0.9;
        ctx.fillStyle = rgb(shade(col, 0.6), 0.85 * S.dots * (1 - i * 0.22));
        ctx.beginPath(); ctx.arc(px, py, R * (0.055 - i * 0.01), 0, Math.PI * 2); ctx.fill();
      }
    }

    drawOrbit(cx, cy, R, col, t, true);

    // ---- HUD rings while he's using tools (a little Jarvis)
    if (S.hud > 0.01) {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.lineCap = "round";
      const rr = R * 1.4 * Math.max(sx, sy);
      ctx.strokeStyle = rgb(shade(col, 0.55), 0.6 * S.hud);
      ctx.lineWidth = R * 0.02;
      for (let i = 0; i < 3; i++) {
        const a0 = t * 1.5 + i * (Math.PI * 2 / 3);
        ctx.beginPath(); ctx.arc(0, 0, rr, a0, a0 + 1.0 + 0.3 * Math.sin(t * 2 + i)); ctx.stroke();
      }
      ctx.strokeStyle = rgb(shade(col, 0.35), 0.3 * S.hud);
      ctx.lineWidth = R * 0.01;
      for (let i = 0; i < 36; i++) {
        const a = -t * 0.4 + (i * Math.PI) / 18;
        ctx.beginPath(); ctx.arc(0, 0, rr * 1.08, a, a + 0.07); ctx.stroke();
      }
      ctx.restore();
    }

    // (The small focus ring round me is gone, owner, 5 Nov: while the canvas or browser panel is
    // open every focus widget hides; the big ring and the strip in focus.js come back after.)

    // ---- particles (positions are relative to body centre)
    ctx.save();
    ctx.translate(cx, cy);
    for (const p of S.particles) {
      if ((p.type === "tear" || p.type === "sweat") && !p.anchored) {
        // anchor drops to an eye / the head the first time we draw them
        p.anchored = true;
        if (p.type === "tear") { p.x = eyeX - spacing + R * 0.05; p.y = eyeY + R * 0.18; }
        else { p.x = R * 0.7 * sx; p.y = -R * 0.55 * sy; }
      }
      if (p.type === "z" && !p.anchored) { p.anchored = true; p.x = R * 0.55; p.y = -R * 0.8; }
      if (p.type === "question" && !p.anchored) { p.anchored = true; p.x = R * 0.75; p.y = -R * 0.95; }
      drawParticle(p, R, col);
    }
    ctx.restore();

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // -------------------------------------------------------------- captions
  let captionTimer = 0, captionPartial = false;
  // Karaoke: Walter's captions arrive with per-word times ([[word, start, end], ...]
  // in seconds from now). The whole sentence shows at once, dim, and each word
  // brightens as it's spoken, so the layout never jumps.
  const AUDIO_LAG = 0.12;  // seconds between the cue and the sound leaving the speakers
  let karaoke = null;      // {t0, spans:[{el, s, e}], i}
  function karaokeTick() {
    if (!karaoke) return;
    const t = (performance.now() - karaoke.t0) / 1000;
    const k = karaoke;
    while (k.i < k.spans.length && k.spans[k.i].s <= t) {
      if (k.i > 0) k.spans[k.i - 1].el.classList.remove("now");
      k.spans[k.i].el.classList.add("said", "now");
      k.i++;
    }
    const last = k.spans[k.i - 1];
    if (last && t > last.e + 0.35) last.el.classList.remove("now");
    if (k.i >= k.spans.length && (!last || t > last.e + 0.35)) { karaoke = null; return; }
    requestAnimationFrame(karaokeTick);
  }
  // Captions are off by default, but while you talk to Walter he shows,
  // faintly, what he's understanding (live partials), so you know he's following.
  function showCaption(who, text, partial, words, dur, muted) {
    const liveYou = who === "you" && S.live;
    if (!S.captions && !S.muted && !liveYou) return; // muted: I only speak in captions
    captionPartial = !!partial;
    karaoke = null;
    captionEl.className = who === "you" ? "you" : "";
    if (who === "walter" && Array.isArray(words) && words.length) {
      captionEl.textContent = "";
      captionEl.classList.add("karaoke");
      const spans = [];
      words.forEach((w, i) => {
        if (i) captionEl.appendChild(document.createTextNode(" "));
        const el = document.createElement("span");
        el.className = "w";
        el.textContent = w[0];
        captionEl.appendChild(el);
        spans.push({ el, s: +w[1] || 0, e: +w[2] || 0 });
      });
      karaoke = { t0: performance.now() + (muted ? 0 : AUDIO_LAG * 1000), spans, i: 0 };
      requestAnimationFrame(karaokeTick);
    } else {
      captionEl.textContent = partial ? text + " …" : text;
    }
    captionEl.style.opacity = 1;
    clearTimeout(captionTimer);
    // stay up until the sentence has been said, then linger long enough to read
    const said = (+dur || 0) * 1000;
    captionTimer = setTimeout(() => (captionEl.style.opacity = 0),
      partial ? 4000 : said + (S.captions ? 4000 : 2500));
  }
  function clearPartialCaption() {
    if (captionPartial) { captionPartial = false; captionEl.style.opacity = 0; }
  }

  // ------------------------------------------------ "still talking?" indicator
  // {t:"listen", phase:"open|heard|sent|off"}: Listening… -> Got it -> Thinking… -> hidden.
  // Every phase times out on its own, so a missed message never leaves it stuck.
  const listenEl = document.getElementById("listen");
  const LISTEN = {
    open:  { cls: "open",     text: "Listening…", ttl: 6000 },   // refreshed by "hear"
    heard: { cls: "heard",    text: "Got it",     ttl: 12000 },
    sent:  { cls: "thinking", text: "Thinking…",  ttl: 45000 },
  };
  let listenPhase = "off", listenTimer = 0, listenSwap = 0, listenShownAt = 0, listenSawBusy = false;
  function setListen(phase) {
    clearTimeout(listenTimer); clearTimeout(listenSwap);
    const p = LISTEN[phase];
    if (!listenEl) return;
    if (!p) {
      if (listenPhase === "open" || listenPhase === "heard") clearPartialCaption();
      listenPhase = "off"; listenEl.className = ""; return;
    }
    const prev = listenPhase;
    listenPhase = phase;
    if (phase === "sent") listenSawBusy = false;
    const apply = () => { listenEl.textContent = p.text; listenEl.className = p.cls; listenShownAt = performance.now(); };
    // let "Got it" register for a moment before it turns into "Thinking…"
    const wait = prev === "heard" ? 800 - (performance.now() - listenShownAt) : 0;
    if (phase === "sent" && prev !== "heard") {
      listenEl.textContent = LISTEN.heard.text; listenEl.className = LISTEN.heard.cls;
      listenShownAt = performance.now();
      listenSwap = setTimeout(apply, 800);
    } else if (wait > 0) listenSwap = setTimeout(apply, wait);
    else apply();
    listenTimer = setTimeout(() => setListen("off"), p.ttl);
  }
  function listenOnState(st) {
    if (listenPhase === "off") return;
    if (st === "speaking" || st === "idle" || st === "sleeping") { setListen("off"); return; }
    if (listenPhase !== "sent") return;
    if (st === "thinking" || st === "working") listenSawBusy = true;
    else if (st === "listening" && listenSawBusy) setListen("off"); // finished without speaking
  }
  function listenOnHear() {
    if (listenPhase !== "open") return;
    clearTimeout(listenTimer);
    listenTimer = setTimeout(() => setListen("off"), LISTEN.open.ttl);
  }

  // ------------------------------------------------------------- websocket
  let ws = null;
  // The page remembers which version of the face code it was loaded with. A
  // brain restart just drops the websocket: the face keeps animating (with the
  // "Restarting…" pill) and only reloads if the face code really changed.
  let faceVersion = null, reloading = false;
  function safeReload() {
    // Never reload while the server is down: that would leave an error page
    // (a blank screen) behind. Wait until it answers, then reload.
    if (reloading) return;
    reloading = true;
    const attempt = () => fetch((window.WALTER_FACE && window.WALTER_FACE.home) || "/", { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error(r.status);
        // turned by rotate.html (software rotation): reload the wrapper too, in case it changed
        try { if (window.top !== window && /rotate\.html/.test(top.location.pathname)) { top.location.reload(); return; } } catch (e) {}
        location.reload(); })
      .catch(() => setTimeout(attempt, 1000));
    attempt();
  }
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}${(window.WALTER_FACE && window.WALTER_FACE.ws) || "/ws"}`);
    ws.onopen = () => { S.connected = true; ws.send(JSON.stringify({ t: "hello" })); if (window.walterLink) window.walterLink(true); };
    ws.onclose = () => { S.connected = false; if (window.walterLink) window.walterLink(false); setTimeout(connect, 1500); };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      window.dispatchEvent(new CustomEvent("walter-msg", { detail: m })); // for setup.js
      switch (m.t) {
        case "state": setState(m.state); listenOnState(m.state); break;
        case "emotion": setEmotion(m.name, m.hold); break;
        case "action": addAction(m.name); break;
        case "level": S.levelTarget = clamp(m.v, 0, 1); break;
        case "hear": S.hearTarget = Math.max(S.hearTarget, clamp(m.v, 0, 1)); listenOnHear(); break;
        case "caption": showCaption(m.who, m.text, m.partial || m.live, m.words, m.dur, m.muted); break;
        case "listen": setListen(m.phase); break;
        case "config":
          if ("captions" in m) S.captions = !!m.captions;
          if ("live" in m) S.live = !!m.live;
          break;
        case "jobs": S.jobs = Math.max(0, m.n | 0); break;
        case "setup": S.setup = !!m.needed; break;
        case "mute": setMuted(!!m.on); break;
        case "hwmute": setHwMuted(!!m.on); break;
        case "followup": S.followupUntil = +m.until || 0; break;
        case "camera": {
          const c = document.getElementById("camon"); if (c) c.classList.toggle("on", !!m.on);
          const k = document.getElementById("cloudon"); if (k) k.classList.toggle("withcam", !!m.on);
          break;
        }
        case "cloud": setCloud(m); break;
        case "look": // {t:"look", x, y, hold}: glance at a spot (-1..1), e.g. toward someone arriving
          S.lookOverride = { x: clamp(+m.x || 0, -1, 1), y: clamp(+m.y || 0, -1, 1) };
          S.lookUntil = now() + clamp(+m.hold || 2.5, 0.2, 10);
          break;
        case "version": // sent first on every (re)connect
          if (!m.v) break;
          if (faceVersion === null) faceVersion = m.v;
          else if (m.v !== faceVersion) safeReload(); // face edited while the brain was away
          break;
        case "reload": // face files changed (m.v = new version), or a forced reload
          if (m.force || !m.v || m.v !== faceVersion) setTimeout(safeReload, 300);
          break;
      }
    };
  }
  if (location.protocol.startsWith("http")) connect();
  else S.connected = true; // opened as a file: just show the face

  // ------------------------------------------------------- cloud speech
  // {t:"cloud", tts, stt, idle_tts, idle_stt}: is anything going to OpenAI?
  // On = in use; idle (dimmed) = switched on but falling back to local.
  function setCloud(m) {
    const el = document.getElementById("cloudon"), txt = document.getElementById("cloudtxt");
    if (!el) return;
    const on = [m.rt && "live", m.tts && !m.rt && "voice", m.stt && !m.rt && "ears"].filter(Boolean);
    const idle = [m.idle_tts && "voice", m.idle_stt && "ears", m.idle_rt && "live"].filter(Boolean);
    const parts = on.length ? on : idle;
    el.classList.toggle("on", on.length > 0);
    el.classList.toggle("idle", !on.length && idle.length > 0);
    if (txt) txt.textContent = parts.join(" + ");
    el.title = on.length ? `Cloud speech in use: ${on.join(" and ")}`
      : idle.length ? `Cloud ${idle.join(" and ")} chosen, using local for now` : "";
  }

  // ------------------------------------------------------------ mic mute
  // {t:"mute", on}: the backend owns the state and broadcasts it to every face.
  const micOff = document.getElementById("micoff");
  function setMuted(on) {
    S.muted = on;
    showMicOff();
    if (on) { setListen("off"); S.hearTarget = 0; }
  }
  // {t:"hwmute", on}: the mic's own mute button (the ears saw digital silence). Shown
  // like the Space-bar mute but amber; the software mute state is not touched.
  function setHwMuted(on) {
    S.hwMuted = on;
    showMicOff();
    if (on && !S.muted) { setListen("off"); S.hearTarget = 0; }
  }
  function showMicOff() {
    if (!micOff) return;
    const hw = !S.muted && !!S.hwMuted;
    micOff.classList.toggle("on", !!S.muted || hw);
    micOff.classList.toggle("hw", hw);
    const label = hw ? "Mic muted on the mic" : "Mic off", last = micOff.lastChild;
    if (last && last.nodeType === 3 && last.nodeValue !== label) last.nodeValue = label;
  }
  function toggleMute() {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: "mute" })); // backend toggles + broadcasts
  }

  // -------------------------------------------------------------- keyboard
  // While the dashboard shows (I'm asleep), the type box adds to the person's list instead
  // of waking me: "todo buy milk", "shopping: eggs", "note ..." or just text (a task).
  const TALK_HINT = typein.placeholder;
  let typeAdds = false;
  const dashUp = () => !!(window.walterDash && window.walterDash.showing());
  function openType() {
    typeAdds = dashUp();
    typein.placeholder = typeAdds ? "Add to the list… (todo / shopping / note; Enter to add, Esc to close)" : TALK_HINT;
    typebox.style.display = "block"; typein.focus(); document.body.style.cursor = "auto";
  }
  function closeType() { typebox.style.display = "none"; typein.value = ""; typein.blur(); document.body.style.cursor = "none"; }
  const sendText = (text) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: "text", text })); };
  typein.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") closeType();
    if (e.key === "Enter") {
      const text = typein.value.trim();
      if (text && typeAdds) window.walterDash.add(text, () => sendText("Add this to my list: " + text));
      else if (text) sendText(text);
      closeType();
    }
  });
  window.addEventListener("keydown", (e) => {
    if (S.setup) return; // setup.js owns the keyboard during first-time setup
    const np = document.getElementById("netPanel");
    if (np && np.style.display === "flex") return; // the Wi-Fi panel is open
    if (e.key === "Enter" || e.key === "t") { e.preventDefault(); openType(); return; }
    if (e.key === "Escape" && dashUp()) { // Esc on the dashboard wakes me (or ends a preview)
      if (!window.walterDash.hidePreview() && ws && ws.readyState === 1) ws.send(JSON.stringify({ t: "mute", on: false }));
      return;
    }
    if (e.key === " " || e.code === "Space") {
      const tag = (e.target && e.target.tagName) || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable)) return;
      e.preventDefault();
      if (!e.repeat) toggleMute();
      return;
    }
    if (/^[0-9]$/.test(e.key)) { setEmotion(EMOTION_KEYS[(+e.key + 9) % 10] || "neutral", 8); return; }
    const act = { b: "bounce", w: "wiggle", n: "nod", s: "shake", j: "jump" }[e.key];
    if (act) addAction(act);
    if (e.key === "c") S.captions = !S.captions;
    // speaker volume: Up/Down arrows (or + / -), M toggles speaker mute
    const vol = { ArrowUp: "up", "+": "up", "=": "up", ArrowDown: "down", "-": "down" }[e.key];
    if (vol) { e.preventDefault(); fetch("/api/volume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step: vol }) }); return; }
    // Z = the sleep button: tell the backend to go to sleep (dashboard, mic muted).
    // Space (unmute) wakes me. If the backend is down, just preview the sleeping face.
    if (e.key === "z" || e.key === "Z") {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: "sleep" }));
      else setState(S.state === "sleeping" ? "idle" : "sleeping");
    }
  });

  // expose for debugging / screenshots
  window.walter = { setEmotion, setState, addAction, setListen, listenOnState, showCaption, S };
})();
