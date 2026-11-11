/* Conversation gate: decides WHEN the focused conversation mode may start.
 *
 * the owner (9 Nov 13:12): a single quick question and answer must leave the board
 * exactly as it is. Conversation mode only starts after a real back-and-forth:
 * N exchanges (a final "you" caption that Walter then actually answered by
 * speaking), each new turn within `window_s` of the previous reply ending.
 * Once active it stays active while the talk continues; it ends after `hold_s`
 * of quiet (no speech / no state change) and the counter starts again from 0.
 *
 * What never counts: partial captions, wake-word-only or rejected noise
 * (no reply follows), ducking / background audio (no "you" caption), Walter
 * speaking on his own (reminders, worker reports: no pending turn).
 *
 * Settings (face/display.json, `walterctl display conversation ...`):
 *   conversation_mode  "on" | "off"   (default on)
 *   conversation_turns 2..6           (default 3)
 *   conversation_window_s             (default 45)
 *   conversation_hold_s               (default 8)
 *
 * Sticky build mode (setSticky, fed from buildgate.js by converse.js) overrides all of the above until
 * the owner exits it.
 *
 * Mute (the owner, 9 Nov 13:33): mute() drops conversation mode at once (no hold timer) and zeroes the
 * exchange counter; settings are untouched and unmuting does not bring it back by itself.
 *
 * Voice open (the owner, 9 Nov 19:56; chatlib.js command()): open() shows it at once, close() ends a
 * voice-opened one; it ends after a quiet minute (OPEN_HOLD_S) or on mute.
 *
 * Pure logic, no DOM: usable from the face (window.WalterConvGate) and from node tests.
 */
(function (root) {
  "use strict";

  const DEFAULTS = { mode: "on", turns: 3, window_s: 45, hold_s: 8 };
  const PENDING_MAX_S = 30; // a heard turn not answered within this is dropped (rejected noise)
  const OPEN_HOLD_S = 60;   // opened by voice ("show the chat"): ends after a quiet minute (or the hold, if longer)

  function num(v, lo, hi, dflt) {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
  }

  function create(opts) {
    const cfg = Object.assign({}, DEFAULTS);
    const g = {
      count: 0,          // answered exchanges in the current run
      pending: false,    // a final "you" caption not yet answered
      pendingAt: 0,      // ...and when it was heard (s)
      answering: false,  // the pending turn is being answered right now (counted once)
      lastReplyEnd: 0,   // time (s) Walter last stopped speaking after a counted reply
      lastActivity: 0,   // last speech / state change (for the hold timer)
      active: false,
      speaking: false,
      sticky: false,     // sticky build mode (buildgate.js): conversation mode holds until the owner exits
      opened: false,     // opened by voice ("conversation mode" / "show the chat", chatlib.js): up at once
    };

    function configure(d) {
      d = d || {};
      const mode = String(d.conversation_mode == null ? cfg.mode : d.conversation_mode).toLowerCase();
      cfg.mode = (mode === "off" || mode === "false" || mode === "0") ? "off" : "on";
      cfg.turns = Math.round(num(d.conversation_turns, 2, 6, cfg.turns));
      cfg.window_s = num(d.conversation_window_s, 5, 600, cfg.window_s);
      cfg.hold_s = num(d.conversation_hold_s, 1, 120, cfg.hold_s);
      if (cfg.mode === "off") reset();
    }

    function reset() {
      g.count = 0; g.pending = false; g.answering = false; g.active = false; g.lastReplyEnd = 0;
    }

    // A new turn from the person. `now` in seconds.
    function heard(now, partial) {
      g.lastActivity = now;          // live partials keep an active conversation alive
      if (partial || cfg.mode === "off") return;
      // Too long since the last reply ended: the back-and-forth was broken, start over.
      if (!g.active && g.count > 0 && g.lastReplyEnd && now - g.lastReplyEnd > cfg.window_s) g.count = 0;
      g.pending = true;
      g.pendingAt = now;
      g.answering = false;
      g.lastActivity = now;
    }

    // Walter's state ("idle" | "listening" | "thinking" | "speaking" ...).
    function state(now, st) {
      g.lastActivity = now;
      if (cfg.mode === "off") return;
      if (st === "speaking") {
        g.speaking = true;
        if (g.pending && !g.answering && now - g.pendingAt <= PENDING_MAX_S) {
          g.answering = true;      // this reply answers the pending turn: one exchange
          g.count += 1;
          if (g.count >= cfg.turns) g.active = true;
        }
      } else if (g.speaking) {
        g.speaking = false;
        if (g.answering) { g.lastReplyEnd = now; g.pending = false; g.answering = false; }
      }
    }

    // Walter's own caption (some replies arrive as captions before / without a state flip).
    function spoke(now) { g.lastActivity = now; }

    // Sticky build mode on / off (the owner, 9 Nov 13:25). While on, conversation mode shows right away
    // and ignores exchange counts, the hold timer and silence; it wins over conversation_mode "off"
    // too (an explicit "build mode" is an explicit ask). Leaving it starts a fresh count.
    function setSticky(on, now) {
      on = !!on;
      if (on === g.sticky) return;
      g.sticky = on;
      if (!on) { g.active = false; g.count = 0; g.pending = false; g.answering = false; g.lastReplyEnd = 0; }
      else if (now != null) g.lastActivity = now;
    }

    // Opened by voice (the owner, 9 Nov 19:56: "conversation mode" / "show the chat" / "chat mode"): up at
    // once, no exchange count, even with conversation_mode "off" (an explicit ask); ends after a quiet
    // minute, on mute, or close().
    function open(now) {
      g.opened = true; g.active = true; g.count = Math.max(g.count, cfg.turns);
      if (now != null) g.lastActivity = now;
    }

    // "close the chat": ends a voice-opened conversation mode now (true if it did). A conversation
    // that started by itself keeps going (the chat view just goes back to the normal layout).
    function close(now) {
      if (!g.opened) return false;
      g.opened = false; reset();
      if (now != null) g.lastActivity = now;
      return true;
    }

    // The mic was muted: leave conversation (and sticky build) mode right now and start the count over.
    function mute(now) {
      reset(); g.sticky = false; g.speaking = false; g.opened = false;
      if (now != null) g.lastActivity = now;
    }

    // Call on a timer: returns whether conversation mode should be showing now.
    function tick(now) {
      if (g.sticky) return true;
      if (g.opened) {
        if (!g.speaking && !g.pending && now - g.lastActivity > Math.max(cfg.hold_s, OPEN_HOLD_S)) {
          g.opened = false; g.active = false; g.count = 0;
          return false;
        }
        return true;
      }
      if (cfg.mode === "off") { g.active = false; return false; }
      if (g.pending && !g.answering && now - g.pendingAt > PENDING_MAX_S) g.pending = false;
      if (g.active && !g.speaking && !g.pending && now - g.lastActivity > cfg.hold_s) {
        g.active = false;
        g.count = 0;               // a fresh back-and-forth is needed next time
      }
      // Not active: the window passing resets the counter.
      if (!g.active && g.count > 0 && !g.speaking && !g.pending && g.lastReplyEnd
          && now - g.lastReplyEnd > cfg.window_s) g.count = 0;
      return g.active;
    }

    configure(opts);
    return { configure, reset, heard, state, spoke, tick, setSticky, mute, open, close, cfg, g };
  }

  const api = { create, DEFAULTS, OPEN_HOLD_S };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WalterConvGate = api;
})(typeof window !== "undefined" ? window : this);
