// Claude usage (lower left): the owner's plan usage from usage.json, written every
// 5 minutes by bin/claude-usage (systemd --user walter-usage.timer).
// Hidden when the data is missing, failed or stale.
(function () {
  const css = document.createElement('style');
  css.textContent = `
  #usage { position: fixed; left: 3vh; top: 10.5vh; pointer-events: none;
    color: rgba(255,255,255,0.28); font-size: 1.25vh; line-height: 1.25;
    opacity: 0; transition: opacity 0.6s; font-variant-numeric: tabular-nums; }
  #usage .hdr { font-size: 1.1vh; letter-spacing: 0.12em; text-transform: uppercase;
    color: rgba(255,255,255,0.2); margin-bottom: 0.3vh; }
  #usage .hrs { text-transform: none; letter-spacing: 0.02em; }
  #usage .row { display: flex; align-items: center; gap: 0.9vh; margin-top: 0.4vh; }
  #usage .lbl { width: 6vh; }
  #usage .bar { width: 9vh; height: 0.35vh; border-radius: 0.3vh; background: rgba(255,255,255,0.1); overflow: hidden; }
  #usage .bar i { display: block; height: 100%; background: rgba(88,196,255,0.6); }
  #usage .bar i.warn { background: rgba(255,210,122,0.75); }
  #usage .bar i.hot { background: rgba(255,138,138,0.8); }
  #usage .pct { width: 4.5vh; text-align: right; color: rgba(255,255,255,0.5); }
  #usage .rs { color: rgba(255,255,255,0.25); }`;
  document.head.appendChild(css);
  const el = document.createElement('div');
  el.id = 'usage';
  document.body.appendChild(el);

  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const t = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
    const days = (d - new Date()) / 864e5;
    if (days < 0.9 && d.getDate() === new Date().getDate()) return 'resets ' + t;
    return 'resets ' + d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + t;
  }
  function row(name, w) {
    if (!w) return '';
    const p = Math.max(0, Math.min(100, w.pct));
    const cls = p >= 90 ? 'hot' : p >= 70 ? 'warn' : '';
    return '<div class="row"><span class="lbl">' + name + '</span><span class="bar"><i class="' + cls +
      '" style="width:' + p + '%"></i></span><span class="pct">' + Math.round(p) + '%</span></div>';
  }
  async function poll() {
    try {
      const r = await fetch('usage.json', { cache: 'no-store' });
      if (!r.ok) throw 0;
      const u = await r.json();
      if (!u.ok || Date.now() / 1000 - u.updated > 20 * 60 || (!u.session && !u.week)) throw 0;
      const rs = (u.week && u.week.resets_at) ? when(u.week.resets_at) : (u.session && u.session.resets_at) ? when(u.session.resets_at) : '';
      el.innerHTML = '<div class="hdr">Claude' + (rs ? '<span class="hrs"> · ' + rs + '</span>' : '') + '</div>' +
        row('Session', u.session) + row('Week', u.week);
      el.style.opacity = 1;
    } catch (e) { el.style.opacity = 0; }
  }
  poll(); setInterval(poll, 60000);
})();
