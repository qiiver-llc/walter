/* First-run wizard on the wall (walter/firstrun.py): the current step, big, with a
   progress row and a QR code so the phone drives it. Self-contained: builds its own
   DOM and styles. Shown only while the server says the wizard is active. */
(() => {
  "use strict";
  const WALL = {
    welcome: ["Hi! Let's get me set up.", "Scan the code with your phone. About 15 minutes, and every step can be skipped."],
    network: ["Network, time and place", "I'm checking the internet. Pick your town on your phone for weather and the right time."],
    audio: ["Can we hear each other?", "Tap “Play test sound” on your phone, then say “Hey Walter” when it asks."],
    claude: ["Connect my brain", "Sign in with your own Claude account in the window on this screen, or use your own API key. I never see your password or code."],
    privacy: ["Your privacy, your choice", "Camera, cloud speech and recognition are all off unless you switch them on."],
    extras: ["A few extras", "Calendar, podcasts, your teams. All optional."],
    cloudlink: ["Reach me from anywhere", "Optional: type your invite code on your phone and my app works away from home too."],
    pair: ["Pair your phone", "Scan the pairing code to put me in your pocket."],
    hello: ["Say hello!", "Say “Hey Walter, hello”."],
  };
  const css = `
  #firstrun { position: fixed; inset: 0; z-index: 60; display: none; background: rgba(4,6,12,.9);
    color: #e8eefc; font-family: system-ui, sans-serif; text-align: center; padding: 6vh 6vw; box-sizing: border-box; }
  #firstrun .fr-dots { display: flex; gap: 1vw; justify-content: center; margin-bottom: 4vh; }
  #firstrun .fr-dot { width: 3.4vh; height: 3.4vh; border-radius: 50%; border: .3vh solid #2a3550;
    font-size: 1.6vh; line-height: 3.4vh; color: #7d8cab; }
  #firstrun .fr-dot.done { background: #58c4ff; color: #05060a; border-color: #58c4ff; }
  #firstrun .fr-dot.skipped { border-style: dashed; }
  #firstrun .fr-dot.cur { border-color: #8affc1; color: #fff; transform: scale(1.25); }
  #firstrun h1 { font-size: 6vh; margin: 0 0 2vh; font-weight: 650; }
  #firstrun p { font-size: 3vh; color: #b9c6e0; margin: 0 auto 3vh; max-width: 60vw; line-height: 1.35; }
  #firstrun .fr-row { display: flex; gap: 4vw; justify-content: center; align-items: center; }
  #firstrun .fr-qr { background: #fff; padding: 1.2vh; border-radius: 1.2vh; width: 26vh; height: 26vh; }
  #firstrun .fr-qr img { width: 100%; height: 100%; }
  #firstrun .fr-side { font-size: 2.4vh; color: #9fb0d0; max-width: 34vw; text-align: left; }
  #firstrun .fr-code { font-size: 7vh; letter-spacing: .5vw; color: #fff; font-weight: 700; }
  #firstrun .fr-meter { height: 2vh; width: 40vw; margin: 2vh auto; background: #10182a; border-radius: 1vh; overflow: hidden; }
  #firstrun .fr-meter div { height: 100%; width: 0; background: #8affc1; transition: width .15s; }
  #firstrun .fr-pass { font-size: 3vh; font-weight: 650; }
  #firstrun .fr-tour { text-align: left; font-size: 2.7vh; max-width: 60vw; margin: 0 auto; color: #e8eefc; }`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  const box = document.createElement("div"); box.id = "firstrun";
  box.innerHTML = `<div class="fr-dots"></div><h1></h1><p class="fr-sub"></p>
    <div class="fr-extra"></div>
    <div class="fr-row"><div class="fr-qr"><img alt=""></div>
      <div class="fr-side">Scan with your phone's camera.<br>Your phone needs the same Wi-Fi.</div></div>`;
  document.body.appendChild(box);
  const $ = (s) => box.querySelector(s);
  let last = null, qrLoaded = false, level = 0;

  function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function render(m) {
    last = m;
    box.style.display = m.active ? "block" : "none";
    if (!m.active) return;
    const cur = m.current;
    $(".fr-dots").innerHTML = (m.steps || []).map((s) =>
      `<div class="fr-dot ${s.status} ${s.id === cur ? "cur" : ""}" title="${esc(s.title)}">${s.n}</div>`).join("");
    const name = ((m.data || {}).welcome || {}).walter_name || "Walter";
    const [h, p] = WALL[cur] || ["", ""];
    $("h1").textContent = cur === "welcome" ? "Hi, I'm " + name + "!" : h;
    $(".fr-sub").textContent = p;
    let extra = "";
    const a = (m.data || {}).audio || {};
    if (cur === "audio") {
      extra = `<div class="fr-meter"><div style="width:${Math.min(100, level)}%"></div></div>` +
        (a.heard_wake ? `<div class="fr-pass" style="color:#8affc1">I heard my name!</div>` :
         a.mic_ok === false ? `<div class="fr-pass" style="color:#ff8a8a">Too quiet: try another mic or come closer.</div>` :
         a.mic_ok ? `<div class="fr-pass" style="color:#8affc1">Mic: OK</div>` : "");
    } else if (cur === "network") {
      const n = (m.data || {}).network || {};
      if (n.online !== undefined) extra = `<div class="fr-pass" style="color:${n.online ? "#8affc1" : "#ff8a8a"}">Internet: ${n.online ? "OK" : "not connected"}</div>`;
    } else if (cur === "pair" && m.pair) {
      extra = `<div class="fr-code">${esc(m.pair.code.slice(0, 3))} ${esc(m.pair.code.slice(3))}</div>`;
    } else if (cur === "hello" && m.tour) {
      extra = `<ul class="fr-tour">${m.tour.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
    }
    $(".fr-extra").innerHTML = extra;
    const img = $(".fr-qr img");
    if (cur === "pair" && m.pair && m.pair.link) {
      img.src = "/api/firstrun/qr.svg?which=pair&link=" + encodeURIComponent(m.pair.link);
      qrLoaded = false;
    } else if (!qrLoaded) {
      img.src = "/api/firstrun/qr.svg?which=phone&t=" + Date.now();
      qrLoaded = true;
    }
  }

  window.addEventListener("walter-msg", (e) => {
    const m = e.detail;
    if (m.t === "firstrun") render(m);
    else if (m.t === "hear" && last && last.active && last.current === "audio") {
      level = Math.round(Math.min(1, (m.v || 0) * 4) * 100);
      const bar = box.querySelector(".fr-meter div");
      if (bar) bar.style.width = Math.min(100, level) + "%";
    }
  });
})();
