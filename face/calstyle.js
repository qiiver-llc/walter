/* Calendar style (10 Nov): how the portrait month calendar draws the weeks that are NOT
 * expanded (every week except the current one).
 *
 *   chips  (default, the look from before): every event as a small chip with its time and title
 *   counts one solid tile per calendar with a big count ("3 Kids", "1 Main") instead of the chip
 *          pile, readable from across the room. Followed games keep their own team chips, multi-day
 *          bars stay bars, and the current (expanded) week keeps its full event text either way.
 *
 * Setting: face/display.json {"calendar_style": "chips" | "counts"}
 *   (walterctl display calendar chips|counts|toggle|status). Polled every 2 s.
 * Voice (the owner's final captions): "calendar counts", "count view", "calendar chips", "chip view",
 *   "toggle the calendar style". A spoken pick is held on this screen (localStorage) until
 *   display.json's value changes, like a spoken theme.
 *
 * Pure parts (STYLES, tiles, command, norm) are exported for node tests (tests/calstyle.test.js).
 */
(function (root) {
  "use strict";

  const STYLES = ["chips", "counts"];
  const DEFAULT = "chips";
  const MAX_TILES = 4;

  // A day's calendar items -> tiles: one per calendar with its count. Multi-day bars are not
  // counted (they keep their bar). Order: `order` first (Main, then the configured calendars in
  // config order), any other calendar after them by count (then name). More than MAX_TILES
  // calendars: the first MAX_TILES - 1 tiles plus "+N" (N = the events left over), so the day never
  // shows more than MAX_TILES boxes.
  // evs: [{e: {calendar, title}, multi, past}], order: ["Main", ...]
  // returns {tiles: [{cal, n, past}], more: N}
  function tiles(evs, order, max) {
    max = max || MAX_TILES;
    order = Array.isArray(order) ? order : [];
    const by = new Map();
    for (const v of evs || []) {
      if (!v || v.multi || !v.e) continue;
      const cal = String(v.e.calendar || "Other");
      const t = by.get(cal) || { cal, n: 0, past: true };
      t.n += 1;
      if (!v.past) t.past = false;
      by.set(cal, t);
    }
    const rank = (c) => { const i = order.indexOf(c); return i < 0 ? order.length : i; };
    const all = Array.from(by.values()).sort((a, b) => rank(a.cal) - rank(b.cal) || b.n - a.n || a.cal.localeCompare(b.cal));
    if (all.length <= max) return { tiles: all, more: 0 };
    const keep = all.slice(0, max - 1);
    return { tiles: keep, more: all.slice(max - 1).reduce((s, t) => s + t.n, 0) };
  }

  // ---- voice ------------------------------------------------------------------------------------
  function norm(text) {
    return " " + String(text || "").toLowerCase().replace(/[‘’'`]/g, " ").replace(/[^a-z0-9 ]+/g, " ")
      .replace(/\s+/g, " ").trim() + " ";
  }
  // Needs "calendar" or "view"/"mode" next to the style word, so "count me in" or "potato chips" never switch.
  function command(text) {
    const t = norm(text);
    if (/\b(toggle|switch|flip|change)(?: the)?(?: calendar)? (style|view|look)\b/.test(t) && /\bcalendar\b/.test(t)) return { cmd: "toggle" };
    if (/\bcalendar (?:in |to |as )?(?:the )?counts?\b|\bcounts? (?:view|mode|tiles)\b|\bcalendar count (?:view|mode)\b|\bcount tiles\b/.test(t)) return { cmd: "set", style: "counts" };
    if (/\bcalendar (?:in |to |as |back to )?(?:the )?chips?\b|\bchips? (?:view|mode)\b/.test(t)) return { cmd: "set", style: "chips" };
    return null;
  }
  function resolve(v) { v = String(v || "").toLowerCase(); return STYLES.includes(v) ? v : DEFAULT; }

  const api = { STYLES, DEFAULT, MAX_TILES, tiles, command, norm, resolve };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.WalterCalStyle = api;

  // =================================================================================== the face
  const STORE = "walter.calStyleVoice";
  let voice = null; // {pick, seen: display.json value when spoken}
  try { voice = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) {}
  let fileVal = null;
  const current = () => resolve(voice && voice.seen === fileVal ? voice.pick : fileVal);
  root.WALTER_CAL_STYLE = current;

  async function poll() {
    try {
      const res = await fetch("display.json", { cache: "no-store" });
      const d = res.ok ? await res.json() : {};
      fileVal = resolve(d && d.calendar_style);
      if (voice && voice.seen === null) voice.seen = fileVal;
      if (voice && voice.seen !== fileVal) { voice = null; try { localStorage.removeItem(STORE); } catch (e) {} }
    } catch (e) {} // server restarting: keep what we have
  }
  function pick(p) {
    voice = { pick: p, seen: fileVal };
    try { localStorage.setItem(STORE, JSON.stringify(voice)); } catch (e) {}
  }

  root.addEventListener("walter-msg", (ev) => {
    const m = ev.detail || {};
    if (m.t !== "caption" || m.partial || m.live || !m.text || m.who !== "you" || m.muted) return;
    const c = command(m.text);
    if (!c) return;
    pick(c.cmd === "toggle" ? (current() === "counts" ? "chips" : "counts") : c.style);
  });

  poll();
  setInterval(poll, 2000);
})(typeof window !== "undefined" ? window : this);
