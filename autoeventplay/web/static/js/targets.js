/* Target picker built from the driver's declared kinds (channel, bus, matrix, main, DCA, …). */
(function () {
  "use strict";
  const AEP = window.AEP;
  const T = AEP.t;

  AEP.targetPicker = function (container, kinds, value) {
    value = value || {};
    container.innerHTML = `
      <div class="row g-2">
        <div class="col-5"><label class="form-label">${AEP.esc(T("Target"))}</label><select class="form-select form-select-sm" data-f="kind"></select></div>
        <div class="col-4" data-wrap="number"><label class="form-label" data-l="number">${AEP.esc(T("No."))}</label><select class="form-select form-select-sm" data-f="number"></select></div>
        <div class="col-3" data-wrap="dest" hidden><label class="form-label" data-l="dest">${AEP.esc(T("Destination bus"))}</label><select class="form-select form-select-sm" data-f="dest"></select></div>
        <div class="col-7" data-wrap="raw" hidden><label class="form-label">${AEP.esc(T("Address / path"))}</label><input class="form-control form-control-sm mono" data-f="raw" placeholder="/ch/01/mix/on"></div>
      </div>`;
    const $ = (f) => container.querySelector(`[data-f="${f}"]`);
    const wrap = (f) => container.querySelector(`[data-wrap="${f}"]`);
    $("kind").innerHTML = kinds.map((k) => `<option value="${k.kind}">${AEP.esc(k.label)}</option>`).join("");
    if (value.kind && kinds.some((k) => k.kind === value.kind)) $("kind").value = value.kind;

    function options(max, labels, label) {
      let html = "";
      for (let i = 1; i <= max; i++) html += `<option value="${i}">${AEP.esc((labels && labels[i]) || (label + " " + i))}</option>`;
      return html;
    }
    function sync(first) {
      const k = kinds.find((x) => x.kind === $("kind").value) || kinds[0];
      const isRaw = k.kind === "raw";
      wrap("raw").hidden = !isRaw;
      wrap("number").hidden = isRaw || (k.max <= 1);
      wrap("dest").hidden = k.kind !== "send";
      container.querySelector('[data-l="number"]').textContent = k.kind === "send" ? T("Channel") : T("No.");
      $("number").innerHTML = options(k.max, k.number_labels, k.kind === "send" ? T("Channel") : k.label);
      if (k.kind === "send") {
        container.querySelector('[data-l="dest"]').textContent = k.dest_label || T("Target");
        $("dest").innerHTML = options(k.dest_max || 1, k.dest_labels, k.dest_label || T("Bus"));
      }
      if (first) {
        if (value.number) $("number").value = String(value.number);
        if (value.dest) $("dest").value = String(value.dest);
        if (value.raw) $("raw").value = value.raw;
      }
      container.dataset.mute = k.mute ? "1" : "0";
      container.dataset.level = k.level ? "1" : "0";
      container.dispatchEvent(new CustomEvent("kindchange", { detail: k }));
    }
    $("kind").addEventListener("change", () => sync(false));
    sync(true);
    return {
      value() {
        const kind = $("kind").value;
        return {
          kind,
          number: kind === "raw" ? null : parseInt($("number").value || "1", 10),
          dest: kind === "send" ? parseInt($("dest").value || "1", 10) : null,
          raw: kind === "raw" ? $("raw").value.trim() : null,
        };
      },
      spec() { return kinds.find((x) => x.kind === $("kind").value); },
    };
  };
})();
