/* Conversation CHAT view, pure logic (the owner approved 9 Nov 19:56,
 * journal/2026-11-09-walter-product/PLAN-conversation-chat.md). No DOM: window.WalterChatLib in the
 * face (chat.js draws it), module.exports for node tests.
 *
 *   command(text)  "conversation mode" / "show the chat" / "chat mode" -> {cmd:"open"};
 *                  "close the chat" / "hide the chat" / "chat mode off" ... -> {cmd:"close"} (checked first)
 *   viewOf(v)      display.json chat_view -> "chat" | "default"
 *   md(text)       SAFE minimal markdown: everything is HTML-escaped FIRST, then only these come back
 *                  as tags: **bold**, *italic*, `code`, ``` fenced code ```, "- " / "* " lists, "1. " lists,
 *                  "# " headings (as bold lines). Links show their text only (not clickable), images
 *                  show [image: alt] (never loaded), raw URLs stay plain text. Emotion / action tags
 *                  like [happy] or [action:grow] are dropped.
 *   createThread() the in-memory thread: the last MAX messages (~8 back-and-forths), partials for the
 *                  person while speaking, Walter's reply growing sentence by sentence, app turns marked
 *                  "phone", cleared when the local date changes (no history across days).
 */
(function (root) {
  "use strict";

  const MAX = 16;            // messages kept (~8 turns of you + me)
  const SAME_TURN_S = 20;    // a Walter sentence this soon after the previous one joins the same bubble
  const DUP_S = 3;           // the same sentence twice within this (cue + karaoke caption) shows once

  function norm(text) {
    return " " + String(text || "").toLowerCase().replace(/[‘’'`]/g, " ").replace(/[^a-z0-9 ]+/g, " ")
      .replace(/\s+/g, " ").trim() + " ";
  }

  // OFF first so "close the chat" / "chat mode off" never read as "chat mode".
  const OFF = /\b((?:close|hide|end|exit|leave|stop|quit|dismiss)(?: the| this)? (?:chat|chat view|chat mode|conversation mode|conversation view)|(?:chat|conversation) mode off|(?:turn|switch) off(?: the)? (?:chat|chat mode|conversation mode)|no more chat|out of (?:chat|conversation) mode)\b/;
  const ON = /\b(conversation mode|chat mode|chat view|(?:show|open|bring up|put up|pull up)(?: me)?(?: the| our)? (?:chat|conversation|transcript)|(?:turn|switch) on(?: the)? (?:chat|chat mode|conversation mode))\b/;

  function command(text) {
    const t = norm(text);
    if (t.trim() === "") return null;
    if (OFF.test(t)) return { cmd: "close" };
    if (ON.test(t)) return { cmd: "open" };
    return null;
  }

  function viewOf(v) { return String(v == null ? "" : v).toLowerCase() === "chat" ? "chat" : "default"; }

  // ------------------------------------------------------------------ markdown (escape first)
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const TAG = /\[(?:[a-z]+(?::[a-z0-9:_-]+)?)\](?!\()\s*/gi;  // [happy] [action:grow] [think:hard]

  // inline formatting on an ALREADY ESCAPED line
  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = s.replace(/!\[([^\]\n]*)\]\(([^)\n]*)\)/g, (m, alt) => "[image" + (alt ? ": " + alt : "") + "]");
    s = s.replace(/\[([^\]\n]+)\]\(([^)\n]*)\)/g, "$1");               // links: text only
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/__([^_\n]+)__/g, "<b>$1</b>");
    s = s.replace(/(^|[\s(])\*([^*\s][^*\n]*?)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>");
    return s.replace(/\u0000(\d+)\u0000/g, (m, i) => "<code>" + codes[+i] + "</code>");
  }

  function md(text) {
    const src = String(text == null ? "" : text).replace(/\r\n?/g, "\n").replace(TAG, "");
    const lines = esc(src).split("\n");
    const out = [];
    let list = null, para = [], code = null;
    const flushPara = () => { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } };
    const flushList = () => { if (list) { out.push("<" + list.tag + ">" + list.items.map((x) => "<li>" + inline(x) + "</li>").join("") + "</" + list.tag + ">"); list = null; } };
    for (const raw of lines) {
      if (code !== null) {
        if (/^\s*```/.test(raw)) { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = null; }
        else code.push(raw);
        continue;
      }
      if (/^\s*```/.test(raw)) { flushPara(); flushList(); code = []; continue; }
      const line = raw.trim();
      let m;
      if (!line) { flushPara(); flushList(); continue; }
      if ((m = line.match(/^[-*•]\s+(.*)$/))) {
        flushPara(); if (list && list.tag !== "ul") flushList();
        (list = list || { tag: "ul", items: [] }).items.push(m[1]); continue;
      }
      if ((m = line.match(/^(\d{1,3})[.)]\s+(.*)$/))) {
        flushPara(); if (list && list.tag !== "ol") flushList();
        (list = list || { tag: "ol", items: [] }).items.push(m[2]); continue;
      }
      if ((m = line.match(/^#{1,6}\s+(.*)$/))) { flushPara(); flushList(); out.push("<p><b>" + inline(m[1]) + "</b></p>"); continue; }
      flushList();
      para.push(line);
    }
    if (code !== null) out.push("<pre><code>" + code.join("\n") + "</code></pre>");
    flushPara(); flushList();
    return out.join("");
  }

  // a spoken sentence joins the reply so far: lists / code start a new line, prose a space
  function joinSentence(prev, s) {
    if (!prev) return s;
    if (/^\s*([-*•]\s|\d{1,3}[.)]\s|```|#)/.test(s) || /```\s*$/.test(prev)) return prev + "\n" + s;
    return prev + " " + s;
  }

  // ------------------------------------------------------------------ the thread
  function dayKey(ms) { const d = new Date(ms); return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate(); }

  function createThread(opts) {
    opts = opts || {};
    const max = opts.max || MAX;
    const T = { msgs: [], day: null, seq: 0, open: null, lastSaid: "", lastSaidAt: 0 };

    function checkDay(nowMs) {
      const k = dayKey(nowMs);
      if (T.day !== null && k !== T.day) { T.msgs = []; T.open = null; T.lastSaid = ""; }
      T.day = k;
    }
    function trim() { while (T.msgs.length > max) T.msgs.shift(); }
    function push(m) { m.id = ++T.seq; T.msgs.push(m); trim(); return m; }
    function last() { return T.msgs[T.msgs.length - 1] || null; }

    // the person: partial = still talking (one live bubble, replaced in place)
    function heard(nowMs, text, partial, from) {
      checkDay(nowMs);
      text = String(text || "").trim();
      if (!text) return null;
      const L = last();
      T.open = null; // Walter's previous reply is finished once the person speaks again
      if (L && L.who === "you" && L.partial) {
        L.text = text; L.partial = !!partial; L.t = nowMs; if (from) L.from = from; L.v = (L.v || 0) + 1;
        return L;
      }
      return push({ who: "you", text, partial: !!partial, from: from || "", t: nowMs, v: 1 });
    }

    // a partial that never became a turn (noise, wake word only): drop it
    function dropPartial() {
      const L = last();
      if (L && L.who === "you" && L.partial) { T.msgs.pop(); return true; }
      return false;
    }

    // Walter says a sentence (wall captions): grows his current bubble
    function said(nowMs, text, from) {
      checkDay(nowMs);
      text = String(text || "").replace(TAG, "").trim();
      if (!text) return null;
      if (text === T.lastSaid && nowMs - T.lastSaidAt < DUP_S * 1000) return null;
      T.lastSaid = text; T.lastSaidAt = nowMs;
      const L = last();
      if (L && L.who === "you" && L.partial) dropPartial();
      const O = T.open;
      if (O && O === last() && nowMs - O.t < SAME_TURN_S * 1000 && (O.from || "") === (from || "")) {
        O.text = joinSentence(O.text, text); O.t = nowMs; O.v += 1;
        return O;
      }
      T.open = push({ who: "walter", text, partial: false, from: from || "", t: nowMs, v: 1 });
      return T.open;
    }

    // a whole message at once (companion app turns: who "you" | "walter", from "phone")
    function message(nowMs, who, text, from) {
      checkDay(nowMs);
      text = String(text || "").trim();
      if (!text) return null;
      T.open = null;
      return push({ who: who === "walter" ? "walter" : "you", text, partial: false, from: from || "", t: nowMs, v: 1 });
    }

    // Walter stopped talking (idle / listening): the next sentence starts a new bubble
    function endReply() { T.open = null; }
    function clear() { T.msgs = []; T.open = null; T.lastSaid = ""; }

    return { heard, dropPartial, said, message, endReply, clear, checkDay, msgs: () => T.msgs, T };
  }

  // one bubble's HTML (person: escaped plain text; Walter: safe markdown)
  function bubble(m) {
    const cls = "cb " + (m.who === "walter" ? "w" : "y") + (m.partial ? " live" : "") + (m.from ? " ph" : "");
    const tag = m.from === "phone" ? '<div class="from">' + (m.who === "walter" ? "replied on phone" : "from phone") + "</div>" : "";
    const body = m.who === "walter" ? md(m.text) : "<p>" + esc(m.text) + (m.partial ? ' <span class="dots">…</span>' : "") + "</p>";
    return '<div class="' + cls + '" data-id="' + m.id + '">' + tag + body + "</div>";
  }

  const api = { command, viewOf, md, esc, inline, joinSentence, createThread, bubble, norm, MAX, dayKey };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WalterChatLib = api;
})(typeof window !== "undefined" ? window : this);
