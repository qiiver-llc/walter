/* First-time setup overlay: a QR code for the phone page, or (Enter, with a
   keyboard plugged in) Claude Code's own sign-in window on this screen.
   Walter never sees the Claude password or login code: nothing is typed here. */
(() => {
  "use strict";
  const box = document.getElementById("setup");
  const phone = document.getElementById("setupPhone");
  const keys = document.getElementById("setupKeys");
  const msg = document.getElementById("setupMsg");
  let active = false;

  function show(needed, error) {
    active = needed;
    box.style.display = needed ? "block" : "none";
    if (needed && keys.style.display === "none") {
      document.getElementById("qrPhone").src = "/api/setup/qr.svg?which=phone&t=" + Date.now();
      phone.style.display = "block";
    }
    if (needed && error) { msg.style.color = "#ff8a8a"; msg.textContent = error; }
    document.body.style.cursor = "none";
  }

  async function post(action, body) {
    const r = await fetch("/api/setup/" + action, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return r.json();
  }

  window.addEventListener("walter-msg", (e) => { if (e.detail.t === "setup") show(!!e.detail.needed, e.detail.error); });

  window.addEventListener("keydown", async (e) => {
    if (!active) return;
    if (e.key === "Escape") { keys.style.display = "none"; show(true); return; }
    if (e.key === "Enter") {
      phone.style.display = "none"; keys.style.display = "block";
      msg.style.color = "#9fb0d0"; msg.textContent = "Opening Claude's sign-in…";
      const r = await post("signin", { from: "wall" });
      msg.style.color = r.ok ? "#8affc1" : "#ff8a8a";
      msg.textContent = r.message || "";
    }
  });
})();
