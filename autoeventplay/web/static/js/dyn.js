/* Channel compressor editor: static transfer curve, rotary-style numeric controls and a live gain-reduction meter.
   The curve mirrors autoeventplay/domain/dynamics.py (soft knee). */
(function () {
  "use strict";
  const AEP = window.AEP;
  const T = AEP.t;
  const RANGE = 60; // graph shows -60..0 dB in and out

  const PARAMS = [
    { key: "threshold", label: "Threshold", unit: "dB", min: -60, max: 0, step: 0.5 },
    { key: "ratio", label: "Ratio", unit: ": 1", min: 1, max: 20, step: 0.1 },
    { key: "attack", label: "Attack", unit: "ms", min: 0.1, max: 200, step: 0.1 },
    { key: "release", label: "Release", unit: "ms", min: 10, max: 3000, step: 10 },
    { key: "knee", label: "Knee", unit: "dB", min: 0, max: 24, step: 0.5 },
    { key: "makeup", label: "Make-up", unit: "dB", min: 0, max: 24, step: 0.5 },
  ];
  const PRESETS = {
    speech: { threshold: -24, ratio: 3, attack: 8, release: 120, knee: 6, makeup: 4 },
    music: { threshold: -18, ratio: 2, attack: 25, release: 250, knee: 10, makeup: 2 },
    limiter: { threshold: -6, ratio: 20, attack: 0.5, release: 80, knee: 0, makeup: 0 },
  };

  AEP.staticGainDb = function (x, d) {
    const t = d.threshold, r = d.ratio, w = d.knee, slope = 1 / r - 1, over = x - t;
    if (w > 0 && Math.abs(over) <= w / 2) return slope * Math.pow(over + w / 2, 2) / (2 * w);
    return over > 0 ? slope * over : 0;
  };

  let editor = null;
  let grListener = false;
  AEP.openDyn = function (opts) {
    if (editor) editor.close();
    editor = new DynEditor(opts);
    if (!grListener) {
      grListener = true;
      AEP.on("meters", (m) => { if (editor) editor.meters(m); });
    }
  };

  class DynEditor {
    constructor(opts) {
      this.opts = opts;
      this.dyn = JSON.parse(JSON.stringify(opts.dyn || {}));
      this.gr = 0; this.inLevel = -90;
      this.root = document.createElement("div");
      this.root.className = "eq-overlay";
      this.root.innerHTML = `
        <div class="eq-panel dyn-panel aep-panel" role="dialog" aria-label="${AEP.esc(T("Compressor"))} ${AEP.esc(opts.name)}">
          <div class="aep-panel-head">
            <span>${AEP.esc(T("Compressor"))} · <span class="text-white">${AEP.esc(opts.name)}</span></span>
            <div class="d-flex gap-2 align-items-center" style="text-transform:none;letter-spacing:0">
              <select class="form-select form-select-sm w-auto" data-act="preset" aria-label="${AEP.esc(T("Preset"))}">
                <option value="">${AEP.esc(T("Preset …"))}</option>
                <option value="speech">${AEP.esc(T("Speech"))}</option>
                <option value="music">${AEP.esc(T("Music"))}</option>
                <option value="limiter">${AEP.esc(T("Limiter"))}</option>
              </select>
              <button class="strip-btn dyn-in" style="width:auto;padding:4px 12px">COMP IN</button>
              <button class="btn-close btn-close-white btn-sm" data-act="close" aria-label="${AEP.esc(T("Close"))}"></button>
            </div>
          </div>
          <div class="aep-panel-body">
            <div class="dyn-layout">
              <div class="dyn-graph"><canvas></canvas></div>
              <div class="dyn-gr" title="${AEP.esc(T("Gain reduction"))}"><div class="dyn-gr-bar"><div class="dyn-gr-fill"></div></div><div class="dyn-gr-label">GR</div><div class="dyn-gr-val mono">0.0</div></div>
              <div class="dyn-controls"></div>
            </div>
            <div class="eq-hint text-dim small mt-2">${AEP.esc(T("Drag on the graph: threshold · mouse wheel on the graph: ratio · mouse wheel on a field: value (Shift = fine)"))}</div>
          </div>
        </div>`;
      document.body.appendChild(this.root);
      this.canvas = this.root.querySelector("canvas");
      this.grFill = this.root.querySelector(".dyn-gr-fill");
      this.grVal = this.root.querySelector(".dyn-gr-val");
      this.root.addEventListener("click", (e) => { if (e.target === this.root) this.close(); });
      this.root.querySelector('[data-act="close"]').addEventListener("click", () => this.close());
      this.root.querySelector('[data-act="preset"]').addEventListener("change", (e) => {
        const p = PRESETS[e.target.value];
        if (p) { Object.assign(this.dyn, p); this.dyn.enabled = true; this.changed(true); }
        e.target.value = "";
      });
      this.inBtn = this.root.querySelector(".dyn-in");
      this.inBtn.addEventListener("click", () => { this.dyn.enabled = !this.dyn.enabled; this.changed(true); });
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
      this.inBtn.classList.toggle("on", !!this.dyn.enabled);
      this.root.querySelector(".dyn-panel").classList.toggle("eq-bypassed", !this.dyn.enabled);
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
        const r = await AEP.api(this.opts.endpoint, { dyn: this.dyn });
        if (this.opts.onSaved) this.opts.onSaved(r.dyn, r.active);
      } catch (e) { AEP.toast(e.message, "err"); }
    }

    meters(m) {
      const gr = (m.gr && m.gr[this.opts.grKey]) || 0;
      const key = this.opts.grKey.split(":");
      const lvl = key[0] === "in" ? (m.inputs || {})[key[1]] : (m.outputs || {})[key[1]];
      this.gr = gr;
      this.inLevel = lvl == null ? -90 : lvl;
      const v = Math.max(0, Math.min(1, -gr / 20));
      this.grFill.style.height = (v * 100) + "%";
      this.grVal.textContent = (gr < -0.05 ? gr.toFixed(1) : "0.0");
      this.draw();
    }

    // -------------------------------------------------------------- numeric controls
    buildControls() {
      const c = this.root.querySelector(".dyn-controls");
      c.innerHTML = PARAMS.map((p) => `
        <div class="dyn-field">
          <label class="form-label" for="dyn-${p.key}">${AEP.esc(T(p.label))}</label>
          <div class="input-group input-group-sm">
            <input class="form-control mono" id="dyn-${p.key}" data-k="${p.key}" type="number" min="${p.min}" max="${p.max}" step="${p.step}">
            <span class="input-group-text">${p.unit}</span>
          </div>
          <input class="form-range" data-r="${p.key}" type="range" min="0" max="1000" aria-label="${AEP.esc(T(p.label))}">
        </div>`).join("");
      PARAMS.forEach((p) => {
        const num = c.querySelector(`[data-k="${p.key}"]`), rng = c.querySelector(`[data-r="${p.key}"]`);
        const log = p.key === "attack" || p.key === "release" || p.key === "ratio";
        const toR = (v) => log ? Math.round(Math.log(v / p.min) / Math.log(p.max / p.min) * 1000) : Math.round((v - p.min) / (p.max - p.min) * 1000);
        const fromR = (r) => log ? p.min * Math.pow(p.max / p.min, r / 1000) : p.min + (p.max - p.min) * r / 1000;
        const set = (v) => {
          if (isNaN(v)) return;
          v = Math.max(p.min, Math.min(p.max, v));
          this.dyn[p.key] = Math.round(v / p.step) * p.step;
          this.dyn[p.key] = +this.dyn[p.key].toFixed(2);
          this.changed(true);
        };
        p._sync = () => {
          if (document.activeElement !== num) num.value = this.dyn[p.key];
          rng.value = toR(this.dyn[p.key]);
        };
        num.addEventListener("change", () => set(parseFloat(num.value)));
        rng.addEventListener("input", () => set(fromR(+rng.value)));
        num.addEventListener("wheel", (e) => {
          e.preventDefault();
          const step = p.step * (e.shiftKey ? 1 : (p.key === "release" ? 5 : 2));
          set(this.dyn[p.key] + (e.deltaY < 0 ? step : -step));
        }, { passive: false });
      });
    }

    syncControls() { PARAMS.forEach((p) => p._sync && p._sync()); }

    // -------------------------------------------------------------- graph
    geom() {
      const r = this.canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      return { w: r.width, h: r.height, dpr, pad: 28 };
    }
    toX(db, g) { return g.pad + (db + RANGE) / RANGE * (g.w - g.pad - 8); }
    toY(db, g) { return 8 + (1 - (db + RANGE) / RANGE) * (g.h - g.pad - 8); }
    fromX(x, g) { return (x - g.pad) / (g.w - g.pad - 8) * RANGE - RANGE; }

    draw() {
      const g = this.geom();
      const c = this.canvas;
      if (c.width !== Math.round(g.w * g.dpr) || c.height !== Math.round(g.h * g.dpr)) { c.width = Math.round(g.w * g.dpr); c.height = Math.round(g.h * g.dpr); }
      const ctx = c.getContext("2d");
      ctx.setTransform(g.dpr, 0, 0, g.dpr, 0, 0);
      ctx.clearRect(0, 0, g.w, g.h);
      ctx.font = "9px sans-serif";
      ctx.fillStyle = "#6c7480";
      for (let db = -60; db <= 0; db += 12) {
        const x = this.toX(db, g), y = this.toY(db, g);
        ctx.strokeStyle = db === 0 ? "rgba(255,255,255,0.18)" : "rgba(255,255,255,0.07)";
        ctx.beginPath(); ctx.moveTo(x, 8); ctx.lineTo(x, g.h - g.pad); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(g.pad, y); ctx.lineTo(g.w - 8, y); ctx.stroke();
        ctx.textAlign = "center"; ctx.fillText(String(db), x, g.h - g.pad + 12);
        ctx.textAlign = "right"; ctx.fillText(String(db), g.pad - 4, y + 3);
      }
      ctx.textAlign = "center"; ctx.fillText(T("Input dB"), (g.w + g.pad) / 2, g.h - 4);
      // unity line
      ctx.setLineDash([3, 4]); ctx.strokeStyle = "rgba(255,255,255,0.2)";
      ctx.beginPath(); ctx.moveTo(this.toX(-60, g), this.toY(-60, g)); ctx.lineTo(this.toX(0, g), this.toY(0, g)); ctx.stroke();
      ctx.setLineDash([]);
      // curve (incl. make-up)
      const d = this.dyn, active = !!d.enabled;
      ctx.strokeStyle = active ? "#f5a524" : "#8a8f98"; ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i <= 240; i++) {
        const x = -60 + i * 60 / 240;
        const y = Math.min(0 + d.makeup, x + AEP.staticGainDb(x, d) + d.makeup);
        const px = this.toX(x, g), py = this.toY(Math.max(-60, Math.min(0, y)), g);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.stroke(); ctx.lineWidth = 1;
      // threshold handle
      const tx = this.toX(d.threshold, g), ty = this.toY(Math.min(0, d.threshold + d.makeup), g);
      ctx.fillStyle = active ? "#f5a524" : "#8a8f98";
      ctx.beginPath(); ctx.arc(tx, ty, 6, 0, Math.PI * 2); ctx.fill();
      // live level dot
      if (this.inLevel > -60) {
        const x = Math.min(0, this.inLevel);
        const y = x + (active ? AEP.staticGainDb(x, d) + d.makeup : 0);
        ctx.fillStyle = "#36cf6f";
        ctx.beginPath(); ctx.arc(this.toX(x, g), this.toY(Math.max(-60, Math.min(0, y)), g), 4, 0, Math.PI * 2); ctx.fill();
      }
      if (this.dragging) {
        ctx.fillStyle = "#e8edf2"; ctx.textAlign = "left";
        ctx.fillText(`${T("Threshold")} ${d.threshold.toFixed(1)} dB`, g.pad + 8, 20);
      }
      this._handle = { x: tx, y: ty };
    }

    bindGraph() {
      const c = this.canvas;
      c.addEventListener("pointerdown", (e) => {
        const r = c.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
        if (!this._handle || Math.hypot(x - this._handle.x, y - this._handle.y) > 14) {
          // click elsewhere: jump threshold
          this.dyn.threshold = Math.round(Math.max(-60, Math.min(0, this.fromX(x, this.geom()))) * 2) / 2;
        }
        this.dragging = true;
        c.setPointerCapture(e.pointerId);
        this.changed(true);
      });
      c.addEventListener("pointermove", (e) => {
        if (!this.dragging) return;
        const r = c.getBoundingClientRect();
        this.dyn.threshold = Math.round(Math.max(-60, Math.min(0, this.fromX(e.clientX - r.left, this.geom()))) * 2) / 2;
        this.changed(true);
      });
      const end = () => { if (this.dragging) { this.dragging = false; this.draw(); } };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        this.dyn.ratio = +Math.max(1, Math.min(20, this.dyn.ratio * f)).toFixed(2);
        this.changed(true);
      }, { passive: false });
    }
  }
})();
