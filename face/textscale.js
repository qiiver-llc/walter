/* Global text scale (owner, 6 Nov: "smaller text across the board").
 *
 * One setting shrinks or grows every piece of text on the face and the dashboard
 * (captions, clock, usage meter, workers, cards, pills, focus strip and ring, canvas and
 * browser headers, setup and network panels) without touching the eyes/blob, QR codes,
 * panel sizes or tap targets.
 *
 * How: every CSS rule with an absolute font-size (vh, vw, vmin, vmax, px, rem, pt) is
 * rewritten in place to calc(<size> * var(--ts, 1)), and --ts is set on <html>.
 * em/% sizes are relative to their parent, so they follow on their own (no double
 * scaling). Only font-size changes; widths, heights, paddings and QR sizes stay as
 * they are, so nothing new can clip and every button keeps its size.
 *
 * Setting: face/display.json {"text_scale": 0.5..1.5} (walterctl display text-scale 0.8),
 * polled every 2 s, applied live. Missing = DEFAULT (0.8). ?ts=1 in the URL overrides
 * (tests). window.WALTER_TEXT_SCALE() gives the current value (canvas.js uses it for the
 * text inside its sandboxed frame).
 * Load after every script that injects CSS (portrait.js last before this); style sheets
 * added later are picked up by a MutationObserver.
 */
(() => {
  "use strict";
  const DEFAULT = 0.8, MIN = 0.5, MAX = 1.5;
  const ABS = /^-?\d*\.?\d+(vh|vw|vmin|vmax|px|rem|pt)$/;
  const clamp = (v) => {
    const n = parseFloat(v);
    return isFinite(n) ? Math.min(MAX, Math.max(MIN, n)) : DEFAULT;
  };
  const urlTs = (/[?&]ts=([0-9.]+)/.exec(location.search) || [])[1];
  let scale = urlTs ? clamp(urlTs) : DEFAULT;

  function rewrite(rules) {
    for (const r of rules) {
      if (r.type === CSSRule.STYLE_RULE) {
        const v = r.style.getPropertyValue("font-size").trim();
        if (v && ABS.test(v)) {
          r.style.setProperty("font-size", "calc(" + v + " * var(--ts, 1))",
            r.style.getPropertyPriority("font-size"));
        }
      } else if (r.cssRules && r.type !== CSSRule.KEYFRAMES_RULE) {
        rewrite(r.cssRules); // @media, @supports
      }
    }
  }
  function scanAll() {
    for (const sh of Array.from(document.styleSheets)) {
      try { rewrite(sh.cssRules); } catch (e) {} // cross-origin sheet: not ours
    }
  }
  function apply(v) {
    scale = clamp(v);
    document.documentElement.style.setProperty("--ts", String(scale));
  }
  window.WALTER_TEXT_SCALE = () => scale;

  // text with no font-size rule at all inherits the browser default (16px): scale that too
  // (zero specificity, first in <head>, so every real rule still wins)
  const base = document.createElement("style");
  base.dataset.textscale = "base";
  base.textContent = ":where(body) { font-size: 16px; }";
  document.head.prepend(base);

  apply(scale);
  scanAll();
  // style sheets injected or rewritten later (dashboard board/forecast CSS, etc.)
  let pending = 0;
  new MutationObserver((muts) => {
    if (pending) return;
    if (!muts.some((m) => m.target.nodeName === "STYLE" ||
        (m.target.parentNode && m.target.parentNode.nodeName === "STYLE") ||
        Array.from(m.addedNodes).some((n) => n.nodeName === "STYLE" || n.nodeName === "LINK"))) return;
    pending = requestAnimationFrame(() => { pending = 0; scanAll(); });
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

  if (!urlTs) {
    const poll = async () => {
      try {
        const res = await fetch("display.json", { cache: "no-store" });
        const d = res.ok ? await res.json() : {};
        const v = d && d.text_scale != null ? clamp(d.text_scale) : DEFAULT;
        if (v !== scale) apply(v);
      } catch (e) {} // server restarting: keep what we have
    };
    poll(); setInterval(poll, 2000);
  }
})();
