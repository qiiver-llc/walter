/* Portrait layout (owner, 5 Nov): the wall screen may be taller than wide (9:16, 10:16).
 *
 * Landscape is untouched: everything here only applies under the CSS media query
 * (orientation: portrait), i.e. when the window is at least as tall as it is wide.
 *
 * 1. Size: every face rule sized in vh (written for a wide screen) gets a portrait copy
 *    with vh -> vw, inserted right after the original style sheet (same selectors, so the
 *    cascade is unchanged). On a 1080x1920 screen 1vw = 10.8 px, exactly what 1vh is on
 *    1920x1080, so text and pills keep the size the owner knows.
 * 2. Layout: hand-written portrait rules (below): the canvas and browser panels come up
 *    from the bottom (lower ~60%) while I move up into the free top part; the
 *    dashboard stacks vertically; the focus ring, cards, setup QR and pills fit the width.
 * walter.js reads window.WALTER_PORTRAIT() for my own position and size.
 * Must load after every script that injects CSS (canvas, browser, dashboard, focus...).
 */
(() => {
  "use strict";
  const MQ = "(orientation: portrait)";
  const mq = window.matchMedia(MQ);
  window.WALTER_PORTRAIT = () => mq.matches;

  // ---- 0. software rotation (no wlr-randr needed): face/display.json {"rotate": 90|270}
  // (walterctl display css-rotate 90|270|0). The plain face hands over to rotate.html,
  // which shows this same page in a turned iframe (?embedded=1), so everything below
  // simply sees a tall window. rotate.html switches back here when it goes to 0.
  if (window.top === window && !/[?&]embedded=1/.test(location.search)) {
    const checkRotate = async () => {
      try {
        const res = await fetch("display.json", { cache: "no-store" });
        if (!res.ok) return;
        const r = parseInt((await res.json()).rotate, 10) || 0;
        if (r === 90 || r === 270) {
          const q = new URLSearchParams(location.search); q.set("r", String(r));
          location.replace(((window.WALTER_FACE && window.WALTER_FACE.base) || "/") + "rotate.html?" + q.toString());
        }
      } catch (e) {}
    };
    checkRotate(); setInterval(checkRotate, 2000);
  }
  // ---- big-screen layout (owner, 5 Nov: 32" portrait monitor, "you don't have to be so big"):
  // html.big = portrait AND layout large. I sit small at the top and an always-on info
  // board (dashboard.js, board mode) fills the rest. Setting: face/display.json
  // {"layout": "auto|compact|large"} (walterctl display layout ...), ?layout= overrides.
  // auto = large when the portrait window is at least 1000 x 1700 CSS px (the rotated
  // 1080x1920 wall screen); smaller portrait screens keep the compact layout.
  let layoutPref = (/[?&]layout=(auto|compact|large)\b/.exec(location.search) || [])[1] || "";
  let layoutSaved = "auto";
  const bigWanted = () => {
    const pref = layoutPref || layoutSaved;
    if (!mq.matches || pref === "compact") return false;
    return pref === "large" || (innerWidth >= 1000 && innerHeight >= 1700);
  };
  window.WALTER_BIG = () => document.documentElement.classList.contains("big");
  window.WALTER_LAYOUT = () => ({ pref: layoutPref || layoutSaved, big: window.WALTER_BIG() });
  const sync = () => {
    const h = document.documentElement.classList;
    h.toggle("portrait", mq.matches);
    h.toggle("big", bigWanted());
  };
  sync();
  if (mq.addEventListener) mq.addEventListener("change", sync); else mq.addListener(sync);
  window.addEventListener("resize", sync);
  const pollLayout = async () => {
    try {
      const res = await fetch("display.json", { cache: "no-store" });
      const d = res.ok ? await res.json() : {};
      const l = String(d.layout || "auto");
      layoutSaved = l === "compact" || l === "large" ? l : "auto";
    } catch (e) {}
    sync();
  };
  pollLayout(); setInterval(pollLayout, 3000);

  // ---- 1. vh -> vw copies of every vh-sized rule, right after its own sheet
  const VH = /(-?\d*\.?\d+)vh\b/g;
  const TALL = /^(height|min-height|max-height|top|bottom)$/, BIG = /^\s*([2-9]\d|\d{3,})(\.\d+)?vh\s*$/;
  function portraitCopy(rules) {
    let out = "";
    for (const r of rules) {
      if (r.type === CSSRule.STYLE_RULE) {
        const decls = [];
        for (let i = 0; i < r.style.length; i++) {
          const p = r.style[i], v = r.style.getPropertyValue(p);
          // full-height layout (canvas height: 100vh, panels top/bottom 30vh...) stays in vh
          if (VH.test(v) && !(TALL.test(p) && BIG.test(v))) {
            VH.lastIndex = 0;
            decls.push(p + ": " + v.replace(VH, "$1vw") + (r.style.getPropertyPriority(p) ? " !important" : ""));
          }
          VH.lastIndex = 0;
        }
        if (decls.length) out += r.selectorText + " { " + decls.join("; ") + "; }\n";
      } else if (r.type === CSSRule.MEDIA_RULE && !/portrait/.test(r.conditionText || "")) {
        const inner = portraitCopy(r.cssRules);
        if (inner) out += "@media " + r.conditionText + " {\n" + inner + "}\n";
      }
    }
    return out;
  }
  for (const node of Array.from(document.querySelectorAll("style"))) {
    if (node.dataset.portrait || !node.sheet) continue;
    let text = "";
    try { text = portraitCopy(node.sheet.cssRules); } catch (e) { continue; }
    if (!text) continue;
    const s = document.createElement("style");
    s.dataset.portrait = "auto";
    s.media = MQ;
    s.textContent = text;
    node.after(s);
  }

  // ---- 2. portrait layout (sizes here are already in portrait units: vw for size)
  const css = document.createElement("style");
  css.dataset.portrait = "layout";
  css.media = MQ;
  css.textContent = `
  /* face furniture */
  #caption { max-width: 88vw; font-size: 2.6vw; bottom: 4vw; }
  #workers { width: 46vw; max-width: none; top: 6.5vw; }
  #checklist { width: 44vw !important; max-width: none !important; }
  #card { min-width: 60vw; max-width: 90vw; }
  #card .img { width: 52vw; height: 52vw; }
  #typebox { width: 86vw; bottom: 10vh; }
  #sayname, #listen, #micoff { top: 3vw; }
  #focusstrip { width: 46vw; top: 15vw; }

  /* first-time setup / QR screens: under me, centred, big enough to scan */
  #setup { left: 50%; right: auto; top: auto; bottom: 7vh; transform: translateX(-50%);
    width: 86vw; max-width: none; }
  #setup h1 { font-size: 5vw; } #setup p { font-size: 3vw; } #setup .hint { font-size: 2.4vw; }
  #setup .qr { width: 46vw; height: 46vw; }
  #setup input { font-size: 3vw; }

  /* the big focus ring: fills the width, a little above the middle */
  #focusring { top: 43vh; width: 88vw; height: 88vw; }
  #focusring .in { width: 62vw; height: 54vw; }
  #focusring .title { max-height: 28vw; }
  #focusring .left { font-size: 8.4vw; }
  #focusring .lbl { font-size: 2.4vw; } #focusring .until { font-size: 2.6vw; }

  /* canvas: comes up from the bottom and takes the lower ~60%; I sit in the top part */
  #canvas { top: 40vh; left: 2.5vw; right: 2.5vw; bottom: 2.5vw; width: auto;
    transform: translateY(calc(100% + 5vh)); }
  #canvas.on { transform: none; }
  body.canvas-on #card { left: 50%; min-width: 60vw; max-width: 90vw; }
  body.canvas-on #caption { left: 50%; max-width: 88vw; bottom: calc(60vh + 2vw); }
  body.canvas-on #listen, body.canvas-on #micoff { left: 50%; }
  body.canvas-on #typebox { left: 50%; width: 86vw; bottom: calc(60vh + 7vw); }
  body.canvas-on #restart { right: 3vw; bottom: calc(60vh + 2vw); max-width: 40vw; }

  /* live browser: the lower ~60% too (the page is landscape-shaped; a taller panel would only letterbox) */
  #browser { top: 40vh; left: 2.5vw; right: 2.5vw; bottom: 2.5vw; width: auto;
    transform: translateY(calc(100% + 5vh)); }
  #browser.on { transform: none; }
  body.browser-on #card { left: 50%; min-width: 60vw; max-width: 90vw; }
  body.browser-on #caption { left: 50%; max-width: 88vw; bottom: calc(60vh + 2vw); }
  body.browser-on #listen, body.browser-on #micoff { left: 50%; }
  body.browser-on #typebox { left: 50%; width: 86vw; bottom: calc(60vh + 7vw); }
  body.browser-on #restart { right: 3vw; bottom: calc(60vh + 2vw); max-width: 40vw; }

  /* dashboard: one column (clock, weather, calendar, list, away, QR + status);
     I sleep in the bottom-right corner next to the QR */
  #dash .dayv { position: absolute; inset: 0; display: flex; flex-direction: column; gap: 1.8vw;
    padding: 3vw 5vw 4vw; box-sizing: border-box; }
  #dash .dayv > * { position: relative; left: auto; right: auto; top: auto; bottom: auto; width: auto; flex: none; }
  #dash .clk .t { font-size: 17vw; }
  #dash .clk .t small { font-size: 5.5vw; }
  #dash .clk .d { font-size: 4vw; }
  /* calendar and list share what's left; anything cut fades out at the bottom */
  #dash .cal, #dash .cal.withaway { flex: 0 1 auto; min-height: 14vh; max-height: 34vh;
    -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 2.4vw), transparent); }
  #dash .lst { flex: 1 1 0; min-height: 14vh; max-height: none; }
  #dash .away { flex: 0 1 auto; min-height: 0; max-height: 15vh;
    -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 2.4vw), transparent); }
  #dash .foot { margin-top: auto; width: 62vw; min-height: 22vw; } /* keeps the corner free for me */
  #dash .nightv { left: 6vw; right: 6vw; }
  #dash .nightv .t { font-size: 24vw; }
  #dash .nightv .ln { max-width: 88vw; white-space: normal; }
  body.dash-dark #dash .nightv .t { font-size: 20vw; }
  #dash .toast { bottom: 26vw; }`;
  document.head.appendChild(css);
})();
