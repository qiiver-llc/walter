/* Dashboard mode: while I'm asleep the wall stays useful. I shrink into the
 * bottom-right corner (walter.js reads S.dash / S.dashDark) and this shows a clock,
 * the weather, today's and tomorrow's calendar, the person's list and my status.
 *
 * Data (all written with a fixed old mtime, so no face reload):
 *   dashboard-list.json     bin/walter-dashboard + /api/dashboard (the list, title, QR, hours)
 *   dashboard-weather.json  bin/walter-dashboard refresh (walter-dashboard.timer, 15 min)
 *   dashboard-events.json   bin/walter-dashboard set-events (Walter, from his calendars; 14 days:
 *                           today + tomorrow in the calendar card, all of it in the two-week view)
 *   dashboard-away.json     "While you were away": reminders, workers, routines I couldn't say
 *   dashboard-wrapup.json   the evening wrap-up (bin/walter-wrapup, 8:30 PM routine)
 * Modes: day (asleep in the daytime, e.g. a muted nap), night (quiet hours: dim and
 * minimal) and dark (dark_hours: only a faint clock).
 * Debug/screenshots: ?dash=1 forces it on, &at=21:30 pretends it's that time.
 */
(() => {
  "use strict";
  const params = new URLSearchParams(location.search);
  const css = document.createElement("style");
  css.textContent = `
  #dashdim { position: fixed; inset: 0; z-index: -1; pointer-events: none; background: #05060a;
    opacity: 0; transition: opacity 1.6s ease; }
  #dash { position: fixed; inset: 0; z-index: 1; pointer-events: none; color: #eef3ff;
    opacity: 0; visibility: hidden; transition: opacity 1.2s ease, visibility 0s linear 1.2s;
    font-variant-numeric: tabular-nums; }
  body.dash-on #dash { opacity: 1; visibility: visible; transition: opacity 1.6s ease 0.5s, visibility 0s; }
  body.dash-on #typebox, body.dash-on #card { z-index: 10; }
  body.dash-on #clock, body.dash-on #usage, body.dash-on #checklist, body.dash-on #workers,
  body.dash-on #sayname, body.dash-on #micoff, body.dash-on #camon, body.dash-on #cloudon,
  body.dash-on #listen, body.dash-on #caption { opacity: 0 !important; transition: opacity 0.8s; }
  body.dash-instant * { transition: none !important; }
  body.dash-dark #bg { visibility: hidden; }
  #c { transition: opacity 2s ease; }
  body.dash-dark #c { opacity: 0; }
  #dash .card { position: absolute; box-sizing: border-box; padding: 2.4vh 2.8vh; border-radius: 2.4vh;
    background: rgba(9,13,22,0.66); border: 1px solid rgba(255,255,255,0.07);
    overflow: hidden; }
  #dash h3 { margin: 0 0 1.2vh; font-size: 1.9vh; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; color: rgba(160,200,255,0.75); }
  #dash .muted { color: rgba(255,255,255,0.45); }
  /* clock */
  #dash .clk { position: absolute; left: 4.5vh; top: 2.5vh; }
  #dash .clk .t { font-size: 19vh; font-weight: 200; line-height: 1; letter-spacing: -0.02em;
    text-shadow: 0 0.4vh 3vh rgba(0,0,0,0.6); }
  #dash .clk .t small { font-size: 6vh; font-weight: 300; margin-left: 1.2vh; letter-spacing: 0; color: rgba(255,255,255,0.7); }
  #dash .clk .d { font-size: 3.8vh; font-weight: 400; color: rgba(255,255,255,0.75); margin-top: 0.6vh;
    text-shadow: 0 0.3vh 2vh rgba(0,0,0,0.6); }
  /* weather */
  #dash .wx { left: 4.5vh; top: 31vh; width: 36vw; }
  #dash .wx .now { display: flex; align-items: center; gap: 2.4vh; }
  #dash .wx .ic { width: 10vh; height: 10vh; flex: none; }
  #dash .wx .tmp { font-size: 10vh; font-weight: 300; line-height: 1; }
  #dash .wx .desc { font-size: 3.2vh; line-height: 1.2; }
  #dash .wx .desc .muted { font-size: 2.4vh; }
  #dash .wx .facts { display: flex; flex-wrap: wrap; gap: 1vh 3.2vh; margin-top: 2vh; font-size: 2.7vh; }
  #dash .wx .facts b { font-weight: 600; }
  #dash .wx .tmrw { margin-top: 1.6vh; font-size: 2.3vh; color: rgba(255,255,255,0.55); }
  #dash .alert { margin: 0 0 1.8vh; padding: 1.2vh 1.6vh; border-radius: 1.4vh; font-size: 2.5vh; font-weight: 600;
    color: #ffd27a; background: rgba(255,180,60,0.13); border: 1px solid rgba(255,190,80,0.35); }
  #dash .alert.sev { color: #ffb0a8; background: rgba(255,80,60,0.16); border-color: rgba(255,110,90,0.45); }
  #dash .alert span { font-weight: 400; color: rgba(255,255,255,0.65); margin-left: 1vh; font-size: 2.1vh; }
  /* calendar */
  #dash .cal { left: calc(36vw + 8vh); top: 4vh; width: 31vw; max-height: 92vh; }
  #dash .day + .day { margin-top: 2.6vh; }
  #dash .ev { display: flex; gap: 1.6vh; align-items: baseline; padding: 0.9vh 0; border-top: 1px solid rgba(255,255,255,0.06); }
  #dash .ev:first-of-type { border-top: 0; }
  #dash .ev .when { flex: none; width: 13vh; font-size: 2.4vh; color: rgba(255,255,255,0.65); text-align: right; }
  #dash .ev .what { flex: 1; min-width: 0; font-size: 2.9vh; line-height: 1.2; }
  #dash .ev .what .nm { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #dash .ev .what .lb { font-size: 1.8vh; color: rgba(255,255,255,0.42); }
  #dash .ev .what .lb i { display: inline-block; width: 1vh; height: 1vh; border-radius: 50%; margin-right: 0.7vh; vertical-align: 0.05vh; }
  #dash .ev.now .when { color: #8affc1; font-weight: 600; }
  #dash .ev.past { opacity: 0.4; }
  /* weather chip next to outdoor events (icon, temp, rain chance) */
  #dash .ev .wxe { display: inline-flex; align-items: center; gap: 0.4em; font-size: 2.1vh; line-height: 1.2; vertical-align: middle;
    margin-left: 0.8em; color: rgba(230,240,255,0.85); white-space: nowrap; padding: 0.1em 0.6em; border-radius: 1em;
    background: rgba(255,255,255,0.07); }
  #dash .ev .what .nm + .wxe { margin-left: 0; }
  #dash .ev .wxe .wxi { width: 1.35em; height: 1.35em; flex: none; }
  #dash .ev .wxe b { font-weight: 600; color: #8fd3ff; }
  html.big body.board-on #dash .ev .wxe { font-size: 2.2vw; }
  #dash .none { font-size: 2.5vh; color: rgba(255,255,255,0.4); padding: 0.6vh 0; }
  #dash .more { font-size: 2vh; color: rgba(255,255,255,0.4); padding-top: 0.6vh; }
  #dash .stale { font-size: 1.8vh; color: rgba(255,210,122,0.6); margin-top: 1.6vh; }
  /* the list */
  #dash .lst { right: 4.5vh; top: 4vh; width: calc(100vw - 36vw - 31vw - 17vh); max-height: 62vh; }
  #dash .sec + .sec { margin-top: 2vh; }
  #dash .lst.over { -webkit-mask-image: linear-gradient(to bottom, #000 82%, transparent 99%); }
  #dash .sec h4 { margin: 0 0 0.6vh; font-size: 1.7vh; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(255,255,255,0.4); font-weight: 600; }
  #dash .it { display: flex; gap: 1.3vh; align-items: baseline; font-size: 2.8vh; line-height: 1.25; padding: 0.4vh 0;
    transition: opacity 1.5s; }
  #dash .it .bx { flex: none; width: 1.9vh; height: 1.9vh; border-radius: 0.5vh; border: 0.25vh solid rgba(88,196,255,0.8);
    position: relative; top: 0.15vh; box-sizing: border-box; }
  #dash .it.note .bx { border: 0; width: 1vh; height: 1vh; border-radius: 50%; background: rgba(255,255,255,0.4); top: -0.3vh; margin: 0 0.45vh; }
  #dash .it .tx { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  #dash .it .due { display: block; font-size: 1.9vh; color: rgba(255,210,122,0.8); white-space: nowrap; }
  #dash .it .due.late { color: #ff9a8a; }
  #dash .it.done .tx { text-decoration: line-through; color: rgba(255,255,255,0.45); }
  #dash .it.done .bx { background: rgba(88,196,255,0.7); }
  #dash .cal.withaway { max-height: 52vh; }
  #dash .wrap { font-size: 2.1vh; color: rgba(138,255,193,0.8); margin: -0.4vh 0 1.4vh; }
  /* while you were away (under the calendar; I sit in the bottom-right corner) */
  #dash .away { left: calc(36vw + 8vh); bottom: 3.5vh; width: 31vw; max-height: 36vh; display: none;
    border-color: rgba(255,210,122,0.28); }
  #dash .away.on { display: block; }
  #dash .al, #dash .sys { display: none; } /* big-screen board only (below) */
  #dash .fc, #dash .tw { display: none; } /* the old 7-day strip (always hidden now), month calendar: portrait only */
  #dash .away h3 { color: rgba(255,210,122,0.85); }
  #dash .aw { display: flex; gap: 1.4vh; align-items: baseline; padding: 0.5vh 0; font-size: 2.4vh; line-height: 1.25; }
  #dash .aw .when { flex: none; width: 10vh; font-size: 1.9vh; color: rgba(255,255,255,0.5); text-align: right; }
  #dash .aw .tx { flex: 1; min-width: 0; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  #dash .aw .tx b { font-weight: 600; }
  #dash .aw.seen { opacity: 0.45; }
  /* bottom-left: QR + my status */
  #dash .foot { position: absolute; left: 4.5vh; bottom: 3.5vh; width: 36vw; display: flex; gap: 2vh; align-items: flex-end; }
  #dash .qr { flex: none; width: 11vh; height: 11vh; padding: 0.8vh; background: #fff; border-radius: 1.2vh; box-sizing: border-box; opacity: 0.85; }
  #dash .qr img { width: 100%; height: 100%; display: block; }
  #dash .foot .hint { font-size: 1.8vh; color: rgba(255,255,255,0.5); line-height: 1.35; }
  #dash .foot .hint b { color: rgba(255,255,255,0.75); font-weight: 600; }
  #dash .stat { display: flex; flex-wrap: wrap; gap: 0.6vh 2.2vh; margin-top: 1.2vh; font-size: 1.8vh; color: rgba(255,255,255,0.45); }
  #dash .stat .on { color: rgba(138,255,193,0.75); }
  #dash .stat .off { color: rgba(255,138,138,0.7); }
  #dash .stat .off.hw { color: rgba(255,210,122,0.75); }
  #dash .toast { position: absolute; left: 50%; bottom: 20vh; transform: translateX(-50%); padding: 1.2vh 2.4vh; border-radius: 3vh;
    font-size: 2.4vh; color: #8affc1; background: rgba(10,30,20,0.85); border: 1px solid rgba(120,255,180,0.3);
    opacity: 0; transition: opacity 0.4s; white-space: nowrap; }
  #dash .toast.on { opacity: 1; }
  #dash .toast.err { color: #ffb0a8; background: rgba(40,10,10,0.85); border-color: rgba(255,110,90,0.4); }
  /* night (quiet hours): dim and minimal */
  #dash .nightv { display: none; position: absolute; left: 7vh; top: 50%; transform: translateY(-55%); }
  body.dash-night #dash .dayv, body.dash-dark #dash .dayv { display: none; }
  body.dash-night #dash .nightv, body.dash-dark #dash .nightv { display: block; }
  #dash .nightv .t { font-size: 22vh; font-weight: 200; line-height: 1; color: rgba(220,228,255,0.5); }
  #dash .nightv .t small { font-size: 6vh; margin-left: 1.2vh; }
  #dash .nightv .d { font-size: 3.4vh; color: rgba(220,228,255,0.32); margin-top: 0.8vh; }
  #dash .nightv .ln { font-size: 3vh; color: rgba(220,228,255,0.36); margin-top: 2.2vh; max-width: 70vw;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #dash .nightv .ln b { font-weight: 500; color: rgba(220,228,255,0.5); }
  body.dash-dark #dash .nightv { left: 50%; transform: translate(-50%, -50%); text-align: center; }
  body.dash-dark #dash .nightv .t { font-size: 16vh; color: rgba(150,140,130,0.2); }
  body.dash-dark #dash .nightv .d, body.dash-dark #dash .nightv .ln { display: none; }
  /* verse of the day (owner, 6 Nov): a calm line under the clock, at most two lines, reference last */
  #dash .vs { display: none; max-width: 36vw; margin-top: 1.4vh; font-size: 2.5vh; line-height: 1.3; font-style: normal;
    font-weight: 400; color: rgba(225,232,255,0.62); text-shadow: 0 0.3vh 2vh rgba(0,0,0,0.6); overflow: hidden; }
  body.vs-on #dash .vs { display: block; }
  #dash .vs .vr { white-space: nowrap; color: rgba(160,200,255,0.72); font-size: 0.86em; letter-spacing: 0.02em; }
  #dash .nightv .vs { max-width: 70vw; margin-top: 2vh; font-size: 2.8vh; color: rgba(220,228,255,0.34); text-shadow: none; }
  #dash .nightv .vs .vr { color: rgba(200,212,255,0.32); }
  /* now playing (owner, 7 Nov): a calm podcast line under the clock/verse; hidden in the dark
     hours and while the canvas/browser is open, dimmed in quiet hours */
  #dash .pc { display: none; max-width: 36vw; margin-top: 1.6vh; font-size: 2.1vh; line-height: 1.3;
    color: rgba(225,232,255,0.66); text-shadow: 0 0.3vh 2vh rgba(0,0,0,0.6); }
  body.pc-on #dash .pc { display: block; }
  #dash .pc.q { opacity: 0.5; }
  #dash .pc .ln { display: flex; align-items: center; gap: 0.5em; white-space: nowrap; min-width: 0; }
  #dash .pc .tt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  #dash .pc .tt b { font-weight: 500; color: rgba(160,200,255,0.78); }
  #dash .pc svg { flex: none; width: 1em; height: 1em; fill: none; stroke: currentColor; stroke-width: 2;
    stroke-linecap: round; stroke-linejoin: round; opacity: 0.8; }
  #dash .pc .sp { flex: none; font-size: 0.82em; opacity: 0.75; }
  #dash .lv { flex: none; padding: 0.05em 0.5em; border-radius: 1em; font-size: 0.72em; letter-spacing: 0.08em; text-transform: uppercase;
    color: rgba(255,170,160,0.9); background: rgba(255,120,110,0.14); } /* radio: "live" tag (walter/radio.py, source: radio) */
  #dash .pc .pb { margin-top: 0.45em; font-size: 0.8em; color: rgba(225,232,255,0.5); font-variant-numeric: tabular-nums; }
  #dash .pc .bar { flex: 1; height: 0.28em; border-radius: 1em; background: rgba(255,255,255,0.12); overflow: hidden; }
  #dash .pc .bar i { display: block; height: 100%; border-radius: 1em; background: rgba(160,200,255,0.6); transition: width 1s linear; }
  #dash .pc.paused .bar i { background: rgba(200,205,220,0.38); }
  #dash .pc.paused .tt { opacity: 0.7; }
  #dash .pc .chip { display: inline-block; max-width: 100%; box-sizing: border-box; padding: 0.2em 0.8em; border-radius: 1em;
    background: rgba(255,255,255,0.06); font-size: 0.88em; color: rgba(225,232,255,0.48);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #dash .nightv .pc { max-width: 70vw; margin-top: 1.6vh; color: rgba(220,228,255,0.4); text-shadow: none; }
  /* up next (owner, 7 Nov): the next unplayed podcast episodes under the now-playing line, same calm style */
  #dash .un { display: none; max-width: 36vw; margin-top: 1.2vh; font-size: 1.9vh; line-height: 1.3;
    color: rgba(225,232,255,0.58); text-shadow: 0 0.3vh 2vh rgba(0,0,0,0.6); }
  body.un-on #dash .un { display: block; }
  #dash .un.q { opacity: 0.5; }
  #dash .un .hd { font-size: 0.78em; letter-spacing: 0.08em; text-transform: uppercase; color: rgba(160,200,255,0.6); margin-bottom: 0.2em; }
  #dash .un .ln { display: flex; align-items: baseline; gap: 0.5em; white-space: nowrap; min-width: 0; margin-top: 0.15em; }
  #dash .un .tt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  #dash .un .tt b { font-weight: 500; color: rgba(160,200,255,0.72); }
  #dash .un .mt { flex: none; font-size: 0.82em; opacity: 0.7; font-variant-numeric: tabular-nums; }
  #dash .un .dot { flex: none; width: 0.45em; height: 0.45em; border-radius: 50%; background: rgba(160,200,255,0.85); align-self: center; }
  #dash .un .dot.off { background: none; }
  #dash .un .rs { flex: none; font-size: 0.74em; padding: 0 0.5em; border-radius: 1em; background: rgba(255,255,255,0.08); color: rgba(225,232,255,0.6); }
  #dash .nightv .un { max-width: 70vw; margin-top: 1.4vh; color: rgba(220,228,255,0.36); text-shadow: none; }`;
  document.head.appendChild(css);
  const lcss = document.createElement("style");
  lcss.dataset.portrait = "verse-landscape";
  lcss.media = "(orientation: landscape)";
  lcss.textContent = "body.vs-on.dash-on #dash .wx { top: max(31vh, var(--vsbot, 0px)); }";
  document.head.appendChild(lcss);


  // ---- big-screen info board (portrait.js html.big; owner, 5 Nov: 32" portrait monitor).
  // While I'm awake on the big screen this same dashboard stays on as a calm board under
  // me (I sit small at the top): clock + weather beside me, alerts, calendar, the list,
  // the away feed and my status (workers, queue, usage, mic/voice). Asleep, the normal
  // dashboard takes over as before. Sizes are in vw (portrait units); data-portrait keeps
  // portrait.js from re-copying them.
  const bcss = document.createElement("style");
  bcss.dataset.portrait = "board";
  bcss.media = "(orientation: portrait)";
  bcss.textContent = `
  html.big body.board-on #dash { opacity: 1; visibility: visible; transition: opacity 1.2s ease, visibility 0s; }
  html.big body.board-on #clock, html.big body.board-on #usage, html.big body.board-on #workers,
  html.big body.board-on #checklist, html.big body.board-on #focusstrip { opacity: 0 !important; visibility: hidden; }
  html.big #caption, html.big #listen, html.big #micoff, html.big #sayname, html.big #restart, html.big #typebox { z-index: 7; }
  html.big #card { background: rgba(13,17,27,0.98); }
  html.big #focusring { z-index: 2; top: 61vh; width: 78vw; height: 78vw; }
  html.big #focusring .in { width: 56vw; height: 48vw; }
  /* captions: just under me, on a soft backdrop so they read over the board */
  html.big #caption { top: 22.2vh; bottom: auto; max-width: 84vw; font-size: 3vw; padding: 1vw 2.4vw;
    border-radius: 2.4vw; background: rgba(5,6,10,0.78); color: rgba(255,255,255,0.75); }
  html.big body.canvas-on #caption, html.big body.browser-on #caption { left: 50%; bottom: auto; }
  html.big #caption.karaoke .w.said { color: rgba(255,255,255,0.75); }
  html.big #caption.karaoke .w.now { color: #fff; }
  html.big #camon, html.big #cloudon { top: 1.4vw; }
  html.big body.board-on #dash .dayv { position: absolute; inset: 0; display: grid; box-sizing: border-box;
    padding: 2.6vw 4vw 0.8vw; column-gap: 2.6vw; row-gap: 2.2vw;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto;
    grid-template-areas: "clk wx" "al al" "cal lst" "away lst" "sys sys"; }
  html.big body.board-on #dash .dayv > * { position: relative; left: auto; right: auto; top: auto; bottom: auto;
    width: auto; max-height: none; min-height: 0; margin: 0; -webkit-mask-image: none; }
  html.big body.board-on #dash .card { padding: 2.2vw 2.6vw; border-radius: 2.2vw; background: rgba(9,13,22,0.72); }
  html.big body.board-on #dash h3 { font-size: 2vw; margin-bottom: 1.1vw; }
  /* beside me at the top: plain text, no cards */
  html.big body.board-on #dash .clk { grid-area: clk; justify-self: start; width: max-content; max-width: 32vw; padding-top: 1.4vw; }
  html.big body.board-on #dash .clk .t { font-size: 10vw; }
  html.big body.board-on #dash .clk .t small { font-size: 3.6vw; margin-left: 1vw; }
  html.big body.board-on #dash .clk .d { font-size: 3vw; line-height: 1.25; margin-top: 1vw; }
  html.big body.board-on #dash .wx { grid-area: wx; justify-self: end; width: max-content; max-width: 32vw; background: none; border: 0;
    box-shadow: none; padding: 1.4vw 0 0; text-align: right; overflow: visible; }
  html.big body.board-on #dash .wx .now { justify-content: flex-end; gap: 1.6vw; }
  html.big body.board-on #dash .wx .ic { width: 8vw; height: 8vw; }
  html.big body.board-on #dash .wx .tmp { font-size: 9vw; font-weight: 200; }
  html.big body.board-on #dash .wx .desc { font-size: 3vw; margin-top: 0.6vw; }
  html.big body.board-on #dash .wx .desc .muted { font-size: 2.4vw; }
  html.big body.board-on #dash .wx .facts { flex-direction: column; align-items: flex-end; font-size: 2.4vw; gap: 0.6vw 2vw; margin-top: 1vw;
    color: rgba(255,255,255,0.75); }
  html.big body.board-on #dash .wx .tmrw { font-size: 2.3vw; margin-top: 1vw; }
  html.big body.board-on #dash .wx .stale { font-size: 1.9vw; }
  /* weather alerts: a full-width strip */
  html.big body.board-on #dash .al { grid-area: al; display: none; padding: 0; background: none; border: 0; box-shadow: none; }
  html.big body.board-on #dash .al.on { display: block; }
  html.big body.board-on #dash .al .alert { margin: 0 0 1vw; padding: 1.4vw 2vw; border-radius: 1.6vw; font-size: 3vw; }
  html.big body.board-on #dash .al .alert span { font-size: 2.4vw; }
  /* calendar + away (left), the list (right) */
  html.big body.board-on #dash .cal { grid-area: cal; overflow: hidden;
    -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 2.4vw), transparent); }
  html.big body.board-on #dash .day + .day { margin-top: 2.4vw; }
  html.big body.board-on #dash .ev { gap: 1.6vw; padding: 1vw 0; }
  html.big body.board-on #dash .ev .when { width: 14vw; font-size: 2.6vw; }
  html.big body.board-on #dash .ev .what { font-size: 3vw; }
  html.big body.board-on #dash .ev .what .nm { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  html.big body.board-on #dash .ev .what .lb { font-size: 2vw; }
  html.big body.board-on #dash .ev .what .lb i { width: 1.2vw; height: 1.2vw; }
  html.big body.board-on #dash .none { font-size: 2.6vw; }
  html.big body.board-on #dash .more, html.big body.board-on #dash .stale { font-size: 2vw; }
  html.big body.board-on #dash .lst { grid-area: lst; overflow: hidden; }
  html.big body.board-on #dash .lst.over { -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 4vw), transparent); }
  html.big body.board-on #dash .sec + .sec { margin-top: 2.2vw; }
  html.big body.board-on #dash .sec h4 { font-size: 1.9vw; margin-bottom: 0.6vw; }
  html.big body.board-on #dash .it { font-size: 3vw; gap: 1.4vw; padding: 0.5vw 0; }
  html.big body.board-on #dash .it .bx { width: 2.2vw; height: 2.2vw; border-width: 0.3vw; border-radius: 0.6vw; }
  html.big body.board-on #dash .it.note .bx { width: 1.1vw; height: 1.1vw; border-width: 0; margin: 0 0.55vw; }
  html.big body.board-on #dash .it .due { font-size: 2.1vw; }
  html.big body.board-on #dash .wrap { font-size: 2.3vw; margin: 0 0 1.4vw; }
  html.big body.board-on #dash .away { grid-area: away; }
  html.big body.board-on #dash .aw { gap: 1.4vw; font-size: 2.5vw; padding: 0.6vw 0; }
  html.big body.board-on #dash .aw .when { width: 11vw; font-size: 2vw; }
  /* my status: the "me" strip (icons, see below) */
  html.big body.board-on #dash .sys { grid-area: sys; }
  html.big body.board-on #dash .foot, html.big body.board-on #dash .nightv { display: none; }
  html.big body.board-on #dash .toast { bottom: 6vw; font-size: 2.6vw; }
  /* canvas / browser: slide up over the board (a little higher than compact: I'm small) */
  html.big #canvas, html.big #browser { top: 29vh; }
  html.big body.canvas-on #dash .dayv > :not(.clk):not(.wx),
  html.big body.browser-on #dash .dayv > :not(.clk):not(.wx) { visibility: hidden; }
  /* a focus box: the ring sits over the board, which steps back */
  html.big body.board-on:has(#focusring.on) #dash .dayv > :not(.clk):not(.wx) { opacity: 0.16; transition: opacity 1.2s; }
  /* quiet hours while I'm awake: dimmer and greyer */
  html.big body.board-on.board-night #dash { opacity: 0.6; filter: saturate(0.4); }
  html.big body.board-on.board-dark #dash { opacity: 0.42; filter: saturate(0.2); }`;
  document.head.appendChild(bcss);

  // ---- "me" strip (owner, 6 Nov: the status about me was "too busy with text"). Portrait only
  // (landscape keeps its text status line): small icons and badges in one calm row, on the
  // awake board (.sys) and the sleep dashboard (foot + faint at night). State dot (idle /
  // listening / thinking / speaking / asleep), mic (struck through when muted), workers as a
  // count (+ queued), Claude usage as a tiny ring with its percent, camera and cloud only
  // while on. Words only for things that need attention: Restarting, Offline, usage nearly
  // gone. Icons are sized in em so they follow the text scale (textscale.js).
  const mcss = document.createElement("style");
  mcss.dataset.portrait = "me";
  mcss.textContent = `
  #dash .mei { display: none; }
  @media (orientation: portrait) {
  #dash .foot .stat { display: none; }
  #dash .mei { display: flex; align-items: center; flex-wrap: wrap; gap: 0.5em 1.15em; font-size: 2.7vw; line-height: 1;
    color: rgba(255,255,255,0.6); }
  #dash .foot .mei { margin-top: 1.4vw; }
  #dash .nightv .mei { margin-top: 3vw; font-size: 2.6vw; opacity: 0.55; }
  body.dash-dark #dash .nightv .mei { opacity: 0.4; }
  body.dash-dark #dash .nightv .mei > :not(.att) { display: none; }
  #dash .mei .b { display: inline-flex; align-items: center; gap: 0.32em; white-space: nowrap; }
  #dash .mei svg { width: 1.45em; height: 1.45em; flex: none; display: block; }
  #dash .mei .n { font-weight: 600; font-size: 1.05em; color: rgba(255,255,255,0.85); }
  /* state dot: still when idle, a soft ring when listening, a turning arc when thinking, a pulse when speaking */
  #dash .mei .orb { position: relative; width: 1.45em; height: 1.45em; flex: none; }
  #dash .mei .orb i { position: absolute; inset: 0.42em; border-radius: 50%; background: rgba(160,200,255,0.55); }
  #dash .mei .orb::after { content: ""; position: absolute; inset: 0.1em; border-radius: 50%; border: 0.12em solid transparent; }
  #dash .mei .orb.listening i { background: #8affc1; }
  #dash .mei .orb.listening::after { border-color: rgba(138,255,193,0.7); animation: meping 1.6s ease-out infinite; }
  #dash .mei .orb.thinking i { background: rgba(255,255,255,0.75); inset: 0.5em; }
  #dash .mei .orb.thinking::after { border-color: rgba(255,255,255,0.65) transparent transparent transparent; animation: mespin 1.3s linear infinite; }
  #dash .mei .orb.speaking i { background: #58c4ff; animation: mebeat 0.9s ease-in-out infinite; }
  #dash .mei .orb.speaking::after { border-color: rgba(88,196,255,0.45); }
  #dash .mei .orb.away i { background: rgba(255,255,255,0.25); }
  @keyframes meping { 0% { transform: scale(0.75); opacity: 1; } 100% { transform: scale(1.25); opacity: 0; } }
  @keyframes mespin { to { transform: rotate(360deg); } }
  @keyframes mebeat { 50% { transform: scale(1.3); } }
  #dash .mei .moon { color: rgba(200,210,255,0.7); }
  #dash .mei .mic { color: rgba(255,255,255,0.5); }
  #dash .mei .mic.off { color: #ff8a8a; }
  #dash .mei .mic.off.hw { color: #ffd27a; }  /* muted by the mic's own button */
  #dash .mei .wk { color: #58c4ff; }
  #dash .mei .wk .q { font-size: 0.85em; font-weight: 600; color: rgba(255,210,122,0.85); }
  #dash .mei .use .n { font-size: 0.9em; font-weight: 500; color: rgba(255,255,255,0.6); }
  #dash .mei .use.warn .n { color: rgba(255,210,122,0.9); }
  #dash .mei .use.hot .n { color: #ff9a8a; }
  #dash .mei .cam { color: rgba(138,255,193,0.75); }
  #dash .mei .cloud { color: rgba(170,200,255,0.75); }
  /* things that need attention: the only words */
  #dash .mei .att { padding: 0.32em 0.75em; border-radius: 1em; font-size: 0.95em; font-weight: 600; gap: 0.4em; }
  #dash .mei .att svg { width: 1.15em; height: 1.15em; }
  #dash .mei .att.amber { color: #ffd27a; background: rgba(255,190,80,0.14); animation: meblink 1.6s ease-in-out infinite; }
  #dash .mei .att.red { color: #ffb0a8; background: rgba(255,110,90,0.16); }
  @keyframes meblink { 50% { opacity: 0.5; } }
  /* awake big-screen board: one slim row instead of the old two-column card */
  html.big body.board-on #dash .sys { display: flex; align-items: center; justify-content: center; padding: 1.5vw 2.6vw; }
  html.big body.board-on #dash .sys .mei { font-size: 3vw; gap: 0.6em 1.6em; justify-content: center; }
  html.big body.board-on:has(#focusring.on) #dash .dayv > .sys { opacity: 0.5; }
  /* the strip shows these, so the separate pills step aside (back while a panel hides the board) */
  html.big body.board-on:not(.canvas-on):not(.browser-on) #micoff,
  html.big body.board-on:not(.canvas-on):not(.browser-on) #camon,
  html.big body.board-on:not(.canvas-on):not(.browser-on) #cloudon,
  html.big body.board-on:not(.canvas-on):not(.browser-on) #restart { opacity: 0 !important; visibility: hidden; }
  }`;
  document.head.appendChild(mcss);

  // ---- 7-day forecast strip (owner, 6 Nov): portrait only, on the awake info board (html.big)
  // and the sleep dashboard (day + quiet hours; hidden in the dark hours and while the canvas
  // or browser is open). The owner (6 Nov): it sits ABOVE the QR code and the "me" row, so from the
  // top: me/captions, calendar + list, the strip, the QR (when shown), the me row at the very
  // bottom. renderFc() moves the element into the right container (board grid row "fc",
  // the day dashboard's column just before .foot, the night view just before its .mei) and
  // sets body.fc-on. Landscape never shows it (#dash .fc is display:none there).
  const fcss = document.createElement("style");
  fcss.dataset.portrait = "forecast";
  fcss.media = "(orientation: portrait)";
  fcss.textContent = `
  :root { --fch: 20vw; } /* 80% of the first strip (owner, 6 Nov: "a little smaller") */
  body.fc-on #dash .fc { display: flex; position: relative; left: auto; right: auto; top: auto; bottom: auto;
    flex: none; width: auto; height: auto; min-height: calc(var(--fch) * var(--ts, 1)); max-height: none; margin: 0;
    padding: 1.3vw 0.8vw; border-radius: 2vw; background: rgba(9,13,22,0.72); -webkit-mask-image: none;
    transition: opacity 1.2s; }
  #dash .fc .d { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; align-items: center;
    justify-content: space-between; gap: 0.4vw; text-align: center; }
  #dash .fc .d + .d { border-left: 1px solid rgba(255,255,255,0.07); }
  #dash .fc .dn { font-size: 2.3vw; font-weight: 600; letter-spacing: 0.06em; color: rgba(160,200,255,0.8); }
  #dash .fc .d.today .dn { color: #fff; }
  #dash .fc .ic { width: 5.4vw; height: 5.4vw; }
  #dash .fc .hl { display: flex; flex-direction: column; align-items: center; line-height: 1.05; white-space: nowrap; }
  #dash .fc .hl b { font-size: 3.05vw; font-weight: 600; color: #fff; }
  #dash .fc .hl span { font-size: 2.4vw; color: rgba(255,255,255,0.55); }
  #dash .fc .rn { font-size: 2vw; line-height: 1; color: #8fd3ff; font-weight: 600; min-height: 2vw; }
  #dash .fc .rn.no { visibility: hidden; }
  #dash .fc .rn .drop { width: 0.85em; height: 0.85em; margin-right: 0.2em; vertical-align: -0.06em; }
  /* awake board: its own grid row between the calendar/list and the me row */
  html.big body.board-on.fc-on #dash .dayv { grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto auto;
    grid-template-areas: "clk wx" "al al" "cal lst" "away lst" "fc fc" "sys sys"; }
  html.big body.board-on #dash .fc { grid-area: fc; }
  /* asleep, day: in the column just above the footer (QR + hint + me row); I sit beside the footer */
  body.fc-on #dash .dayv > .fc { margin-top: auto; }
  body.fc-on #dash .dayv > .fc + .foot { margin-top: 0; }
  /* asleep, quiet hours: under the night clock, above the me row */
  body.fc-on #dash .nightv > .fc { margin-top: 3vw; }
  body.fc-on.dash-night #dash .fc { opacity: 0.5; background: rgba(9,13,22,0.4); filter: saturate(0.5); }
  /* a focus box: steps back with the board; the ring sits between me and the strip (--fctop: renderFc) */
  html.big body.board-on:has(#focusring.on) #dash .fc { opacity: 0.16; }
  html.big body.board-on.fc-on #focusring { top: max(50vh, min(61vh, calc(var(--fctop, 100vh) - 41vw))); }`;
  document.head.appendChild(fcss);

  // ---- month calendar (owner, 6 Nov, "like Google would"): portrait only, under the
  // today/tomorrow calendar (+ list). The month containing today as Sun-Sat rows (4-6),
  // month name + year on top, neighbouring-month days dimmed, past days dimmer, today
  // highlighted. The CURRENT WEEK row has larger text; every row grows to fit its busiest
  // day, so ALL items show, titles wrap (never clipped). A day shows the followed games first (team
  // colour bar; "NEB L 10-17" on past days, "7 PM NEB at ILL" ahead, a LIVE chip on today;
  // dashboard-sports.json "games"), then calendar chips (12-hour start + title). Days with a
  // forecast (today + up to 15 more, Open-Meteo daily) carry a small icon, high/low and rain %
  // (20%+) top-right, beside the date: this replaced the separate 7-day strip. Hidden in the dark hours
  // and while the canvas or browser is open; dimmed in quiet hours.
  const tcss = document.createElement("style");
  tcss.dataset.portrait = "month";
  tcss.media = "(orientation: portrait)";
  tcss.textContent = `
  /* (owner, 7 Nov) a hairline grid instead of boxed cells: one 1px line between rows and columns,
     tight padding, so each day gets the room; font sizes unchanged */
  body.tw-on #dash .tw { margin-top: -1.4vw !important; background: none !important; border: 0 !important; }
  #dash .tw .wk:first-of-type .cb { border-top-color: transparent !important; }
  #dash .tw .wk:last-of-type .cb { border-bottom: 0 !important; }
  body.tw-on #dash .tw { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0;
    position: relative; left: auto; right: auto; top: auto; bottom: auto; flex: none; width: auto; height: auto;
    max-height: none; margin: 0; padding: 0.5vw 0.4vw 0.4vw; border-radius: 1.2vw; background: rgba(9,13,22,0.72);
    -webkit-mask-image: none; transition: opacity 1.2s; }
  #dash .tw .mh { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 1em; font-size: 3.6vw; font-weight: 600;
    color: #fff; padding: 0 0.25em 0.1em; letter-spacing: 0.02em; }
  /* calendar legend, on the title line (right) */
  #dash .tw .lg { margin-left: auto; display: flex; gap: 1.1em; font-size: 0.45em; font-weight: 500; letter-spacing: 0.03em;
    color: rgba(255,255,255,0.6); white-space: nowrap; }
  #dash .tw .lg i { display: inline-block; width: 0.3em; height: 0.95em; margin-right: 0.35em; border-radius: 0.15em;
    vertical-align: -0.12em; background: var(--cc); }
  #dash .tw .wn { font-size: 2.1vw; font-weight: 600; letter-spacing: 0.08em; color: rgba(160,200,255,0.7); text-align: center;
    text-transform: uppercase; padding-bottom: 0.15em; }
  /* a week: its own 7-column grid; row 1 = day headers, then one lane per multi-day bar, then the day's items */
  #dash .tw .wk { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr));
    font-size: calc(1.15vw * var(--twk, 1)); line-height: 1.22; min-height: 4em; }
  #dash .tw .wk.cw { min-height: 6em; }
  #dash .tw.w6 .wk:not(.cw) { font-size: calc(1.1vw * var(--twk, 1)); min-height: 4.2em; } /* a six-week month */
  #dash .tw .cb { min-width: 0; border-top: 1px solid rgba(255,255,255,0.09); }
  #dash .tw .cb.l { border-left: 1px solid rgba(255,255,255,0.09); }
  #dash .tw .cb.we { background: rgba(160,190,255,0.035); }
  #dash .tw .cb.today { background: rgba(88,196,255,0.12); box-shadow: inset 0 0 0 0.2vw rgba(88,196,255,0.65); border-radius: 0.3vw; }
  #dash .tw .cx { min-width: 0; padding: 0 0.3em 0.3em; }
  #dash .tw .gone { opacity: 0.45; }  /* days gone by */
  #dash .tw .cx.om:not(.gone) { opacity: 0.7; }
  /* day number top-left, the day's weather top-right on the same line (owner, 6 Nov) */
  #dash .tw .dh { display: flex; align-items: center; gap: 0.3em; white-space: nowrap; min-width: 0; padding: 0.2em 0.3em 0; }
  #dash .tw .dd { font-size: 1.1em; font-weight: 600; color: rgba(255,255,255,0.85); }
  #dash .tw .dh.today .dd { color: #06121f; background: #58c4ff; border-radius: 1em; padding: 0 0.35em; }
  #dash .tw .dh.om .dd { color: rgba(255,255,255,0.45); font-weight: 500; }
  /* the day's forecast: icon, high/low, rain % (20%+) */
  #dash .tw .w { display: flex; align-items: center; gap: 0.15em; white-space: nowrap; overflow: hidden; min-width: 0;
    margin-left: auto; font-size: 0.72em; line-height: 1.1; color: rgba(255,255,255,0.6); }
  #dash .tw .w .ic { width: 1.5em; height: 1.5em; flex: none; }
  #dash .tw .w b { font-weight: 600; color: #fff; }
  #dash .tw .w .rn { color: #8fd3ff; font-weight: 600; }
  #dash .tw .w .rn .drop { width: 0.8em; height: 0.8em; vertical-align: -0.05em; }
  #dash .tw .wk.cw .w { font-size: 0.8em; }
  #dash .tw .wk.cw .w .ic { width: 1.7em; height: 1.7em; }
  /* an event: thin calendar-colour bar on the left, bold small time, title; wraps (never clipped) */
  #dash .tw .ch { position: relative; margin-top: 0.2em; padding-left: 0.42em; color: rgba(255,255,255,0.9);
    display: block; white-space: normal; overflow-wrap: anywhere; hyphens: auto; }
  #dash .tw .ch i { position: absolute; left: 0; top: 0.1em; bottom: 0.1em; width: max(2px, 0.2em); border-radius: 0.1em; background: var(--cc, #58c4ff); }
  #dash .tw .ch b { font-size: 0.8em; font-weight: 700; color: #fff; white-space: nowrap; }
  /* all-day: quieter, above the timed ones */
  #dash .tw .ch.ad { color: rgba(255,255,255,0.66); }
  #dash .tw .ch.ad i { opacity: 0.6; }
  #dash .tw .ch.past { opacity: 0.45; }
  /* a multi-day event: one continuous bar across its days in the week (split at week ends) */
  #dash .tw .mb { align-self: start; min-width: 0; margin: 0.15em 0.15em 0; padding: 0 0.35em 0 0.5em; color: rgba(255,255,255,0.85);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: color-mix(in srgb, var(--cc, #58c4ff) 20%, transparent); }
  #dash .tw .mb.s { border-radius: 0.3em 0 0 0.3em; margin-left: 0.3em; }
  #dash .tw .mb.e { border-top-right-radius: 0.3em; border-bottom-right-radius: 0.3em; margin-right: 0.3em; }
  #dash .tw .mb:not(.s) i { opacity: 0.35; }
  /* a followed game: compact team-colour chip ("NEB L 10-17", "AUR FB 7 PM"), not an event row */
  #dash .tw .gm { display: inline-block; max-width: 100%; box-sizing: border-box; margin: 0.2em 0.2em 0 0; padding: 0 0.3em;
    border-radius: 0.3em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; color: #fff;
    background: color-mix(in srgb, var(--cc, #58c4ff) 34%, rgba(255,255,255,0.06)); }
  #dash .tw .gm i { display: none; }
  #dash .tw .gm small { font-size: 0.75em; font-weight: 500; color: rgba(255,255,255,0.7); }
  #dash .tw .gm b { font-size: 0.85em; font-weight: 600; }
  #dash .tw .gm .op { font-weight: 400; color: rgba(255,255,255,0.7); }
  #dash .tw .gm .W { color: #8affc1; } #dash .tw .gm .L { color: #ff9a8a; } #dash .tw .gm .T { color: #ffd27a; }
  #dash .tw .gm.past { opacity: 0.5; }
  #dash .tw .gm.lv .ld { display: inline-block; width: 0.55em; height: 0.55em; margin-right: 0.3em; border-radius: 50%;
    background: #ff5a4f; vertical-align: 0.05em; animation: twlive 2.4s ease-out infinite; }
  /* count tiles (calendar_style "counts", non-current weeks): one solid calendar-colour tile per
     calendar, big count + small label, full cell width; rgba text so every theme re-inks it */
  #dash .tw .ct { display: flex; flex-direction: column; gap: 0.2em; margin-top: 0.2em; }
  #dash .tw .ct .t { display: flex; align-items: baseline; gap: 0.35em; min-width: 0; box-sizing: border-box; width: 100%;
    padding: 0.05em 0.35em; border-radius: 0.3em; line-height: 1.05; white-space: nowrap; overflow: hidden;
    color: rgba(255,255,255,0.95); background: color-mix(in srgb, var(--cc, #58c4ff) 55%, transparent); }
  #dash .tw .ct .t b { font-size: 1.7em; font-weight: 700; }
  #dash .tw .ct .t small { font-size: 0.8em; font-weight: 600; overflow: hidden; text-overflow: ellipsis; color: rgba(255,255,255,0.85); }
  #dash .tw .ct .t.past { opacity: 0.6; }
  #dash .tw .ct .t.more { background: rgba(255,255,255,0.1); color: rgba(255,255,255,0.75); }
  #dash .tw .ct .t.more b { font-size: 1.2em; }
  html.theme-light #dash .tw .ct .t:not(.more) { background: color-mix(in srgb, var(--cc, #58c4ff) 42%, transparent); }
  @keyframes twlive { 0% { box-shadow: 0 0 0 0 rgba(255,90,79,0.45); } 70%, 100% { box-shadow: 0 0 0 0.45em rgba(255,90,79,0); } }
  #dash .tw .st { grid-column: 1 / -1; font-size: 2vw; color: rgba(255,210,122,0.6); padding: 0 0.4vw; }
  /* awake board: its own grid row under the calendar/list */
  html.big body.board-on #dash .tw { grid-area: tw; }
  /* (owner, 6 Nov: calendar much bigger) the rest of the board compacts around it */
  html.big body.board-on.tw-on #dash .cal { display: none; }  /* month calendar already shows today and tomorrow */
  html.big body.board-on.tw-on #dash .dayv { row-gap: 1vw; column-gap: 2vw; padding-left: 2.4vw; padding-right: 2.4vw; padding-bottom: 1.4vw; }
  /* (owner, 7 Nov) month calendar is not a card: full screen width, no rounded box */
  html.big body.board-on.tw-on #dash .tw { margin-left: -2.4vw; margin-right: -2.4vw; border-radius: 0; background: transparent; padding-left: 0.3vw; padding-right: 0.3vw; }
  html.big body.board-on.tw-on #dash .card { padding: 1.2vw 1.8vw; }
  html.big body.board-on.tw-on #dash h3 { font-size: 1.8vw; margin-bottom: 0.4vw; }
  html.big body.board-on.tw-on #dash .clk .t { font-size: 8.6vw; }
  html.big body.board-on.tw-on #dash .clk .d { font-size: 2.6vw; margin-top: 0.6vw; }
  html.big body.board-on.tw-on #dash .wx .tmp { font-size: 7.6vw; }
  html.big body.board-on.tw-on #dash .wx .ic { width: 6.6vw; height: 6.6vw; }
  html.big body.board-on.tw-on #dash .wx .facts { font-size: 2.1vw; gap: 0.3vw 2vw; margin-top: 0.6vw; }
  html.big body.board-on.tw-on #dash .ev { padding: 0.5vw 0; }
  html.big body.board-on.tw-on #dash .ev .what { font-size: 2.6vw; }
  html.big body.board-on.tw-on #dash .ev .when { font-size: 2.3vw; }
  html.big body.board-on.tw-on #dash .day + .day { margin-top: 1.4vw; }
  html.big body.board-on.tw-on #dash .it { font-size: 2.6vw; padding: 0.25vw 0; }
  html.big body.board-on.tw-on #dash .sec + .sec { margin-top: 1.2vw; }
  html.big body.board-on.tw-on #dash .aw { font-size: 2.2vw; padding: 0.3vw 0; }
  html.big body.board-on.tw-on #dash .away { max-height: 14vh; overflow: hidden; }
  html.big body.board-on.tw-on.tw-tight #dash .away { max-height: 7vh; }
  body.tw-on.tw-tight.dash-on #dash .dayv > .cal { min-height: 0; max-height: 14vh; }
  body.tw-on.tw-tight.dash-on #dash .dayv > .lst { min-height: 0; }
  body.tw-on.tw-tight.dash-on #dash .dayv > .away { min-height: 0; max-height: 6vh; }
  html.big body.board-on.tw-on #dash .dayv { grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto auto;
    grid-template-areas: "clk wx" "al al" "cal lst" "away lst" "sys sys" "tw tw"; }
  html.big body.board-on.tw-on.fc-on #dash .dayv { grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto auto auto;
    grid-template-areas: "clk wx" "al al" "cal lst" "away lst" "fc fc" "sys sys" "tw tw"; }
  html.big body.board-on:has(#focusring.on) #dash .tw { opacity: 0.16; }
  /* asleep, day: in the column above the strip / footer; the cards above give way */
  body.tw-on #dash .dayv > .tw { margin-top: auto; }
  body.tw-on #dash .dayv > .tw + .fc, body.tw-on #dash .dayv > .tw + .foot { margin-top: 0; }
  body.tw-on.dash-on #dash .dayv > .cal { min-height: 8vh; max-height: 20vh; }
  body.tw-on.dash-on #dash .dayv > .lst { min-height: 7vh; }
  body.tw-on.dash-on #dash .dayv > .away { min-height: 5vh; max-height: 9vh; }
  body.tw-on.dash-on #dash .dayv > .wx .tmrw { display: none; } /* the month calendar shows tomorrow */
  /* asleep, quiet hours: under the night clock, dimmed */
  body.tw-on #dash .nightv > .tw { margin-top: 3vw; }
  body.tw-on #dash .nightv > .tw + .fc { margin-top: 1.4vw; }
  body.tw-on.dash-night #dash .tw { opacity: 0.5; background: rgba(9,13,22,0.4); filter: saturate(0.5); }`;
  document.head.appendChild(tcss);

  // ---- verse of the day, portrait sizes (the sleep dashboard column, the night view and the
  // awake board, where it sits in the clock's column left of me, under the date)
  const vcss = document.createElement("style");
  vcss.dataset.portrait = "verse";
  vcss.media = "(orientation: portrait)";
  vcss.textContent = `
  #dash .vs { max-width: 86vw; margin-top: 1.4vw; font-size: 3vw; }
  #dash .nightv .vs { max-width: 86vw; margin-top: 2.6vw; font-size: 3.2vw; }
  html.big body.board-on #dash .clk .vs { max-width: 32vw; margin-top: 1.4vw; font-size: 2.5vw; line-height: 1.28; }
  #dash .pc { max-width: 86vw; margin-top: 1.6vw; font-size: 2.6vw; }
  #dash .nightv .pc { max-width: 86vw; margin-top: 2.2vw; font-size: 2.8vw; }
  html.big body.board-on #dash .clk .pc { width: 32vw; max-width: 32vw; margin-top: 1.2vw; font-size: 2.1vw; }
  #dash .un { max-width: 86vw; margin-top: 1.4vw; font-size: 2.3vw; }
  #dash .nightv .un { max-width: 86vw; margin-top: 2vw; font-size: 2.5vw; }
  html.big body.board-on #dash .clk .un { width: 32vw; max-width: 32vw; margin-top: 1vw; font-size: 1.8vw; }`;
  document.head.appendChild(vcss);

  // ---- podcast card (owner, 7 Nov 17:30): ONE glassy card beside the list on the portrait board
  // (list 55%, card 45%, same row height): big cover art for the playing / next episode, then the
  // next two as compact rows with thumbnails. Replaces the now-playing + up-next rows under the
  // clock there (the sleep dashboard keeps its slim now-playing line). Hidden when there's
  // nothing to play (the list takes the full width), in the dark hours and under canvas/browser;
  // dimmed in quiet hours. Items carry "source" (podcast; youtube later; the now-playing file may say "radio") and a local "art" path.
  const pcss = document.createElement("style");
  pcss.dataset.portrait = "podcard";
  pcss.media = "(orientation: portrait)";
  pcss.textContent = `
  #dash .pod { display: none; }
  html.big body.board-on.pod-on #dash .pod { display: flex; grid-area: pod; flex-direction: column; gap: 1.2vw; overflow: hidden;
    font-size: 2.2vw; line-height: 1.25; color: rgba(235,240,255,0.92);
    background: linear-gradient(160deg, rgba(40,52,84,0.55), rgba(9,13,22,0.78) 55%);
    border: 1px solid rgba(160,200,255,0.14); 
    -webkit-backdrop-filter: blur(1.4vw); backdrop-filter: blur(1.4vw); transition: opacity 1.2s; }
  html.big body.board-on.pod-on #dash .pod.over { -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 3vw), transparent); }
  html.big body.board-on.pod-on #dash .pod.q { opacity: 0.5; filter: saturate(0.6); }
  html.big body.board-on:has(#focusring.on) #dash .pod { opacity: 0.16; }
  /* the list gives the card its 45% (the month calendar already shows today + tomorrow) */
  html.big body.board-on.pod-on #dash .dayv { grid-template-columns: minmax(0, 55fr) minmax(0, 45fr);
    grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) minmax(0, 1fr) auto auto;
    grid-template-areas: "clk wx" "al al" "cal cal" "lst pod" "away away" "sys sys"; }
  html.big body.board-on.tw-on.pod-on #dash .dayv { grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto auto;
    grid-template-areas: "clk wx" "al al" "lst pod" "away away" "sys sys" "tw tw"; }
  html.big body.board-on.tw-on.fc-on.pod-on #dash .dayv { grid-template-rows: calc(24.5vh - 2.6vw) auto minmax(0, 1fr) auto auto auto auto;
    grid-template-areas: "clk wx" "al al" "lst pod" "away away" "fc fc" "sys sys" "tw tw"; }
  html.big body.board-on.pod-on #dash .clk .pc, html.big body.board-on.pod-on #dash .clk .un { display: none !important; }
  #dash .pod .ph { display: flex; align-items: center; gap: 0.5em; font-size: 0.82em; font-weight: 600; letter-spacing: 0.1em;
    text-transform: uppercase; color: rgba(160,200,255,0.78); }
  #dash .pod .ph svg { width: 1.2em; height: 1.2em; }
  #dash .pod .ph .wv i { display: inline-block; width: 0.16em; margin-right: 0.12em; height: 0.5em; border-radius: 0.1em;
    background: currentColor; vertical-align: middle; }
  #dash .pod .ph .wv i:nth-child(2) { height: 0.95em; } #dash .pod .ph .wv i:nth-child(3) { height: 0.7em; }
  #dash .pod.live:not(.paused) .ph .wv i { animation: podwv 1.1s ease-in-out infinite; }
  #dash .pod.live .ph .wv i:nth-child(2) { animation-delay: -0.35s; } #dash .pod.live .ph .wv i:nth-child(3) { animation-delay: -0.7s; }
  @keyframes podwv { 0%, 100% { transform: scaleY(0.45); } 50% { transform: scaleY(1); } }
  /* hero: big rounded art, then show / title / meta */
  #dash .pod .hero { display: flex; flex-direction: column; gap: 0.9vw; flex: none; } /* never shrink: a squeezed hero let the art spill over "Then" */
  #dash .pod .art { position: relative; flex: none; width: min(100%, 22vw); aspect-ratio: 1; align-self: center; border-radius: 2vw;
    overflow: visible; }
  #dash .pod .art img, #dash .pod .art .ph0, #dash .pod .th img, #dash .pod .th .ph0 { display: block; width: 100%; height: 100%;
    object-fit: cover; border-radius: inherit; }
  #dash .pod .art img { }
  #dash .pod .ph0 { box-sizing: border-box; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, rgba(88,196,255,0.35), rgba(150,110,255,0.3)); color: rgba(255,255,255,0.7); }
  #dash .pod .ph0 svg { width: 40%; height: 40%; }
  #dash .pod.live:not(.paused) .art::after { content: ""; position: absolute; inset: -0.6vw; border-radius: 2.5vw;
    border: 0.3vw solid rgba(140,200,255,0.55); animation: podring 2.6s ease-in-out infinite; pointer-events: none; }
  @keyframes podring { 0%, 100% { opacity: 0.25; transform: scale(0.985); } 50% { opacity: 0.9; transform: scale(1.015); } }
  #dash .pod.paused .art img { filter: saturate(0.6) brightness(0.8); }
  #dash .pod .sh { font-size: 0.8em; color: rgba(160,200,255,0.8); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #dash .pod .tl { font-size: 1.12em; font-weight: 600; color: #fff; overflow: hidden; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow-wrap: anywhere; }
  #dash .pod .mt { display: flex; align-items: center; gap: 0.5em; font-size: 0.8em; color: rgba(225,232,255,0.6);
    font-variant-numeric: tabular-nums; white-space: nowrap; min-width: 0; }
  #dash .pod .mt svg { flex: none; width: 1.1em; height: 1.1em; fill: none; stroke: currentColor; stroke-width: 2;
    stroke-linecap: round; stroke-linejoin: round; }
  #dash .pod .mt .sp { padding: 0 0.45em; border-radius: 1em; background: rgba(255,255,255,0.1); }
  #dash .pod .mt .gr { flex: 1; }
  #dash .pod .nd { flex: none; width: 0.5em; height: 0.5em; border-radius: 50%; background: #58c4ff; box-shadow: 0 0 0.6em rgba(88,196,255,0.8); }
  #dash .pod .bar { height: 0.3em; border-radius: 1em; background: rgba(255,255,255,0.12); overflow: hidden; }
  #dash .pod .bar i { display: block; height: 100%; border-radius: 1em; background: linear-gradient(90deg, #58c4ff, #a58bff); transition: width 1s linear; }
  #dash .pod.paused .hero .bar i { background: rgba(200,205,220,0.4); }
  /* the next two: compact rows with thumbnails */
  #dash .pod .rl { font-size: 0.72em; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.4);
    padding-top: 0.6vw; border-top: 1px solid rgba(255,255,255,0.08); }
  #dash .pod .rw { display: flex; gap: 0.7em; align-items: center; min-width: 0; }
  #dash .pod .th { flex: none; width: 5.6vw; height: 5.6vw; border-radius: 1vw; overflow: hidden; }
  #dash .pod .rw .tx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.15em; }
  #dash .pod .rw .tl { font-size: 0.86em; font-weight: 500; }
  #dash .pod .rw .mt { font-size: 0.7em; }
  #dash .pod .rw .bar { height: 0.22em; }`;
  document.head.appendChild(pcss);

  const dim = document.createElement("div");
  dim.id = "dashdim";
  const bgEl = document.getElementById("bg");
  if (bgEl && bgEl.nextSibling) document.body.insertBefore(dim, bgEl.nextSibling);
  else document.body.insertBefore(dim, document.body.firstChild);
  const root = document.createElement("div");
  root.id = "dash";
  root.innerHTML = `
    <div class="dayv">
      <div class="clk"><div class="t"></div><div class="d"></div><div class="vs"></div><div class="pc"></div><div class="un"></div></div>
      <div class="card wx"></div>
      <div class="card cal"></div>
      <div class="card lst"></div>
      <div class="card pod"></div>
      <div class="card away"></div>
      <div class="card al"></div>
      <div class="card sys"></div>
      <div class="foot"><div class="qr"><img alt=""></div><div><div class="hint"></div><div class="stat"></div><div class="mei"></div></div></div>
    </div>
    <div class="nightv"><div class="t"></div><div class="d"></div><div class="ln ev1"></div><div class="ln wx1"></div><div class="mei"></div></div>
    <div class="card tw"></div>
    <div class="card fc"></div>
    <div class="toast"></div>`;
  document.body.appendChild(root);
  const q = (s) => root.querySelector(s);
  const setHTML = (el, h) => { if (el._h !== h) { el._h = h; el.innerHTML = h; } }; // no needless relayouts
  const esc = (x) => String(x == null ? "" : x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---------------------------------------------------------------- time
  let fakeOffset = 0;
  if (params.get("at")) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(params.get("at"));
    if (m) { const d = new Date(); d.setHours(+m[1], +m[2], 0, 0); fakeOffset = d - new Date(); }
  }
  const nowD = () => new Date(Date.now() + fakeOffset);
  const hm = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  const clockHTML = (d) => { const [t, ap] = hm(d).split(" "); return esc(t) + "<small>" + esc(ap || "") + "</small>"; };
  const dayKey = (d) => d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  const inHours = (h, r) => Array.isArray(r) && r.length === 2 && r[0] !== r[1] &&
    (r[0] < r[1] ? h >= r[0] && h < r[1] : h >= r[0] || h < r[1]);
  const parseWhen = (s) => (s && s.length === 10 ? new Date(s + "T00:00:00") : new Date(s));

  // ---------------------------------------------------------------- data
  let list = { items: [] }, wx = null, events = null, tasks = [], usage = null;
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  let away = { items: [] }, wrapup = null, lastAway = "";
  async function pollAway() {
    try {
      const r = await fetch("dashboard-away.json", { cache: "no-store" });
      if (!r.ok) return;
      const txt = await r.text();
      if (txt === lastAway) return;
      lastAway = txt; away = JSON.parse(txt); render();
    } catch (e) {}
  }
  let lastList = "";
  async function pollList() {
    try {
      const r = await fetch("dashboard-list.json", { cache: "no-store" });
      if (!r.ok) return;
      const txt = await r.text();
      if (txt === lastList) return;
      lastList = txt; list = JSON.parse(txt); render();
    } catch (e) {}
  }
  async function pollSlow() {
    try { wx = await getJSON("dashboard-weather.json"); } catch (e) {}
    try { events = await getJSON("dashboard-events.json"); } catch (e) {}
    try { wrapup = await getJSON("dashboard-wrapup.json"); } catch (e) { wrapup = null; }
    await pollSports();
    if (showing || boarding) {
      try { usage = await getJSON("usage.json"); } catch (e) { usage = null; }
      try { tasks = await getJSON("/api/tasks"); } catch (e) {}
    }
    render();
  }
  let sports = null;
  async function pollSports() { try { sports = await getJSON("dashboard-sports.json"); } catch (e) { sports = null; } }
  // the big-screen board also shows my workers, queue and current backlog item, so poll those faster
  let queued = [], backlog = null;
  async function pollSys() {
    if (!boarding && !showing) return;
    try { tasks = await getJSON("/api/tasks"); } catch (e) {}
    try {
      queued = (await getJSON("/api/reminders")).map((x) => /^QUEUED \[([^\]]+)\]/.exec((x && x.text) || "")).filter(Boolean).map((m) => m[1]);
    } catch (e) { queued = []; }
    try { backlog = await getJSON("checklist.json"); } catch (e) { backlog = null; }
    render();
  }

  // ---------------------------------------------------------------- weather
  const ICON = {
    sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    cloud: '<path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.1 4.75 4.75 0 0 0 7 18.5z"/>',
    part: '<path d="M8.5 3.8v1.4M3.8 8.5h1.4M5.2 5.2l1 1M12.3 6.1a3.6 3.6 0 0 0-6.4 2.9"/><path d="M8.5 20h9a3.5 3.5 0 0 0 .5-6.96A5.2 5.2 0 0 0 8.2 12a4 4 0 0 0 .3 8z"/>',
    rain: '<path d="M7 14.5h10.5a3.6 3.6 0 0 0 .5-7.15A5.4 5.4 0 0 0 6.7 6.1 4.2 4.2 0 0 0 7 14.5z"/><path d="M8 17.5l-1 2.5M12 17.5l-1 2.5M16 17.5l-1 2.5"/>',
    snow: '<path d="M7 14.5h10.5a3.6 3.6 0 0 0 .5-7.15A5.4 5.4 0 0 0 6.7 6.1 4.2 4.2 0 0 0 7 14.5z"/><path d="M8 18.5h.01M12 18.5h.01M16 18.5h.01M10 21h.01M14 21h.01" stroke-width="2.6"/>',
    storm: '<path d="M7 14.5h10.5a3.6 3.6 0 0 0 .5-7.15A5.4 5.4 0 0 0 6.7 6.1 4.2 4.2 0 0 0 7 14.5z"/><path d="M12.5 15.5l-2 3.2h3l-2 3.3"/>',
    fog: '<path d="M4 9h16M3 13h18M5 17h14"/>',
  };
  function iconFor(code, isDay) {
    const c = +code;
    const k = c === 0 || c === 1 ? (isDay ? "sun" : "moon") : c === 2 ? "part" : c === 3 ? "cloud" : c === 45 || c === 48 ? "fog"
      : (c >= 71 && c <= 77) || c === 85 || c === 86 ? "snow" : c >= 95 ? "storm" : c >= 51 ? "rain" : "cloud";
    return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="rgba(200,225,255,0.85)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + ICON[k] + "</svg>";
  }
  const deg = (v) => (v == null || isNaN(v) ? "--" : Math.round(v) + "°");
  function wxFresh() { return wx && wx.temp != null && Date.now() / 1000 - (wx.updated || 0) < 3 * 3600; }
  function alertsHTML() {
    if (!wx || !wx.alerts || !wx.alerts.length) return "";
    return wx.alerts.slice(0, 2).map((a) => {
      const until = a.ends ? new Date(a.ends) : null;
      const sev = /extreme|severe/i.test(a.severity || "");
      return '<div class="alert' + (sev ? " sev" : "") + '">' + esc(a.event) +
        (until && !isNaN(until) ? "<span>until " + esc(until.toLocaleDateString("en-US", { weekday: "short" }) + " " + hm(until)) + "</span>" : "") + "</div>";
    }).join("");
  }
  function renderWx(board) {
    const el = q(".wx");
    if (board) { // alerts get their own full-width strip on the board
      const al = alertsHTML();
      q(".al").classList.toggle("on", !!al);
      setHTML(q(".al"), al);
    }
    if (!wx || wx.temp == null) { setHTML(el, "<h3>Weather</h3><div class='none'>No forecast yet</div>"); return; }
    if (board) { // beside me on the big screen: narrow, right-aligned, few lines
      const t = wx.today || {}, tm = wx.tomorrow, age = Date.now() / 1000 - (wx.updated || 0);
      setHTML(el, '<div class="now">' + iconFor(wx.code, wx.is_day) + '<div class="tmp">' + deg(wx.temp) + "</div></div>" +
        '<div class="desc">' + esc(wx.text) + "</div>" +
        '<div class="facts"><span>High <b>' + deg(t.high) + "</b> &nbsp;Low <b>" + deg(t.low) + "</b></span>" +
        "<span>Feels " + deg(wx.feels) + (t.rain != null && t.rain >= 10 ? " &nbsp;Rain <b>" + Math.round(t.rain) + "%</b>" : "") + "</span>" +
        (wx.wind != null ? "<span>Wind <b>" + esc(wx.wind_dir) + " " + Math.round(wx.wind) + "</b>" +
          (wx.gust && wx.gust > wx.wind + 5 ? ", gusts " + Math.round(wx.gust) : "") + "</span>" : "") + "</div>" +
        (tm ? '<div class="tmrw">Tomorrow ' + esc(tm.text).toLowerCase() + ", " + deg(tm.high) + " / " + deg(tm.low) + "</div>" : "") +
        (age > 3 * 3600 ? '<div class="stale">As of ' + esc(hm(new Date(wx.updated * 1000))) + "</div>" : ""));
      return;
    }
    const t = wx.today || {}, tm = wx.tomorrow;
    const age = Date.now() / 1000 - (wx.updated || 0);
    setHTML(el, (board ? "" : alertsHTML()) +
      '<div class="now">' + iconFor(wx.code, wx.is_day) + '<div class="tmp">' + deg(wx.temp) + '</div><div class="desc">' + esc(wx.text) +
      '<br><span class="muted">Feels like ' + deg(wx.feels) + "</span></div></div>" +
      '<div class="facts"><span>High <b>' + deg(t.high) + "</b> &nbsp;Low <b>" + deg(t.low) + "</b></span>" +
      (t.rain != null ? "<span>Rain <b>" + Math.round(t.rain) + "%</b></span>" : "") +
      (wx.wind != null ? "<span>Wind <b>" + esc(wx.wind_dir) + " " + Math.round(wx.wind) + "</b> mph" +
        (wx.gust && wx.gust > wx.wind + 5 ? ", gusts " + Math.round(wx.gust) : "") + "</span>" : "") + "</div>" +
      (tm ? '<div class="tmrw">Tomorrow: ' + esc(tm.text) + ", " + deg(tm.high) + " / " + deg(tm.low) +
        (tm.rain != null && tm.rain >= 20 ? ", rain " + Math.round(tm.rain) + "%" : "") + "</div>" : "") +
      (age > 3 * 3600 ? '<div class="stale">As of ' + esc(hm(new Date(wx.updated * 1000))) + "</div>" : ""));
  }

  // ---------------------------------------------------------------- calendar
  const PALETTE = ["#58c4ff", "#8affc1", "#ffd27a", "#ff9ad5", "#b9a4ff", "#7fe3e0"];
  // Calendar labels listed in [dashboard] calendar_colors (list.cal_colors) get fixed, calm,
  // distinct hues (the month legend shows them); others hash
  const CAL_COL = { Main: "#5fa8ff" };
  const colorFor = (s) => { const cc = (list && list.cal_colors) || {}; if (cc[s]) return cc[s]; if (CAL_COL[s]) return CAL_COL[s]; let h = 0; for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };
  function upcoming(now) {
    const out = { today: [], tomorrow: [] };
    if (!events || !Array.isArray(events.events)) return out;
    const tk = dayKey(now), tmr = new Date(now); tmr.setDate(tmr.getDate() + 1);
    const mk = dayKey(tmr);
    for (const e of events.events) {
      const s = parseWhen(e.start);
      if (isNaN(s)) continue;
      let end = e.end ? parseWhen(e.end) : new Date(+s + (e.all_day ? 864e5 : 36e5));
      if (isNaN(end)) end = new Date(+s + 36e5);
      if (end <= now) continue;
      const ev = { ...e, s, end, now: s <= now && end > now };
      if (e.all_day) { // spans: show on today if it covers today
        if (s <= now) out.today.push(ev);
        else if (dayKey(s) === mk) out.tomorrow.push(ev);
        continue;
      }
      if (dayKey(s) === tk || ev.now) out.today.push(ev);
      else if (dayKey(s) === mk) out.tomorrow.push(ev);
    }
    return out;
  }
  // Weather at an outdoor event (e.outdoor is set by walter-dashboard set-events from the
  // [dashboard] outdoor_keywords rule or the event's own "outdoor" flag): the hourly
  // forecast for the hour it starts (or now, if it's already on), the day's high for
  // all-day events; nothing when no forecast covers that time.
  function evWx(e, now) {
    if (!e.outdoor || !wx || Date.now() / 1000 - (wx.updated || 0) > 12 * 3600) return null;
    if (e.all_day) {
      const d = [wx.today, wx.tomorrow].find((x) => x && x.date === String(e.start).slice(0, 10));
      return d && d.high != null ? { temp: d.high, rain: d.rain, code: d.code, is_day: 1 } : null;
    }
    const h = wx.hourly || {}, ts = h.time || [];
    const at = Math.max(+e.s, +now - (+now % 36e5));
    for (let i = 0; i < ts.length; i++) {
      const t = +parseWhen(ts[i]);
      if (t <= at && at < t + 36e5) {
        if (h.temp == null || h.temp[i] == null) return null;
        return { temp: h.temp[i], rain: h.rain && h.rain[i], code: h.code && h.code[i], is_day: h.is_day ? h.is_day[i] : 1 };
      }
    }
    return null;
  }
  function wxChip(w) {
    if (!w) return "";
    const rain = w.rain != null && w.rain >= 20 ? " <b>" + Math.round(w.rain) + "%</b>" : "";
    return '<span class="wxe">' + iconFor(w.code, w.is_day).replace('class="ic"', 'class="ic wxi"') + deg(w.temp) + rain + "</span>";
  }
  function evRow(e) {
    const when = e.all_day ? "All day" : e.now ? "Now" : hm(e.s);
    return '<div class="ev' + (e.now ? " now" : "") + '"><div class="when">' + esc(when) + '</div><div class="what"><span class="nm">' +
      esc(e.title) + "</span>" + (e.calendar ? '<span class="lb"><i style="background:' + colorFor(e.calendar) + '"></i>' + esc(e.calendar) +
      (e.location ? " · " + esc(e.location) : "") + "</span>" : e.location ? '<span class="lb">' + esc(e.location) + "</span>" : "") +
      wxChip(evWx(e, nowD())) + "</div></div>";
  }
  function renderCal(now) {
    const el = q(".cal"), up = upcoming(now);
    const day = (name, evs, max) => '<div class="day"><h3>' + name + "</h3>" +
      (evs.length ? evs.slice(0, max).map(evRow).join("") + (evs.length > max ? '<div class="more">+ ' + (evs.length - max) + " more</div>" : "")
        : '<div class="none">' + (name === "Today" ? "Nothing else today" : "Nothing yet") + "</div>") + "</div>";
    let h = day("Today", up.today, 5) + day("Tomorrow", up.tomorrow, 4);
    if (!events) h += '<div class="stale">Calendar not loaded yet</div>';
    else if (Date.now() / 1000 - (events.updated || 0) > 8 * 3600)
      h += '<div class="stale">Calendar as of ' + esc(new Date(events.updated * 1000).toLocaleDateString("en-US", { weekday: "short" }) + " " + hm(new Date(events.updated * 1000))) + "</div>";
    setHTML(el, h);
  }
  function nextEvent(now) {
    const up = upcoming(now);
    const all = up.today.concat(up.tomorrow).filter((e) => !e.all_day && e.s > now);
    if (all.length) {
      const e = all[0], tmr = dayKey(e.s) !== dayKey(now);
      return "Next: <b>" + esc(e.title) + "</b>, " + (tmr ? "tomorrow " : "") + esc(hm(e.s));
    }
    return "";
  }

  // ---------------------------------------------------------------- the list
  const SECS = [["tasks", "To do"], ["inbox", "Inbox"], ["shopping", "Shopping"], ["notes", "Notes"], ["ideas", "Parked ideas"]];
  const DONE_SHOW = 600; // ticked-off items stay (faded) for 10 minutes
  function dueHTML(d, now) {
    if (!d) return "";
    const dt = parseWhen(d);
    if (isNaN(dt)) return "";
    const hasTime = d.length > 10;
    const late = hasTime ? dt < now : dayKey(dt) !== dayKey(now) && dt < now;
    const tmr = new Date(now); tmr.setDate(tmr.getDate() + 1);
    const label = dayKey(dt) === dayKey(now) ? (hasTime ? hm(dt) : "today")
      : dayKey(dt) === dayKey(tmr) ? "tomorrow" + (hasTime ? " " + hm(dt) : "")
      : dt.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) + (hasTime ? " " + hm(dt) : "");
    return '<span class="due' + (late ? " late" : "") + '">' + esc(label) + "</span>";
  }
  function renderList(now) {
    const el = q(".lst"), t = Date.now() / 1000;
    const items = (list.items || []).filter((i) => (!i.done_at || t - i.done_at < DONE_SHOW) && !isVerse(i));
    let h = "<h3>" + esc(list.title || "My list") + "</h3>", n = 0;
    if (wrapup && wrapup.until > t && wrapup.line) h += '<div class="wrap">' + esc(wrapup.line) + "</div>";
    for (const [key, name] of SECS) {
      const its = items.filter((i) => i.section === key).sort((a, b) => (a.done_at ? 1 : 0) - (b.done_at ? 1 : 0));
      if (!its.length) continue;
      h += '<div class="sec"><h4>' + name + "</h4>";
      for (const i of its.slice(0, 8)) {
        const fade = i.done_at ? Math.max(0.15, 0.6 * (1 - (t - i.done_at) / DONE_SHOW)) : 1;
        h += '<div class="it' + (i.done_at ? " done" : "") + (key === "notes" || key === "ideas" || key === "inbox" ? " note" : "") + '" style="opacity:' + fade.toFixed(2) +
          '"><span class="bx"></span><span class="tx">' + esc(i.text) + dueHTML(i.due, now) + "</span></div>";
        n++;
      }
      if (its.length > 8) h += '<div class="more">+ ' + (its.length - 8) + " more</div>";
      h += "</div>";
    }
    if (!n) h += '<div class="none">All clear. Press Enter to add something.</div>';
    setHTML(el, h);
    q(".cal").classList.toggle("withaway", renderAway());
    el.classList.toggle("over", el.scrollHeight > el.clientHeight + 2);
  }

  // ---------------------------------------------------------------- while you were away
  function renderAway() {
    const el = q(".away"), its = (away.items || []).slice(0, 4);
    el.classList.toggle("on", its.length > 0);
    if (!its.length) return false;
    const n = away.unseen || 0;
    let h = "<h3>" + (n ? "While you were away: " + n : "Earlier") + "</h3>";
    for (const i of its) {
      const d = new Date(i.t * 1000), today = dayKey(d) === dayKey(new Date());
      h += '<div class="aw' + (i.seen ? " seen" : "") + '"><span class="when">' + esc((today ? "" : d.toLocaleDateString("en-US", { weekday: "short" }) + " ") + hm(d)) +
        '</span><span class="tx"><b>' + esc(i.title) + "</b>" + (i.body ? " " + esc(i.body) : "") + "</span></div>";
    }
    setHTML(el, h);
    return true;
  }

  // ---------------------------------------------------------------- verse of the day
  // A daily routine may write a notes item "Verse: <text> - <reference>". Shown under the
  // clock only if [dashboard] verse_line is on (list.verse) and it was added today; it never
  // shows in the Notes list (kept in the data).
  const isVerse = (i) => !!i && i.section === "notes" && /^\s*verse\s*:/i.test(i.text || "");
  function verseToday(now) {
    if (list.verse === false) return null;
    const its = (list.items || []).filter((i) => isVerse(i) && !i.done_at &&
      (!i.created || dayKey(new Date(i.created * 1000)) === dayKey(now)));
    if (!its.length) return null;
    const i = its.reduce((a, b) => ((b.created || 0) >= (a.created || 0) ? b : a));
    let body = String(i.text).replace(/^\s*verse\s*:\s*/i, "").trim(), ref = "";
    const m = /^(.*\S)\s+[-–—]+\s+(\S.{0,40})$/.exec(body); // the last spaced dash ("Proverbs 3:5-7" keeps its own)
    if (m) { body = m[1].trim(); ref = m[2].trim(); }
    body = body.replace(/^["“]+|["”]+$/g, "").trim();
    return body ? { body, ref } : null;
  }
  // fit in two lines: shrink a little, then shorten the text (the reference always stays)
  let vsKey = "", vsSize = "";
  function fitVerse(el, v) {
    el.style.fontSize = ""; // the rule's size (text scale included) is the starting point
    const base = parseFloat(getComputedStyle(el).fontSize);
    const key = v.body + "|" + v.ref + "|" + el.clientWidth + "|" + base;
    if (key === vsKey) { el.style.fontSize = vsSize; return; }
    const show = (body, k) => {
      el.style.fontSize = (base * k).toFixed(2) + "px";
      el.innerHTML = esc(body) + (v.ref ? ' <span class="vr">— ' + esc(v.ref) + "</span>" : "");
      return el.scrollHeight <= parseFloat(getComputedStyle(el).lineHeight) * 2 + 2;
    };
    let ok = false;
    for (const k of [1, 0.92, 0.84]) if ((ok = show(v.body, k))) break;
    if (!ok) { // binary search on words, ending with an ellipsis
      const w = v.body.split(/\s+/), cut = (n) => w.slice(0, n).join(" ").replace(/[,;:.]$/, "") + "…";
      let lo = 1, hi = w.length - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (show(cut(mid), 0.84)) lo = mid; else hi = mid - 1; }
      show(cut(lo), 0.84);
    }
    vsKey = key; vsSize = el.style.fontSize;
  }
  function renderVerse(now, where) { // where: ".dayv .clk" | ".nightv" | null (hidden)
    const b = document.body.classList, el = q(".vs");
    const v = where && !b.contains("canvas-on") && !b.contains("browser-on") ? verseToday(now) : null;
    b.toggle("vs-on", !!v);
    if (!v) { root.style.removeProperty("--vsbot"); return; }
    const host = q(where), before = where === ".nightv" ? q(".nightv .ev1") : null;
    if (el.parentNode !== host || (before && el.nextSibling !== before)) { host.insertBefore(el, before); vsKey = ""; }
    fitVerse(el, v);
    // landscape asleep (day): the weather card steps down if the verse reaches it
    if (where === ".dayv .clk") root.style.setProperty("--vsbot", Math.ceil(el.getBoundingClientRect().bottom + innerHeight * 0.025) + "px");
  }

  function renderFoot() {
    const qr = q(".qr"), img = qr.querySelector("img");
    if (list.qr) { if (img.getAttribute("src") !== list.qr) img.src = list.qr; qr.style.display = ""; } else qr.style.display = "none";
    setHTML(q(".hint"), (list.qr ? "<b>Scan to add from your phone</b><br>" : "") + "Enter: type something to add &nbsp;·&nbsp; Space: wake me");
    const S = window.WALTER_STATE || {}, st = [];
    const running = (tasks || []).filter((x) => x.status === "running").length;
    st.push(running ? '<span class="on">' + running + " worker" + (running > 1 ? "s" : "") + " busy</span>" : "<span>Workers idle</span>");
    if (usage && usage.ok && Date.now() / 1000 - usage.updated < 20 * 60) {
      const parts = [usage.session && "session " + Math.round(usage.session.pct) + "%", usage.week && "week " + Math.round(usage.week.pct) + "%"].filter(Boolean);
      if (parts.length) st.push("<span>Claude: " + parts.join(", ") + "</span>");
    }
    const cam = document.getElementById("camon");
    st.push(cam && cam.classList.contains("on") ? '<span class="on">Camera on</span>' : "<span>Camera off</span>");
    st.push(S.muted ? '<span class="off">Mic off</span>' : S.hwMuted ? '<span class="off hw">Mic muted on the mic</span>'
      : '<span class="on">Mic on</span>');
    setHTML(q(".stat"), st.join(""));
  }

  // ---------------------------------------------------------------- podcast now playing
  // face/dashboard-podcast.json: written ~1/s by the podcast watcher (walter/podcast.py publish)
  // while mpv runs; {"playing": false, "resume": {...}} once it stops. No tap-to-pause yet
  // (the server has no podcast endpoint). Polled every second while playing, else every 5 s.
  let pod = null, lastPod = "", podTick = 0;
  async function pollPodcast() {
    const live = pod && pod.playing;
    if (!(showing || boarding) || (!live && podTick++ % 5)) return;
    try {
      const r = await fetch("dashboard-podcast.json", { cache: "no-store" });
      if (!r.ok) { pod = null; return; }
      const txt = await r.text();
      if (txt !== lastPod) { lastPod = txt; pod = JSON.parse(txt); }
    } catch (e) {}
  }
  const PCI = {
    play: '<path d="M8 5.5v13l10-6.5z"/>',
    pause: '<path d="M9 5.5v13M15 5.5v13"/>',
    duck: '<path d="M4 9.5h3l4-3.5v12l-4-3.5H4z"/><path d="M15 12h5"/>', // speaker, turned down
  };
  const pcIcon = (k) => '<svg viewBox="0 0 24 24">' + PCI[k] + "</svg>";
  const pcClock = (s) => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
  function pcHTML() {
    const t = Date.now() / 1000;
    if (pod && pod.playing && t - (pod.updated || 0) < 15) {
      const pos = pod.pos || 0, dur = pod.dur || 0, pct = dur ? Math.min(100, (100 * pos) / dur) : 0;
      const sp = pod.speed && Math.abs(pod.speed - 1) > 0.01 ? '<span class="sp">' + esc(String(+pod.speed)) + "×</span>" : "";
      const head = '<div class="ln">' + pcIcon(pod.paused ? "pause" : "play") + (pod.ducked ? pcIcon("duck") : "") +
        '<span class="tt">' + (pod.show ? "<b>" + esc(pod.show) + "</b> · " : "") + esc(pod.title) + "</span>";
      if (pod.source === "radio") return ["pc" + (pod.paused ? " paused" : ""), head + '<span class="lv">live</span></div>'];
      return ["pc" + (pod.paused ? " paused" : ""), head + sp + "</div>" +
        '<div class="ln pb"><span>' + pcClock(pos) + '</span><span class="bar"><i style="width:' + pct.toFixed(1) + '%"></i></span><span>' +
        (dur ? "-" + pcClock(dur - pos) : "") + "</span></div>"];
    }
    const r = pod && !pod.playing && pod.resume;
    if (r && t - (r.at || 0) < 3 * 86400) // only a recent, unfinished episode
      return ["pc", '<span class="chip" title="' + esc(r.title) + '">' + esc(r.show || r.title) + ": continue?</span>"];
    return null;
  }
  function renderPc(where, quiet) { // where: ".dayv .clk" | ".nightv" | null (hidden)
    const b = document.body.classList, el = q(".pc");
    const v = where && !b.contains("canvas-on") && !b.contains("browser-on") ? pcHTML() : null;
    b.toggle("pc-on", !!v);
    if (!v) return;
    const host = q(where), before = where === ".nightv" ? q(".nightv .mei") : null;
    if (el.parentNode !== host || el.nextSibling !== before) host.insertBefore(el, before);
    el.className = v[0] + (quiet ? " q" : "");
    setHTML(el, v[1]);
  }

  // ---------------------------------------------------------------- podcast up next
  // face/dashboard-podcast-upnext.json: {"items": [{source: podcast|youtube, show, title, date, dur, new, resume, pos, art}], updated},
  // written by walter-dashboard refresh (15 min timer, RSS cache) and after play/stop. Polled every 60 s.
  // While something plays, the playing episode is dropped here too (the file may predate it).
  let upn = null, lastUpn = "";
  async function pollUpnext() {
    try {
      const r = await fetch("dashboard-podcast-upnext.json", { cache: "no-store" });
      if (!r.ok) { upn = null; return; }
      const txt = await r.text();
      if (txt !== lastUpn) { lastUpn = txt; upn = JSON.parse(txt); }
    } catch (e) {}
  }
  const unDate = (ts) => { if (!ts) return ""; const d = new Date(ts * 1000), days = (Date.now() - d) / 864e5;
    if (days < 1 && d.getDate() === new Date().getDate()) return "today";
    if (days < 7) return d.toLocaleDateString("en-US", { weekday: "short" });
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }); };
  const unLen = (s) => { const m = Math.round((s || 0) / 60); return !m ? "" : m < 60 ? m + " min" : Math.floor(m / 60) + " h" + (m % 60 ? " " + (m % 60) : ""); };
  function unHTML() {
    if (!upn || !Array.isArray(upn.items) || Date.now() / 1000 - (upn.updated || 0) > 2 * 86400) return null;
    const live = pod && pod.playing && Date.now() / 1000 - (pod.updated || 0) < 15;
    const items = upn.items.filter((e) => !(live && e.title === pod.title && (!pod.show || e.show === pod.show))).slice(0, 3);
    if (!items.length) return null;
    return '<div class="hd">' + (live ? "After this" : "Up next") + "</div>" + items.map((e) =>
      '<div class="ln"><span class="dot' + (e.new ? "" : " off") + '"></span><span class="tt"><b>' + esc(e.show || "") + "</b> · " + esc(e.title) + "</span>" +
      (e.resume ? '<span class="rs">resume</span>' : "") +
      '<span class="mt">' + esc([unDate(e.date), e.resume && e.dur ? pcClock(e.dur - (e.pos || 0)) + " left" : unLen(e.dur)].filter(Boolean).join(" · ")) + "</span></div>").join("");
  }
  function renderUn(where, quiet) { // same placement rules as the now-playing line, right under it
    const b = document.body.classList, el = q(".un");
    const v = where && !b.contains("canvas-on") && !b.contains("browser-on") ? unHTML() : null;
    b.toggle("un-on", !!v);
    if (!v) return;
    const pc = q(".pc"), host = q(where);
    const before = pc.parentNode === host ? pc.nextSibling : where === ".nightv" ? q(".nightv .mei") : null;
    if (el.parentNode !== host || el.nextSibling !== before) host.insertBefore(el, before);
    el.className = "un" + (quiet ? " q" : "");
    setHTML(el, v);
  }

  // ---------------------------------------------------------------- podcast card (board)
  // One card: the playing episode (or the next one) with big art, then two compact rows.
  // Art comes only from upn items' local "art" paths (walter/podcast.py cache_art), never a remote URL.
  const POD_MIC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>';
  const POD_PLAY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round">' + // YouTube items (walter/youtube.py)
    '<rect x="3" y="5.5" width="18" height="13" rx="3.5"/><path d="M10 9.2v5.6l4.8-2.8z" fill="currentColor"/></svg>';
  const podArt = (e) => { const a = e && typeof e.art === "string" && /^[\w./-]+$/.test(e.art) && !e.art.includes("..") ? e.art : "";
    return a ? '<img alt="" src="' + esc(a) + '">' : '<div class="ph0">' + (e && e.source === "youtube" ? POD_PLAY : POD_MIC) + "</div>"; };
  const podBar = (pct) => '<div class="bar"><i style="width:' + Math.max(0, Math.min(100, pct)).toFixed(1) + '%"></i></div>';
  const podMeta = (e) => [unDate(e.date), e.resume && e.dur ? pcClock(e.dur - (e.pos || 0)) + " left" : unLen(e.dur)].filter(Boolean).join(" · ");
  function podHTML() { // [className, html] or null (nothing to show)
    const t = Date.now() / 1000;
    const okUp = upn && Array.isArray(upn.items) && t - (upn.updated || 0) <= 2 * 86400;
    const live = pod && pod.playing && t - (pod.updated || 0) < 15;
    const same = (e) => e && live && e.title === pod.title && (!pod.show || !e.show || e.show === pod.show);
    let items = okUp ? upn.items.filter((e) => e && e.title && !same(e)) : [];
    let hero, cls = "pod";
    if (live) {
      const nw = okUp && same(upn.now) ? upn.now : (upn && Array.isArray(upn.items) && upn.items.find(same)) || {};
      hero = { ...nw, show: pod.show || nw.show, title: pod.title };
      cls += " live" + (pod.paused ? " paused" : "");
    } else {
      hero = items.shift();
    }
    if (live && pod.source === "radio") // live internet radio (walter/radio.py): station, what's on, a live tag
      return [cls + " radio", '<div class="ph"><span class="wv"><i></i><i></i><i></i></span>' + (pod.paused ? "Paused" : "Radio") + "</div>" +
        '<div class="hero"><div class="art"><div class="ph0">' + POD_MIC + '</div></div><div class="sh">' + esc(pod.show || "") + "</div>" +
        '<div class="tl">' + esc(pod.title || "Live radio") + '</div><div class="mt">' + pcIcon(pod.paused ? "pause" : "play") +
        (pod.ducked ? pcIcon("duck") : "") + '<span class="lv">live</span></div></div>'];
    if (!hero) return null;
    let h = '<div class="ph"><span class="wv"><i></i><i></i><i></i></span>' + (live ? (pod.paused ? "Paused" : "Now playing") : "Up next") + "</div>";
    h += '<div class="hero"><div class="art">' + podArt(hero) + '</div><div class="sh">' + esc(hero.show || "") + "</div>" +
      '<div class="tl">' + esc(hero.title) + "</div>";
    if (live) {
      const pos = pod.pos || 0, dur = pod.dur || 0;
      const sp = pod.speed && Math.abs(pod.speed - 1) > 0.01 ? '<span class="sp">' + esc(String(+pod.speed)) + "×</span>" : "";
      h += podBar(dur ? (100 * pos) / dur : 0) + '<div class="mt">' + pcIcon(pod.paused ? "pause" : "play") + (pod.ducked ? pcIcon("duck") : "") + sp +
        "<span>" + pcClock(pos) + '</span><span class="gr"></span><span>' + (dur ? "-" + pcClock(dur - pos) : "") + "</span></div>";
    } else {
      if (hero.resume && hero.dur) h += podBar((100 * (hero.pos || 0)) / hero.dur);
      h += '<div class="mt">' + (hero.new ? '<span class="nd"></span>' : "") + (hero.resume ? pcIcon("play") + "<span>Resume</span><span>·</span>" : "") +
        "<span>" + esc(podMeta(hero)) + "</span></div>";
    }
    h += "</div>";
    items = items.slice(0, 2);
    if (items.length) h += '<div class="rl">' + (live ? "After this" : "Then") + "</div>" + items.map((e) =>
      '<div class="rw"><div class="th">' + podArt(e) + '</div><div class="tx"><div class="tl">' + esc(e.title) + "</div>" +
      (e.resume && e.dur ? podBar((100 * (e.pos || 0)) / e.dur) : "") +
      '<div class="mt">' + (e.new ? '<span class="nd"></span>' : "") + "<span>" + esc([e.show, podMeta(e)].filter(Boolean).join(" · ")) + "</span></div></div></div>").join("");
    return [cls, h];
  }
  function renderPod(on, quiet) { // on: the awake portrait board, not in the dark hours
    const b = document.body.classList, el = q(".pod");
    const v = on && document.documentElement.classList.contains("big") && !b.contains("canvas-on") && !b.contains("browser-on") ? podHTML() : null;
    b.toggle("pod-on", !!v);
    if (!v) return;
    el.className = "card " + v[0] + (quiet ? " q" : "");
    setHTML(el, v[1]);
    el.classList.toggle("over", el.scrollHeight > el.clientHeight + 2);
  }

  // ---------------------------------------------------------------- 7-day strip (portrait)
  const ymd = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const DROP = '<svg class="drop" viewBox="0 0 24 24"><path d="M12 3c3.2 4.4 6.2 7.8 6.2 11.3a6.2 6.2 0 0 1-12.4 0C5.8 10.8 8.8 7.4 12 3z" fill="currentColor"/></svg>';
  function fcDays(now) { // today onwards (up to 16 days); nothing if the forecast is a day old or missing
    if (!wx || !Array.isArray(wx.days) || Date.now() / 1000 - (wx.updated || 0) > 24 * 3600) return [];
    const today = ymd(now);
    return wx.days.filter((d) => d && typeof d.date === "string" && d.date >= today).slice(0, 16);
  }
  function renderFc() { // the strip is gone (the month calendar shows the forecast); the element stays as an anchor
    document.body.classList.remove("fc-on");
    const el = q(".fc");
    const night = showing && !boarding && mode !== "day";
    const host = q(night ? ".nightv" : ".dayv"), before = q(night ? ".nightv .mei" : ".dayv .foot");
    if (el.parentNode !== host || el.nextSibling !== before) host.insertBefore(el, before);
    if (el.innerHTML) el.innerHTML = "";
  }

  // ---------------------------------------------------------------- month calendar (portrait)
  // Calendar: dashboard-events.json (the month grid, walter-dashboard events-window, from the routines). Games:
  // dashboard-sports.json (walter/sports.py, ESPN + walter/hs_sports.py, NSAA high school
  // teams, which carry a small sport tag like "FB"), each team's "games" list; days in
  // the household's time zone. Old data hides it: the games after 3 h without a good sports fetch, a live score
  // after 12 minutes (offline: say nothing rather than something wrong).
  const TW_STALE_S = 3 * 86400;
  const SP_STALE_S = 3 * 3600, SP_LIVE_STALE_S = 12 * 60;
  const twTime = (d) => { const m = d.getMinutes(), h = d.getHours() % 12 || 12;
    return h + (m ? ":" + String(m).padStart(2, "0") : "") + (d.getHours() < 12 ? " AM" : " PM"); };
  function twGrid(now) { // [first Sunday, number of days]: Sunday on/before the 1st to the Saturday on/after the last day
    const first = new Date(now.getFullYear(), now.getMonth(), 1), last = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const s0 = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
    const s1 = new Date(last.getFullYear(), last.getMonth(), last.getDate() + 6 - last.getDay());
    return [s0, Math.round((s1 - s0) / 864e5) + 1];
  }
  function twGames(idx, days, now) { // followed games onto their days (first, before calendar chips)
    const real = Date.now() / 1000;
    if (!sports || !Array.isArray(sports.teams) || real - (sports.updated || 0) > SP_STALE_S) return;
    const tz = sports.tz || undefined, liveOk = real - (sports.updated || 0) < SP_LIVE_STALE_S;
    sports.teams.forEach((tm, ti) => {
      if (!tm || !tm.abbr) return;
      const seen = {};
      const lid = tm.live && (tm.live.id || tm.live.start);
      for (let g of (tm.live ? [tm.live] : []).concat(tm.games || [])) { // the live entry wins (freshest)
        if (!g || !g.start || !g.them || seen[g.id || g.start]) continue;
        seen[g.id || g.start] = 1;
        if (g.state === "in" && (g.id || g.start) !== lid) g = { ...g, state: "pre" }; // stuck "in": no live chip
        const d = new Date(g.start);
        if (isNaN(d)) continue;
        const i = idx[d.toLocaleDateString("en-CA", { timeZone: tz })];
        if (i == null || g.state === "off") continue;
        if (g.state === "in" && !liveOk) continue;
        days[i].games.push({ g, tm, at: +d, ti });
      }
    });
    for (const x of days) x.games.sort((a, b) => (b.g.state === "in") - (a.g.state === "in") || a.at - b.at || a.ti - b.ti);
  }
  function twDays(now) { // [{d, evs, games, fc}] for the month grid; [] if there's nothing to show
    const calOk = events && Array.isArray(events.events) && Date.now() / 1000 - (events.updated || 0) <= TW_STALE_S;
    const days = [], idx = {}, [s0, n] = twGrid(now), fc = {};
    for (const f of fcDays(now)) fc[f.date] = f;
    for (let i = 0; i < n; i++) {
      const d = new Date(s0.getFullYear(), s0.getMonth(), s0.getDate() + i);
      idx[ymd(d)] = days.length; days.push({ d, evs: [], games: [], fc: fc[ymd(d)] });
    }
    const ti = idx[ymd(now)];
    for (const e of calOk ? events.events : []) {
      const s = parseWhen(e.start);
      if (isNaN(s) || !e.title) continue;
      if (e.all_day) { // every day it covers (the end date is exclusive)
        let end = e.end ? parseWhen(e.end) : null;
        if (!end || isNaN(end) || end <= s) end = new Date(s.getFullYear(), s.getMonth(), s.getDate() + 1);
        const multi = end - s > 864e5 + 2 * 36e5; // two or more days (DST-safe): one bar per week row
        for (let d = new Date(s); d < end; d.setDate(d.getDate() + 1)) {
          const i = idx[ymd(d)];
          if (i != null) days[i].evs.push({ e, at: -1, multi, last: ymd(new Date(end - 36e5)) });
        }
        continue;
      }
      let end = e.end ? parseWhen(e.end) : new Date(+s + 36e5);
      if (isNaN(end)) end = new Date(+s + 36e5);
      let i = idx[ymd(s)];
      if (i == null && s < now && end > now) i = ti; // started before the window, still on
      if (i == null) continue;
      days[i].evs.push({ e, at: +s, past: end <= now });
    }
    for (const x of days) x.evs.sort((a, b) => a.at - b.at || String(a.e.title).localeCompare(String(b.e.title)));
    twGames(idx, days, now);
    return calOk || days.some((x) => x.games.length || x.fc) ? days : [];
  }
  function twWx(f) { // icon, high/low, rain % when 20% or more
    const rain = f.rain != null && f.rain >= 20 ? Math.round(f.rain) : null;
    return '<div class="w">' + iconFor(f.code, 1) + "<b>" + deg(f.high) + "</b>/" + deg(f.low) +
      (rain == null ? "" : ' <span class="rn">' + DROP + rain + "%</span>") + "</div>";
  }
  function twChip(x) {
    const e = x.e, col = colorFor(e.calendar);
    return '<div class="ch' + (e.all_day ? " ad" : "") + (x.past ? " past" : "") + '" style="--cc:' + col + '"><i></i>' +
      (e.all_day ? "" : "<b>" + esc(twTime(new Date(x.at))) + "</b> ") + "<span>" + esc(e.title) + "</span></div>";
  }
  // calendar_style "counts" (face/calstyle.js): one solid tile per calendar with a big count, in the
  // weeks that aren't expanded; Main first, then the [dashboard] calendar_colors order; max 4 boxes
  function twTiles(evs) {
    const r = window.WalterCalStyle.tiles(evs, ["Main"].concat(Object.keys((list && list.cal_colors) || {})));
    if (!r.tiles.length) return [];
    return ['<div class="ct">' + r.tiles.map((t) => '<span class="t' + (t.past ? " past" : "") + '" style="--cc:' + colorFor(t.cal) +
      '" title="' + esc(t.n + " " + t.cal) + '"><b>' + t.n + "</b><small>" + esc(t.cal) + "</small></span>").join("") +
      (r.more ? '<span class="t more"><b>+' + r.more + "</b></span>" : "") + "</div>"];
  }
  function twGame(x, now) { // compact chip: "NEB L 10-17" | "AUR FB 7 PM" | live "NEB 21-10 Q3"; opponent in the tooltip
    const g = x.g, tm = x.tm, sc = (v) => esc(v == null ? "-" : v);
    const team = esc(tm.abbr) + (tm.tag ? "<small> " + esc(tm.tag) + "</small>" : "");
    const vs = (g.home ? "vs " : "at ") + g.them.abbr;
    let body, cls = "";
    if (g.state === "in") {
      cls = " lv";
      body = '<span class="ld"></span>' + team + " " + sc(g.us.score) + "-" + sc(g.them.score) + (g.detail ? ' <span class="op">' + esc(g.detail) + "</span>" : "");
    } else if (g.state === "post" && g.us && g.us.score != null) {
      const r = g.result || "";
      body = team + ' <span class="' + esc(r) + '">' + esc(r) + "</span> " + sc(g.us.score) + "-" + sc(g.them.score);
    } else {
      const d = new Date(g.start), upcoming = x.at > +now - 4 * 3600e3;
      body = team + " " + (upcoming ? "<b>" + (g.tbd ? "TBD" : esc(twTime(d))) + "</b>" : '<span class="op">' + esc(vs) + "</span>");
      if (!upcoming) cls = " past"; // no result posted yet
    }
    const col = tm.color ? "#" + esc(tm.color) : "#58c4ff";
    return '<span class="ch gm' + cls + '" style="--cc:' + col + '" title="' + esc(tm.abbr + " " + vs) + '"><i></i>' + body + "</span>";
  }
  function renderTw(now, wanted) {
    const b = document.body.classList;
    const days = wanted && window.WALTER_PORTRAIT && window.WALTER_PORTRAIT() &&
      !b.contains("canvas-on") && !b.contains("browser-on") ? twDays(now) : [];
    b.toggle("tw-on", days.length > 0);
    const el = q(".tw"), fc = q(".fc");
    if (el.parentNode !== fc.parentNode || el.nextSibling !== fc) fc.parentNode.insertBefore(el, fc); // right above the (empty) strip anchor
    if (!days.length) return;
    const today = ymd(now), mon = now.getMonth(), wk0 = new Date(now.getFullYear(), mon, now.getDate() - now.getDay());
    const wk1 = new Date(wk0.getFullYear(), wk0.getMonth(), wk0.getDate() + 7);
    el.classList.toggle("w6", days.length > 35);
    const lg = Object.keys(CAL_COL).map((k) => '<span><i style="--cc:' + CAL_COL[k] + '"></i>' + esc(k) + "</span>").join("");
    let h = '<div class="mh"><span>' + esc(now.toLocaleDateString("en-US", { month: "long", year: "numeric" })) + '</span><span class="lg">' + lg + "</span></div>" +
      ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((w) => '<div class="wn">' + w + "</div>").join("");
    for (let w = 0; w * 7 < days.length; w++) h += twWeek(days.slice(w * 7, w * 7 + 7), now, today, mon, wk0, wk1);
    if (events && Date.now() / 1000 - (events.updated || 0) > 8 * 3600)
      h += '<div class="st">Calendar as of ' + esc(new Date(events.updated * 1000).toLocaleDateString("en-US", { weekday: "short" }) + " " + hm(new Date(events.updated * 1000))) + "</div>";
    setHTML(el, h);
    twFit(el);
  }
  // one week row: day backgrounds (hairlines, weekend/today tint), headers, a lane per multi-day bar, then each day's items
  function twWeek(wd, now, today, mon, wk0, wk1) {
    const bars = [], seen = new Map();
    wd.forEach((x, c) => { for (const ev of x.evs) if (ev.multi) {
      const b = seen.get(ev.e);
      if (b) b.c1 = c; else { const nb = { ev, c0: c, c1: c }; seen.set(ev.e, nb); bars.push(nb); }
    } });
    bars.sort((a, b) => a.c0 - b.c0 || (b.c1 - b.c0) - (a.c1 - a.c0));
    const lanes = []; // last column used per lane
    for (const b of bars) { let l = lanes.findIndex((e) => e < b.c0); if (l < 0) l = lanes.push(0) - 1; lanes[l] = b.c1; b.l = l; }
    const L = lanes.length, cw = wd[0].d >= wk0 && wd[0].d < wk1;
    const counts = !!(window.WALTER_CAL_STYLE && window.WalterCalStyle && window.WALTER_CAL_STYLE() === "counts");
    let h = '<div class="wk' + (cw ? " cw" : "") + '" style="grid-template-rows:auto' + (L ? " repeat(" + L + ",auto)" : "") + ' 1fr">';
    const meta = wd.map((x) => { const k = ymd(x.d), dw = x.d.getDay();
      return { k, today: k === today, we: dw === 0 || dw === 6, gone: x.d < now && k !== today, om: x.d.getMonth() !== mon }; });
    wd.forEach((x, c) => { const m = meta[c];
      h += '<div class="cb' + (c ? " l" : "") + (m.we ? " we" : "") + (m.today ? " today" : "") + '" style="grid-area:1/' + (c + 1) + '/-1/' + (c + 2) + '"></div>'; });
    wd.forEach((x, c) => { const m = meta[c], cls = (m.today ? " today" : "") + (m.om ? " om" : "") + (m.gone ? " gone" : "");
      const num = x.d.getDate() === 1 ? x.d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : String(x.d.getDate());
      h += '<div class="dh' + cls + '" style="grid-area:1/' + (c + 1) + '"><span class="dd">' + esc(num) + "</span>" +
        (x.fc && !m.gone ? twWx(x.fc) : "") + "</div>"; });
    for (const b of bars) {
      const e = b.ev.e, s = ymd(parseWhen(e.start)), last = b.ev.last;
      const gone = meta[b.c1].gone, st = meta[b.c0].k === s || b.c0 > 0 && s > meta[b.c0 - 1].k, en = meta[b.c1].k === last || b.c1 < 6 && last < meta[b.c1 + 1].k;
      h += '<div class="ch mb' + (st ? " s" : "") + (en ? " e" : "") + (gone ? " gone" : "") + '" style="--cc:' + colorFor(e.calendar) +
        ";grid-area:" + (b.l + 2) + "/" + (b.c0 + 1) + "/" + (b.l + 3) + "/" + (b.c1 + 2) + '" title="' + esc(e.title) + '"><i></i>' + esc(e.title) + "</div>";
    }
    wd.forEach((x, c) => { const m = meta[c];
      // every item shows (owner, 6 Nov): the week's row grows to fit its busiest day; games, then all-day, then timed
      const games = x.games.map((g) => twGame(g, now));
      const items = counts && !cw ? games.concat(twTiles(x.evs)) : games.concat(x.evs.filter((v) => !v.multi).map(twChip));
      h += '<div class="cx' + (m.om ? " om" : "") + (m.gone ? " gone" : "") + '" style="grid-area:' + (L + 2) + "/" + (c + 1) + '">' + items.join("") + "</div>"; });
    return h + "</div>";
  }
  // if the month doesn't fit: first the other cards give up space (tw-tight), then the
  // calendar's text shrinks a little at a time (--twk, down to 70%); events never hide
  function twFit(el) {
    const host = el.parentNode, b = document.body.classList;
    const key = el._h + "|" + innerWidth + "x" + innerHeight + "|" + document.body.className.replace(/\s*tw-tight/, "") +
      "|" + (host ? host.className : "") + "|" + Math.floor(Date.now() / 60000); // re-check at least once a minute
    if (el._fk === key) return;
    el._fk = key;
    const over = () => el.getBoundingClientRect().bottom > innerHeight + 1 ||
      (host && host.clientHeight && host.scrollHeight > host.clientHeight + 1);
    el.style.removeProperty("--twk"); b.remove("tw-tight");
    if (!over()) return;
    b.add("tw-tight");
    for (let k = 0.95; k >= 0.7 && over(); k -= 0.05) el.style.setProperty("--twk", k.toFixed(2));
  }

  // ---------------------------------------------------------------- my status (board)
  // the "me" strip (portrait): icons + badges, words only when something needs attention
  const SV = (body, cls) => '<svg' + (cls ? ' class="' + cls + '"' : "") + ' viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + "</svg>";
  const IC = {
    mic: '<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
    micOff: '<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/><path d="M3.5 3.5l17 17" stroke-width="2.4"/>',
    gear: '<circle cx="12" cy="12" r="5.6"/><circle cx="12" cy="12" r="1.9"/><path d="M18.90 12.00L21.60 12.00 M16.88 16.88L18.79 18.79 M12.00 18.90L12.00 21.60 M7.12 16.88L5.21 18.79 M5.10 12.00L2.40 12.00 M7.12 7.12L5.21 5.21 M12.00 5.10L12.00 2.40 M16.88 7.12L18.79 5.21" stroke-width="2.6"/>',
    cam: '<path d="M3 7.5h11a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H3A1.5 1.5 0 0 1 1.5 15V9A1.5 1.5 0 0 1 3 7.5z"/><path d="M15.5 10.5l6-3v9l-6-3"/>',
    cloud: '<path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.1 4.75 4.75 0 0 0 7 18.5z"/>',
    moon: '<path d="M20 14.6A8.6 8.6 0 1 1 9.4 4a6.9 6.9 0 0 0 10.6 10.6z"/>',
    wifiOff: '<path d="M2 8.8a15 15 0 0 1 20 0M5.2 12.2a10 10 0 0 1 13.6 0M8.5 15.5a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="0.9" fill="currentColor"/><path d="M3 3l18 18" stroke-width="2.4"/>',
    restart: '<path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v4.5h-4.5"/>',
  };
  function useRing(u) { // inner ring = this session, outer hairline = the week; the number is the session
    const pc = (w) => Math.max(0, Math.min(100, +(w && w.pct) || 0));
    const s = pc(u.session), w = pc(u.week);
    const col = (p) => (p >= 90 ? "#ff8a8a" : p >= 70 ? "#ffd27a" : "#58c4ff");
    const arc = (r, p, sw, c) => '<circle cx="12" cy="12" r="' + r + '" pathLength="100" stroke="' + c + '" stroke-width="' + sw +
      '" stroke-dasharray="' + p.toFixed(1) + ' 100" transform="rotate(-90 12 12)"/>';
    const main = u.session ? s : w;
    const svg = '<svg viewBox="0 0 24 24" fill="none" stroke-linecap="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="7" stroke="rgba(255,255,255,0.13)" stroke-width="3.2"/>' +
      (main > 0 ? arc(7, main, 3.2, col(main)) : "") +
      (u.session && u.week ? '<circle cx="12" cy="12" r="10.6" stroke="rgba(255,255,255,0.1)" stroke-width="1.2"/>' +
        (w > 0 ? arc(10.6, w, 1.4, col(w)) : "") : "") + "</svg>";
    const tip = [u.session && "session " + Math.round(s) + "%", u.week && "week " + Math.round(w) + "%"].filter(Boolean).join(", ");
    return { html: '<span class="b use' + (main >= 90 ? " hot" : main >= 70 ? " warn" : "") + '" title="Claude usage: ' + tip + '">' + svg +
      '<span class="n">' + Math.round(main) + "%</span></span>", week: w };
  }
  function meStrip() {
    const S = window.WALTER_STATE || {}, t = Date.now() / 1000, h = [], att = [];
    const all = Array.isArray(tasks) ? tasks : [];
    const running = all.filter((x) => x.status === "running");
    const nRun = Math.max(running.length, S.jobs | 0);
    const asleep = S.state === "sleeping";
    const rs = document.getElementById("restart"), restarting = !!(rs && rs.classList.contains("down"));
    const ni = document.getElementById("netIcon"), offline = !!(ni && ni.style.display === "block");
    if (restarting) att.push('<span class="b att amber">' + SV(IC.restart) + "Restarting</span>");
    if (offline) att.push('<span class="b att red">' + SV(IC.wifiOff) + "Offline</span>");
    // state: asleep = a moon; awake = the dot (grey while my brain is away)
    if (asleep) h.push('<span class="b moon" title="Asleep">' + SV(IC.moon) + "</span>");
    else {
      const st = !S.connected || restarting ? "away" : S.state === "working" ? "thinking" : S.state || "idle";
      h.push('<span class="b" title="' + esc(st) + '"><span class="orb ' + esc(st) + '"><i></i></span></span>');
    }
    h.push(S.muted ? '<span class="b mic off" title="Mic off">' + SV(IC.micOff) + "</span>"
      : S.hwMuted ? '<span class="b mic off hw" title="Mic muted on the mic">' + SV(IC.micOff) + "</span>"
      : '<span class="b mic" title="Mic on">' + SV(IC.mic) + "</span>");
    if (nRun || queued.length) {
      const tip = running.map((x) => x.title).concat(queued.map((x) => "queued: " + x)).join("; ");
      h.push('<span class="b wk" title="' + esc(tip || "workers") + '">' + SV(IC.gear) + '<span class="n">' + nRun + "</span>" +
        (queued.length ? '<span class="q">+' + queued.length + "</span>" : "") + "</span>");
    }
    if (usage && usage.ok && t - (usage.updated || 0) < 20 * 60 && (usage.session || usage.week)) {
      const u = useRing(usage);
      h.push(u.html);
      if (u.week >= 90) att.push('<span class="b att red">Week ' + Math.round(u.week) + "% used</span>");
    }
    const cam = document.getElementById("camon");
    if (cam && cam.classList.contains("on")) h.push('<span class="b cam" title="Camera on">' + SV(IC.cam) + "</span>");
    const cloud = document.getElementById("cloudon");
    if (cloud && cloud.classList.contains("on"))
      h.push('<span class="b cloud" title="' + esc(cloud.title || "Cloud speech") + '">' + SV(IC.cloud) + "</span>");
    return att.join("") + h.join("");
  }
  function renderMe(sel) { for (const el of root.querySelectorAll(sel)) setHTML(el, meStrip()); }
  function renderSys() {
    // portrait only (the board is portrait only): the "me" strip
    let el = q(".sys .mei");
    if (!el) { setHTML(q(".sys"), '<div class="mei"></div>'); el = q(".sys .mei"); }
    setHTML(el, meStrip());
  }

  // ---------------------------------------------------------------- mode + render
  let boarding = false;
  let showing = false, mode = "day", forced = params.has("dash"), localHide = 0;
  // portrait big screen (owner, 6 Nov): no separate sleep dashboard; the awake board is the only
  // screen and I just fall asleep in place on it (sleeping eyes, no corner). Landscape unchanged.
  const boardOnly = () => !!(window.WALTER_BIG && window.WALTER_BIG());
  function wantShow() {
    const S = window.WALTER_STATE || {};
    if (list.enabled === false && !forced) return false;
    // going to sleep (Z, muted nap, quiet hours) closes an open canvas so the dashboard takes over
    if (S.state === "sleeping" && !boardOnly() && document.body.classList.contains("canvas-on") && window.walterCanvas) {
      window.walterCanvas.hide();
      const card = document.getElementById("card");
      if (card) card.className = "";
    }
    if (S.setup || document.body.classList.contains("canvas-on")) return false;
    const t = Date.now() / 1000;
    const preview = (list.show_until || 0) > t && (list.show_until || 0) !== localHide;
    return forced || (S.state === "sleeping" && !boardOnly()) || preview;
  }
  function render() {
    const now = nowD(), S = window.WALTER_STATE || {};
    const show = wantShow();
    const h = now.getHours();
    mode = inHours(h, list.dark || [23, 5]) ? "dark" : inHours(h, list.quiet || [22, 8]) ? "night" : "day";
    if (show !== showing) { showing = show; if (show) pollSlow(); }
    // big screen: the info board under me, awake or asleep (boardOnly above)
    const board = !show && boardOnly() && !S.setup;
    if (board !== boarding) { boarding = board; if (board) { pollSlow(); pollSys(); } }
    const b = document.body.classList;
    b.toggle("dash-on", show);
    b.toggle("dash-night", show && mode === "night");
    b.toggle("dash-dark", show && mode === "dark");
    b.toggle("board-on", board);
    b.toggle("board-night", board && mode === "night");
    b.toggle("board-dark", board && mode === "dark");
    S.dash = show;
    S.board = board;
    if (forced && show && !S.dashMix) { S.dashMix = 1; b.add("dash-instant"); } // ?dash=1: no animation (screenshots)
    S.dashMode = show ? mode : "";
    dim.style.opacity = board ? (mode === "day" ? 0.4 : 0.75) : !show ? 0 : mode === "dark" ? 1 : mode === "night" ? 0.8 : 0.5;
    renderFc(now, board || (show && mode !== "dark"));
    renderTw(now, board || (show && mode !== "dark"));
    renderVerse(now, mode === "dark" ? null : board || (show && mode === "day") ? ".dayv .clk" : show ? ".nightv" : null);
    renderPod(board && mode !== "dark", mode === "night");
    renderPc(mode === "dark" || (board && document.body.classList.contains("pod-on")) ? null
      : board || (show && mode === "day") ? ".dayv .clk" : show ? ".nightv" : null, mode === "night");
    // the board shows the card instead; the sleep dashboard keeps its slim lines
    renderUn(board || mode === "dark" ? null : show && mode === "day" ? ".dayv .clk" : show ? ".nightv" : null, mode === "night");
    { // the focus ring sits between me and the top of the strip
      const top = b.contains("fc-on") ? q(".fc") : null; // (never now: the month calendar just steps back)
      if (top && board) document.documentElement.style.setProperty("--fctop", Math.round(top.getBoundingClientRect().top) + "px");
    }
    if (board) {
      setHTML(q(".clk .t"), clockHTML(now));
      setHTML(q(".clk .d"), esc(now.toLocaleDateString("en-US", { weekday: "long" })) + "<br>" +
        esc(now.toLocaleDateString("en-US", { month: "long", day: "numeric" })));
      renderWx(true); renderCal(now); renderList(now); renderSys();
      return;
    }
    q(".al").classList.remove("on");
    if (!show) return;
    if (mode === "day") {
      setHTML(q(".clk .t"), clockHTML(now));
      setHTML(q(".clk .d"), esc(now.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })));
      renderWx(); renderCal(now); renderList(now); renderFoot(); renderMe(".foot .mei");
    } else {
      setHTML(q(".nightv .t"), clockHTML(now));
      q(".nightv .d").textContent = now.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
      setHTML(q(".ev1"), nextEvent(now));
      const al = wx && wx.alerts && wx.alerts[0];
      setHTML(q(".wx1"), wxFresh() ? (al ? "<b>" + esc(al.event) + "</b> · " : "") + esc(deg(wx.temp) + " " + (wx.text || "").toLowerCase()) +
        (wx.today ? ", low " + deg(wx.today.low) : "") : "");
      renderMe(".nightv .mei");
    }
  }

  // ---------------------------------------------------------------- adding from the keyboard
  let toastT = null;
  function toast(text, err) {
    const el = q(".toast");
    el.textContent = text; el.className = "toast on" + (err ? " err" : "");
    clearTimeout(toastT); toastT = setTimeout(() => { el.className = "toast" + (err ? " err" : ""); }, 3500);
  }
  const NAMES = { tasks: "To do", inbox: "Inbox", shopping: "Shopping", notes: "Notes", ideas: "Parked ideas" };
  // Typed commands are commands, not to-dos ("walter wake up" became a task on 5 Nov).
  function typedCommand(text) {
    const m = /^\W*(?:(?:hey|ok|okay)\s+)?(?:walter\W*)?(?:please\s+)?(wake(?:\s*up)?|wakeup|go\s+to\s+sleep|sleep|un-?mute|mute)?\W*(?:please)?\W*$/i.exec(text);
    if (!m) return null;
    const w = (m[1] || "").toLowerCase();
    if (!w) return /walter/i.test(text) ? "wake" : null;
    return w.startsWith("wake") ? "wake" : w.includes("sleep") ? "sleep" : w.startsWith("un") ? "unmute" : "mute";
  }
  async function runCommand(cmd) {
    const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (cmd === "sleep") { await post("/api/sleep"); toast("Going to sleep"); }
    else if (cmd === "mute") { await post("/api/mute", { on: true }); toast("Microphone off"); }
    else { await post("/api/mute", { on: false }); toast(cmd === "wake" ? "Waking up" : "Microphone on"); } // unmuting wakes me
  }
  // walter.js calls this instead of sending the text to my brain while the dashboard shows.
  window.walterDash = {
    showing: () => showing,
    async add(text, fallback) {
      const cmd = typedCommand(text);
      if (cmd) { try { await runCommand(cmd); } catch (e) { toast("Couldn't do that", true); } return; }
      try {
        const r = await fetch("/api/dashboard", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", text, quick: true }) });
        if (r.status === 404 || r.status === 405) { fallback(); toast("Sent to Walter"); return; } // server predates the dashboard
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { toast(j.error || "Couldn't add that", true); return; }
        list.items = j.items || list.items; lastList = "";
        toast("Added to " + (NAMES[j.item.section] || "the list") + ": " + j.item.text);
        render();
      } catch (e) { toast("Couldn't add that", true); }
    },
    hidePreview() { if ((list.show_until || 0) > Date.now() / 1000) { localHide = list.show_until; render(); return true; } return false; },
  };

  pollList(); pollSlow(); pollAway();
  setInterval(pollList, 3000);
  setInterval(pollAway, 5000);
  setInterval(pollPodcast, 1000);
  pollUpnext(); setInterval(pollUpnext, 60000);
  setInterval(pollSlow, 60000);
  setInterval(pollSys, 5000);
  setInterval(() => { if (sports && sports.live && (showing || boarding)) pollSports().then(render); }, 20000); // live scores
  setInterval(render, 1000);
})();
