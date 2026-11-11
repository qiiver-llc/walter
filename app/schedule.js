// Schedule (the owner asked 11 Nov 10:48): everything that runs on a timer, in one place, editable.
// Settings > Schedule shows it inline; a "Schedule" button beside Rotate/Projects in the Mode sheet opens the
// same list in a sheet. Sections: Loops (heartbeat stepper, sleep-time learning toggle, links to the nightly
// self-review / morning briefing / evening wrap-up routines), Reminders, Do later, Routines (pause toggle),
// System timers (greyed, read-only). Tap a row to edit it (time, days or date, text; Save / Delete with a
// confirm); "+ Add" for reminders, jobs and routines. GET/POST /api/app/schedule (walter/app_schedule.py).
// The last list is cached on this device so it still shows (with a note) when Walter can't be reached.
// Loops > "Heartbeat instructions" (11 Nov 10:58) opens an editor for the wake-up line ([life] heartbeat_prompt) and
// the checklist (initiative.md): draft kept on this device as you type, Save / Revert, stale-edit guard (409),
// and "Restore a previous version" from Walter's last 10 backups. GET /api/app/schedule/heartbeat.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("scSheet")) return;
  const CACHE = "walter.schedule.v1";
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const DKEY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const TXT_MAX = 2000;
  let st = null, needs = false, busy = false, stale = false, open = false, editing = null, adding = null;
  let delAsk = false, expanded = new Set(), hbTimer = 0, hbWant = null;
  const DRAFT = "walter.schedule.hbdraft.v1", INI_MAX = 60 * 1024;
  let hb = null, hbBase = null, draftTimer = 0, bkAsk = null, bkTimer = 0;

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function haptic() { try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* no vibration */ } }
  function msg(text, bad) { const m = $("scMsg"); m.textContent = text || ""; m.classList.toggle("bad", !!bad); }
  async function raw(json, path) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 12000);
    try {
      const r = await fetch(path || "/api/app/schedule", { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store",
        signal: ctl.signal, body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } finally { clearTimeout(tm); }
  }
  function save(d) { try { localStorage.setItem(CACHE, JSON.stringify({ t: Date.now(), d })); } catch (e) { /* storage full / private */ } }
  function load() { try { const c = JSON.parse(localStorage.getItem(CACHE) || "null"); return c && c.d && Array.isArray(c.d.routines) ? c.d : null; } catch (e) { return null; } }
  function take(d) { if (d && Array.isArray(d.routines)) { st = d; stale = false; needs = false; save(d); } }

  async function refresh() {
    try {
      const r = await raw();
      if (r.status === 404) needs = true;
      else if (r.status === 200) take(r.data);
      else if (!st) { st = load(); stale = !!st; }
    } catch (e) { if (!st) st = load(); stale = !!st; }
    render();
  }

  // ---- formatting (12-hour times, like Walter says them)
  function t12(hhmm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || ""); if (!m) return hhmm || "";
    const h = +m[1]; return ((h % 12) || 12) + ":" + m[2] + (h < 12 ? " AM" : " PM");
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function hhmmOf(ts) { const d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function dateOf(ts) { const d = new Date(ts * 1000); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function whenOf(ts) {
    const d = new Date(ts * 1000), now = new Date(), tm = new Date(now); tm.setDate(now.getDate() + 1);
    const t = t12(hhmmOf(ts));
    if (d.toDateString() === now.toDateString()) return "Today " + t;
    if (d.toDateString() === tm.toDateString()) return "Tomorrow " + t;
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + ", " + t;
  }
  function daysChip(r) {
    const l = r.days_label || "";
    const name = { daily: "Daily", weekdays: "Weekdays", weekends: "Weekends" }[l];
    return name || (r.days || []).map((d) => DAYS[d]).join(" ");
  }
  function firstLine(t) { const s = String(t || ""); const i = s.indexOf("\n"); return i < 0 ? s : s.slice(0, i); }
  function isLong(t) { const s = String(t || ""); return s.indexOf("\n") >= 0 || s.length > 90; }
  const ROLE = { briefing: "Morning briefing", wrapup: "Evening wrap-up", self_review: "Nightly self-review" };

  // ---- the list (same markup in Settings and in the sheet; everything wired by data- attributes)
  function textBlock(id, text) {
    const long = isLong(text), ex = expanded.has(id);
    return `<span class="sc-text${ex ? " full" : ""}">${esc(ex ? text : firstLine(text))}</span>` +
      (long ? `<button class="sc-more" type="button" data-more="${id}">${ex ? "less" : "more"}</button>` : "");
  }
  function row(r, kind) {
    const rt = kind === "routine";
    const when = rt ? t12(r.time) : whenOf(r.due);
    const chip = rt ? `<span class="sc-chip">${esc(daysChip(r))}</span>` : (r.kind === "do" ? '<span class="sc-chip">Walter does it</span>' : "");
    const off = rt && !r.enabled;
    const sw = rt ? `<label class="sc-sw" title="${off ? "Paused" : "Running"}"><input type="checkbox" data-pause="${r.id}"${off ? "" : " checked"}${busy ? " disabled" : ""} aria-label="${off ? "Resume" : "Pause"} this routine"><span></span></label>` : "";
    return `<div class="sc-row${off ? " paused" : ""}"><div class="sc-main" role="button" tabindex="0" data-edit="${r.id}">` +
      `<span class="sc-top"><b class="sc-when">${esc(when)}</b>${chip}${off ? '<span class="sc-chip warn">Paused</span>' : ""}</span>` +
      `${textBlock(r.id, r.text)}</div>${sw}</div>`;
  }
  function section(title, items, kind, addLabel) {
    const list = items.length ? items.map((r) => row(r, kind)).join("") : '<p class="muted small sc-empty">Nothing yet.</p>';
    return `<div class="sc-sec"><div class="sc-head"><span>${title} <span class="sc-n">${items.length}</span></span>` +
      `<button class="chip" type="button" data-add="${kind}"${busy ? " disabled" : ""}>+ ${addLabel}</button></div>${list}</div>`;
  }
  function loops() {
    const L = st.loops || {}, hb = L.heartbeat || { minutes: 0, min: 5, max: 120 }, q = hb.quiet_hours || [];
    const min = hbWant != null ? hbWant : hb.minutes;
    const quiet = q.length === 2 ? `, not in quiet hours (${t12(pad(q[0]) + ":00")} to ${t12(pad(q[1]) + ":00")})` : "";
    const links = Object.keys(ROLE).map((k) => {
      const r = (st.routines || []).find((x) => x.id === (L.routines || {})[k]);
      if (!r) return "";
      return `<button class="sc-link" type="button" data-edit="${r.id}"><span>${ROLE[k]}</span>` +
        `<span class="muted">${esc(t12(r.time))} ${esc(daysChip(r).toLowerCase())}${r.enabled ? "" : ", paused"}</span></button>`;
    }).join("");
    return `<div class="sc-sec sc-loops"><div class="sc-head"><span>Loops</span></div>` +
      `<div class="sc-loop"><div><b>Heartbeat</b><p class="muted small">Walter checks in on his own after ${min ? min + " quiet minutes" : "never (off)"}${quiet}.</p></div>` +
      `<div class="sc-step"><button class="btn" type="button" data-hb="-5" aria-label="Less often"${busy || min <= hb.min ? " disabled" : ""}>&minus;</button>` +
      `<b class="sc-hb">${min ? min + " min" : "Off"}</b>` +
      `<button class="btn" type="button" data-hb="5" aria-label="More minutes"${busy || min >= hb.max ? " disabled" : ""}>+</button></div></div>` +
      `<label class="toggle sc-loop"><input type="checkbox" data-sl${(L.sleep_review || {}).on ? " checked" : ""}${busy ? " disabled" : ""}>` +
      `<span><b>Sleep-time learning</b><span class="muted small"> one quiet self-review while he sleeps</span></span></label>` +
      links + (hb_editable(L) ? `<button class="sc-link" type="button" data-hbedit><span>Heartbeat instructions</span>` +
        `<span class="muted">${(L.heartbeat || {}).custom_prompt ? "custom wake-up line" : "checklist + wake-up line"}</span></button>` : "") + "</div>";
  }
  function hb_editable(L) { const h = (L || {}).heartbeat || {}; return h.editable !== false; }
  function timers() {
    const ts = st.timers || [];
    if (!ts.length) return "";
    return `<div class="sc-sec sc-sys"><div class="sc-head"><span>System timers <span class="sc-n">${ts.length}</span></span><span class="muted small">read-only</span></div>` +
      ts.map((t) => `<div class="sc-row ro"><div class="sc-main"><span class="sc-top"><b class="sc-when">${esc(t.name)}</b>` +
        `${t.every ? `<span class="sc-chip">${esc(t.every)}</span>` : ""}</span>` +
        `<span class="sc-text">${esc(t.description || "")}${t.next ? " · next " + esc(whenOf(t.next)) : ""}</span></div></div>`).join("") + "</div>";
  }
  function content() {
    if (!st) return '<p class="muted small">' + (needs ? "Needs one restart to enable." : "Loading...") + "</p>";
    return (stale ? '<p class="muted small sc-stale">Walter can\'t be reached: this is the last list seen.</p>' : "") +
      loops() + section("Reminders", st.reminders || [], "reminder", "Add") +
      section("Do later", st.later || [], "later", "Add") +
      section("Routines", st.routines || [], "routine", "Add") + timers();
  }
  function counts() {
    if (!st) return "";
    const n = (st.reminders || []).length + (st.later || []).length, r = (st.routines || []).length;
    return "(" + r + " routine" + (r === 1 ? "" : "s") + (n ? ", " + n + " one-off" : "") + ")";
  }
  function render() {
    const html = content();
    const set = $("scSetList"); if (set) set.innerHTML = html;
    const now = $("msScNow"); if (now) now.textContent = counts();
    if (!open) return;
    $("scNeeds").classList.toggle("hidden", !needs || !!st);
    if (!$("scMain").classList.contains("hidden")) $("scList").innerHTML = html;
  }

  // ---- sending
  async function send(body) {
    if (busy) return null;
    busy = true; render();
    try {
      const r = await raw(body);
      if (r.status === 404 && !r.data.code) { needs = true; msg("Needs one restart to enable.", true); return null; }
      return r;
    } catch (e) {
      msg("Walter didn't answer. Check the wall, then try again.", true);
      A.toast("Schedule", "Walter didn't answer.", "err", 5);
      return null;
    } finally { busy = false; render(); }
  }
  function fail(r) {
    const e = (r && r.data && r.data.error) || "That didn't work. Try again.";
    msg(e, true); A.toast("Schedule", e, "err", 6);
  }
  async function act(body, fallback) {
    const r = await send(body);
    if (r && r.status === 200) { take(r.data); msg(""); A.toast("Schedule", r.data.done || fallback, "", 3); render(); return true; }
    if (r) { fail(r); refresh(); }
    return false;
  }

  // ---- the sheet
  function show(which) {
    $("scMain").classList.toggle("hidden", which !== "main");
    $("scEdit").classList.toggle("hidden", which !== "edit");
    $("scHb").classList.toggle("hidden", which !== "hb");
    if (which === "main") { editing = null; adding = null; render(); }
  }
  function openSheet() {
    haptic();
    const ms = $("modeSheet"); if (ms && !ms.classList.contains("hidden")) $("msClose").click();
    open = true; $("scSheet").classList.remove("hidden"); msg(""); show("main"); refresh();
  }
  function closeSheet() { open = false; editing = null; adding = null; $("scSheet").classList.add("hidden"); }

  function find(id) {
    if (!st) return null;
    for (const k of ["reminders", "later", "routines"]) {
      const r = (st[k] || []).find((x) => x.id === id);
      if (r) return { r, kind: k === "routines" ? "routine" : k === "later" ? "later" : "reminder" };
    }
    return null;
  }
  function setDays(list) {
    document.querySelectorAll("#scDays [data-d]").forEach((b) => b.setAttribute("aria-pressed", list.indexOf(+b.dataset.d) >= 0 ? "true" : "false"));
  }
  function getDays() {
    const out = []; document.querySelectorAll("#scDays [data-d]").forEach((b) => { if (b.getAttribute("aria-pressed") === "true") out.push(+b.dataset.d); });
    return out;
  }
  function daysSpec(list) {
    const s = list.slice().sort().join(",");
    if (s === "0,1,2,3,4,5,6") return "daily";
    if (s === "0,1,2,3,4") return "weekdays";
    if (s === "5,6") return "weekends";
    return list.slice().sort().map((d) => DKEY[d]).join(",");
  }
  function counter() { $("scCount").textContent = $("scText").value.length + " / " + TXT_MAX; }
  function form(kind, r) {
    const rt = kind === "routine";
    $("scRoutineBox").classList.toggle("hidden", !rt);
    $("scDateBox").classList.toggle("hidden", rt);
    $("scDelete").classList.toggle("hidden", !r);
    $("scAsk").classList.add("hidden"); delAsk = false;
    const what = { routine: "routine", later: "job for Walter", reminder: "reminder" }[kind];
    $("scEditTitle").textContent = (r ? "Edit " : "New ") + what;
    $("scHint").textContent = kind === "reminder" ? "Walter says this out loud when it's due." :
      kind === "later" ? "Walter carries this out when it's due." : "Walter runs this on these days at this time.";
    if (rt) { $("scTime").value = r ? r.time : "08:00"; setDays(r ? r.days : [0, 1, 2, 3, 4]); }
    else {
      const due = r ? r.due : Date.now() / 1000 + 3600;
      $("scTime").value = hhmmOf(due); $("scDate").value = dateOf(due);
      $("scDate").min = dateOf(Date.now() / 1000);
    }
    $("scText").value = r ? r.text : ""; counter();
    msg(""); show("edit");
  }
  function startEdit(id) {
    const f = find(id); if (!f) return;
    if (!open) openSheet();
    editing = f; adding = null; form(f.kind, f.r);
  }
  function startAdd(kind) {
    if (!open) openSheet();
    adding = kind; editing = null; form(kind, null);
    $("scText").focus();
  }
  async function submit() {
    const kind = editing ? editing.kind : adding; if (!kind) return;
    const text = $("scText").value.replace(/\r\n/g, "\n").trim();
    if (!text) { msg("Say what it's for.", true); $("scText").focus(); return; }
    if (text.length > TXT_MAX) { msg(TXT_MAX + " characters at most.", true); return; }
    const time = $("scTime").value;
    if (!/^\d{2}:\d{2}$/.test(time)) { msg("Pick a time.", true); return; }
    const body = { text, time };
    if (kind === "routine") {
      const ds = getDays(); if (!ds.length) { msg("Pick at least one day.", true); return; }
      body.days = daysSpec(ds);
    } else {
      const date = $("scDate").value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { msg("Pick a date.", true); return; }
      if (new Date(date + "T" + time).getTime() <= Date.now()) { msg("That time has already passed.", true); return; }
      body.date = date;
    }
    let ok;
    if (editing) {
      const r = editing.r, ch = { action: "edit", id: r.id };
      if (text !== r.text) ch.text = text;
      if (kind === "routine") {
        if (time !== r.time || body.days !== daysSpec(r.days)) { ch.time = time; ch.days = body.days; }
      } else if (time !== hhmmOf(r.due) || body.date !== dateOf(r.due)) { ch.time = time; ch.date = body.date; }
      if (Object.keys(ch).length === 2) { show("main"); return; }
      ok = await act(ch, "Saved.");
    } else ok = await act({ action: "add", type: kind, ...body }, "Added.");
    if (ok) show("main");
  }
  async function del(confirmed) {
    if (!editing) return;
    if (!confirmed) {
      $("scAskLine").textContent = "Delete this " + (editing.kind === "routine" ? "routine" : editing.kind === "later" ? "job" : "reminder") +
        "? " + (editing.kind === "routine" ? "Pausing keeps it for later instead." : "This can't be undone.");
      $("scAsk").classList.remove("hidden"); delAsk = true; return;
    }
    if (await act({ action: "delete", id: editing.r.id, confirm: true }, "Deleted.")) show("main");
  }

  // ---- heartbeat instructions (wake-up line + initiative.md)
  function bytes(t) { try { return new Blob([t]).size; } catch (e) { return t.length; } }
  function kb(n) { return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB"; }
  function readDraft() { try { const d = JSON.parse(localStorage.getItem(DRAFT) || "null"); return d && typeof d.text === "string" ? d : null; } catch (e) { return null; } }
  function dropDraft() { clearTimeout(draftTimer); try { localStorage.removeItem(DRAFT); } catch (e) { /* private */ } }
  function keepDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      const t = $("scHbText").value;
      if (hb && t === hb.initiative_md) { dropDraft(); hbNote(); return; }
      try { localStorage.setItem(DRAFT, JSON.stringify({ t: Date.now(), text: t, base_mtime: hbBase })); } catch (e) { /* storage full */ }
      hbNote();
    }, 400);
  }
  function hbCounts() {
    const n = bytes($("scHbText").value);
    $("scHbCount").textContent = kb(n) + " / 60 KB";
    $("scHbCount").classList.toggle("bad", n > INI_MAX);
    $("scHbPCount").textContent = $("scHbPrompt").value.length + " / 1500";
  }
  function hbNote() {
    const d = readDraft(), el = $("scHbDraft");
    const dirty = hb && $("scHbText").value !== hb.initiative_md;
    el.classList.toggle("hidden", !dirty);
    if (dirty) el.textContent = "Unsaved changes" + (d ? ", kept on this device" : "") +
      (hb && hbBase !== hb.initiative_md_mtime ? ". Walter's copy changed since you started; Save replaces it, Revert loads his." : ".");
    hbCounts();
  }
  function backupsHtml() {
    const bs = (hb && hb.backups) || [];
    if (!bs.length) return '<p class="muted small">No earlier versions yet: one is kept every time the checklist is saved from here.</p>';
    return bs.map((b) => {
      const d = new Date(b.time * 1000);
      const when = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + ", " + t12(hhmmOf(b.time));
      const ask = bkAsk === b.id;
      return `<div class="sc-bk"><span>${esc(when)} <span class="muted small">${esc(kb(b.size))}</span></span>` +
        `<button type="button" class="chip${ask ? " warn" : ""}" data-restore="${esc(b.id)}"${busy ? " disabled" : ""}>${ask ? "Tap again to restore" : "Restore"}</button></div>`;
    }).join("");
  }
  function fillHb(data, keepText) {
    hb = data;
    if (!keepText) {
      hbBase = data.initiative_md_mtime;
      const d = readDraft();
      if (d && d.text !== data.initiative_md) { $("scHbText").value = d.text; hbBase = d.base_mtime === undefined ? null : d.base_mtime; }
      else { $("scHbText").value = data.initiative_md; dropDraft(); }
      $("scHbPrompt").value = data.prompt || "";
      $("scHbPrompt").placeholder = data.default_prompt || "";
    }
    $("scHbBackups").innerHTML = backupsHtml();
    $("scHbSave").disabled = !!data.initiative_md_truncated;
    if (data.initiative_md_truncated) msg("The checklist is over 60 KB on Walter, so it's shown cut short and can't be saved from here.", true);
    hbNote();
  }
  async function loadHb() {
    try {
      const r = await raw(undefined, "/api/app/schedule/heartbeat");
      if (r.status === 200) return r.data;
      if (r.status === 404) msg("Needs one restart to enable.", true);
      else msg((r.data && r.data.error) || "Couldn't load the heartbeat instructions.", true);
    } catch (e) { msg("Walter didn't answer. Check the wall, then try again.", true); }
    return null;
  }
  async function openHb() {
    if (!open) openSheet();
    haptic(); msg(""); bkAsk = null; $("scHbAsk").classList.add("hidden");
    show("hb");
    $("scHbText").value = ""; $("scHbDraft").classList.add("hidden");
    const d = await loadHb();
    if (d) fillHb(d, false);
  }
  async function hbSend(body, fallback) {
    const r = await send(body);
    if (!r) return null;
    if (r.status === 200) {
      take(r.data); msg(""); A.toast("Schedule", r.data.done || fallback, "", 3);
      if (r.data.heartbeat) fillHb(r.data.heartbeat, true);
      return r;
    }
    if (r.status === 409 && r.data.code === "conflict") {
      const d = await loadHb();       // keep the text; next Save replaces Walter's newer copy on purpose
      if (d) { hb = d; hbBase = d.initiative_md_mtime; $("scHbBackups").innerHTML = backupsHtml(); hbNote(); }
      msg("Walter's copy changed since you opened it. Your text is still here: Save again to replace his, or Revert to load his.", true);
      return null;
    }
    fail(r); return null;
  }
  async function saveIni(allowEmpty) {
    const text = $("scHbText").value.replace(/\r\n/g, "\n");
    if (bytes(text) > INI_MAX) { msg("The checklist is 60 KB at most.", true); return; }
    if (!text.trim() && !allowEmpty) { $("scHbAsk").classList.remove("hidden"); return; }
    $("scHbAsk").classList.add("hidden");
    const r = await hbSend({ action: "set_initiative", text, base_mtime: hbBase, allow_empty: !!allowEmpty }, "Checklist saved.");
    if (r) { dropDraft(); hbBase = hb.initiative_md_mtime; $("scHbText").value = hb.initiative_md; hbNote(); }
  }
  function revertIni() {
    if (!hb) return;
    dropDraft(); hbBase = hb.initiative_md_mtime; $("scHbText").value = hb.initiative_md;
    $("scHbAsk").classList.add("hidden"); msg(""); hbNote();
  }
  async function savePrompt(reset) {
    const text = reset ? "" : $("scHbPrompt").value.replace(/\s*[\r\n]+\s*/g, " ").trim();
    if (text.length > 1500) { msg("The wake-up line is 1500 characters at most.", true); return; }
    const r = await hbSend(reset || !text ? { action: "reset_heartbeat_prompt" } : { action: "set_heartbeat_prompt", text },
      reset || !text ? "Wake-up line back to the default." : "Wake-up line saved.");
    if (r && hb) $("scHbPrompt").value = hb.prompt || "";
  }
  async function restoreIni(id) {
    if (bkAsk !== id) {
      bkAsk = id; $("scHbBackups").innerHTML = backupsHtml();
      clearTimeout(bkTimer); bkTimer = setTimeout(() => { bkAsk = null; if (hb) $("scHbBackups").innerHTML = backupsHtml(); }, 4000);
      return;
    }
    bkAsk = null; clearTimeout(bkTimer);
    const r = await hbSend({ action: "restore_initiative", backup: id }, "Earlier version restored.");
    if (r) { dropDraft(); hbBase = hb.initiative_md_mtime; $("scHbText").value = hb.initiative_md; hbNote(); }
  }

  // ---- list clicks (Settings and the sheet)
  function onList(e) {
    const mo = e.target.closest("[data-more]");
    if (mo) { const id = +mo.dataset.more; if (expanded.has(id)) expanded.delete(id); else expanded.add(id); render(); return; }
    const pz = e.target.closest("[data-pause]");
    if (pz) { haptic(); act({ action: pz.checked ? "resume" : "pause", id: +pz.dataset.pause }, pz.checked ? "Running again." : "Paused."); return; }
    if (e.target.closest(".sc-sw")) return;
    const sl = e.target.closest("[data-sl]");
    if (sl) { haptic(); act({ action: "sleep_review", on: sl.checked }, "Saved."); return; }
    const hb = e.target.closest("[data-hb]");
    if (hb && st) {
      const H = (st.loops || {}).heartbeat || { minutes: 0, min: 5, max: 120 };
      const cur = hbWant != null ? hbWant : H.minutes;
      hbWant = Math.max(H.min, Math.min(H.max, (cur || 0) + (+hb.dataset.hb)));
      haptic(); render();
      clearTimeout(hbTimer);   // a few taps in a row = one change
      hbTimer = setTimeout(async () => {
        const want = hbWant;
        await act({ action: "heartbeat", minutes: want }, "Saved.");
        if (hbWant === want) hbWant = null;
        render();
      }, 700);
      return;
    }
    if (e.target.closest("[data-hbedit]")) { openHb(); return; }
    const ad = e.target.closest("[data-add]"); if (ad) { startAdd(ad.dataset.add); return; }
    const ed = e.target.closest("[data-edit]"); if (ed) startEdit(+ed.dataset.edit);
  }
  function onKey(e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".sc-main[data-edit]")) { e.preventDefault(); startEdit(+e.target.dataset.edit); }
  }

  // ---- wiring
  if ($("msSchedule")) $("msSchedule").addEventListener("click", openSheet);
  $("scClose").addEventListener("click", closeSheet);
  $("scSheet").addEventListener("click", (e) => { if (e.target.id === "scSheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (open && e.key === "Escape") closeSheet(); });
  [$("scList"), $("scSetList")].forEach((el) => { if (el) { el.addEventListener("click", onList); el.addEventListener("keydown", onKey); } });
  $("scEdit").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  $("scEdit").addEventListener("click", (e) => {
    if (e.target.closest("[data-cancel]")) { msg(""); show("main"); return; }
    const d = e.target.closest("#scDays [data-d]");
    if (d) { d.setAttribute("aria-pressed", d.getAttribute("aria-pressed") === "true" ? "false" : "true"); return; }
    const q = e.target.closest("[data-days]");
    if (q) setDays({ daily: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] }[q.dataset.days]);
  });
  $("scText").addEventListener("input", counter);
  $("scHbText").addEventListener("input", () => { hbCounts(); keepDraft(); });
  $("scHbPrompt").addEventListener("input", hbCounts);
  $("scHbSave").addEventListener("click", () => saveIni(false));
  $("scHbAskYes").addEventListener("click", () => saveIni(true));
  $("scHbAskNo").addEventListener("click", () => $("scHbAsk").classList.add("hidden"));
  $("scHbRevert").addEventListener("click", revertIni);
  $("scHbPSave").addEventListener("click", () => savePrompt(false));
  $("scHbPReset").addEventListener("click", () => savePrompt(true));
  $("scHbBack").addEventListener("click", () => { msg(""); show("main"); });
  $("scHbBackups").addEventListener("click", (e) => { const b = e.target.closest("[data-restore]"); if (b) restoreIni(b.dataset.restore); });
  $("scDelete").addEventListener("click", () => del(false));
  $("scAskYes").addEventListener("click", () => del(true));
  $("scAskNo").addEventListener("click", () => { $("scAsk").classList.add("hidden"); delAsk = false; });
  if ($("modeFab")) $("modeFab").addEventListener("click", refresh);   // keeps "Schedule (17 routines)" current

  const setView = $("v-settings");
  if (setView && window.MutationObserver) new MutationObserver(() => { if (setView.classList.contains("active")) refresh(); })
    .observe(setView, { attributes: true, attributeFilter: ["class"] });
  st = load(); stale = !!st; render();
  refresh();
})();
