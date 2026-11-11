// A small live Walter: the glowing blob and two big eyes, coloured by mood.
// Light on purpose (the wall's face/walter.js is built for a big kiosk screen).
(function () {
  const MOODS = {
    neutral: [88, 196, 255], happy: [255, 205, 86], excited: [255, 128, 92], love: [255, 120, 180],
    sad: [70, 110, 220], angry: [255, 72, 72], surprised: [140, 235, 255], confused: [170, 120, 255],
    thinking: [130, 120, 255], curious: [80, 230, 190], sleepy: [60, 90, 170], proud: [255, 190, 70],
    scared: [190, 180, 255], embarrassed: [255, 130, 150], mischievous: [170, 230, 90],
    wink: [255, 200, 110], calm: [110, 200, 210],
  };
  const lerp = (a, b, t) => a + (b - a) * t;

  class MiniFace {
    constructor(canvas) {
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.mood = "neutral"; this.state = "idle"; this.level = 0; this.connected = false;
      this.col = MOODS.neutral.slice(); this.lid = 0; this.blinkAt = performance.now() + 2500;
      this.moodUntil = 0; this.hop = 0;
      requestAnimationFrame((t) => this.frame(t));
    }
    static color(name) { return MOODS[name] || MOODS.neutral; }
    setMood(name, hold = 20) {
      if (!MOODS[name]) return;
      this.mood = name; this.moodUntil = performance.now() + hold * 1000;
      if (["happy", "excited", "proud", "surprised"].includes(name)) this.hop = 1;
    }
    setState(s) { this.state = s; }
    setLevel(v) { this.level = v; }
    frame(t) {
      const { c, ctx } = this;
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (t > this.moodUntil && this.mood !== "neutral") this.mood = "neutral";
      let target = MOODS[this.mood].slice();
      if (this.state === "thinking" || this.state === "working") target = target.map((v, i) => lerp(v, MOODS.thinking[i], 0.5));
      const asleep = this.state === "sleeping";
      if (asleep) target = target.map((v, i) => lerp(v, [50, 70, 140][i], 0.7));
      if (!this.connected) target = target.map((v) => lerp(v, 120, 0.75));
      this.col = this.col.map((v, i) => lerp(v, target[i], 0.08));
      // blink
      if (t > this.blinkAt) { this.blinkAt = t + 2500 + Math.random() * 3500; this.blinkT = t; }
      const bk = this.blinkT ? Math.max(0, 1 - Math.abs(t - this.blinkT - 90) / 90) : 0;
      const lidTarget = asleep ? 0.85 : (!this.connected ? 0.35 : this.state === "thinking" ? 0.2 : 0);
      this.lid = lerp(this.lid, Math.max(lidTarget, bk), 0.3);
      this.hop = Math.max(0, this.hop - 0.03);
      const breathe = Math.sin(t / (asleep ? 1400 : 700)) * 0.02;
      const talk = this.level * 0.08;
      const r = Math.min(w, h) * 0.36;
      const cx = w / 2, cy = h / 2 + r * 0.06 - Math.sin(this.hop * Math.PI) * r * 0.25;
      const sx = 1 + breathe + talk * 0.5, sy = 1 - breathe + talk;
      const [R, G, B] = this.col.map(Math.round);
      const glow = ctx.createRadialGradient(cx, cy, r * 0.6, cx, cy, r * 1.5);
      glow.addColorStop(0, `rgba(${R},${G},${B},0.35)`); glow.addColorStop(1, `rgba(${R},${G},${B},0)`);
      ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
      const body = ctx.createLinearGradient(0, cy - r, 0, cy + r);
      body.addColorStop(0, `rgb(${Math.min(255, R + 25)},${Math.min(255, G + 25)},${Math.min(255, B + 25)})`);
      body.addColorStop(1, `rgb(${Math.round(R * 0.8)},${Math.round(G * 0.8)},${Math.round(B * 0.8)})`);
      ctx.fillStyle = body;
      ctx.beginPath(); ctx.ellipse(cx, cy, r * sx, r * 0.95 * sy, 0, 0, Math.PI * 2); ctx.fill();
      // eyes
      const look = (this.state === "thinking" || this.state === "working") ? -0.25 : 0;
      for (const side of [-1, 1]) {
        const ex = cx + side * r * 0.36, ey = cy - r * 0.1 + look * r * 0.3;
        const erx = r * 0.2, ery = r * 0.3 * (1 - this.lid * 0.92);
        ctx.fillStyle = "#fafcff";
        ctx.beginPath(); ctx.ellipse(ex, ey + r * 0.3 * this.lid * 0.5, erx, Math.max(1, ery), 0, 0, Math.PI * 2); ctx.fill();
        if (ery > r * 0.08) {
          ctx.fillStyle = "#0c1628";
          ctx.beginPath(); ctx.ellipse(ex + r * 0.02, ey - r * 0.02 + look * r * 0.15 + r * 0.15 * this.lid,
            erx * 0.55, Math.min(ery * 0.55, r * 0.16), 0, 0, Math.PI * 2); ctx.fill();
        }
      }
      requestAnimationFrame((tt) => this.frame(tt));
    }
  }
  window.MiniFace = MiniFace;
})();
