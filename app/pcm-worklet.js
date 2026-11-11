// Microphone -> 16 kHz mono int16 chunks (~100 ms) for Walter's ears.
class PcmDown extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.out = new Int16Array(1600);
    this.n = 0;
    this.carry = new Float32Array(0);
    this.port.onmessage = (e) => { if (e.data === "flush") this.flush(); };
  }
  flush() {
    if (this.n) this.port.postMessage(this.out.slice(0, this.n).buffer);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const buf = new Float32Array(this.carry.length + ch.length);
    buf.set(this.carry); buf.set(ch, this.carry.length);
    let p = this.pos;
    while (p + this.ratio <= buf.length) {
      // average the source samples that fall in this output sample (cheap low-pass)
      const a = Math.floor(p), b = Math.min(buf.length, Math.floor(p + this.ratio));
      let s = 0;
      for (let i = a; i < b; i++) s += buf[i];
      s /= Math.max(1, b - a);
      this.out[this.n++] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
      if (this.n === this.out.length) this.flush();
      p += this.ratio;
    }
    const keep = Math.floor(p);
    this.carry = buf.slice(keep);
    this.pos = p - keep;
    let peak = 0;
    for (let i = 0; i < ch.length; i += 8) peak = Math.max(peak, Math.abs(ch[i]));
    this.port.postMessage({ level: peak });
    return true;
  }
}
registerProcessor("pcm-down", PcmDown);
