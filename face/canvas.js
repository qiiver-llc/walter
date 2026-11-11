// Canvas: a panel that slides in from the right with rich content (Markdown or
// HTML/CSS/JS), fed by bin/walter-canvas. Two data files with a fixed old mtime
// (so the face's auto-reload ignores them):
//   canvas.json      small control file, polled every 1.5 s:
//                    {id, title, until, hide?, scroll: {id, dir}}
//   canvas-doc.json  the content, fetched only when the id changes:
//                    {id, kind: "md"|"html", content}
// SAFETY: nothing from the content ever runs in this page. Everything (even
// Markdown) is rendered inside <iframe sandbox="allow-scripts" srcdoc> with an
// opaque origin plus a CSP that blocks network access (connect-src 'none'), so
// it can't touch the face, its websocket or the backend API.
(function () {
  const css = document.createElement('style');
  css.textContent = `
  #canvas { position: fixed; top: 2.5vh; bottom: 2.5vh; right: 2.5vh; width: 50vw; z-index: 5;
    display: flex; flex-direction: column; box-sizing: border-box; overflow: hidden;
    background: rgba(20,20,22,0.95); border: 1px solid rgba(240,180,90,0.25); border-radius: 2.4vh;
    box-shadow: 0 2vh 8vh rgba(0,0,0,0.55); color: #ececec;
    transform: translateX(calc(100% + 5vh)); opacity: 0; visibility: hidden;
    transition: transform 0.55s cubic-bezier(.2,.8,.2,1), opacity 0.45s, visibility 0s linear 0.55s; }
  #canvas.on { transform: none; opacity: 1; visibility: visible;
    transition: transform 0.55s cubic-bezier(.2,.8,.2,1), opacity 0.45s, visibility 0s; }
  #canvas .hd { display: flex; align-items: center; gap: 1.2vh; padding: 1.8vh 2.6vh 1.4vh;
    border-bottom: 1px solid rgba(255,255,255,0.06); }
  #canvas .hd .ti { flex: 1; font-size: 1.9vh; letter-spacing: 0.12em; text-transform: uppercase;
    color: #f0b45a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #canvas .hd .ki { font-size: 1.4vh; color: rgba(255,255,255,0.28); letter-spacing: 0.08em; }
  #canvas iframe { color-scheme: dark; flex: 1; width: 100%; border: 0; background: transparent; transition: opacity 0.25s; }
  #canvas iframe.swap { opacity: 0; }
  #netIcon, #netPanel { z-index: 10; }
  /* keep the other face furniture in the free left part while the canvas is open */
  body.canvas-on #workers, body.canvas-on #restart { right: calc(52.5vw + 3vh); max-width: 40vw; }
  body.canvas-on #card { left: 25vw; min-width: 0; max-width: 44vw; }
  body.canvas-on #caption { left: 25vw; max-width: 44vw; }
  body.canvas-on #listen, body.canvas-on #micoff, body.canvas-on #typebox { left: 25vw; }
  body.canvas-on #typebox { width: 42vw; }
  body.canvas-on #usage, body.canvas-on #workers { visibility: hidden; }
  #workers, #restart, #card, #caption, #listen, #typebox { transition-property: opacity, transform, left, right, color, background; }`;
  document.head.appendChild(css);

  const el = document.createElement('div');
  el.id = 'canvas';
  el.innerHTML = '<div class="hd"><div class="ti"></div><div class="ki">Esc to close</div></div>';
  const frame = document.createElement('iframe');
  frame.setAttribute('sandbox', 'allow-scripts'); // NO allow-same-origin: opaque origin
  frame.setAttribute('referrerpolicy', 'no-referrer');
  el.appendChild(frame);
  document.body.appendChild(el);
  const ti = el.querySelector('.ti');

  const ORIGIN = location.protocol.startsWith('http') ? location.origin : '';
  const V = ORIGIN + '/vendor/';
  const jsStr = (s) => JSON.stringify(String(s)).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');

  function head(extra) {
    const ts = window.WALTER_TEXT_SCALE ? window.WALTER_TEXT_SCALE() : 1; // global text scale (textscale.js)
    const fs = Math.round(Math.max(12, Math.min(window.innerHeight, window.innerWidth) * 0.017) * ts); // portrait: by width
    const csp = "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' " + V +
      "; style-src 'unsafe-inline' " + V + "; img-src data: blob: https: " + V +
      "; font-src data:; media-src data: blob: https:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri " + (ORIGIN || "'none'");
    return `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">
<base href="${ORIGIN}/">${extra || ''}
<style>
  :root { color-scheme: dark; --fg: #ececec; --dim: #a8a8a8; --acc: #f0b45a; --line: rgba(255,255,255,0.09); --code: #0c0c0d; }
  html { background: transparent; }
  body { margin: 0; padding: 2.4vh 3.2vh 6vh; color: var(--fg); font: ${fs}px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; cursor: none; }
  h1, h2, h3, h4 { line-height: 1.2; margin: 1.1em 0 0.45em; font-weight: 650; }
  h1 { font-size: 1.75em; } h2 { font-size: 1.4em; color: #f5d9ad; } h3 { font-size: 1.15em; color: #f5d9ad; }
  body > :first-child, #doc > :first-child { margin-top: 0; }
  p, ul, ol, table, pre, blockquote { margin: 0 0 0.9em; }
  ul, ol { padding-left: 1.4em; } li { margin: 0.2em 0; } li::marker { color: var(--acc); }
  a { color: var(--acc); text-decoration: none; border-bottom: 1px solid rgba(240,180,90,0.35); }
  strong { color: #fff; } em { color: #e6e6e6; } hr { border: 0; border-top: 1px solid var(--line); margin: 1.4em 0; }
  blockquote { border-left: 3px solid var(--acc); margin-left: 0; padding: 0.2em 0 0.2em 1em; color: var(--dim); }
  code { font-family: ui-monospace, "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 0.88em;
    background: rgba(255,255,255,0.07); padding: 0.1em 0.35em; border-radius: 0.3em; }
  pre { background: var(--code); border: 1px solid var(--line); border-radius: 0.6em; padding: 0.9em 1.1em; overflow: auto; }
  pre code, pre code.hljs { background: none; padding: 0; font-size: 0.84em; line-height: 1.45; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td { border-bottom: 1px solid var(--line); padding: 0.45em 0.7em; text-align: left; vertical-align: top; }
  th { color: var(--acc); font-weight: 600; border-bottom-color: rgba(240,180,90,0.35); }
  tr:nth-child(even) td { background: rgba(255,255,255,0.025); }
  img, svg, video, canvas { max-width: 100%; height: auto; } img { border-radius: 0.5em; }
  input[type=checkbox] { accent-color: var(--acc); }
  ::-webkit-scrollbar { width: 8px; height: 8px; } ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  .card { background: rgba(255,255,255,0.04); border: 1px solid var(--line); border-radius: 0.8em; padding: 1em 1.2em; }
</style>
<script>
  // Chart.js dark defaults, applied the moment vendor/chart.umd.min.js defines Chart
  (function () { var C; Object.defineProperty(window, 'Chart', { configurable: true, get: function () { return C; },
    set: function (v) { C = v; try { v.defaults.color = '#d4d4d4'; v.defaults.borderColor = 'rgba(255,255,255,0.09)';
      v.defaults.font.family = 'system-ui, sans-serif'; v.defaults.font.size = ${Math.round(fs * 0.7)}; v.defaults.animation.duration = 700; } catch (e) {} } }); })();
  addEventListener('message', function (e) {
    var d = e.data || {}; if (d.walter !== 'scroll') return;
    var h = innerHeight * 0.75, se = document.scrollingElement || document.documentElement;
    if (d.dir === 'top') scrollTo({ top: 0, behavior: 'smooth' });
    else if (d.dir === 'bottom') scrollTo({ top: se.scrollHeight, behavior: 'smooth' });
    else scrollBy({ top: d.dir === 'up' ? -h : h, behavior: 'smooth' });
  });
  addEventListener('keydown', function (e) { if (e.key === 'Escape') parent.postMessage({ walter: 'esc' }, '*'); });
  // a kiosk shouldn't wander off: links don't navigate (in-page #anchors still work)
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest && e.target.closest('a[href]');
    if (a && (a.getAttribute('href') || '').charAt(0) !== '#') e.preventDefault();
  }, true);
</script>`;
  }

  function build(kind, content) {
    if (kind === 'html') {
      const h = head();
      if (/<head[^>]*>/i.test(content)) return content.replace(/<head[^>]*>/i, (m) => m + h);
      if (/<html[^>]*>/i.test(content)) return content.replace(/<html[^>]*>/i, (m) => m + '<head>' + h + '</head>');
      return '<!DOCTYPE html><html><head>' + h + '</head><body>' + content + '</body></html>';
    }
    // Markdown: parsed and sanitised inside the sandbox too
    return '<!DOCTYPE html><html><head>' + head(`<link rel="stylesheet" href="${V}github-dark.min.css">`) +
      `<script src="${V}marked.min.js"></` + `script><script src="${V}purify.min.js"></` + `script><script src="${V}highlight.min.js"></` + `script>
</head><body><div id="doc"></div><script>
  var md = ${jsStr(content)};
  try {
    var html = marked.parse(md, { gfm: true, breaks: false });
    document.getElementById('doc').innerHTML = DOMPurify.sanitize(html);
    document.querySelectorAll('pre code').forEach(function (b) { try { hljs.highlightElement(b); } catch (e) {} });
  } catch (e) { var p = document.createElement('pre'); p.textContent = md; document.getElementById('doc').appendChild(p); }
</` + 'script></body></html>';
  }

  let lastId = null, lastScroll = null, until = 0, open = false, first = true;
  function setOpen(on) {
    open = on;
    el.classList.toggle('on', on);
    document.body.classList.toggle('canvas-on', on);
    if (window.walter && window.walter.S) window.walter.S.canvas = on; // walter.js moves the blob left
    if (!on) { until = 0; setTimeout(() => { if (!open) frame.srcdoc = ''; }, 700); }
  }
  function dismiss() { // Escape: close it and remember, so a face reload doesn't bring it back
    try { localStorage.setItem('walterCanvasDismissed', String(lastId)); } catch (e) {}
    setOpen(false);
  }
  window.walterCanvas = { hide: dismiss, isOpen: () => open };

  async function poll() {
    try {
      const c = await (await fetch('canvas.json', { cache: 'no-store' })).json();
      if (c.id !== lastId) {
        lastId = c.id;
        let gone = ''; try { gone = localStorage.getItem('walterCanvasDismissed'); } catch (e) {}
        if (c.hide || (c.until && Date.now() / 1000 > c.until) || String(c.id) === gone) { if (open) setOpen(false); }
        else {
          const d = await (await fetch('canvas-doc.json', { cache: 'no-store' })).json();
          if (d.id === c.id) {
            ti.textContent = c.title || '';
            const src = build(d.kind, d.content || '');
            if (open) { frame.classList.add('swap'); setTimeout(() => { frame.srcdoc = src; frame.classList.remove('swap'); }, 250); }
            else frame.srcdoc = src;
            until = c.until || 0;
            setOpen(true);
          } else lastId = null; // doc not written yet: retry next poll
        }
      }
      const sc = c.scroll && c.scroll.id;
      if (sc && sc !== lastScroll) {
        if (!first && open && frame.contentWindow) frame.contentWindow.postMessage({ walter: 'scroll', dir: c.scroll.dir }, '*');
        lastScroll = sc;
      }
      first = false;
    } catch (e) { /* no canvas yet / offline */ }
    if (open && until && Date.now() / 1000 > until) setOpen(false);
  }
  poll(); setInterval(poll, 1500);

  window.addEventListener('message', (e) => {
    if (e.source === frame.contentWindow && e.data && e.data.walter === 'esc') { dismiss(); window.focus(); }
  });
  window.addEventListener('keydown', (e) => {
    // the type box stops propagation of its own keys, so its Escape never reaches here
    if (e.key === 'Escape' && open) dismiss();
  });
})();
