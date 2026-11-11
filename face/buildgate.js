/* Build-context gate (the owner, 9 Nov 13:15): the conversation-mode BUILD VIEW appears only when we are
 * actually building, not in every conversation.
 *
 * BUILD CONTEXT = (a worker is running, or one started / finished within the last 3 min)
 *                 AND (one of the last 3 caption / turn texts mentions a build word)
 *   or STICKY build mode (the owner, 9 Nov 13:25: "stay in build mode until I tell you to leave"):
 *   the owner said "build mode" / "let's build" / "show me the build" / "we're building", or display.json
 *   build_mode is "on". No timeout: only "exit / leave build mode", "done / stop building", "back to
 *   the board", Escape on the wall keyboard (buildview.js) or `walterctl display build off|auto` end it.
 *
 * MUTE EXITS (the owner, 9 Nov 13:33): the moment the mic is muted (Space, walterctl mute, the app, the USB
 * mic's button, auto-mute at sleep, or already muted when the face starts) sticky build mode ends at once
 * (mute()). It is a one-time exit: display.json is NOT changed and unmuting does not bring it back. With
 * build_mode "on" a runtime exitedByMute flag keeps it off until the owner's next explicit "build mode" phrase
 * or until display.json build_mode changes (walterctl display build ...). Persisted like voiceOff.
 *
 * Mode (face/display.json build_mode, walterctl display build on|off|auto): "auto" (default) = the
 * gate above; "on" = sticky build mode (the owner can still say "done building"); "off" = never.
 *
 * Voice commands from the owner's final captions (also settable via display.json build_focus / build_pane /
 * build_scroll): "focus worker 2", "zoom in on worker 2", "show the plan", "show the diff",
 * "what changed", "scroll down", "scroll up"; workspaces view: "next project", "previous project",
 * "show all projects" (= zoom projects), "back".
 *
 * Pure logic, no DOM: window.WalterBuildGate in the face, module.exports for node tests.
 */
(function (root) {
  "use strict";

  const WORDS = /\b(build(?:s|ing)?|worker(?:s)?|commit(?:s|ted)?|deploy(?:s|ed|ing)?|release(?:s|d)?|ship(?:s|ped|ping)?|test(?:s|ing)?|bug(?:s)?|fix(?:es|ed|ing)?|feature(?:s)?|repo(?:s)?|code|site|installer|branch(?:es)?|merge(?:s|d)?|diff(?:s)?|file(?:s)?)\b/i;
  // Matched on normalised text (lowercase, punctuation and apostrophes -> spaces, one space).
  // OFF is checked first, so "exit build mode" never reads as "build mode".
  // "dev mode" (T47, the owner says it in the app, 9 Nov) means build mode too.
  const ON = /\b(build mode|build view|dev mode|developer mode|development mode|let s build|lets build|let us build|show me the build|show the build|(?:we re|were|we are) building)\b/;
  const OFF = /\b((?:exit|leave|end|close|quit|stop)(?: the)? (?:build|dev|developer) mode|(?:done|finished) with(?: the)? (?:build|dev|developer) mode|(?:dev|developer) mode off|build mode off|(?:turn|switch) off(?: the)? (?:build|dev|developer) mode|out of (?:build|dev|developer) mode|(?:done|stop|finished) building|back to the board)\b/;
  const NUMS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, first: 1, second: 2, third: 3, fourth: 4, fifth: 5, to: 2, too: 2, for: 4 };
  const ZOOM = { diff: "diff", diffs: "diff", test: "tests", tests: "tests", "test runner": "tests", board: "board",
    "task board": "board", tasks: "board", stream: "stream", activity: "stream", "activity stream": "stream", feed: "stream",
    graph: "graph", git: "graph", "git graph": "graph", commits: "graph", plan: "plan", pipeline: "pipeline",
    project: "projects", projects: "projects", "all projects": "projects", workspaces: "projects",
    ship: "pipeline", "ship pipeline": "pipeline", "shipping pipeline": "pipeline", gauges: "gauges", system: "gauges" };
  const RECENT_S = 180;  // worker started / finished within this counts
  const CHEAT_S = 20;     // cheat sheet auto-close
  const KEEP = 3;        // last N caption / turn texts

  function norm(text) {
    return " " + String(text || "").toLowerCase().replace(/[\u2018\u2019'`]/g, " ").replace(/[^a-z0-9 ]+/g, " ")
      .replace(/\s+/g, " ").trim() + " ";
  }

  function command(text) {
    const t = norm(text);
    if (OFF.test(t)) return { cmd: "off" };
    let m = t.match(/\b(?:focus(?: on)?|zoom(?: in)?(?: on)?)\s+worker\s+(?:number\s+)?(\d+|[a-z]+)\b/);
    if (m) {
      const n = /^\d+$/.test(m[1]) ? parseInt(m[1], 10) : NUMS[m[1]];
      if (n) return { cmd: "focus", n };
    }
    // build mode v2 cockpit (owner approved 9 Nov 13:37): cheat sheet, zoom, back, build it
    if (/^ (?:ok |okay |walter )?(?:close|close it|close that|hide it|hide that|close (?:the )?(?:cheat ?sheet|commands)|hide (?:the )?(?:cheat ?sheet|commands))(?: please)? $/.test(t)) return { cmd: "close" };
    if (/\b(cheat ?sheet|what can i say|what do i say|show (?:me )?(?:the )?commands|voice commands|list (?:the )?commands)\b/.test(t)) return { cmd: "cheat" };
    // workspaces view (owner approved 11 Nov 09:37): "next project", "previous project", "show all projects"
    m = t.match(/^ (?:ok |okay |walter |hey walter )?(?:(?:go|switch|move|jump|flip) (?:to |on to |over to )?)?(?:the )?(next|previous|prev|last|prior) (?:project|workspace)(?: please)? $/);
    if (m) return { cmd: "ws", dir: m[1] === "next" ? 1 : -1 };
    if (/\bshow (?:me )?(?:all )?(?:of )?(?:the |my )?(?:projects|workspaces)\b|^ (?:ok |okay |walter )?(?:all (?:the |my )?(?:projects|workspaces)|(?:the )?projects? (?:view|overview))(?: please)? $/.test(t)) return { cmd: "zoom", panel: "projects" };
    m = t.match(/\bzoom (?:in )?(?:on |into |to )?(?:the )?(diffs?|tests?|test runner|board|task board|tasks|stream|activity(?: stream)?|feed|graph|git(?: graph)?|commits|plan|pipeline|ship(?:ping)? pipeline|gauges|system|all projects|projects?|workspaces)\b/);
    if (m) return { cmd: "zoom", panel: ZOOM[m[1]] || m[1] };
    if (/^ (?:ok |okay |walter )?(?:go |zoom |come )?back(?: out)?(?: please)? $|\bzoom (?:back )?out\b|\b(?:go )?back to (?:the )?(?:cockpit|overview|all panels)\b/.test(t)) return { cmd: "back" };
    if (/\bshow (?:me )?(?:the )?(tests|test results|test runner)\b/.test(t)) return { cmd: "zoom", panel: "tests" };
    if (/\bshow (?:me )?(?:the )?(task board|board|tasks)\b/.test(t)) return { cmd: "zoom", panel: "board" };
    if (/\bshow (?:me )?the plan\b/.test(t)) return { cmd: "pane", pane: "plan" };
    if (/\b(show (?:me )?the diff|what changed|what s changed|whats changed)\b/.test(t)) return { cmd: "pane", pane: "diff" };
    if (/\bscroll (?:it )?down\b/.test(t)) return { cmd: "scroll", dir: -1 }; // scroll = steps back from the newest change
    if (/\bscroll (?:it )?up\b/.test(t)) return { cmd: "scroll", dir: 1 };
    if (/\b(build it|go for it|go ahead and build|start building)\b|^ (?:ok |okay |walter |yes |yeah )?(?:go|go go|let s go|lets go|go ahead)(?: walter)?(?: please)? $/.test(t)) return { cmd: "go" };
    if (ON.test(t)) return { cmd: "on" };
    return null;
  }

  function create() {
    const g = {
      mode: "auto",       // display.json build_mode
      texts: [],          // [{t, text}] last KEEP final caption / turn texts
      sticky: false,      // the owner said "build mode" (auto mode): stays until he exits, no timeout
      exitedByMute: false, // mic muted while build_mode "on": off until an explicit phrase / mode change
      voiceOff: false,    // the owner said "done building" while display.json says "on": wins until the mode changes
      modeSeen: null,     // last display.json build_mode applied (null = not read yet)
      exited: null,        // when the owner said "exit build mode" (auto gate stays off until new build talk)
      lastWord: -1e12,      // last build word heard (s)
      lastWorker: -1e12,     // last time a worker was running / started / finished recently (s)
      workersLive: false,
      focus: 0, pane: "diff", scroll: 0,
      zoom: "",           // cockpit panel zoomed full-size ("" = all panels; "projects" = every workspace)
      wsStep: null,       // last "next / previous project": {dir, t}
      cheat: 0,           // when the cheat sheet opened (s), 0 = closed; auto-closes after CHEAT_S
      phase: "plan",      // with a plan set: "plan" (plan pane big) until the owner says "build it" / "go"
    };

    function setMode(m) {
      m = String(m || "auto").toLowerCase();
      m = m === "on" || m === "off" ? m : "auto";
      if (g.modeSeen !== null && m !== g.modeSeen) cliMode(m); // walterctl changed it: that wins
      g.modeSeen = m;
      g.mode = m;
    }

    // `walterctl display build on|off|auto` (also when re-run with the same value): a clean slate.
    function cliMode(m) {
      g.voiceOff = false;
      g.exitedByMute = false;
      g.sticky = false;
      if (m === "off" || m === "auto") g.exited = null;
    }

    // STICKY build mode (the owner, 9 Nov 13:25): explicitly on, by display.json "on" or by voice. Holds the
    // conversation + build view up regardless of turns, hold timer, silence or workers finishing.
    function sticky() {
      if (g.mode === "off" || g.voiceOff || g.exitedByMute) return false;
      return g.mode === "on" || g.sticky;
    }

    function exit(now) { g.sticky = false; g.exited = now; g.texts = []; if (g.mode === "on") g.voiceOff = true; }

    // The mic was muted: leave build mode now (one-time; display.json untouched, unmute does not re-enter).
    function mute(now) { g.sticky = false; g.exited = now; g.texts = []; if (g.mode === "on") g.exitedByMute = true; }

    // A final caption / turn text (either speaker). Returns the command it carried, if any.
    function text(now, s, fromOwner) {
      s = String(s || "").trim();
      if (!s) return null;
      const c = fromOwner ? command(s) : null;
      if (c && c.cmd === "off") { exit(now); return c; }
      g.texts.push({ t: now, text: s });
      if (g.texts.length > KEEP) g.texts.shift();
      if (WORDS.test(s)) g.lastWord = now;
      if (c) {
        if (c.cmd === "on") { g.sticky = true; g.voiceOff = false; g.exitedByMute = false; g.exited = null; g.lastWord = now; }
        else if (c.cmd === "focus") { g.focus = c.n; g.scroll = 0; }
        else if (c.cmd === "pane") { g.pane = c.pane; g.scroll = 0; if (g.zoom || c.pane === "plan") g.zoom = c.pane; }
        else if (c.cmd === "zoom") { g.zoom = c.panel; g.scroll = 0; g.cheat = 0; if (c.panel === "plan" || c.panel === "diff") g.pane = c.panel; }
        else if (c.cmd === "back") { if (g.cheat) g.cheat = 0; else g.zoom = ""; }
        else if (c.cmd === "cheat") g.cheat = now;
        else if (c.cmd === "close") g.cheat = 0;
        else if (c.cmd === "go") { g.phase = "build"; if (g.zoom === "plan") g.zoom = ""; }
        else if (c.cmd === "scroll") g.scroll = Math.max(0, g.scroll + c.dir);
        else if (c.cmd === "ws") g.wsStep = { dir: c.dir, t: now }; // buildview.js asks the cockpit to switch
        if (c.cmd !== "on") { g.lastWord = now; }
      }
      return c;
    }

    // dashboard-workers.json snapshot
    function workers(now, snap) {
      const W = (snap && snap.workers) || [];
      let live = false;
      for (const w of W) {
        if (w.status === "running" || w.status === "queued") live = true;
        const recent = (w.started && now - w.started < RECENT_S) || (w.finished && now - w.finished < RECENT_S);
        if (w.status === "running" || recent) g.lastWorker = Math.max(g.lastWorker, w.status === "running" ? now : (w.finished || w.started));
      }
      g.workersLive = live;
    }

    // the cheat sheet closes by itself after CHEAT_S seconds
    function cheatOpen(now) { if (g.cheat && now - g.cheat > CHEAT_S) g.cheat = 0; return !!g.cheat; }

    function wordsRecent() { return g.texts.some((x) => WORDS.test(x.text)); }

    function tick(now) {
      if (g.mode === "off") return false;
      if (sticky()) return true;
      if (g.mode === "on") return false; // "on" but the owner said "done building": off until the mode changes
      const workerCtx = g.workersLive || now - g.lastWorker < RECENT_S;
      const wordCtx = wordsRecent() && now - g.lastWord < RECENT_S;
      if (g.exited !== null && !(g.lastWord > g.exited)) return false;
      return !!(workerCtx && wordCtx);
    }

    // persisted across face reloads (localStorage in buildview.js)
    function save() { return { sticky: g.sticky, voiceOff: g.voiceOff, exitedByMute: g.exitedByMute }; }
    function restore(o) { if (o) { g.sticky = !!o.sticky; g.voiceOff = !!o.voiceOff; g.exitedByMute = !!o.exitedByMute; } }

    return { setMode, cliMode, sticky, exit, mute, text, workers, tick, save, restore, cheatOpen, g };
  }

  const api = { create, command, norm, WORDS, ZOOM, CHEAT_S };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WalterBuildGate = api;
})(typeof window !== "undefined" ? window : this);
