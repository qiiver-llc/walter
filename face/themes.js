/* Themes (the owner, 10 Nov 09:06): ten looks for my face and boards. Not just colours: corner rounding,
 * fonts, spacing, border weight and glow change too.
 *
 *   midnight (the original look, default) light cream fall spring summer winter sunset ocean forest
 *
 * Setting: face/display.json {"theme": NAME | "auto"} (walterctl display theme list|NAME|auto|next|random,
 * POST /api/app/theme). Polled every 2 s. "auto" = by season and time of day: midnight 10 PM-6 AM,
 * else winter Dec-Feb, spring Mar-May, summer Jun-Aug, fall Sep-Nov. Missing = midnight.
 * Voice (the owner's final captions): "switch to the fall theme", "fall theme", "theme light", "next theme",
 * "random theme", "auto theme", "what themes are there". A spoken pick is saved through
 * POST /api/app/theme (localhost needs no pairing) and also held locally until display.json changes.
 *
 * HOW: every theme is a set of CSS custom properties on :root (--bg, --text, --accent, --radius, ...)
 * plus html[data-theme=NAME]. The face's CSS still has literal colours, so the first time a theme
 * other than midnight is applied, every style rule is rewritten in place (like textscale.js does for
 * font-size): colours -> their role variable (alpha kept), border radii x var(--radius), padding/gap x
 * var(--space), border widths x var(--border-scale), font families -> var(--font)/var(--font-mono),
 * numeric weights + var(--fw-shift). The original values are kept and put back for midnight, so
 * Midnight is exactly the look from before themes. Inline styles set from JS are not touched.
 * The canvas (walter.js) reads window.WALTER_THEME for the neutral blob colour, eyes, background
 * fill and glow. No new animations or filters.
 *
 * Pure parts (THEMES, autoTheme, command, contrast) are exported for node tests.
 */
(function (root) {
  "use strict";

  const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';  // the original stack
  const SERIF = 'Georgia, "Iowan Old Style", "Palatino Linotype", "DejaVu Serif", "Noto Serif", serif';
  const ROUND = '"Nunito", "Varela Round", "Quicksand", "Ubuntu", system-ui, sans-serif';
  const THIN = '"Helvetica Neue", "Inter", "Noto Sans", "DejaVu Sans", system-ui, sans-serif';
  const HUMAN = 'Optima, Candara, "Gill Sans", "Noto Sans", "DejaVu Sans", system-ui, sans-serif';
  const MONO = 'ui-monospace, "DejaVu Sans Mono", "Noto Sans Mono", Menlo, Consolas, monospace';

  // Colours are hex. fg = the "white" that the dark looks use with alpha for text and soft panels
  // (light looks use a dark ink instead); halo = text-shadow colour; shadow = box-shadow colour.
  // blob/eye = my neutral body colour and eyes (other emotions keep their own colours).
  // glow = blob glow strength, radius/space/border = multipliers (1 = original), fwShift = weight shift.
  const THEMES = {
    midnight: { label: "Midnight", light: false,
      bg: "#05060a", surface: "#0d111b", surface2: "#111827", line: "#1d2638", fg: "#ffffff",
      text: "#e8eefc", dim: "#9fb0d0", faint: "#6f7f9f",
      accent: "#58c4ff", accent2: "#8f84ff", ok: "#8affc1", warn: "#ffd27a", bad: "#ff8a8a",
      halo: "#000000", shadow: "#000000", blob: "#58c4ff", eye: "#101424",
      glow: 1, radius: 1, space: 1, border: 1, fwShift: 0, font: SANS, mono: MONO },
    light: { label: "Light", light: true,
      bg: "#eef1f6", surface: "#ffffff", surface2: "#e4e9f1", line: "#c3cddb", fg: "#141c28",
      text: "#141c28", dim: "#3b4759", faint: "#566275",
      accent: "#0b67b5", accent2: "#5b44c4", ok: "#17703f", warn: "#8a5600", bad: "#b3262d",
      halo: "#eef1f6", shadow: "#3c4658", blob: "#3aa3e6", eye: "#0f1424",
      glow: 0.45, radius: 1, space: 1, border: 1, fwShift: 0, font: SANS, mono: MONO },
    cream: { label: "Cream", light: true, surfaceA: 0, lineA: 0.6, noCardEdge: true,
      bg: "#f3ead8", surface: "#f3ead8", surface2: "#f3ead8", line: "#ebe0c8", fg: "#2b2116",
      text: "#2b2116", dim: "#47382a", faint: "#5a4935",
      accent: "#9a4a22", accent2: "#6e4f94", ok: "#476f22", warn: "#865900", bad: "#a3302a",
      halo: "#f3ead8", shadow: "#f3ead8", blob: "#de9152", eye: "#2b1d12",
      glow: 0.4, radius: 0.15, space: 1.08, border: 0.75, fwShift: 0, font: SERIF, mono: MONO },
    fall: { label: "Fall", light: false,
      bg: "#130b07", surface: "#22150d", surface2: "#2b1b11", line: "#4a3020", fg: "#fff3e4",
      text: "#f7e8d4", dim: "#d6b690", faint: "#a8865f",
      accent: "#ee8b3c", accent2: "#d9b04a", ok: "#b5cf72", warn: "#f4c552", bad: "#ef6f55",
      halo: "#000000", shadow: "#000000", blob: "#ea8a3a", eye: "#24120a",
      glow: 0.9, radius: 0.75, space: 1, border: 1, fwShift: 0, font: SERIF, mono: MONO },
    spring: { label: "Spring", light: true,
      bg: "#edf5e9", surface: "#fafdf7", surface2: "#e1eedb", line: "#bfd6b4", fg: "#1b2b18",
      text: "#1b2b18", dim: "#3b5135", faint: "#53684c",
      accent: "#2b7d45", accent2: "#a0447a", ok: "#21703a", warn: "#875a00", bad: "#b0303a",
      halo: "#edf5e9", shadow: "#3d5236", blob: "#4cba74", eye: "#14240f",
      glow: 0.5, radius: 1.3, space: 1.05, border: 1, fwShift: 0, font: ROUND, mono: MONO },
    summer: { label: "Summer", light: true,
      bg: "#fff6df", surface: "#fffcf2", surface2: "#ffeec4", line: "#f0d48c", fg: "#2a1f0c",
      text: "#2a1f0c", dim: "#55421c", faint: "#6c5627",
      accent: "#c2410c", accent2: "#0b7a8a", ok: "#2c7a1c", warn: "#8a5a00", bad: "#b4232c",
      halo: "#fff6df", shadow: "#6b5220", blob: "#f59a1a", eye: "#2a1a08",
      glow: 0.6, radius: 1.6, space: 1.1, border: 1.5, fwShift: 100, font: ROUND, mono: MONO },
    winter: { label: "Winter", light: false,
      bg: "#081018", surface: "#101b27", surface2: "#152332", line: "#24384d", fg: "#f2f9ff",
      text: "#eaf4ff", dim: "#b4cbe0", faint: "#8aa3bb",
      accent: "#9ad6ff", accent2: "#c3b6ff", ok: "#9cf0d0", warn: "#ffe08f", bad: "#ff9aa6",
      halo: "#000000", shadow: "#000000", blob: "#bfe4ff", eye: "#0b1626",
      glow: 1.2, radius: 0.55, space: 0.95, border: 0.75, fwShift: -100, font: THIN, mono: MONO },
    sunset: { label: "Sunset", light: false,
      bg: "#150915", surface: "#231024", surface2: "#2c142c", line: "#4a2346", fg: "#fff0ee",
      text: "#ffece9", dim: "#e2b4bd", faint: "#b68796",
      accent: "#ff8a5c", accent2: "#d97aff", ok: "#9fe6a8", warn: "#ffc96b", bad: "#ff6d7f",
      halo: "#000000", shadow: "#000000", blob: "#ff7f69", eye: "#260b1a",
      glow: 1.3, radius: 1.2, space: 1, border: 1, fwShift: 0, font: SANS, mono: MONO },
    ocean: { label: "Ocean", light: false,
      bg: "#03131a", surface: "#07222b", surface2: "#0a2b35", line: "#124553", fg: "#effdff",
      text: "#e2fbff", dim: "#a6d4dc", faint: "#7aa9b2",
      accent: "#3fd8d2", accent2: "#7aa8ff", ok: "#8ff0b8", warn: "#ffd98a", bad: "#ff8f8f",
      halo: "#000000", shadow: "#000000", blob: "#33c6d9", eye: "#04161e",
      glow: 1.1, radius: 1.25, space: 1.05, border: 1, fwShift: 0, font: SANS, mono: MONO },
    forest: { label: "Forest", light: false,
      bg: "#07110a", surface: "#0f1d13", surface2: "#132519", line: "#244030", fg: "#f1fbef",
      text: "#e6f4e4", dim: "#b2ceb0", faint: "#85a383",
      accent: "#82d48e", accent2: "#d8c46a", ok: "#a6f0a0", warn: "#f2d27a", bad: "#f08a7a",
      halo: "#000000", shadow: "#000000", blob: "#6fc47e", eye: "#0a1a0e",
      glow: 0.8, radius: 0.9, space: 1, border: 1.5, fwShift: 0, font: HUMAN, mono: MONO },
  };
  const NAMES = Object.keys(THEMES);
  const DEFAULT = "midnight";
  const COLOR_KEYS = ["bg", "surface", "surface2", "line", "fg", "text", "dim", "faint", "accent", "accent2",
    "ok", "warn", "bad", "halo", "shadow", "blob", "eye"];
  const CSS_NAME = { surface2: "surface-2", dim: "text-dim", faint: "text-faint", accent2: "accent-2" };
  // every variable a theme sets on :root (tests check them all)
  const VARS = COLOR_KEYS.flatMap((k) => ["--" + (CSS_NAME[k] || k), "--" + (CSS_NAME[k] || k) + "-rgb"])
    .concat(["--surface-a", "--line-a", "--ink", "--glow", "--radius", "--space", "--border-scale", "--fw-shift", "--font", "--font-mono"]);

  // ---- colour helpers ---------------------------------------------------------------------------
  function hexRgb(h) {
    h = String(h).replace("#", "");
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }
  function lum(c) {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }
  function contrast(a, b) {
    const la = lum(typeof a === "string" ? hexRgb(a) : a), lb = lum(typeof b === "string" ? hexRgb(b) : b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  function vars(name) {
    const t = THEMES[name] || THEMES[DEFAULT], out = {};
    for (const k of COLOR_KEYS) {
      const n = "--" + (CSS_NAME[k] || k);
      out[n] = t[k];
      out[n + "-rgb"] = hexRgb(t[k]).join(",");
    }
    out["--surface-a"] = String(t.surfaceA == null ? 1 : t.surfaceA);
    out["--line-a"] = String(t.lineA == null ? 1 : t.lineA);
    out["--ink"] = String(t.light ? 1.35 : 1);
    out["--glow"] = String(t.glow);
    out["--radius"] = String(t.radius);
    out["--space"] = String(t.space);
    out["--border-scale"] = String(t.border);
    out["--fw-shift"] = String(t.fwShift);
    out["--font"] = t.font;
    out["--font-mono"] = t.mono;
    return out;
  }

  // ---- auto: season by month, midnight in the quiet night hours ---------------------------------
  function season(d) {
    const m = d.getMonth(); // 0 = Jan
    if (m === 11 || m <= 1) return "winter";
    if (m <= 4) return "spring";
    if (m <= 7) return "summer";
    return "fall";
  }
  function autoTheme(d) {
    d = d || new Date();
    const h = d.getHours();
    if (h >= 22 || h < 6) return "midnight";
    return season(d);
  }
  function resolve(setting, d) {
    const s = String(setting || "").toLowerCase();
    if (s === "auto") return autoTheme(d);
    return THEMES[s] ? s : DEFAULT;
  }
  function nextName(cur, step) {
    const i = NAMES.indexOf(cur);
    return NAMES[((i < 0 ? 0 : i) + (step || 1) + NAMES.length) % NAMES.length];
  }
  function randomName(cur, rnd) {
    const pool = NAMES.filter((n) => n !== cur);
    return pool[Math.floor((rnd || Math.random)() * pool.length) % pool.length];
  }

  // ---- voice ------------------------------------------------------------------------------------
  const ALIAS = { autumn: "fall", dark: "midnight", night: "midnight", default: "midnight", normal: "midnight",
    original: "midnight", day: "light", white: "light", beige: "cream", sea: "ocean", woods: "forest",
    woodland: "forest", green: "forest", blue: "ocean", snow: "winter" };
  const WORD = "(" + NAMES.concat(Object.keys(ALIAS)).join("|") + ")";
  function norm(text) {
    return " " + String(text || "").toLowerCase().replace(/[‘’'`]/g, " ").replace(/[^a-z0-9 ]+/g, " ")
      .replace(/\s+/g, " ").trim() + " ";
  }
  // Only phrases with the word "theme" count, so "a cold winter" or "the light is on" never switch.
  function command(text) {
    const t = norm(text);
    if (!/\bthemes?\b/.test(t)) return null;
    if (/\b(what|which|list|show(?: me)?)(?: the)?(?: are| do you have| have you got)?(?: the| all| my| your)? themes\b|\bthemes (?:are there|do you have|have you got|can you do)\b|\blist (?:of )?(?:the )?themes\b/.test(t)) return { cmd: "list" };
    if (/\b(next|another|different|other) theme\b|\btheme next\b/.test(t)) return { cmd: "next" };
    if (/\b(random|surprise me|any) theme\b|\btheme random\b|\brandom one\b/.test(t)) return { cmd: "random" };
    if (/\b(auto(?:matic)?|seasonal) themes?\b|\btheme(?: back)?(?: to)? auto(?:matic)?\b|\bthemes? on auto\b/.test(t)) return { cmd: "auto" };
    let m = t.match(new RegExp("\\b" + WORD + "(?: mode)? theme\\b")) || t.match(new RegExp("\\btheme (?:to |called |named )?(?:the )?" + WORD + "\\b"));
    if (m) return { cmd: "set", name: ALIAS[m[1]] || m[1] };
    return null;
  }

  // ---- rewriting the face's literal CSS to theme variables (only once a non-midnight theme is on)
  const PROPS_COLOR = /^(color|background-color|background-image|border-(top|right|bottom|left)-color|outline-color|box-shadow|text-shadow|caret-color|fill|stroke|column-rule-color|text-decoration-color)$/;
  const PROPS_RADIUS = /^border-(top|bottom)-(left|right)-radius$/;
  const PROPS_SPACE = /^(padding-(top|right|bottom|left)|row-gap|column-gap)$/;
  const PROPS_BORDER = /^(border-(top|right|bottom|left)-width|outline-width)$/;
  const LEN = /^-?\d*\.?\d+(vh|vw|vmin|vmax|px|rem|em)$/;
  const COLOR_RE = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)/g;

  // which role a literal colour plays (classification by lightness, saturation and hue)
  function role(r, g, b, prop) {
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), L = (mx + mn) / 2, sat = mx - mn;
    if (mx <= 4) return prop === "text-shadow" ? "halo" : "shadow";
    if (sat < 60 || (L > 120 && sat < 90 && b >= r && b >= g && L > 200)) {
      if (L >= 228) return "fg";
      if (L >= 140) return "text-dim";
      if (L >= 80) return "text-faint";
      if (L >= 22) return "line";
      return L < 12 ? "bg" : "surface";
    }
    let h;
    if (mx === r) h = ((g - b) / sat) * 60; else if (mx === g) h = ((b - r) / sat + 2) * 60; else h = ((r - g) / sat + 4) * 60;
    h = (h + 360) % 360;
    if (L < 30) return "surface"; // very dark tinted panels
    if (h < 25 || h >= 330) return "bad";
    if (h < 70) return "warn";
    if (h < 175) return "ok";
    if (h < 255) return "accent";
    return "accent-2";
  }
  function mapColors(v, prop) {
    return v.replace(COLOR_RE, (all, r, g, b, a) => {
      const k = role(+r, +g, +b, prop);
      let al = a == null ? "1" : /%$/.test(a) ? String(parseFloat(a) / 100) : a;
      // text in light looks: ink with alpha reads weaker on a light page, so --ink lifts it a bit
      if (prop === "color" && /^(fg|text-dim|text-faint)$/.test(k) && al !== "1") al = "calc(" + al + " * var(--ink, 1))";
      if ((k === "surface" || k === "surface-2") && /^background/.test(prop)) al = "calc(" + al + " * var(--surface-a, 1))"; // card fills only (never text)
      else if (k === "line") al = "calc(" + al + " * var(--line-a, 1))";
      else if (/^background/.test(prop) && /^(fg|text-dim|text-faint)$/.test(k) && al !== "1") al = "calc(" + al + " * var(--surface-a, 1))"; // translucent white/grey card fills
      return "rgba(var(--" + k + "-rgb), " + al + ")";
    });
  }
  function scaleLens(v, varName) {
    const toks = v.split(/\s+/);
    if (!toks.every((x) => LEN.test(x) || x === "0px" || x === "0")) return null;
    return toks.map((x) => (LEN.test(x) && parseFloat(x) !== 0 ? "calc(" + x + " * var(" + varName + ", 1))" : x)).join(" ");
  }
  function themed(prop, v) {
    if (!v || v.indexOf("var(") >= 0) return null;
    if (PROPS_COLOR.test(prop)) { if (!/rgba?\(/.test(v)) return null; const n = mapColors(v, prop); return n !== v ? n : null; }
    if (PROPS_RADIUS.test(prop)) return scaleLens(v, "--radius");
    if (PROPS_SPACE.test(prop)) return scaleLens(v, "--space");
    if (PROPS_BORDER.test(prop)) return scaleLens(v, "--border-scale");
    if (prop === "font-family") return /mono/i.test(v) ? "var(--font-mono)" : "var(--font)";
    if (prop === "font-weight") return /^\d+$/.test(v) ? "calc(" + v + " + var(--fw-shift, 0))" : null;
    return null;
  }

  const api = { THEMES, NAMES, DEFAULT, VARS, COLOR_KEYS, vars, hexRgb, lum, contrast, season, autoTheme, resolve,
    nextName, randomName, command, norm, role, themed };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.WalterThemes = api;

  // =================================================================================== the face
  const doc = document, html = doc.documentElement;

  const saved = new Map(); // CSSStyleRule -> [[prop, value, priority], ...] originals
  let rewritten = false;

  function rewriteRules(rules) {
    for (const r of rules) {
      if (r.type === 1) { // CSSRule.STYLE_RULE
        if (saved.has(r)) continue;
        const st = r.style, orig = [];
        const props = [];
        for (let i = 0; i < st.length; i++) props.push(st[i]);
        for (const p of props) {
          const v = st.getPropertyValue(p).trim(), n = themed(p, v);
          if (n == null) continue;
          const pr = st.getPropertyPriority(p);
          orig.push([p, v, pr]);
          st.setProperty(p, n, pr);
        }
        saved.set(r, orig);
      } else if (r.cssRules && r.type !== 7) { // not @keyframes
        rewriteRules(r.cssRules);
      }
    }
  }
  function sheetsOk(sh) { return !sh.ownerNode || !(sh.ownerNode.dataset && sh.ownerNode.dataset.theme === "base"); }
  function rewriteAll() {
    for (const sh of Array.from(doc.styleSheets)) {
      if (!sheetsOk(sh)) continue;
      try { rewriteRules(sh.cssRules); } catch (e) {} // cross-origin: not ours
    }
    for (const r of Array.from(saved.keys())) if (!r.parentStyleSheet) saved.delete(r); // replaced sheets
    rewritten = true;
  }
  function restoreAll() {
    for (const [r, orig] of saved) for (const [p, v, pr] of orig) { try { r.style.setProperty(p, v, pr); } catch (e) {} }
    saved.clear();
    rewritten = false;
  }

  // The page background and font come from index.html's own html/body rule (rewritten like the rest,
  // so body.bg-on stays transparent over the scenic picture); its dim layer turns into a light wash.

  let current = "", setting = DEFAULT;
  function applyTheme(name) {
    name = THEMES[name] ? name : DEFAULT;
    const t = THEMES[name], v = vars(name);
    for (const k in v) html.style.setProperty(k, v[k]);
    html.dataset.theme = name;
    html.classList.toggle("theme-light", !!t.light);
    // paper looks: board cards lose their outline, shadow and blur (calendar grid lines stay)
    let ce = document.getElementById("theme-cardedge-style");
    if (!ce) { ce = document.createElement("style"); ce.id = "theme-cardedge-style"; document.head.appendChild(ce); }
    ce.textContent = t.noCardEdge ? "#dash .card, #dash .pod { border-color: transparent !important; box-shadow: none !important; -webkit-backdrop-filter: none !important; backdrop-filter: none !important; }" : "";
    root.WALTER_THEME = { name, light: !!t.light, blob: hexRgb(t.blob), eye: hexRgb(t.eye), bg: t.bg, glow: t.glow };
    if (name === DEFAULT) { if (rewritten) restoreAll(); }
    else rewriteAll();
    current = name;
    return name;
  }
  root.applyTheme = applyTheme;
  root.WALTER_THEME_NAME = () => current;
  root.WALTER_THEME_SETTING = () => setting;   // "auto" or a name (face/wallstate.js)

  // style sheets injected or replaced later (dashboard, cockpit, portrait): rewrite them too
  let pending = 0;
  new MutationObserver((muts) => {
    if (pending || current === DEFAULT || !current) return;
    if (!muts.some((m) => m.target.nodeName === "STYLE" ||
        (m.target.parentNode && m.target.parentNode.nodeName === "STYLE") ||
        Array.from(m.addedNodes).some((n) => n.nodeName === "STYLE" || n.nodeName === "LINK"))) return;
    pending = requestAnimationFrame(() => { pending = 0; if (current !== DEFAULT) rewriteAll(); });
  }).observe(html, { childList: true, subtree: true, characterData: true });

  // ---- setting: display.json "theme", plus a spoken pick held until display.json changes
  const urlTheme = (/[?&]theme=([a-z]+)/.exec(location.search) || [])[1];
  const STORE = "walter.themeVoice";
  let voice = null; // {pick: "fall"|"auto", seen: display.json value when spoken}
  try { voice = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) {}
  let fileVal = null;

  function wanted() {
    if (urlTheme) return urlTheme;
    if (voice && voice.seen === fileVal) return voice.pick;
    return fileVal || DEFAULT;
  }
  function refresh() {
    setting = wanted();
    const n = resolve(setting);
    if (n !== current) applyTheme(n);
  }
  async function poll() {
    try {
      const res = await fetch("display.json", { cache: "no-store" });
      const d = res.ok ? await res.json() : {};
      fileVal = d && typeof d.theme === "string" ? d.theme.toLowerCase() : DEFAULT;
      if (voice && voice.seen === null) voice.seen = fileVal;
      if (voice && voice.seen !== fileVal) { voice = null; try { localStorage.removeItem(STORE); } catch (e) {} }
    } catch (e) {} // server restarting: keep what we have
    refresh();
  }

  // a short list on the info card (the same #card walter-show uses), then it hides again
  function showCard(title, body, secs) {
    const el = doc.getElementById("card");
    if (!el) return;
    el.querySelector(".title").textContent = title;
    const bo = el.querySelector(".body");
    bo.textContent = body;
    bo.className = "body long";
    const im = el.querySelector(".img"); if (im) { im.removeAttribute("src"); im.className = "img"; }
    el.className = "on";
    clearTimeout(showCard.t);
    showCard.t = setTimeout(() => { if (el.querySelector(".title").textContent === title) el.className = ""; }, secs * 1000);
  }

  function pick(p) {
    voice = { pick: p, seen: fileVal };
    try { localStorage.setItem(STORE, JSON.stringify(voice)); } catch (e) {}
    refresh();
    // persist in display.json (the kiosk on localhost needs no pairing); harmless if it fails
    try {
      fetch("/api/app/theme", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme: p }) }).catch(() => {});
    } catch (e) {}
  }

  root.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    if (m.t !== "caption" || m.partial || m.live || !m.text || m.who !== "you" || m.muted) return;
    const c = command(m.text);
    if (!c) return;
    if (c.cmd === "list") {
      showCard("Themes", NAMES.map((n, i) => (i + 1) + ". " + THEMES[n].label + (n === current ? "  (on now)" : "")).join("\n")
        + "\nSay \"fall theme\", \"next theme\" or \"auto theme\"", 8);
    } else if (c.cmd === "set") pick(c.name);
    else if (c.cmd === "next") pick(nextName(current));
    else if (c.cmd === "random") pick(randomName(current));
    else if (c.cmd === "auto") pick("auto");
  });

  // first paint: a spoken pick (or midnight) right away, then the file
  applyTheme(resolve(wanted()));
  poll();
  setInterval(poll, 2000);
})(typeof window !== "undefined" ? window : this);
