/* Build mode v2 cockpit: pure helpers (no DOM), shared by face/cockpit.js and the node tests.
 *   md(text)              safe minimal markdown -> HTML: headings, lists, numbered lists, checkboxes,
 *                         **bold**, *italic*, `code`; the closing question (last "?" line) is marked .ask
 *   highlight(text, lang) tiny syntax colouring for py / js / css / html / sh / md (every piece escaped)
 *   langOf(file)          file name -> lang
 *   CHEAT                 the voice cheat sheet (grouped)
 *   SAY                   rotating "Say:" examples for the command bar
 *   soundAllowed(date, media, setting)  sound cues: on by default, off in quiet hours (10 PM-8 AM),
 *                         while a podcast / video plays, or when walterctl display build sound off
 *   testCue(prev, cur)    "pass" | "fail" | null when a test run just finished
 *   checklist(text)       plan -> [{text, done}] for the slim checklist beside the cockpit (build phase)
 *   wsList / currentWs / stepWs / pillsHtml / projectsHtml / workerVisible: the workspaces view (pill bar,
 *                         "next project", the "projects" zoom, the stream filtered to the current workspace)
 */
(function (root) {
  "use strict";

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // inline: escape first, then only our own tags come back
  function inline(s) {
    let out = esc(s);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    out = out.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<i>$2</i>");
    return out;
  }

  function md(text) {
    const lines = String(text || "").replace(/\r/g, "").split("\n");
    const html = [];
    let list = null; // "ul" | "ol"
    let lastAsk = -1;
    lines.forEach((l, i) => { if (/\?\s*\**\s*$/.test(l.trim())) lastAsk = i; });
    const close = () => { if (list) { html.push(`</${list}>`); list = null; } };
    lines.forEach((raw, i) => {
      const line = raw.replace(/\s+$/, "");
      const ask = i === lastAsk ? " ask" : "";
      let m;
      if (!line.trim()) { close(); return; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        close();
        const n = Math.min(4, m[1].length + 1);
        html.push(`<h${n} class="md${ask}">${inline(m[2])}</h${n}>`);
        return;
      }
      if ((m = line.match(/^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$/))) {
        if (list !== "ul") { close(); html.push('<ul class="md">'); list = "ul"; }
        const done = m[1].toLowerCase() === "x";
        html.push(`<li class="cb${done ? " done" : ""}${ask}"><span class="box">${done ? "&#10003;" : ""}</span>${inline(m[2])}</li>`);
        return;
      }
      if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
        if (list !== "ul") { close(); html.push('<ul class="md">'); list = "ul"; }
        html.push(`<li class="${ask.trim()}">${inline(m[1])}</li>`);
        return;
      }
      if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        if (list !== "ol") { close(); html.push(`<ol class="md" start="${parseInt(m[1], 10)}">`); list = "ol"; }
        html.push(`<li class="${ask.trim()}">${inline(m[2])}</li>`);
        return;
      }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { close(); html.push("<hr>"); return; }
      close();
      html.push(`<p class="md${ask}">${inline(line.replace(/^>\s?/, ""))}</p>`);
    });
    close();
    return html.join("");
  }

  // the plan as a slim checklist: list items (checkbox state kept), else headings
  function checklist(text, max) {
    const out = [];
    for (const raw of String(text || "").split("\n")) {
      let m;
      if ((m = raw.match(/^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$/))) out.push({ text: m[2], done: m[1] !== " " });
      else if ((m = raw.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/))) out.push({ text: m[1], done: false });
    }
    return out.map((x) => ({ text: x.text.replace(/\*\*|`/g, "").replace(/\s+/g, " ").trim().slice(0, 80), done: x.done }))
      .filter((x) => x.text).slice(0, max || 14);
  }

  // ---- tiny highlighter ----------------------------------------------------------------------
  const KW = {
    py: "def|class|return|if|else|elif|for|while|import|from|try|except|finally|with|as|in|not|and|or|is|None|True|False|self|raise|yield|lambda|pass|break|continue|async|await|global|nonlocal",
    js: "const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|import|from|export|default|try|catch|finally|throw|async|await|of|in|typeof|instanceof|null|true|false|undefined|this|yield",
    sh: "if|then|else|elif|fi|for|while|do|done|case|esac|in|function|return|local|export|set|echo|exit|cd|sudo",
  };
  const RULES = {
    py: { c: "#.*$", s: `"""[\\s\\S]*?"""|'''[\\s\\S]*?'''|[rbfu]?"(?:[^"\\\\]|\\\\.)*"|[rbfu]?'(?:[^'\\\\]|\\\\.)*'`, k: KW.py, d: "@[\\w.]+" },
    js: { c: "//.*$|/\\*[\\s\\S]*?(?:\\*/|$)", s: `"(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|\`(?:[^\`\\\\]|\\\\.)*\`?`, k: KW.js, d: "" },
    sh: { c: "#.*$", s: `"(?:[^"\\\\]|\\\\.)*"|'[^']*'`, k: KW.sh, d: "\\$\\{?[\\w@#?*]+\\}?" },
    css: { c: "/\\*[\\s\\S]*?(?:\\*/|$)", s: `"[^"]*"|'[^']*'`, k: "", d: "#[0-9a-fA-F]{3,8}\\b|@[\\w-]+|[\\w-]+(?=\\s*:)" },
    html: { c: "<!--[\\s\\S]*?(?:-->|$)", s: `"[^"]*"|'[^']*'`, k: "", d: "</?[\\w-]+|/?>" },
    md: { c: "", s: "`[^`]*`", k: "", d: "^#{1,6} .*$|\\*\\*[^*]+\\*\\*|^\\s*(?:[-*+]|\\d+\\.)\\s" },
  };
  const CACHE = {};
  function rx(lang) {
    if (CACHE[lang]) return CACHE[lang];
    const r = RULES[lang];
    const parts = [r.c || "(?!x)x", r.s || "(?!x)x", r.k ? `\\b(?:${r.k})\\b` : "(?!x)x", r.d || "(?!x)x",
      "\\b\\d+(?:\\.\\d+)?\\b"];
    return (CACHE[lang] = new RegExp(parts.map((p) => `(${p})`).join("|"), "gm"));
  }
  function highlight(text, lang) {
    text = String(text == null ? "" : text);
    if (!RULES[lang]) return esc(text);
    const re = rx(lang);
    re.lastIndex = 0;
    let out = "", i = 0, m;
    while ((m = re.exec(text))) {
      if (!m[0]) { re.lastIndex++; continue; }
      out += esc(text.slice(i, m.index));
      const cls = m[1] ? "c" : m[2] ? "s" : m[3] ? "k" : m[4] ? "d" : "u";
      out += `<span class="${cls}">${esc(m[0])}</span>`;
      i = m.index + m[0].length;
    }
    return out + esc(text.slice(i));
  }
  function langOf(f) {
    f = String(f || "");
    const base = f.split("/").pop();
    const e = base.includes(".") ? base.split(".").pop().toLowerCase() : "";
    if (["js", "mjs", "cjs", "ts", "json"].includes(e)) return "js";
    if (["py", "pyi", "toml"].includes(e)) return "py";
    if (["css", "scss"].includes(e)) return "css";
    if (["html", "htm", "svg", "xml"].includes(e)) return "html";
    if (["md", "markdown", "txt"].includes(e)) return "md";
    if (["sh", "bash", "service", "timer", "conf", "env"].includes(e) || !e) return "sh";
    return "";
  }

  // ---- voice --------------------------------------------------------------------------------
  const CHEAT = [
    { group: "Look around", items: [
      ["focus worker 2", "follow one worker"], ["show the diff", "the live change"], ["show the plan", "the plan pane"],
      ["show the tests", "test runner, big"], ["show the board", "Ready / Building / Done"],
      ["zoom in on stream", "or diff, tests, board, graph, plan"], ["back", "all panels again"],
      ["scroll up / scroll down", "move through the diff"] ] },
    { group: "Ask Walter", items: [
      ["run the tests", "a full test run"], ["what changed?", "a quick summary"], ["ship it", "release, after a yes"],
      ["build it / go", "start building the plan"] ] },
    { group: "Projects", items: [
      ["next project / previous project", "switch workspace (or tap a pill)"], ["show all projects", "every workspace at a glance"],
      ["switch to Acme", "or any workspace by name"] ] },
    { group: "Mode", items: [
      ["cheat sheet", "this list"], ["close", "close it"], ["done building", "leave build mode (or Esc)"],
      ["dev mode / exit dev mode", "same as build mode on / off"] ] },
    { group: "Themes", items: [
      ["fall theme", "or light, cream, spring, summer, winter, sunset, ocean, forest, midnight"],
      ["next theme / random theme", "try another look"], ["auto theme", "by season, midnight at night"],
      ["what themes are there", "the list, for a few seconds"] ] },
    { group: "Calendar", items: [
      ["calendar counts / count view", "a tile per calendar with a count"], ["calendar chips / chip view", "every event (the usual)"],
      ["toggle the calendar style", "swap between the two"] ] },
  ];
  const SAY = ["focus worker 2", "show the diff", "run the tests", "zoom in on tests", "what changed?",
    "show the board", "back", "cheat sheet", "ship it", "next project", "show all projects", "done building"];

  function quietHours(d) { const h = d.getHours(); return h >= 22 || h < 8; }
  function soundAllowed(d, media, setting) {
    if (String(setting || "on").toLowerCase() === "off") return false;
    return !quietHours(d) && !media;
  }
  // a finished run that we haven't cued yet
  function testCue(prev, cur) {
    if (!cur || cur.running || !cur.when) return null;
    if (!prev) return null;            // first data after a reload: no sound
    if (prev.when === cur.when && !prev.running) return null;
    return cur.ok ? "pass" : "fail";
  }

  // ---- workspaces (pills across the top + the "projects" zoom; owner approved 11 Nov 09:37) -----------
  // list = dashboard-build.json cockpit.workspaces; cur = the current id from face/dashboard-workspace.json
  // (published the moment a switch happens) or null to trust the list. Walled workspaces that aren't the
  // current one arrive with counts only; these helpers never invent more.
  const LOCK = '<svg class="lock" viewBox="0 0 10 12" aria-label="walled"><rect x="1" y="5" width="8" height="6.5" rx="1.2"/>' +
    '<path d="M3 5V3.5a2 2 0 0 1 4 0V5" fill="none" stroke-width="1.4"/></svg>';
  function wsList(list, cur) {
    const L2 = Array.isArray(list) ? list.filter((w) => w && w.id) : [];
    const known = cur && L2.some((w) => w.id === cur);
    return L2.map((w) => {
      const b = w.badges || {};
      const n = (k) => Math.max(0, parseInt(b[k], 10) || 0);
      return { id: String(w.id), name: String(w.name || w.id), walled: !!w.walled,
        current: known ? w.id === cur : !!w.current, running: n("running"), queued: n("queued"), ready: n("ready"),
        done: n("done_today"), focus: w.focus || "", commit: w.last_commit || null,
        usage: Math.max(0, Math.min(1, +w.usage_share || 0)), budget: Math.max(0, Math.min(1, +w.budget_share || 0)) };
    });
  }
  function currentWs(list, cur) { return wsList(list, cur).find((w) => w.current) || null; }
  // "next project" / "previous project": the id to switch to (wraps), or null with fewer than two
  function stepWs(list, cur, dir) {
    const W = wsList(list, cur);
    if (W.length < 2) return null;
    let i = W.findIndex((w) => w.current);
    if (i < 0) i = dir > 0 ? -1 : 0;
    return W[(i + (dir > 0 ? 1 : -1) + W.length) % W.length].id;
  }
  // the pill bar: current one lit; badges for running workers and ready tasks; a lock on walled ones
  function pillsHtml(list, cur, opts) {
    const W = wsList(list, cur);
    if (!W.length) return "";
    const tight = W.length > 4 ? " tight" : "";
    return `<div class="wsp${tight}">` + W.map((w) =>
      `<button type="button" class="pill${w.current ? " on" : ""}${w.walled ? " walled" : ""}" data-ws="${esc(w.id)}" title="${esc(w.name)}">` +
      (w.walled ? LOCK : "") + `<span class="nm">${esc(w.name)}</span>` +
      (w.running ? `<span class="b run" title="workers running">${w.running}</span>` : "") +
      (w.ready ? `<span class="b rdy" title="ready tasks">${w.ready}</span>` : "") + "</button>").join("") +
      `<button type="button" class="pill all${opts && opts.zoomed ? " on" : ""}" data-ws="*">All projects</button></div>`;
  }
  // the "projects" zoom: one card per workspace
  function projectsHtml(list, cur) {
    const W = wsList(list, cur);
    if (!W.length) return '<div class="empty">No workspaces yet. Walter sets them up with walterctl workspace list.</div>';
    const pct = (x) => Math.round(x * 100);
    return '<div class="prj">' + W.map((w) => {
      const hidden = w.walled && !w.current;
      const stats = `<div class="ps"><span><b>${w.running}</b> running</span>${w.queued ? `<span><b>${w.queued}</b> queued</span>` : ""}` +
        `<span><b>${w.ready}</b> ready</span><span><b>${w.done}</b> done today</span></div>`;
      const body = hidden ? '<div class="pf dim">Walled: counts only</div>' :
        `<div class="pf">${w.focus ? esc(w.focus) : '<span class="dim">No focus set</span>'}</div>` +
        `<div class="pc mono">${w.commit ? esc(w.commit.subject || "") + ` <span class="dim">${esc(w.commit.ago || "")}</span>` : '<span class="dim">No commits</span>'}</div>`;
      const bar = `<div class="pu"><div class="pb"><b style="transform:scaleX(${w.usage.toFixed(3)})"></b>` +
        `<i style="left:${pct(w.budget)}%"></i></div><span>${pct(w.usage)}% of recent work · aim ${pct(w.budget)}%</span></div>`;
      return `<div class="pj${w.current ? " on" : ""}${w.walled ? " walled" : ""}" data-ws="${esc(w.id)}">` +
        `<div class="pn2">${w.walled ? LOCK : ""}${esc(w.name)}${w.current ? '<em>current</em>' : ""}</div>${body}${stats}${bar}</div>`;
    }).join("") + "</div>";
  }
  // the activity stream follows the workspace: tagged workers only in their own; untagged (older) ones
  // everywhere except in a walled workspace. ws = {id, walled} or null (no workspaces: show all).
  function workerVisible(w, ws) {
    if (!ws || !ws.id) return true;
    if (w && w.workspace) return w.workspace === ws.id;
    return !ws.walled;
  }

  // every card follows the current project (owner, 11 Nov 10:34): each workspace has its own plan file
  // (dashboard-plan-<id>.md) and test file (dashboard-tests-<id>.json); without workspaces (id null) the
  // old shared files. Ids are whitelisted, so a file name can never be anything else.
  function wsFile(kind, id) {
    const ok = typeof id === "string" && /^[a-z0-9][a-z0-9-]{0,39}$/.test(id);
    if (kind === "plan") return ok ? `dashboard-plan-${id}.md` : "dashboard-plan.md";
    return ok ? `dashboard-tests-${id}.json` : "dashboard-tests.json";
  }
  // the plan NAME for this workspace from display.json: build_plans {id: name} (walterctl display build plan);
  // an older display.json with just build_plan names a plan the face only shows if that workspace's file
  // exists (it was moved to the Walter repo's workspace), so other projects never see it
  function planFor(disp, id) {
    const d = disp || {};
    if (d.build_plans && typeof d.build_plans === "object" && !Array.isArray(d.build_plans)) {
      return id ? String(d.build_plans[id] || "") : String(d.build_plan || "");
    }
    return String(d.build_plan || "");
  }
  // a test file only counts for the workspace it was written for (null = no run for this project)
  function testsFor(t, id) {
    if (!t || typeof t !== "object") return null;
    if (id && t.workspace && t.workspace !== id) return null;
    return t;
  }

  // GitHub Actions under the local test counters (walter/ghactions.py via dashboard-build.json cockpit.actions):
  // a dot per workflow (ok green, bad red, run amber, off grey), the name, the repo label when there are
  // several repos, "2m ago"; at most `max` lines, then "+N". "" (hide the section) when there is nothing.
  function shortAgo(ts, nowS) {
    const s = Math.max(0, Math.floor(nowS - ts));
    return s < 60 ? "now" : s < 3600 ? Math.floor(s / 60) + "m ago" : s < 86400 ? Math.floor(s / 3600) + "h ago" : Math.floor(s / 86400) + "d ago";
  }
  function actionsHtml(A, nowS, max) {
    if (!A || typeof A !== "object") return "";
    max = max || 4;
    const items = Array.isArray(A.items) ? A.items : [];
    const noacc = Array.isArray(A.noaccess) ? A.noaccess : [];
    if (!items.length && !noacc.length) return "";
    nowS = nowS == null ? Date.now() / 1000 : nowS;
    const DOT = { ok: "ok", bad: "bad", run: "run", off: "off" };
    const rows = items.slice(0, max).map((x) => {
      const st = DOT[x.state] || "off";
      return `<div class="ac ${st}"><span class="dot"></span><span class="an">${esc(x.name || "workflow")}</span>` +
        (A.multi && x.repo ? `<span class="ar">${esc(x.repo)}</span>` : "") +
        `<span class="at">${x.ts ? shortAgo(x.ts, nowS) : ""}</span></div>`;
    }).join("");
    const more = items.length > max ? `<div class="ac more">+${items.length - max} more</div>` : "";
    const notes = [];
    if (A.offline_since) notes.push(`offline · last seen ${shortAgo(A.offline_since, nowS)}`);
    if (noacc.length) notes.push(`${noacc.map(esc).join(", ")}: no Actions access`);
    return `<div class="acts"><div class="lb">Actions</div>${rows}${more}` +
      (notes.length ? `<div class="ac note">${notes.join(" · ")}</div>` : "") + "</div>";
  }

  const api = { esc, actionsHtml, shortAgo, md, inline, checklist, highlight, langOf, CHEAT, SAY, soundAllowed, quietHours, testCue,
    wsList, currentWs, stepWs, pillsHtml, projectsHtml, workerVisible, LOCK, wsFile, planFor, testsFor };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WalterCockpitLib = api;
})(typeof window !== "undefined" ? window : this);
