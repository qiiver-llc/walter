// Add-ons (Settings > Add-ons; walter/app_addons.py, docs/ADDONS.md).
// Installed: a card per add-on with an on/off switch, description and permission chips ("sends code off this
// computer" in red). Turning one on that asks for permissions opens a confirm sheet listing them FIRST. Tap a
// card for its settings form (from the manifest), "Set secret" (opens Walter's private phone page; the secret
// never goes through this app), Update and Remove. Catalog: Install / Update available. Add from a repo: paste
// a GitHub address -> name, permissions and a "not made by Qiiver" warning -> Install (pinned to that commit).
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = window.walterApp;
  if (!A || !$("v-addons")) return;
  let items = [], cat = null, tab = "installed", open = new Set(), busy = false, preview = null;

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function haptic() { try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* no vibration */ } }
  function msg(t, bad) { const m = $("adMsg"); m.textContent = t || ""; m.classList.toggle("bad", !!bad); }
  async function raw(path, json, ms) {
    const headers = {}; const t = A.token(); if (t) headers.Authorization = "Bearer " + t;
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), ms || 15000);
    try {
      const r = await fetch(path, { method: json !== undefined ? "POST" : "GET", headers, cache: "no-store", signal: ctl.signal,
        body: json !== undefined ? JSON.stringify(json) : undefined });
      let data = {}; try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, data };
    } catch (e) { return { status: 0, data: { error: "Can't reach Walter right now." } }; }
    finally { clearTimeout(tm); }
  }
  const act = (body, ms) => raw("/api/app/addons", body, ms || 20000);
  function err(r, what) { A.toast(what, (r.data && r.data.error) || (r.status === 404 ? "Needs one restart of Walter first." : "Something went wrong."), "err", 7); }

  // ---- sheet
  function sheet(title, html, buttons) {
    $("adSheetTitle").textContent = title;
    $("adSheetBody").innerHTML = html;
    const box = $("adSheetBtns"); box.innerHTML = "";
    for (const [label, cls, fn] of buttons) {
      const b = document.createElement("button");
      b.className = "btn " + (cls || ""); b.textContent = label; b.onclick = fn; box.appendChild(b);
    }
    $("adSheet").classList.remove("hidden");
  }
  function closeSheet() { $("adSheet").classList.add("hidden"); }
  $("adSheet").addEventListener("click", (e) => { if (e.target.id === "adSheet") closeSheet(); });

  // the add-on's favicon (or worker icon) beside its name: only Walter's own sanitized data: URIs
  function fav(u) {
    const ok = A.safeIcon ? A.safeIcon(u) : false;
    return ok ? `<img class="ad-ico" alt="" src="${u}">` : "";
  }
  function chips(perms) {
    if (!perms || !perms.length) return '<span class="ad-perm">No special permissions</span>';
    return perms.map((p) => `<span class="ad-perm${p.danger ? " danger" : ""}">${esc(p.label)}</span>`).join("");
  }
  function permList(perms) {
    return "<ul>" + perms.map((p) => `<li class="${p.danger ? "ad-red" : ""}">${esc(p.label)}</li>`).join("") + "</ul>";
  }

  // ---- installed
  async function load() {
    const r = await raw("/api/app/addons");
    if (r.status === 404 || r.status === 405) { msg("Needs one restart of Walter before add-ons work."); items = []; render(); return; }
    if (r.status !== 200) { msg(r.data.error || "Can't reach Walter right now.", true); return; }
    items = r.data.addons || []; msg(r.data.walled ? "You're in a walled project: add-ons that send code off this computer stay unused here." : "");
    const on = items.filter((a) => a.on).length;
    if ($("adRowSub")) $("adRowSub").textContent = items.length ? `${items.length} installed, ${on} on` : "Extras you can switch on, with clear permissions";
    render();
  }

  function settingField(a, s) {
    const v = a.settings[s.key], id = `ad-${a.id}-${s.key}`;
    const lab = `<label for="${id}">${esc(s.label)}</label>`;
    if (s.type === "bool") return `<label class="toggle small"><input type="checkbox" id="${id}" data-ad="${esc(a.id)}" data-key="${esc(s.key)}" ${v ? "checked" : ""}> ${esc(s.label)}</label>`;
    if (s.type === "choice") return lab + `<select id="${id}" data-ad="${esc(a.id)}" data-key="${esc(s.key)}">` +
      s.choices.map((c) => `<option ${c === v ? "selected" : ""}>${esc(c)}</option>`).join("") + "</select>";
    const num = s.type === "number";
    return lab + `<input id="${id}" data-ad="${esc(a.id)}" data-key="${esc(s.key)}" ${num ? 'type="number" inputmode="decimal"' : 'autocapitalize="off" spellcheck="false"'}
      ${num && s.min != null ? `min="${s.min}"` : ""} ${num && s.max != null ? `max="${s.max}"` : ""} maxlength="500" value="${esc(v)}">`;
  }

  function card(a) {
    if (a.broken) return `<div class="ad-card broken"><div class="ad-head"><b>${esc(a.id)}</b></div><p class="small ad-red">Broken: ${esc((a.errors || []).join("; "))}</p>
      <div class="row"><button class="chip" data-remove="${esc(a.id)}">Remove</button></div></div>`;
    const need = (a.secrets || []).filter((s) => s.required && !s.set);
    const more = open.has(a.id);
    let h = `<div class="ad-card${a.on ? " on" : ""}">
      <div class="ad-head"><button class="ad-main" data-open="${esc(a.id)}" aria-expanded="${more}">
        ${fav(a.favicon_url)}<b>${esc(a.name)}</b> <span class="muted small">${esc(a.version)}${a.official ? "" : " &middot; not from Qiiver"}</span>
        <span class="ad-desc small">${esc(a.description)}</span></button>
        <label class="ad-switch"><input type="checkbox" data-toggle="${esc(a.id)}" ${a.on ? "checked" : ""} aria-label="${esc(a.name)} on or off"><span></span></label></div>
      <div class="ad-perms">${chips(a.permissions)}</div>`;
    if (need.length) h += `<p class="small ad-warn">Needs: ${need.map((s) => esc(s.label)).join(", ")}</p>`;
    if (more) {
      h += '<div class="ad-more stack">';
      for (const s of a.settings_schema || []) h += `<div class="ad-field">${settingField(a, s)}${s.help ? `<p class="muted small">${esc(s.help)}</p>` : ""}</div>`;
      for (const s of a.secrets || []) h += `<div class="row ad-secret"><span class="small">${esc(s.label)}: <b>${s.set ? "set" : "not set"}</b></span>
        <button class="chip" data-secret="${esc(a.id)}" data-key="${esc(s.key)}">${s.set ? "Replace" : "Set secret"}</button></div>`;
      const src = a.source || {};
      if (src.repo) h += `<p class="muted small">From ${esc(src.repo)}${src.path ? "/" + esc(src.path) : ""} at ${esc((src.commit || "").slice(0, 10))}</p>`;
      if (a.mcp) h += '<p class="muted small">Brings tools; they arrive after Walter\'s next new session.</p>';
      h += `<div class="row"><button class="chip" data-update="${esc(a.id)}">Check for update</button>
        <button class="chip" data-remove="${esc(a.id)}">Remove</button></div></div>`;
    }
    return h + "</div>";
  }

  function render() {
    const box = $("adInstalled");
    box.innerHTML = items.length ? items.map(card).join("")
      : '<p class="muted small">Nothing installed yet. Have a look in the Catalog.</p>';
  }

  async function toggle(id, on, input) {
    if (busy) { input.checked = !on; return; }
    const a = items.find((x) => x.id === id) || {};
    if (!on) {
      busy = true; const r = await act({ action: "off", id }); busy = false;
      if (r.status !== 200) { input.checked = true; err(r, "Couldn't turn it off"); } else A.toast(a.name + " is off", "", "", 3);
      return load();
    }
    busy = true; let r = await act({ action: "on", id }); busy = false;
    if (r.status === 409 && r.data.needs_confirm) {
      input.checked = false;
      const danger = (r.data.permissions || []).some((p) => p.danger);
      sheet(`Turn on ${r.data.name}?`, `It asks to:${permList(r.data.permissions || [])}` +
        (danger ? '<p class="ad-red">It sends code or files off this computer. Walter never uses it in walled projects.</p>' : "") +
        (a.official ? "" : '<p class="ad-warn">Not made by Qiiver. Only turn on add-ons from people you trust.</p>'),
      [["Cancel", "", closeSheet], ["Turn on", danger ? "warnfill" : "primary", async () => {
        closeSheet(); busy = true; const r2 = await act({ action: "on", id, confirm: true }); busy = false;
        if (r2.status === 200) A.toast(a.name + " is on", r2.data.mcp_note || "Walter picks it up from his next turn.", "", 5); else err(r2, "Couldn't turn it on");
        load();
      }]]);
      return;
    }
    if (r.status === 200) A.toast(a.name + " is on", r.data.mcp_note || "Walter picks it up from his next turn.", "", 5);
    else { input.checked = false; err(r, "Couldn't turn it on"); }
    load();
  }

  async function setSecret(id, key) {
    haptic();
    const r = await act({ action: "secret", id, key }, 20000);
    if (r.status !== 200) return err(r, "Couldn't open the private page");
    sheet("Hand over the secret privately", `Open Walter's private page and paste it there. It goes straight into a locked file on Walter, never into the chat.
      <p class="muted small">The page works on your home Wi-Fi for 15 minutes; its QR code is on the wall too.</p>`,
    [["Close", "", closeSheet], ["Open the page", "primary", () => { window.open(r.data.url, "_blank", "noopener"); closeSheet(); setTimeout(load, 20000); }]]);
  }

  async function update(id) {
    msg("Checking for an update...");
    let r = await act({ action: "update", id }, 60000);
    if (r.status === 409 && r.data.needs_confirm) {
      const p = r.data.preview; msg("");
      sheet(`Update ${p.name} to ${p.version}?`, `From ${esc(p.repo)} at ${esc(p.commit.slice(0, 10))}. It asks to:${permList(p.permissions)}<p class="ad-warn">${esc(p.warning)}</p>`,
        [["Cancel", "", closeSheet], ["Update", "primary", async () => {
          closeSheet(); const r2 = await act({ action: "update", id, confirm: true }, 60000); done(r2);
        }]]);
      return;
    }
    done(r);
    function done(x) {
      msg("");
      if (x.status === 200) A.toast(x.data.addon.name + " " + x.data.addon.version, x.data.addon.permissions_grew ? "It asks for more now, so it's off until you turn it on again." : (x.data.addon.updated ? "Updated." : "Up to date."), "", 6);
      else err(x, "Couldn't update");
      load(); if (cat) loadCatalog();
    }
  }

  function remove(id) {
    const a = items.find((x) => x.id === id) || { name: id };
    sheet(`Remove ${a.name}?`, "Its files, settings and secrets are deleted from Walter.",
      [["Cancel", "", closeSheet], ["Remove", "warnfill", async () => {
        closeSheet(); const r = await act({ action: "remove", id, confirm: true });
        if (r.status === 200) A.toast(a.name + " removed", "", "", 3); else err(r, "Couldn't remove it");
        load(); if (cat) loadCatalog();
      }]]);
  }

  $("adInstalled").addEventListener("click", (e) => {
    const t = e.target.closest("[data-open],[data-secret],[data-update],[data-remove]");
    if (!t) return;
    if (t.dataset.open) { haptic(); open.has(t.dataset.open) ? open.delete(t.dataset.open) : open.add(t.dataset.open); render(); }
    else if (t.dataset.secret) setSecret(t.dataset.secret, t.dataset.key);
    else if (t.dataset.update) update(t.dataset.update);
    else if (t.dataset.remove) remove(t.dataset.remove);
  });
  $("adInstalled").addEventListener("change", async (e) => {
    const t = e.target;
    if (t.dataset.toggle) { haptic(); return toggle(t.dataset.toggle, t.checked, t); }
    if (t.dataset.ad && t.dataset.key) {
      const value = t.type === "checkbox" ? t.checked : t.value;
      const r = await act({ action: "setting", id: t.dataset.ad, key: t.dataset.key, value });
      if (r.status === 200) A.toast("Saved", "", "", 2); else { err(r, "Couldn't save that"); load(); }
    }
  });

  // ---- catalog
  async function loadCatalog() {
    $("adCatalog").innerHTML = '<p class="muted small">Loading the catalog...</p>';
    const r = await raw("/api/app/addons/catalog", undefined, 25000);
    if (r.status !== 200) { $("adCatalog").innerHTML = `<p class="small ad-red">${esc(r.data.error || "Couldn't load the catalog.")}</p>`; return; }
    cat = r.data.addons || [];
    $("adCatalog").innerHTML = cat.length ? cat.map((e) => `<div class="ad-card${e.on ? " on" : ""}">
      <div class="ad-head"><div class="ad-main">${fav(e.favicon_url)}<b>${esc(e.name)}</b> <span class="muted small">${esc(e.version)}</span>
        <span class="ad-desc small">${esc(e.description)}</span></div>
        ${e.update ? `<button class="btn small primary" data-cupdate="${esc(e.id)}">Update available</button>`
          : e.installed ? '<span class="muted small">Installed</span>'
          : `<button class="btn small primary" data-install="${esc(e.id)}">Install</button>`}</div>
      <div class="ad-perms">${chips(e.permissions)}</div></div>`).join("")
      : '<p class="muted small">The catalog is empty for now.</p>';
  }
  $("adCatalog").addEventListener("click", async (e) => {
    const t = e.target.closest("[data-install],[data-cupdate]");
    if (!t || busy) return;
    haptic();
    if (t.dataset.cupdate) return update(t.dataset.cupdate);
    busy = true; t.disabled = true; t.textContent = "Installing...";
    const r = await act({ action: "install", id: t.dataset.install }, 90000);
    busy = false;
    if (r.status === 200) A.toast(r.data.addon.name + " installed", "It's off until you turn it on (Installed tab).", "", 6);
    else err(r, "Couldn't install it");
    loadCatalog(); load();
  });

  // ---- add from a repo
  $("adPreview").onclick = async () => {
    const url = $("adUrl").value.trim();
    if (!url) { A.toast("Repo address", "Paste a GitHub address first.", "err", 4); return; }
    $("adPreviewBox").innerHTML = '<p class="muted small">Checking...</p>';
    const r = await act({ action: "preview", url }, 30000);
    if (r.status !== 200) { preview = null; $("adPreviewBox").innerHTML = `<p class="small ad-red">${esc(r.data.error || "Couldn't read that repo.")}</p>`; return; }
    preview = { url, p: r.data.preview };
    const p = preview.p;
    $("adPreviewBox").innerHTML = `<div class="ad-card">
      <div class="ad-head"><div class="ad-main">${fav(p.favicon_url)}<b>${esc(p.name)}</b> <span class="muted small">${esc(p.version)}</span>
      <span class="ad-desc small">${esc(p.description)}</span></div></div>
      <div class="ad-perms">${chips(p.permissions)}</div>
      <p class="muted small">${esc(p.repo)}${p.path ? "/" + esc(p.path) : ""} at ${esc(p.commit.slice(0, 10))}${p.author ? " &middot; by " + esc(p.author) : ""}</p>
      <p class="ad-warn small">${esc(p.warning)}</p>
      ${p.installed ? `<p class="muted small">Already installed (${esc(p.installed_version)}).</p>` : ""}
      <button id="adAdd" class="btn primary">${p.installed ? "Replace with this version" : "Install"}</button></div>`;
    $("adAdd").onclick = async () => {
      if (!preview || busy) return;
      busy = true; $("adAdd").disabled = true; $("adAdd").textContent = "Installing...";
      const r2 = await act({ action: "add", url: preview.url, commit: preview.p.commit, confirm: true }, 90000);
      busy = false;
      if (r2.status === 200) {
        A.toast(r2.data.addon.name + " installed", "It's off until you turn it on.", "", 6);
        $("adUrl").value = ""; $("adPreviewBox").innerHTML = ""; preview = null; setTab("installed");
      } else { err(r2, "Couldn't install it"); $("adAdd").disabled = false; $("adAdd").textContent = "Install"; }
    };
  };

  // ---- tabs, kill switch, showing the screen
  function setTab(t) {
    tab = t;
    document.querySelectorAll("[data-adtab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.adtab === t ? "true" : "false"));
    $("adInstalled").classList.toggle("hidden", t !== "installed");
    $("adCatalog").classList.toggle("hidden", t !== "catalog");
    $("adRepo").classList.toggle("hidden", t !== "repo");
    if (t === "installed") load(); else if (t === "catalog") loadCatalog();
  }
  document.querySelectorAll("[data-adtab]").forEach((b) => b.addEventListener("click", () => { haptic(); setTab(b.dataset.adtab); }));
  $("adKill").onclick = () => {
    haptic();
    sheet("Turn every add-on off?", "Walter stops using all of them right away. Their settings are kept.",
      [["Cancel", "", closeSheet], ["Turn all off", "warnfill", async () => {
        closeSheet(); const r = await act({ action: "off_all", confirm: true });
        if (r.status === 200) A.toast("All add-ons are off", "", "ok", 4); else err(r, "Couldn't turn them off");
        load();
      }]]);
  };
  let shown = false;
  new MutationObserver(() => {
    const now = $("v-addons").classList.contains("active");
    if (now && !shown) setTab(tab);
    shown = now;
  })
    .observe($("v-addons"), { attributes: true, attributeFilter: ["class"] });
})();
