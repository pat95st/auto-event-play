/* Channel EQ editor: response curve with draggable nodes + numeric controls.
   Coefficient formulas mirror autoeventplay/domain/eq.py (RBJ cookbook). */
(function () {
  "use strict";
  const AEP = window.AEP;
  const FS = 48000, FMIN = 20, FMAX = 20000, GMAX = 18;
  const BUTTER_Q = { 12: [0.70710678], 18: [1.0], 24: [0.5411961, 1.30656296] };
  const TYPE_LABEL = { peak: "Glocke", low_shelf: "Low-Shelf", high_shelf: "High-Shelf" };
  const BAND_COLORS = ["#ff6b6b", "#ffc53d", "#36cf6f", "#3fa9f5"];

  function biquad(kind, f, q, gain) {
    f = Math.min(f, FS * 0.49);
    const w0 = 2 * Math.PI * f / FS, cw = Math.cos(w0), sw = Math.sin(w0), alpha = sw / (2 * q), A = Math.pow(10, gain / 40);
    let b0, b1, b2, a0, a1, a2;
    if (kind === "peak") { b0 = 1 + alpha * A; b1 = -2 * cw; b2 = 1 - alpha * A; a0 = 1 + alpha / A; a1 = -2 * cw; a2 = 1 - alpha / A; }
    else if (kind === "low_shelf") { const sa = 2 * Math.sqrt(A) * alpha;
      b0 = A * ((A + 1) - (A - 1) * cw + sa); b1 = 2 * A * ((A - 1) - (A + 1) * cw); b2 = A * ((A + 1) - (A - 1) * cw - sa);
      a0 = (A + 1) + (A - 1) * cw + sa; a1 = -2 * ((A - 1) + (A + 1) * cw); a2 = (A + 1) + (A - 1) * cw - sa; }
    else if (kind === "high_shelf") { const sa = 2 * Math.sqrt(A) * alpha;
      b0 = A * ((A + 1) + (A - 1) * cw + sa); b1 = -2 * A * ((A - 1) + (A + 1) * cw); b2 = A * ((A + 1) + (A - 1) * cw - sa);
      a0 = (A + 1) - (A - 1) * cw + sa; a1 = 2 * ((A - 1) - (A + 1) * cw); a2 = (A + 1) - (A - 1) * cw - sa; }
    else if (kind === "highpass") { b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2; a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; }
    else { b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2; a0 = 1 + alpha; a1 = -2 * cw; a2 = 1 - alpha; }
    return [b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0];
  }
  function firstOrder(kind, f) {
    const k = Math.tan(Math.PI * Math.min(f, FS * 0.49) / FS);
    const a1 = (k - 1) / (k + 1);
    return kind === "highpass" ? [1 / (1 + k), -1 / (1 + k), 0, a1, 0] : [k / (1 + k), k / (1 + k), 0, a1, 0];
  }
  function cutSections(kind, f, slope) {
    const s = (BUTTER_Q[slope] || BUTTER_Q[12]).map((q) => biquad(kind, f, q, 0));
    if (slope === 18) s.push(firstOrder(kind, f));
    return s;
  }
  function magDb(sections, f) {
    const w = 2 * Math.PI * f / FS;
    const c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
    let db = 0;
    for (const [b0, b1, b2, a1, a2] of sections) {
      const nr = b0 + b1 * c1 + b2 * c2, ni = -(b1 * s1 + b2 * s2);
      const dr = 1 + a1 * c1 + a2 * c2, di = -(a1 * s1 + a2 * s2);
      db += 10 * Math.log10((nr * nr + ni * ni) / (dr * dr + di * di) + 1e-30);
    }
    return db;
  }
  function sectionsFor(eq, onlyBand) {
    const out = [];
    if (onlyBand === undefined || onlyBand === "lc") if (eq.low_cut.on) out.push(...cutSections("highpass", eq.low_cut.freq, eq.low_cut.slope));
    eq.bands.forEach((b, i) => { if ((onlyBand === undefined || onlyBand === i) && b.on && Math.abs(b.gain) >= 0.01) out.push(biquad(b.type, b.freq, b.q, b.gain)); });
    if (onlyBand === undefined || onlyBand === "hc") if (eq.high_cut.on) out.push(...cutSections("lowpass", eq.high_cut.freq, eq.high_cut.slope));
    return out;
  }
  AEP.eqIsActive = function (eq) {
    if (!eq || !eq.enabled) return false;
    return eq.low_cut.on || eq.high_cut.on || eq.bands.some((b) => b.on && Math.abs(b.gain) >= 0.01);
  };

  // ------------------------------------------------------------------ geometry
  const fToX = (f, w) => (Math.log10(f / FMIN) / Math.log10(FMAX / FMIN)) * w;
  const xToF = (x, w) => FMIN * Math.pow(FMAX / FMIN, Math.max(0, Math.min(1, x / w)));
  const gToY = (g, h) => h / 2 - (g / (GMAX + 2)) * (h / 2);
  const yToG = (y, h) => -((y - h / 2) / (h / 2)) * (GMAX + 2);
  const fmtF = (f) => f >= 1000 ? (f / 1000).toFixed(f >= 10000 ? 1 : 2).replace(/\.?0+$/, "") + " k" : Math.round(f) + "";

  // ------------------------------------------------------------------ editor
  let editor = null;

  AEP.openEq = function (opts) {
    if (editor) editor.close();
    editor = new EqEditor(opts);
  };

  class EqEditor {
    constructor(opts) {
      this.opts = opts;
      this.eq = JSON.parse(JSON.stringify(opts.eq));
      this.root = document.createElement("div");
      this.root.className = "eq-overlay";
      this.root.innerHTML = `
        <div class="eq-panel aep-panel" role="dialog" aria-label="Equalizer ${AEP.esc(opts.name)}">
          <div class="aep-panel-head">
            <span>EQ · <span class="text-white">${AEP.esc(opts.name)}</span></span>
            <div class="d-flex gap-2 align-items-center" style="text-transform:none;letter-spacing:0">
              <button class="strip-btn eq-in" style="width:auto;padding:4px 12px">EQ IN</button>
              <button class="btn btn-sm btn-console" data-act="flat" title="Alle Bänder auf 0 dB, Filter aus">Flat</button>
              <button class="btn-close btn-close-white btn-sm" data-act="close" aria-label="Schließen"></button>
            </div>
          </div>
          <div class="aep-panel-body">
            <div class="eq-graph"><canvas></canvas></div>
            <div class="eq-hint text-dim small my-2">Punkte ziehen: Frequenz/Gain · Mausrad auf einem Punkt: Güte (Q) bzw. Flankensteilheit · Doppelklick: Band an/aus</div>
            <div class="eq-controls"></div>
          </div>
        </div>`;
      document.body.appendChild(this.root);
      this.canvas = this.root.querySelector("canvas");
      this.root.addEventListener("click", (e) => { if (e.target === this.root) this.close(); });
      this.root.querySelector('[data-act="close"]').addEventListener("click", () => this.close());
      this.root.querySelector('[data-act="flat"]').addEventListener("click", () => {
        this.eq.low_cut.on = false; this.eq.high_cut.on = false;
        this.eq.bands.forEach((b) => { b.gain = 0; });
        this.changed(true);
      });
      this.inBtn = this.root.querySelector(".eq-in");
      this.inBtn.addEventListener("click", () => { this.eq.enabled = !this.eq.enabled; this.changed(true); });
      this.keyHandler = (e) => { if (e.key === "Escape") this.close(); };
      document.addEventListener("keydown", this.keyHandler);
      this.buildControls();
      this.bindGraph();
      this.resize = () => this.draw();
      window.addEventListener("resize", this.resize);
      this.changed(false);
    }

    close() {
      clearTimeout(this.timer);
      if (this.dirty) this.send();
      document.removeEventListener("keydown", this.keyHandler);
      window.removeEventListener("resize", this.resize);
      this.root.remove();
      if (editor === this) editor = null;
    }

    changed(send) {
      this.inBtn.classList.toggle("on", this.eq.enabled);
      this.root.querySelector(".eq-panel").classList.toggle("eq-bypassed", !this.eq.enabled);
      this.syncControls();
      this.draw();
      if (send) {
        this.dirty = true;
        clearTimeout(this.timer);
        const now = performance.now();
        if (!this.lastSent || now - this.lastSent > 100) this.send(); else this.timer = setTimeout(() => this.send(), 100);
      }
    }

    async send() {
      this.dirty = false;
      this.lastSent = performance.now();
      try {
        const r = await AEP.api(this.opts.endpoint, { eq: this.eq });
        if (this.opts.onSaved) this.opts.onSaved(r.eq, r.active);
      } catch (e) { AEP.toast(e.message, "err"); }
    }

    // -------------------------------------------------------------- numeric controls
    buildControls() {
      const c = this.root.querySelector(".eq-controls");
      const cut = (key, label) => `
        <div class="eq-col" data-col="${key}">
          <div class="eq-col-head"><button class="strip-btn eq-on" data-k="on">${label}</button></div>
          <label class="form-label">Frequenz Hz</label><input class="form-control form-control-sm" data-k="freq" type="number" min="20" max="20000" step="1">
          <label class="form-label">Steilheit</label><select class="form-select form-select-sm" data-k="slope"><option value="12">12 dB/Okt</option><option value="18">18 dB/Okt</option><option value="24">24 dB/Okt</option></select>
        </div>`;
      const band = (i) => `
        <div class="eq-col" data-col="${i}" style="--band-color:${BAND_COLORS[i]}">
          <div class="eq-col-head"><button class="strip-btn eq-on" data-k="on">Band ${i + 1}</button></div>
          <label class="form-label">Typ</label><select class="form-select form-select-sm" data-k="type">${Object.entries(TYPE_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
          <label class="form-label">Frequenz Hz</label><input class="form-control form-control-sm" data-k="freq" type="number" min="20" max="20000" step="1">
          <label class="form-label">Gain dB</label><input class="form-control form-control-sm" data-k="gain" type="number" min="-18" max="18" step="0.5">
          <label class="form-label">Q</label><input class="form-control form-control-sm" data-k="q" type="number" min="0.1" max="16" step="0.1">
        </div>`;
      c.innerHTML = cut("low_cut", "Low-Cut") + [0, 1, 2, 3].map(band).join("") + cut("high_cut", "High-Cut");
      c.querySelectorAll(".eq-col").forEach((col) => {
        const key = col.dataset.col;
        const target = () => (key === "low_cut" || key === "high_cut") ? this.eq[key] : this.eq.bands[+key];
        col.querySelector('[data-k="on"]').addEventListener("click", () => { target().on = !target().on; this.changed(true); });
        col.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", () => {
          const k = el.dataset.k;
          let v = el.tagName === "SELECT" && k !== "slope" ? el.value : parseFloat(el.value);
          if (typeof v === "number" && isNaN(v)) return;
          if (k === "freq") v = Math.max(20, Math.min(20000, v));
          if (k === "gain") v = Math.max(-18, Math.min(18, v));
          if (k === "q") v = Math.max(0.1, Math.min(16, v));
          if (k === "slope") v = parseInt(el.value, 10);
          target()[k] = v;
          if (k === "freq" && (key === "low_cut" || key === "high_cut")) target().on = true;
          this.changed(true);
        }));
      });
    }

    syncControls() {
      this.root.querySelectorAll(".eq-col").forEach((col) => {
        const key = col.dataset.col;
        const t = (key === "low_cut" || key === "high_cut") ? this.eq[key] : this.eq.bands[+key];
        col.querySelector('[data-k="on"]').classList.toggle("on", t.on);
        col.classList.toggle("off", !t.on);
        col.querySelectorAll("input, select").forEach((el) => {
          if (document.activeElement === el) return;
          const v = t[el.dataset.k];
          el.value = el.dataset.k === "freq" ? Math.round(v) : (el.dataset.k === "q" ? (+v).toFixed(2) : v);
        });
      });
    }

    // -------------------------------------------------------------- graph
    nodes(w, h) {
      const out = [];
      const e = this.eq;
      out.push({ id: "lc", x: fToX(e.low_cut.freq, w), y: gToY(0, h) + 0, on: e.low_cut.on, label: "L", color: "#9aa4b1" });
      e.bands.forEach((b, i) => out.push({ id: i, x: fToX(b.freq, w), y: gToY(b.gain, h), on: b.on, label: String(i + 1), color: BAND_COLORS[i] }));
      out.push({ id: "hc", x: fToX(e.high_cut.freq, w), y: gToY(0, h), on: e.high_cut.on, label: "H", color: "#9aa4b1" });
      return out;
    }

    draw() {
      const c = this.canvas, wrap = c.parentElement;
      const dpr = window.devicePixelRatio || 1;
      const W = wrap.clientWidth, H = wrap.clientHeight;
      c.width = W * dpr; c.height = H * dpr;
      const g = c.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, W, H);
      // grid
      g.font = "10px DejaVu Sans Mono, monospace";
      g.lineWidth = 1;
      [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach((f) => {
        const x = Math.round(fToX(f, W)) + 0.5;
        g.strokeStyle = "rgba(255,255,255,0.07)"; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
        g.fillStyle = "#5d6570"; g.fillText(fmtF(f), Math.min(W - 26, x + 3), H - 4);
      });
      [-18, -12, -6, 0, 6, 12, 18].forEach((db) => {
        const y = Math.round(gToY(db, H)) + 0.5;
        g.strokeStyle = db === 0 ? "rgba(255,255,255,0.22)" : "rgba(255,255,255,0.07)";
        g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke();
        g.fillStyle = "#5d6570"; g.fillText((db > 0 ? "+" : "") + db, 3, y - 3);
      });
      const active = this.eq.enabled;
      // individual band curves
      this.eq.bands.forEach((b, i) => {
        if (!b.on || Math.abs(b.gain) < 0.01) return;
        const secs = sectionsFor(this.eq, i);
        g.beginPath();
        for (let x = 0; x <= W; x += 2) { const y = gToY(magDb(secs, xToF(x, W)), H); x === 0 ? g.moveTo(x, y) : g.lineTo(x, y); }
        g.strokeStyle = BAND_COLORS[i] + (active ? "66" : "22"); g.lineWidth = 1; g.stroke();
      });
      // total curve
      const secs = sectionsFor(this.eq);
      g.beginPath();
      for (let x = 0; x <= W; x += 1) { const y = Math.max(-5, Math.min(H + 5, gToY(magDb(secs, xToF(x, W)), H))); x === 0 ? g.moveTo(x, y) : g.lineTo(x, y); }
      g.strokeStyle = active ? "#9be7ff" : "#4d545e"; g.lineWidth = 2.2; g.stroke();
      g.lineTo(W, gToY(0, H)); g.lineTo(0, gToY(0, H)); g.closePath();
      g.fillStyle = active ? "rgba(47,180,232,0.12)" : "rgba(255,255,255,0.03)"; g.fill();
      // nodes
      this.nodes(W, H).forEach((n) => {
        g.beginPath(); g.arc(n.x, n.y, 9, 0, Math.PI * 2);
        g.fillStyle = n.on ? n.color : "#2a2f36"; g.fill();
        g.lineWidth = this.drag && this.drag.id === n.id ? 3 : 1.5; g.strokeStyle = n.on ? "#fff" : n.color; g.stroke();
        g.fillStyle = n.on ? "#111" : n.color; g.font = "bold 10px sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText(n.label, n.x, n.y + 0.5); g.textAlign = "start"; g.textBaseline = "alphabetic";
      });
      if (this.drag) {
        const t = this.target(this.drag.id);
        const txt = typeof this.drag.id === "number" ? `${fmtF(t.freq)} Hz  ${t.gain > 0 ? "+" : ""}${t.gain.toFixed(1)} dB  Q ${t.q.toFixed(2)}` : `${fmtF(t.freq)} Hz  ${t.slope} dB/Okt`;
        g.fillStyle = "#e8edf2"; g.font = "12px DejaVu Sans Mono, monospace"; g.fillText(txt, W - g.measureText(txt).width - 8, 16);
      }
    }

    target(id) { return id === "lc" ? this.eq.low_cut : id === "hc" ? this.eq.high_cut : this.eq.bands[id]; }

    hit(e) {
      const r = this.canvas.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      let best = null, bestD = 18;
      this.nodes(r.width, r.height).forEach((n) => { const d = Math.hypot(n.x - x, n.y - y); if (d < bestD) { best = n; bestD = d; } });
      return best;
    }

    bindGraph() {
      const c = this.canvas;
      c.addEventListener("pointerdown", (e) => {
        const n = this.hit(e);
        if (!n) return;
        e.preventDefault();
        this.drag = { id: n.id };
        c.setPointerCapture(e.pointerId);
        const t = this.target(n.id);
        if (!t.on && typeof n.id !== "number") t.on = true;
        this.draw();
      });
      c.addEventListener("pointermove", (e) => {
        if (!this.drag) { c.style.cursor = this.hit(e) ? "grab" : "default"; return; }
        const r = c.getBoundingClientRect();
        const t = this.target(this.drag.id);
        t.freq = Math.round(Math.max(20, Math.min(20000, xToF(e.clientX - r.left, r.width))));
        if (typeof this.drag.id === "number") {
          let gain = yToG(e.clientY - r.top, r.height);
          gain = Math.max(-18, Math.min(18, Math.round(gain * 2) / 2));
          t.gain = Math.abs(gain) < 0.25 ? 0 : gain;
          t.on = true;
        }
        this.changed(true);
      });
      const end = () => { if (this.drag) { this.drag = null; this.changed(true); } };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
      c.addEventListener("dblclick", (e) => { const n = this.hit(e); if (n) { const t = this.target(n.id); t.on = !t.on; this.changed(true); } });
      c.addEventListener("wheel", (e) => {
        const n = this.hit(e);
        if (!n) return;
        e.preventDefault();
        const t = this.target(n.id);
        if (typeof n.id === "number") {
          const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
          t.q = Math.max(0.1, Math.min(16, Math.round(t.q * factor * 100) / 100));
        } else {
          const order = [12, 18, 24];
          const idx = order.indexOf(t.slope) + (e.deltaY < 0 ? 1 : -1);
          t.slope = order[Math.max(0, Math.min(2, idx))];
          t.on = true;
        }
        this.drag = this.drag || null;
        this.changed(true);
      }, { passive: false });
    }
  }
})();
