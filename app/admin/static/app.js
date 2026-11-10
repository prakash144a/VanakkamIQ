// VanakkamIQ admin: small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  // Focus the error summary so screen readers and keyboard users land on it.
  var focusTarget = document.querySelector("[data-focus]");
  if (focusTarget) focusTarget.focus();

  // Ask before destructive actions.
  function bindConfirms(root) {
    root.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
  }
  bindConfirms(document);

  // --- sound alerts for new orders -----------------------------------------------------------
  var sound = { on: false, ctx: null };
  try { sound.on = localStorage.getItem("viq-sound") === "1"; } catch (e) { /* storage blocked */ }

  function beep() {
    if (!sound.on) return;
    try {
      sound.ctx = sound.ctx || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.22].forEach(function (offset) {
        var t = sound.ctx.currentTime + offset;
        var osc = sound.ctx.createOscillator();
        var gain = sound.ctx.createGain();
        osc.frequency.value = 880;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
        osc.connect(gain);
        gain.connect(sound.ctx.destination);
        osc.start(t);
        osc.stop(t + 0.2);
      });
    } catch (e) { /* audio unavailable */ }
  }

  var soundButton = document.querySelector("[data-sound-toggle]");
  function showSound() {
    soundButton.setAttribute("aria-pressed", sound.on ? "true" : "false");
    soundButton.querySelector("[data-sound-label]").textContent = sound.on ? "Sound alerts on" : "Turn on sound alerts";
  }
  if (soundButton) {
    showSound();
    soundButton.addEventListener("click", function () {
      sound.on = !sound.on;
      try { localStorage.setItem("viq-sound", sound.on ? "1" : "0"); } catch (e) { /* storage blocked */ }
      showSound();
      beep();
    });
  }

  // --- live areas: refresh a fragment every few seconds (works across app replicas) ---------
  function newOrderIds(root) {
    return Array.prototype.map.call(root.querySelectorAll("[data-order-status=confirmed]"), function (el) {
      return el.getAttribute("data-order-id");
    });
  }

  var baseTitle = document.title;
  document.querySelectorAll("[data-poll]").forEach(function (area) {
    var url = area.getAttribute("data-poll");
    var seconds = parseInt(area.getAttribute("data-poll-seconds") || "5", 10);
    var known = newOrderIds(area);
    setInterval(function () {
      // Don't swap the board out from under someone mid-tap.
      var active = document.activeElement;
      if (active && area.contains(active) && active.tagName === "BUTTON") return;
      fetch(url, { credentials: "same-origin" })
        .then(function (r) {
          if (r.redirected || !r.ok) throw new Error("stale");
          return r.text();
        })
        .then(function (html) {
          area.innerHTML = html;
          bindConfirms(area);
          var ids = newOrderIds(area);
          var fresh = ids.filter(function (id) { return known.indexOf(id) === -1; });
          known = ids;
          if (fresh.length) {
            beep();
            if (document.hidden) document.title = "(" + fresh.length + " new) " + baseTitle;
          }
        })
        .catch(function () { /* keep the last good view; try again next tick */ });
    }, seconds * 1000);
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) document.title = baseTitle;
  });

  // --- live transcript on a call page ---------------------------------------------------------
  var transcript = document.querySelector("[data-live-events]");
  if (transcript) {
    var eventsUrl = transcript.getAttribute("data-live-events");
    var timer = setInterval(function () {
      var items = transcript.querySelectorAll("li[data-id]");
      var after = items.length ? items[items.length - 1].getAttribute("data-id") : "0";
      fetch(eventsUrl + "?after=" + encodeURIComponent(after), { credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("gone");
          var status = r.headers.get("X-Call-Status");
          return r.text().then(function (html) { return { html: html, status: status }; });
        })
        .then(function (res) {
          if (res.html.trim()) {
            var empty = document.querySelector("[data-empty]");
            if (empty) empty.remove();
            transcript.insertAdjacentHTML("beforeend", res.html);
          }
          if (res.status && res.status !== "active") {
            clearInterval(timer);
            window.location.reload(); // show the final summary and outcome
          }
        })
        .catch(function () { /* try again next tick */ });
    }, 3000);
  }

  // --- business form: type picker and the ordering module -----------------------------------
  var form = document.querySelector("[data-business-form]");
  if (form) {
    var types = {};
    try { types = JSON.parse(document.getElementById("business-types").textContent); } catch (e) { /* no data */ }
    var ordersToggle = form.querySelector("[data-orders-toggle]");
    var touched = form.hasAttribute("data-orders-touched"); // edit screens keep the current setting
    var syncOrders = function () {
      var on = !ordersToggle || ordersToggle.checked;
      form.querySelectorAll("[data-orders-only]").forEach(function (el) { el.hidden = !on; });
    };
    form.querySelectorAll("[data-type-radio]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        var t = types[radio.value];
        if (!t) return;
        form.querySelectorAll("[data-catalog-word]").forEach(function (el) { el.textContent = t.catalog.toLowerCase(); });
        form.querySelectorAll("[data-catalog-word-cap]").forEach(function (el) { el.textContent = t.catalog; });
        var catalog = form.querySelector("[data-catalog-example]");
        if (catalog && t.catalog_example) catalog.placeholder = t.catalog_example;
        var faq = form.querySelector("[data-faq-example]");
        if (faq && t.faq_example) faq.placeholder = t.faq_example;
        if (ordersToggle && !touched) {
          ordersToggle.checked = t.takes_orders;
          syncOrders();
        }
      });
    });
    if (ordersToggle) {
      ordersToggle.addEventListener("change", function () { touched = true; syncOrders(); });
      syncOrders();
    }
  }
})();
