/* AutoEventPlay – shared front-end helpers: API, toasts, live stream, status bar, fader & meter widgets. */
(function () {
  "use strict";

  const AEP = (window.AEP = window.AEP || {});

  // ------------------------------------------------------------------ API
  AEP.api = async function (path, data, method) {
    const opts = { method: method || (data !== undefined ? "POST" : "GET"), headers: { "X-AEP": "1" }, credentials: "same-origin" };
    if (data !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(data);
    }
    const res = await fetch(path, opts);
    let payload = {};
    try { payload = await res.json(); } catch (e) { /* ignore */ }
    if (res.status === 401 && payload.redirect) { window.location.href = payload.redirect; throw new Error("Nicht angemeldet"); }
    if (!res.ok || payload.ok === false) throw new Error(payload.error || ("HTTP " + res.status));
    return payload.result !== undefined ? payload.result : payload;
  };

  AEP.toast = function (message, kind) {
    let wrap = document.querySelector(".toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
    const el = document.createElement("div");
    el.className = "aep-toast " + (kind || "");
    el.textContent = message;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 7000 : 3500);
  };

  AEP.run = async function (promiseFactory, okMessage) {
    try {
      const r = await promiseFactory();
      if (okMessage) AEP.toast(typeof okMessage === "function" ? okMessage(r) : okMessage, "ok");
      return r;
    } catch (e) {
      AEP.toast(e.message, "err");
      throw e;
    }
  };

  AEP.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  };

  AEP.kindLabel = { jingle: "Jingle", audio: "Audio", tts: "Durchsage", recording: "Aufnahme", stop: "Stopp", any: "Alle", speech: "Sprache", media: "Medien" };
  AEP.statusLabel = { done: "fertig", failed: "fehlgeschlagen", cancelled: "abgebrochen", playing: "spielt", expired: "verfallen" };

  AEP.fmtTime = function (t) {
    if (!t) return "";
    const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
    return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  // ------------------------------------------------------------------ level maths (mirrors domain/levels.py)
  const MIN_DB = -90, MAX_DB = 10;
  AEP.faderToDb = function (f) {
    f = Math.max(0, Math.min(1, f));
    if (f <= 0) return MIN_DB;
    let db;
    if (f >= 0.5) db = f * 40 - 30; else if (f >= 0.25) db = f * 80 - 50; else if (f >= 0.0625) db = f * 160 - 70; else db = f * 480 - 90;
    return Math.max(MIN_DB, db);
  };
  AEP.dbToFader = function (db) {
    if (db == null || db <= MIN_DB) return 0;
    db = Math.min(MAX_DB, db);
    let f;
    if (db >= -10) f = (db + 30) / 40; else if (db >= -30) f = (db + 50) / 80; else if (db >= -60) f = (db + 70) / 160; else f = (db + 90) / 480;
    return Math.max(0, Math.min(1, f));
  };
  AEP.fmtDb = function (db) {
    if (db == null || db <= MIN_DB) return "-∞";
    return (db > 0.05 ? "+" : "") + db.toFixed(1);
  };

  // ------------------------------------------------------------------ live stream (SSE)
  const listeners = {};
  AEP.on = function (event, fn) { (listeners[event] = listeners[event] || []).push(fn); };
  function emit(event, data) { (listeners[event] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); }

  let es = null;
  function connect() {
    es = new EventSource("/api/stream");
    ["meters", "changed", "log"].forEach((ev) => es.addEventListener(ev, (m) => emit(ev, JSON.parse(m.data))));
    es.onopen = () => emit("stream", { connected: true });
    es.onerror = () => emit("stream", { connected: false });
  }
  AEP.connect = function () { if (!es) connect(); };

  // ------------------------------------------------------------------ status bar
  AEP.refreshStatus = async function () {
    try {
      const d = await AEP.api("/api/dashboard");
      AEP.lastDashboard = d;
      const erm = d.erm.status;
      setPill("st-erm", erm.online ? "ok" : (d.erm.paired ? "err" : "warn"), d.erm.rental ? d.erm.rental.name : (erm.online ? "verbunden" : (d.erm.paired ? "offline" : "nicht gekoppelt")), erm.message);
      const audioOk = d.audio.devices.length > 0 && d.audio.devices.every((x) => x.open);
      setPill("st-audio", d.audio.devices.length === 0 ? "warn" : (audioOk ? "ok" : "err"), d.audio.devices.length ? d.audio.devices.length + " Gerät(e)" : "kein Ausgang", d.audio.message);
      const mix = d.mixers.filter((m) => m.enabled);
      const bad = mix.filter((m) => m.status && m.status.ok === false);
      setPill("st-mixers", mix.length === 0 ? "" : (bad.length ? "err" : "ok"), mix.length ? mix.length + " Pult(e)" : "keine Pulte", bad.map((m) => m.name + ": " + m.status.message).join("\n") || "");
      const playing = d.playback.running.filter((r) => r.phase === "spielt");
      setPill("st-play", playing.length ? "play" : "", playing.length ? playing.map((r) => r.label).join(", ").slice(0, 40) : "bereit", "");
      emit("dashboard", d);
    } catch (e) { /* status bar is best effort */ }
  };
  function setPill(id, state, text, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector(".aep-dot").className = "aep-dot " + (state || "");
    el.querySelector(".txt").textContent = text;
    el.title = title || "";
  }

  let refreshTimer = null;
  AEP.on("changed", () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(AEP.refreshStatus, 150); });

  document.addEventListener("DOMContentLoaded", () => {
    const stop = document.getElementById("btn-stop");
    if (stop) stop.addEventListener("click", () => AEP.run(() => AEP.api("/api/playback/stop", {}), "Wiedergabe gestoppt"));
    const logout = document.getElementById("btn-logout");
    if (logout) logout.addEventListener("click", async (e) => { e.preventDefault(); await AEP.api("/api/auth/logout", {}); window.location.href = "/login"; });
    if (document.body.dataset.authed === "1") {
      AEP.connect();
      AEP.refreshStatus();
      setInterval(AEP.refreshStatus, 10000);
    }
  });

  // ------------------------------------------------------------------ meter widget
  class Meter {
    constructor(el) {
      this.el = el;
      this.canvas = document.createElement("canvas");
      el.appendChild(this.canvas);
      this.level = -90; this.peak = -90; this.peakTime = 0;
      this.resize();
      window.addEventListener("resize", () => this.resize());
    }
    resize() {
      const r = this.el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.max(1, r.width * dpr); this.canvas.height = Math.max(1, r.height * dpr);
      this.draw();
    }
    static pos(db) { // meter law: -60..0 dBFS, linear in dB
      return Math.max(0, Math.min(1, (db + 60) / 60));
    }
    set(db) {
      const now = performance.now();
      // fast attack, slow release (~20 dB/s)
      const dt = Math.min(0.2, (now - (this.lastT || now)) / 1000);
      this.lastT = now;
      this.level = db > this.level ? db : Math.max(db, this.level - 20 * dt);
      if (db >= this.peak || now - this.peakTime > 1500) { this.peak = db; this.peakTime = now; }
      this.draw();
    }
    draw() {
      const c = this.canvas, g = c.getContext("2d");
      const w = c.width, h = c.height;
      g.clearRect(0, 0, w, h);
      const seg = Math.max(2, Math.round(h / 60));
      const lit = Meter.pos(this.level) * h;
      for (let y = h; y > 0; y -= seg) {
        const frac = 1 - y / h; // 0 bottom .. 1 top
        const db = frac * 60 - 60;
        const on = h - y < lit;
        let color = db > -3 ? "#ff4d4f" : db > -12 ? "#ffc53d" : "#36cf6f";
        g.fillStyle = on ? color : "rgba(255,255,255,0.045)";
        g.fillRect(1, y - seg + 1, w - 2, seg - 1);
      }
      if (this.peak > -60) {
        const py = h - Meter.pos(this.peak) * h;
        g.fillStyle = this.peak > -3 ? "#ff4d4f" : "#e8edf2";
        g.fillRect(1, Math.max(0, py - 1), w - 2, 2);
      }
    }
  }
  AEP.Meter = Meter;

  // ------------------------------------------------------------------ fader widget
  const MARKS = [10, 5, 0, -5, -10, -20, -30, -40, -60];
  class Fader {
    constructor(el, opts) {
      this.el = el;
      this.db = opts.db;
      this.onChange = opts.onChange;
      this.onInput = opts.onInput;
      el.tabIndex = 0;
      el.setAttribute("role", "slider");
      el.setAttribute("aria-valuemin", "-90");
      el.setAttribute("aria-valuemax", "10");
      el.innerHTML = '<div class="scale"></div><div class="track"></div><div class="fill"></div><div class="cap"></div>';
      const scale = el.querySelector(".scale");
      MARKS.concat([-90]).forEach((m) => {
        const p = (1 - AEP.dbToFader(m)) * 100;
        scale.insertAdjacentHTML("beforeend", `<span style="top:${p}%">${m <= -90 ? "∞" : (m > 0 ? "+" + m : m)}</span><i class="${m === 0 ? "zero" : ""}" style="top:${p}%"></i>`);
      });
      this.cap = el.querySelector(".cap");
      this.fill = el.querySelector(".fill");
      this.render();
      this.bind();
    }
    geometry() { const r = this.el.getBoundingClientRect(); return { top: r.top + 14, height: r.height - 28 }; }
    render() {
      const f = AEP.dbToFader(this.db);
      const g = this.el.clientHeight - 28;
      const y = 14 + (1 - f) * g;
      this.cap.style.top = y + "px";
      this.fill.style.height = (f * g) + "px";
      this.el.setAttribute("aria-valuenow", String(Math.round(this.db * 10) / 10));
      this.el.setAttribute("aria-valuetext", AEP.fmtDb(this.db) + " dB");
    }
    set(db, notify) {
      db = Math.max(-90, Math.min(10, db));
      if (Math.abs(db) < 0.25 && db !== 0 && Math.abs(this.db) < 0.25) db = 0;
      this.db = db;
      this.render();
      if (this.onInput) this.onInput(db);
      if (notify) this.schedule();
    }
    setExternal(db) { if (!this.dragging) { this.db = db; this.render(); } }
    schedule() {
      clearTimeout(this.timer);
      const now = performance.now();
      const send = () => { this.lastSent = performance.now(); this.onChange && this.onChange(this.db); };
      if (!this.lastSent || now - this.lastSent > 80) send(); else this.timer = setTimeout(send, 80);
    }
    bind() {
      let startY = 0, startF = 0, fine = false;
      const move = (e) => {
        const g = this.geometry();
        const dy = e.clientY - startY;
        const f = startF - dy / g.height * (fine ? 0.25 : 1);
        this.set(AEP.faderToDb(f), true);
      };
      this.el.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        this.el.focus();
        this.dragging = true;
        fine = e.shiftKey;
        const g = this.geometry();
        const capRect = this.cap.getBoundingClientRect();
        if (e.clientY < capRect.top || e.clientY > capRect.bottom) { // jump
          const f = 1 - (e.clientY - g.top) / g.height;
          this.set(AEP.faderToDb(f), true);
        }
        startY = e.clientY; startF = AEP.dbToFader(this.db);
        this.el.setPointerCapture(e.pointerId);
        this.el.addEventListener("pointermove", move);
      });
      const end = (e) => {
        if (!this.dragging) return;
        this.dragging = false;
        this.el.removeEventListener("pointermove", move);
        clearTimeout(this.timer);
        this.onChange && this.onChange(this.db);
      };
      this.el.addEventListener("pointerup", end);
      this.el.addEventListener("pointercancel", end);
      this.el.addEventListener("dblclick", () => { this.set(0, true); });
      this.el.addEventListener("wheel", (e) => {
        e.preventDefault();
        const step = e.shiftKey ? 0.1 : 0.5;
        const base = this.db <= -90 ? -60 : this.db;
        this.set(base + (e.deltaY < 0 ? step : -step), true);
      }, { passive: false });
      this.el.addEventListener("keydown", (e) => {
        const step = e.shiftKey ? 0.1 : 1;
        if (e.key === "ArrowUp") { this.set((this.db <= -90 ? -60 : this.db) + step, true); e.preventDefault(); }
        if (e.key === "ArrowDown") { this.set(this.db - step, true); e.preventDefault(); }
        if (e.key === "Home") { this.set(10, true); e.preventDefault(); }
        if (e.key === "End") { this.set(-90, true); e.preventDefault(); }
        if (e.key === "0") { this.set(0, true); e.preventDefault(); }
      });
      window.addEventListener("resize", () => this.render());
    }
  }
  AEP.Fader = Fader;

  // ------------------------------------------------------------------ channel strip
  AEP.buildStrip = function (container, ch, type, extras) {
    const el = document.createElement("div");
    el.className = "strip" + (ch.muted ? " is-muted" : "");
    el.dataset.type = type;
    el.dataset.id = ch.id;
    if (ch.bus) el.dataset.bus = ch.bus;
    const sub = type === "input" ? ({ media: "Medien", tts: "Auto-Ansage", recording: "Durchsage" }[ch.bus] || "") : ((ch.device === "default" ? "Standard" : ch.device) + " · " + (ch.channel + 1));
    el.innerHTML = `
      <div class="scribble" title="Doppelklick zum Umbenennen"><span class="name">${AEP.esc(ch.name)}</span><span class="sub">${AEP.esc(sub)}</span></div>
      ${type === "output" ? `<button class="strip-btn tone ${extras && extras.tone ? "on" : ""}" title="Testton 1 kHz / -18 dBFS">1 kHz</button>` : `<div class="strip-btn spacer" aria-hidden="true">&nbsp;</div>`}
      <button class="strip-btn eq ${ch.eq_active ? "on" : ""}" title="Equalizer öffnen">EQ</button>
      <button class="strip-btn mute ${ch.muted ? "on" : ""}">MUTE</button>
      <div class="fader-area"><div class="meter"></div><div class="fader" aria-label="${AEP.esc(ch.name)}"></div></div>
      <div class="db-readout">${AEP.fmtDb(ch.level_db)}</div>`;
    container.appendChild(el);
    const base = type === "input" ? "/api/inputs/" : "/api/outputs/";
    const readout = el.querySelector(".db-readout");
    const showDb = (db) => { readout.textContent = AEP.fmtDb(db); readout.classList.toggle("off", db <= -90); };
    showDb(ch.level_db);
    const fader = new AEP.Fader(el.querySelector(".fader"), {
      db: ch.level_db,
      onInput: showDb,
      onChange: (db) => AEP.api(base + encodeURIComponent(ch.id) + "/level", { level_db: db }).catch((e) => AEP.toast(e.message, "err")),
    });
    const meter = new AEP.Meter(el.querySelector(".meter"));
    const muteBtn = el.querySelector(".mute");
    muteBtn.addEventListener("click", async () => {
      const muted = !muteBtn.classList.contains("on");
      muteBtn.classList.toggle("on", muted);
      el.classList.toggle("is-muted", muted);
      try { await AEP.api(base + encodeURIComponent(ch.id) + "/mute", { muted }); }
      catch (e) { muteBtn.classList.toggle("on", !muted); el.classList.toggle("is-muted", !muted); AEP.toast(e.message, "err"); }
    });
    const tone = el.querySelector(".tone");
    if (tone) tone.addEventListener("click", async () => {
      const on = !tone.classList.contains("on");
      tone.classList.toggle("on", on);
      try { await AEP.api(base + encodeURIComponent(ch.id) + "/tone", { enabled: on }); }
      catch (e) { tone.classList.toggle("on", !on); AEP.toast(e.message, "err"); }
    });
    const eqBtn = el.querySelector(".eq");
    eqBtn.addEventListener("click", () => {
      if (!AEP.openEq) return;
      AEP.openEq({
        name: ch.name, eq: ch.eq, endpoint: base + encodeURIComponent(ch.id) + "/eq",
        onSaved: (eq, active) => { ch.eq = eq; ch.eq_active = active; eqBtn.classList.toggle("on", active); },
      });
    });
    el.querySelector(".scribble").addEventListener("dblclick", async () => {
      const name = prompt("Name des Kanals", ch.name);
      if (!name || name === ch.name) return;
      await AEP.run(() => AEP.api(base + encodeURIComponent(ch.id) + "/name", { name }));
      ch.name = name;
      el.querySelector(".name").textContent = name;
    });
    return {
      el, fader, meter,
      update(c) {
        fader.setExternal(c.level_db);
        if (!fader.dragging) showDb(c.level_db);
        muteBtn.classList.toggle("on", c.muted);
        el.classList.toggle("is-muted", c.muted);
        if (c.eq) { ch.eq = c.eq; ch.eq_active = c.eq_active; eqBtn.classList.toggle("on", !!c.eq_active); }
      },
    };
  };
})();
