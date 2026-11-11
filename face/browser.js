// Live browser panel: watch Walter browse. ~2/3 of the screen on the right, Walter
// steps aside to the left (like the canvas, but wider). Fed by
// home/skills/walter-browse (browse_live.py):
//   browser.json  control file (fixed old mtime so auto-reload ignores it), polled
//                 every 1.5 s: {id, open, port, token, w, h, until, title, url}
//   http://127.0.0.1:PORT/stream.mjpg?t=TOKEN   MJPEG frames of the headless page
//   http://127.0.0.1:PORT/events?t=TOKEN        SSE: {type: state|cursor|hide, ...}
//   POST http://127.0.0.1:PORT/input?t=TOKEN    the person's own mouse/keyboard (browse_input.py)
// SAFETY: the page itself never runs here; we only show JPEG frames, and title/URL
// are set with textContent. The stream connection only exists while the panel is
// open, so the daemon's screencast (and its CPU) stops whenever this is hidden.
(function () {
  const css = document.createElement('style');
  css.textContent = `
  #browser { position: fixed; top: 2.5vh; bottom: 2.5vh; right: 2.5vh; width: 64vw; z-index: 6;
    display: flex; flex-direction: column; box-sizing: border-box; overflow: hidden;
    background: rgba(14,14,16,0.97); border: 1px solid rgba(240,180,90,0.25); border-radius: 2vh;
    box-shadow: 0 2vh 8vh rgba(0,0,0,0.55); color: #ececec;
    transform: translateX(calc(100% + 5vh)); opacity: 0; visibility: hidden;
    transition: transform 0.55s cubic-bezier(.2,.8,.2,1), opacity 0.45s, visibility 0s linear 0.55s; }
  #browser.on { transform: none; opacity: 1; visibility: visible;
    transition: transform 0.55s cubic-bezier(.2,.8,.2,1), opacity 0.45s, visibility 0s; }
  #browser .hd { display: flex; align-items: center; gap: 1.2vh; padding: 1.1vh 2vh; min-height: 3vh;
    border-bottom: 1px solid rgba(255,255,255,0.07); }
  #browser .lk { width: 1.1vh; height: 1.1vh; border-radius: 50%; background: #6c6; flex: none; }
  #browser .lk.no { background: #d96; }
  #browser .tx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.2vh; }
  #browser .ti { font-size: 1.8vh; color: #f3f3f3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #browser .ur { font-size: 1.35vh; color: #f0b45a; letter-spacing: 0.02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #browser .ki { font-size: 1.3vh; color: rgba(255,255,255,0.28); letter-spacing: 0.08em; flex: none; }
  #browser .bar { height: 0.35vh; background: transparent; position: relative; overflow: hidden; }
  #browser.loading .bar::after { content: ''; position: absolute; top: 0; bottom: 0; width: 30%;
    background: #f0b45a; animation: wbload 1.1s ease-in-out infinite; }
  @keyframes wbload { from { left: -30%; } to { left: 100%; } }
  #browser .vp { flex: 1; position: relative; overflow: hidden; background: #0b0b0c; }
  #browser .st { position: absolute; }
  #browser .st img { display: block; width: 100%; height: 100%; }
  #browser .wait { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 2vh; color: rgba(255,255,255,0.35); letter-spacing: 0.1em; }
  #browser .hl { position: absolute; border: 0.35vh solid #f0b45a; border-radius: 0.6vh; pointer-events: none;
    box-shadow: 0 0 2vh rgba(240,180,90,0.55); opacity: 0; transition: all 0.35s cubic-bezier(.2,.8,.2,1), opacity 0.3s; }
  #browser .cu { position: absolute; width: 3.2vh; height: 3.2vh; margin: -1.6vh 0 0 -1.6vh; border-radius: 50%;
    background: rgba(240,180,90,0.35); border: 0.35vh solid #f0b45a; pointer-events: none; opacity: 0;
    transition: left 0.4s cubic-bezier(.2,.8,.2,1), top 0.4s cubic-bezier(.2,.8,.2,1), opacity 0.3s, transform 0.15s; }
  #browser .cu.click { transform: scale(0.65); }
  #browser .rp { position: absolute; width: 3vh; height: 3vh; margin: -1.5vh 0 0 -1.5vh; border-radius: 50%;
    border: 0.4vh solid #f0b45a; pointer-events: none; animation: wbripple 0.6s ease-out forwards; }
  @keyframes wbripple { from { transform: scale(0.5); opacity: 1; } to { transform: scale(3); opacity: 0; } }
  /* the person's own mouse/keyboard (browse_input.py): a real pointer over the panel */
  #browser { cursor: default; }
  #browser .st { touch-action: none; cursor: default; }
  #browser .st img { -webkit-user-drag: none; user-select: none; }
  #browser.ctl { border-color: rgba(120,200,255,0.75); box-shadow: 0 0 0 0.25vh rgba(120,200,255,0.35), 0 2vh 8vh rgba(0,0,0,0.55); }
  #browser .yc { display: none; flex: none; font-size: 1.35vh; letter-spacing: 0.06em; color: #0b1a26;
    background: #78c8ff; border-radius: 1.2vh; padding: 0.45vh 1.2vh; transition: opacity 0.4s; }
  #browser.ctl .yc { display: block; }
  #browser .rc { display: none; flex: none; font-size: 1.35vh; letter-spacing: 0.06em; color: #0b1a26;
    background: #78c8ff; border-radius: 1.2vh; padding: 0.45vh 1.2vh; }
  #browser.rmt:not(.ctl) .rc { display: block; }
  #browser.ctl.idle .yc { opacity: 0.45; }
  #browser .x { flex: none; width: 3.4vh; height: 3.4vh; border-radius: 50%; display: flex; align-items: center;
    justify-content: center; font-size: 2.4vh; line-height: 1; color: rgba(255,255,255,0.55); cursor: pointer; }
  #browser .x:hover { background: rgba(255,255,255,0.1); color: #fff; }
  /* the other face furniture squeezes into the free left third */
  body.browser-on #card { left: 17.5vw; min-width: 0; max-width: 30vw; }
  body.browser-on #caption { left: 17.5vw; max-width: 31vw; }
  body.browser-on #listen, body.browser-on #micoff, body.browser-on #typebox { left: 17.5vw; }
  body.browser-on #typebox { width: 30vw; }
  body.browser-on #usage, body.browser-on #workers, body.browser-on #canvas { visibility: hidden; }
  body.browser-on #restart { right: calc(66.5vw + 3vh); max-width: 28vw; }`;
  document.head.appendChild(css);

  const el = document.createElement('div');
  el.id = 'browser';
  el.innerHTML = '<div class="hd"><div class="lk"></div><div class="tx"><div class="ti"></div><div class="ur"></div></div>' +
    '<div class="yc">You\'re in control</div><div class="rc">Phone in control</div><div class="ki">Click the page to use it \u00b7 Esc closes</div>' +
    '<div class="x" title="Close">\u00d7</div></div><div class="bar"></div>' +
    '<div class="vp"><div class="wait">Opening the page...</div><div class="st"><div class="hl"></div><div class="cu"></div></div></div>';
  document.body.appendChild(el);
  const $ = (s) => el.querySelector(s);
  const ti = $('.ti'), ur = $('.ur'), lk = $('.lk'), vp = $('.vp'), st = $('.st'), hl = $('.hl'), cu = $('.cu'), wait = $('.wait');

  let cur = null, open = false, img = null, es = null, vw = 1024, vh = 810, until = 0, base = '', tok = '';

  function fit() { // keep the page's aspect ratio inside the panel (letterbox)
    const W = vp.clientWidth, H = vp.clientHeight;
    if (!W || !H) return;
    const s = Math.min(W / vw, H / vh), w = Math.round(vw * s), h = Math.round(vh * s);
    Object.assign(st.style, { width: w + 'px', height: h + 'px', left: Math.round((W - w) / 2) + 'px', top: Math.round((H - h) / 2) + 'px' });
  }
  window.addEventListener('resize', fit);

  function setState(d) {
    if (d.title !== undefined) ti.textContent = d.title || '';
    if (d.url !== undefined) ur.textContent = d.url || '';
    if (d.secure !== undefined) lk.classList.toggle('no', !d.secure);
    if (d.loading !== undefined) el.classList.toggle('loading', !!d.loading);
  }

  function cursor(d) {
    if (d.phase === 'off') { cu.style.opacity = 0; hl.style.opacity = 0; return; }
    if (Date.now() - Math.max(lastIn, lastRemote) < 10000) return; // the person is driving: my cursor sits it out
    const pct = (v) => (v * 100).toFixed(2) + '%';
    cu.style.left = pct(d.x); cu.style.top = pct(d.y); cu.style.opacity = 1;
    if (d.bw !== undefined) {
      Object.assign(hl.style, { left: `calc(${pct(d.bx)} - 0.6vh)`, top: `calc(${pct(d.by)} - 0.6vh)`,
        width: `calc(${pct(d.bw)} + 1.2vh)`, height: `calc(${pct(d.bh)} + 1.2vh)`, opacity: 1 });
    }
    if (d.phase === 'click') {
      cu.classList.add('click'); setTimeout(() => cu.classList.remove('click'), 180);
      const r = document.createElement('div'); r.className = 'rp'; r.style.left = cu.style.left; r.style.top = cu.style.top;
      st.appendChild(r); setTimeout(() => r.remove(), 700);
      clearTimeout(cursor.t); cursor.t = setTimeout(() => { hl.style.opacity = 0; cu.style.opacity = 0.0; }, 2500);
    } else { clearTimeout(cursor.t); cursor.t = setTimeout(() => { hl.style.opacity = 0; cu.style.opacity = 0; }, 8000); }
  }

  function connect(c) {
    disconnect();
    base = `http://127.0.0.1:${c.port}`; tok = c.token;
    const t = encodeURIComponent(c.token);
    img = new Image();
    img.alt = '';
    img.onload = () => { wait.style.display = 'none'; };
    img.onerror = () => { wait.style.display = ''; wait.textContent = 'Browser not reachable'; };
    img.src = `${base}/stream.mjpg?t=${t}`;
    st.insertBefore(img, st.firstChild);
    wait.style.display = ''; wait.textContent = 'Opening the page...';
    try {
      es = new EventSource(`${base}/events?t=${t}`);
      es.onmessage = (m) => {
        let d; try { d = JSON.parse(m.data); } catch (e) { return; }
        if (d.type === 'state') setState(d);
        else if (d.type === 'cursor') cursor(d);
        else if (d.type === 'hide') setOpen(false);
        else if (d.type === 'remote') remote();
      };
    } catch (e) {}
  }
  function disconnect() { // removing the <img> closes the MJPEG connection: the daemon stops casting
    if (img) { img.onload = img.onerror = null; img.src = 'data:,'; img.remove(); img = null; }
    if (es) { es.close(); es = null; }
  }

  function setOpen(on, c) {
    if (on === open && !c) return;
    open = on;
    el.classList.toggle('on', on);
    document.body.classList.toggle('browser-on', on);
    if (window.walter && window.walter.S) window.walter.S.browser = on; // walter.js steps me left
    if (on) { vw = c.w || vw; vh = c.h || vh; fit(); requestAnimationFrame(fit); connect(c); }
    else { until = 0; control(false); cursor({ phase: 'off' }); setTimeout(() => { if (!open) disconnect(); }, 600); }
  }
  function dismiss() { // Escape: close and remember, so a face reload doesn't bring it back
    try { localStorage.setItem('walterBrowserDismissed', String(cur)); } catch (e) {}
    setOpen(false);
  }
  window.walterBrowser = { hide: dismiss, isOpen: () => open };

  async function poll() {
    try {
      const c = await (await fetch('browser.json', { cache: 'no-store' })).json();
      let gone = ''; try { gone = localStorage.getItem('walterBrowserDismissed'); } catch (e) {}
      const nowS = Date.now() / 1000;
      const want = !!c.open && String(c.id) !== gone && !(c.until && nowS > c.until) && !(c.alive && nowS - c.alive > 40);
      const asleep = !!(window.walter && window.walter.S && window.walter.S.dash); // dashboard mode: step out of the way
      if (asleep && open) setOpen(false);
      if (asleep) { cur = c.id; return; }
      if (want && (!open || c.id !== cur)) { setState({ title: c.title, url: c.url }); setOpen(true, c); }
      else if (!want && open) setOpen(false);
      cur = c.id; until = c.until || 0;
    } catch (e) { if (open) setOpen(false); }
    if (open && until && Date.now() / 1000 > until) setOpen(false);
  }
  poll(); setInterval(poll, 1500);

  // ------------------------------------------------ the person's own mouse + keyboard
  // Click/tap the page to take control: keys then go to the page (not to me), a blue
  // "You're in control" pill shows, my cursor pauses and my own commands wait in the
  // daemon until 10 s after the last input. Click outside the panel (or 2 idle minutes)
  // to hand the keyboard back. Escape closes the panel, except while a text box on the
  // page has focus: then one Escape goes to the page and a second one (quickly) closes.
  // PRIVACY: keys are forwarded and forgotten; nothing here logs or keeps them.
  // The phone app's Remote tab drives it too (walter/app_remote.py): a "Phone in control" pill
  // for 10 s after its last input, and my own cursor sits out meanwhile.
  let lastRemote = 0, remoteT = 0;
  function remote() {
    lastRemote = Date.now(); el.classList.add('rmt'); cu.style.opacity = 0; hl.style.opacity = 0;
    clearTimeout(remoteT); remoteT = setTimeout(() => el.classList.remove('rmt'), 10000);
  }
  let ctl = false, edit = false, lastIn = 0, lastEsc = 0, q = [], busy = false, tick = 0, touch = null;
  let clk = { t: 0, x: 0, y: 0, n: 0 }; // pointer events don't count clicks: double/triple clicks by hand
  const yc = $('.yc'), ki = $('.ki'), HINT = ki.textContent;
  function hint() {
    if (Date.now() - lastEsc < 1400) return; // keep "Press Esc again" up for a moment
    ki.textContent = !ctl ? HINT : (edit ? 'Typing into the page \u00b7 Esc Esc closes' : 'Keys go to the page \u00b7 Esc closes');
  }
  function control(on) {
    if (on === ctl) return;
    ctl = on; el.classList.toggle('ctl', on);
    if (on) {
      const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); // e.g. my type box
      tick = setInterval(() => {
        el.classList.toggle('idle', Date.now() - lastIn > 10000);
        if (Date.now() - lastIn > 120000) control(false);
      }, 1000);
    } else { clearInterval(tick); edit = false; sent.clear(); send({ t: 'release' }); el.classList.remove('idle'); }
    hint();
  }
  function send(ev) { // queue, merging moves and wheel ticks; one request in flight at a time
    if (!tok) return;
    const last = q[q.length - 1];
    if (ev.t === 'move' && last && last.t === 'move') q[q.length - 1] = ev;
    else if (ev.t === 'wheel' && last && last.t === 'wheel') { last.dx += ev.dx; last.dy += ev.dy; last.x = ev.x; last.y = ev.y; }
    else q.push(ev);
    if (ev.t !== 'release') { lastIn = Date.now(); el.classList.remove('idle'); }
    flush();
  }
  async function flush() {
    if (busy || !q.length) return;
    busy = true;
    let batch = q; q = [];
    try {
      const r = await fetch(`${base}/input?t=${encodeURIComponent(tok)}`, { method: 'POST',
        headers: { 'Content-Type': 'text/plain' }, body: JSON.stringify({ ev: batch }), cache: 'no-store' });
      const d = await r.json();
      if (d && typeof d.edit === 'boolean') { edit = d.edit; hint(); }
    } catch (e) {}
    batch = null; busy = false;
    if (q.length) flush();
  }
  function pt(e) { // panel pixels -> page CSS pixels
    const r = st.getBoundingClientRect();
    return { x: Math.round((e.clientX - r.left) / r.width * vw), y: Math.round((e.clientY - r.top) / r.height * vh) };
  }
  st.addEventListener('pointerdown', (e) => {
    if (!open) return;
    e.preventDefault(); control(true);
    try { st.setPointerCapture(e.pointerId); } catch (x) {}
    const p = pt(e);
    if (e.pointerType === 'touch') { touch = { id: e.pointerId, x: e.clientX, y: e.clientY, p, moved: false }; return; }
    const now = Date.now(), again = now - clk.t < 500 && Math.abs(e.clientX - clk.x) < 6 && Math.abs(e.clientY - clk.y) < 6;
    clk = { t: now, x: e.clientX, y: e.clientY, n: again ? Math.min(3, clk.n + 1) : 1 };
    send({ t: 'down', x: p.x, y: p.y, b: e.button, n: clk.n });
  });
  st.addEventListener('pointermove', (e) => {
    if (!open) return;
    if (touch && e.pointerId === touch.id) { // finger drag = scroll (content follows the finger)
      const r = st.getBoundingClientRect(), dx = e.clientX - touch.x, dy = e.clientY - touch.y;
      if (!touch.moved && Math.hypot(dx, dy) < 10) return;
      touch.moved = true; touch.x = e.clientX; touch.y = e.clientY;
      const p = pt(e);
      send({ t: 'wheel', x: p.x, y: p.y, dx: -dx / r.width * vw, dy: -dy / r.height * vh });
      return;
    }
    if (!ctl && !e.buttons) return; // hover only once the person has taken control
    const p = pt(e);
    send({ t: 'move', x: p.x, y: p.y });
  });
  const up = (e) => {
    if (!open) return;
    if (touch && e.pointerId === touch.id) {
      if (!touch.moved && e.type === 'pointerup') { const p = touch.p; send({ t: 'down', x: p.x, y: p.y, b: 0, n: 1 }); send({ t: 'up', x: p.x, y: p.y, b: 0, n: 1 }); }
      touch = null; return;
    }
    const p = pt(e);
    send({ t: 'up', x: p.x, y: p.y, b: e.button < 0 ? 0 : e.button, n: clk.n || 1 });
  };
  st.addEventListener('pointerup', up);
  st.addEventListener('pointercancel', up);
  st.addEventListener('contextmenu', (e) => e.preventDefault());
  st.addEventListener('dragstart', (e) => e.preventDefault());
  st.addEventListener('wheel', (e) => {
    if (!open) return;
    e.preventDefault();
    const k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? vh * 0.9 : 1, p = pt(e);
    send({ t: 'wheel', x: p.x, y: p.y, dx: e.deltaX * k, dy: e.deltaY * k });
  }, { passive: false });
  $('.x').addEventListener('click', (e) => { e.stopPropagation(); dismiss(); });
  // a click anywhere else on the wall hands the keyboard back to me
  window.addEventListener('pointerdown', (e) => { if (ctl && !el.contains(e.target)) control(false); }, true);
  window.addEventListener('blur', () => { if (ctl) send({ t: 'release' }); });
  const sent = new Map(); // physical key -> the key that went down on the page (Shift can change e.key)
  window.addEventListener('keydown', (e) => {
    if (!open || !ctl || (window.walter && window.walter.S && window.walter.S.setup)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.key === 'Escape') {
      const now = Date.now();
      if (!edit || now - lastEsc < 900) { lastEsc = 0; dismiss(); return; }
      lastEsc = now; ki.textContent = 'Press Esc again to close';
      setTimeout(hint, 1500);
    }
    sent.set(e.code || e.key, e.key);
    send({ t: 'kd', k: e.key });
  }, true);
  window.addEventListener('keyup', (e) => {
    const c = e.code || e.key, k = sent.get(c);
    if (k === undefined) return;
    sent.delete(c);
    e.preventDefault(); e.stopImmediatePropagation();
    send({ t: 'ku', k });
  }, true);

  window.addEventListener('keydown', (e) => { // before canvas.js: Escape closes the top panel only
    const tag = (e.target && e.target.tagName) || '';
    if (/^(INPUT|TEXTAREA)$/.test(tag)) return; // the type box closes itself first
    if (e.key === 'Escape' && open) { dismiss(); e.stopImmediatePropagation(); }
  }, true);
})();
