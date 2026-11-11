// Scenic background: a picture behind Walter instead of black space, fed by
// bin/walter-background. Pictures + tags live in face/backgrounds/ (index.json).
// Control file face/background.json (fixed old mtime, so no face reload), polled
// every 3 s:
//   {id, mode: "off"|"manual"|"auto", file, every (min), lat, lon, motion, dim}
// Auto mode picks a picture that suits the time of day (night after dusk, sunrise
// pictures in the morning, sunset ones in the evening) and the season, and rotates
// every `every` minutes. The choice is deterministic (time slot), so
// `walter-background current` can tell what's on screen; keep pick() in sync with
// auto_pick() in bin/walter-background.
(function () {
  const css = document.createElement('style');
  css.textContent = `
  #bg { position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none; background: #05060a; }
  #bg .ph { position: absolute; inset: -3%; background: center / cover no-repeat;
    filter: blur(1.2px) saturate(1.1); opacity: 0; transition: opacity 3s ease-in-out; will-change: opacity, transform; }
  #bg .ph.on { opacity: 1; }
  #bg.motion .ph { animation: kenburns 120s ease-in-out infinite alternate; }
  #bg .ph.b { animation-delay: -60s; }
  @keyframes kenburns {
    from { transform: scale(1.0) translate3d(0, 0, 0); }
    to   { transform: scale(1.07) translate3d(-1.2%, -0.8%, 0); } }
  /* dim + vignette; darker at the top-left (clock), bottom (captions) and edges */
  #bg .shade { position: absolute; inset: 0; transition: opacity 1.5s;
    background:
      radial-gradient(ellipse 75% 70% at 50% 48%, rgba(5,6,10,0) 0%, rgba(5,6,10,0.3) 70%, rgba(5,6,10,0.7) 100%),
      linear-gradient(to bottom, rgba(5,6,10,0.35) 0%, rgba(5,6,10,0) 22%, rgba(5,6,10,0) 72%, rgba(5,6,10,0.6) 100%); }
  #bg .dim { position: absolute; inset: 0; background: #05060a; opacity: 0.5; transition: opacity 1.2s; }
  body.bg-on { background: transparent; }
  body.bg-on #caption, body.bg-on #clock, body.bg-on #workers, body.bg-on #sayname, body.bg-on #camon, body.bg-on #usage {
    text-shadow: 0 0.15vh 0.6vh rgba(0,0,0,0.9), 0 0 2vh rgba(0,0,0,0.6); }
  body.bg-on #caption { color: rgba(255,255,255,0.72); }
  body.bg-on #clock { color: rgba(255,255,255,0.75); }
  body.bg-on #clock .date { color: rgba(255,255,255,0.55); }
  body.bg-on #workers { color: rgba(255,255,255,0.68); }
  body.bg-on #sayname { background: rgba(5,6,10,0.45); }
  body.bg-on #caption.karaoke .w { color: rgba(255,255,255,0.45); }
  body.bg-on #caption.karaoke .w.said { color: rgba(255,255,255,0.75); }
  body.bg-on #caption.karaoke .w.now { color: #fff; }`;
  document.head.appendChild(css);

  const root = document.createElement('div');
  root.id = 'bg';
  root.innerHTML = '<div class="ph a"></div><div class="ph b"></div><div class="dim"></div><div class="shade"></div>';
  document.body.insertBefore(root, document.body.firstChild);
  const layers = [root.querySelector('.ph.a'), root.querySelector('.ph.b')];
  const dimEl = root.querySelector('.dim'), shadeEl = root.querySelector('.shade');
  let front = 0, shownFile = null, index = [], indexAt = 0, cfg = { mode: 'off' }, lastId = null;

  // ---- sun position (NOAA-style approximation, good to a few minutes)
  function sunElevation(date, lat, lon) {
    const rad = Math.PI / 180, d = date.getTime() / 86400000 + 2440587.5 - 2451545.0;
    const g = (357.529 + 0.98560028 * d) * rad, q = 280.459 + 0.98564736 * d;
    const L = (q + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * rad, e = (23.439 - 0.00000036 * d) * rad;
    const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)), dec = Math.asin(Math.sin(e) * Math.sin(L));
    const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
    const ha = ((gmst * 15 + lon) * rad) - ra;
    return Math.asin(Math.sin(lat * rad) * Math.sin(dec) + Math.cos(lat * rad) * Math.cos(dec) * Math.cos(ha)) / rad;
  }
  function defaultLon() { // no location set: guess from the standard-time zone offset (15 degrees per hour)
    const jan = new Date(new Date().getFullYear(), 0, 1).getTimezoneOffset();
    const jul = new Date(new Date().getFullYear(), 6, 1).getTimezoneOffset();
    return -Math.max(jan, jul) / 4;
  }
  function timeBucket(date, lat, lon) {
    const el = sunElevation(date, lat, lon);
    if (el < -6) return 'night';
    if (el > 10) return 'day';
    const rising = sunElevation(new Date(date.getTime() + 600000), lat, lon) > el;
    return rising ? 'sunrise' : 'sunset';
  }
  function season(date, lat) {
    const m = date.getMonth(), north = lat >= 0;
    const s = m <= 1 || m === 11 ? 'winter' : m <= 4 ? 'spring' : m <= 7 ? 'summer' : 'autumn';
    if (north) return s;
    return { winter: 'summer', summer: 'winter', spring: 'autumn', autumn: 'spring' }[s];
  }
  function pick(date) {
    const lat = cfg.lat ?? 45, lon = cfg.lon ?? defaultLon(), every = Math.max(5, cfg.every || 45);
    const bucket = timeBucket(date, lat, lon), sea = season(date, lat);
    let c = index.filter((im) => im.tags && im.tags.time === bucket);
    const inSeason = c.filter((im) => im.tags.season === sea || im.tags.season === 'any');
    if (inSeason.length >= 3) c = inSeason;
    if (!c.length) c = index.slice();
    c.sort((a, b) => (a.file < b.file ? -1 : 1));
    const slot = Math.floor(date.getTime() / 60000 / every);
    return c.length ? c[slot % c.length] : null;
  }

  function show(file) {
    if (file === shownFile) return;
    shownFile = file;
    const next = layers[1 - front];
    if (!file) { layers.forEach((l) => l.classList.remove('on')); document.body.classList.remove('bg-on'); return; }
    const url = 'backgrounds/' + file.split('/').filter((x) => x && x !== '..').map(encodeURIComponent).join('/');
    const img = new Image();
    img.onload = () => {
      if (shownFile !== file) return;
      next.style.backgroundImage = 'url("' + url + '")';
      next.classList.add('on'); layers[front].classList.remove('on'); front = 1 - front;
      document.body.classList.add('bg-on');
    };
    img.onerror = () => { if (shownFile === file) shownFile = null; };
    img.src = url;
  }

  // dim more for bright pictures, while asleep/muted, behind setup and the canvas panel
  function updateDim() {
    const S = window.WALTER_STATE || {};
    const im = index.find((x) => x.file === shownFile);
    const luma = im && typeof im.luma === 'number' ? im.luma : 0.4;
    let a = 0.1 + 0.5 * luma;                        // base: keep the glow the brightest thing
    a = 1 - (1 - a) / Math.max(0.3, Math.min(1.6, cfg.dim || 1));
    const life = typeof S.dim === 'number' ? S.dim : 1;  // 1 awake, 0.72 muted, 0.45 asleep
    a = 1 - (1 - a) * (0.25 + 0.75 * (life - 0.45) / 0.55) ** 1.5;
    a = Math.max(a, 0.85 * (S.setupMix || 0), (S.canvasMix || 0) * 0.55);
    dimEl.style.opacity = Math.max(0.15, Math.min(0.97, a)).toFixed(3);
  }

  async function loadIndex() {
    try {
      const j = await (await fetch('backgrounds/index.json', { cache: 'no-store' })).json();
      index = j.images || []; indexAt = Date.now();
    } catch (e) {}
  }
  async function poll() {
    if (!index.length || Date.now() - indexAt > 600000) await loadIndex();
    try {
      const c = await (await fetch('background.json', { cache: 'no-store' })).json();
      if (c.id !== lastId) { lastId = c.id; cfg = c; }
    } catch (e) { /* no state file yet: stay off */ }
    root.classList.toggle('motion', cfg.motion !== false);
    // during a focus box the scenery holds still: no time-of-day swaps (a picked one still applies)
    const boxOn = !!(window.WALTER_FOCUS && window.WALTER_FOCUS.on);
    if (cfg.mode === 'auto') { if (!(boxOn && shownFile)) { const p = pick(new Date()); show(p ? p.file : null); } }
    else if (cfg.mode === 'manual' && cfg.file) show(cfg.file);
    else show(null);
  }
  window.walterBackground = { pick, timeBucket, sunElevation }; // for debugging in the console
  poll(); setInterval(poll, 3000);
  updateDim(); setInterval(updateDim, 250);
})();
