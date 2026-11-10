// VanakkamIQ website: rotating headline, the example call (silent loop, or with sound), demo form.
(function () {
  "use strict";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- rotating words ---------------------------------------------------------------------
  var words = document.querySelectorAll(".rot");
  var current = 0;
  if (words.length > 1 && !reduceMotion) {
    setInterval(function () {
      var prev = words[current];
      current = (current + 1) % words.length;
      prev.classList.remove("is-on");
      prev.classList.add("is-out");
      words[current].classList.remove("is-out");
      words[current].classList.add("is-on");
      setTimeout(function () { prev.classList.remove("is-out"); }, 650);
    }, 2600);
  }

  // ---- example call -----------------------------------------------------------------------
  var bubbles = document.querySelector("[data-bubbles]");
  var bar = document.querySelector("[data-bar]");
  var timeEl = document.querySelector("[data-call-time]");
  var nameEl = document.querySelector("[data-call-name]");
  var modeEl = document.querySelector("[data-mode]");
  var listen = document.querySelector("[data-listen]");
  var listenLabel = document.querySelector("[data-listen-label]");
  var tabs = document.querySelectorAll("[data-call]");
  var audios = {};
  document.querySelectorAll("[data-audio]").forEach(function (a) { audios[a.getAttribute("data-audio")] = a; });

  var calls = null, callId = "restaurant", t = 0, playing = false, shownCount = -1, activeIndex = -1;
  var VISIBLE = 5;

  function fmt(s) { s = Math.floor(s || 0); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }

  function render() {
    if (!calls) return;
    var call = calls[callId];
    var lines = call.lines;
    var shown = 0, active = -1;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i][1] <= t) { shown = i + 1; active = i; }
    }
    if (shown !== shownCount || active !== activeIndex) {
      // Rebuild only when a line appears or the speaker changes.
      var start = Math.max(0, shown - VISIBLE);
      bubbles.textContent = "";
      for (var j = start; j < shown; j++) {
        var li = document.createElement("li");
        li.className = "bubble" + (lines[j][0] === "caller" ? " caller" : "") + (j === active ? " is-active" : "");
        var who = document.createElement("span");
        who.className = "who";
        who.textContent = lines[j][0] === "caller" ? "Caller" : "VanakkamIQ";
        li.appendChild(who);
        li.appendChild(document.createTextNode(lines[j][2]));
        bubbles.appendChild(li);
      }
      shownCount = shown;
      activeIndex = active;
    }
    bar.style.width = Math.min(100, (t / call.duration) * 100).toFixed(1) + "%";
    timeEl.textContent = fmt(t) + " / " + fmt(call.duration);
    nameEl.textContent = call.name;
    modeEl.textContent = playing ? "Playing with sound" : "Playing silently · press Hear the call";
    listenLabel.textContent = playing ? "Pause the call" : "Hear the call";
  }

  function pauseAll() { Object.keys(audios).forEach(function (k) { audios[k].pause(); }); }

  function select(id) {
    pauseAll();
    playing = false;
    callId = id;
    t = 0;
    shownCount = -1;
    tabs.forEach(function (b) { b.setAttribute("aria-selected", b.getAttribute("data-call") === id ? "true" : "false"); });
    render();
  }

  tabs.forEach(function (b) { b.addEventListener("click", function () { select(b.getAttribute("data-call")); }); });

  Object.keys(audios).forEach(function (id) {
    var a = audios[id];
    a.addEventListener("timeupdate", function () { if (id === callId) { t = a.currentTime; render(); } });
    a.addEventListener("play", function () { playing = true; render(); });
    a.addEventListener("pause", function () { playing = false; render(); });
    a.addEventListener("ended", function () { playing = false; render(); });
  });

  if (listen) {
    listen.addEventListener("click", function () {
      var a = audios[callId];
      if (!a) return;
      if (a.paused) {
        pauseAll();
        a.currentTime = 0;
        t = 0;
        shownCount = -1;
        var p = a.play();
        if (p && p.catch) p.catch(function () { playing = false; render(); });
      } else {
        a.pause();
      }
    });
  }

  // Without sound the conversation still plays out, silently, in a loop.
  setInterval(function () {
    if (!calls || playing) return;
    t += 0.25;
    if (t > calls[callId].duration + 3) { t = 0; shownCount = -1; }
    render();
  }, 250);

  fetch("/static/site/calls.json")
    .then(function (r) { return r.json(); })
    .then(function (data) { calls = data; if (reduceMotion) t = 999; render(); })
    .catch(function () { /* the rest of the page works without it */ });

  // ---- demo form ----------------------------------------------------------------------------
  var form = document.querySelector("[data-demo-form]");
  var status = document.querySelector("[data-form-status]");
  function say(text, ok) { status.textContent = text; status.className = "form-status wide " + (ok ? "ok" : "err"); }

  if (location.search.indexOf("demo=sent") !== -1) say("Thanks! We'll be in touch soon to set up your demo.", true);

  if (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var data = new FormData(form);
      if (!String(data.get("name") || "").trim() || !String(data.get("business_name") || "").trim()) {
        say("Please tell us your name and your business's name.", false);
        return;
      }
      if (!String(data.get("email") || "").trim() && !String(data.get("phone") || "").trim()) {
        say("Please leave an email or a phone number so we can reach you.", false);
        return;
      }
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      say("Sending…", true);
      fetch("/demo", { method: "POST", body: data, headers: { Accept: "application/json" } })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok && j.ok, message: j.message }; }); })
        .then(function (res) {
          say(res.message || (res.ok ? "Thanks!" : "Something went wrong."), res.ok);
          if (res.ok) form.reset();
        })
        .catch(function () { say("We couldn't send that just now. Please email info@nativemindsai.com.", false); })
        .then(function () { button.disabled = false; });
    });
  }
})();
