(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Places that use °F / mph by default; everyone else gets metric.
  const IMPERIAL = new Set(["US", "PR", "GU", "VI", "AS", "MP", "LR", "MM"]);
  const UNITS_KEY = "ewt-units";
  const NEED = ["place", "lat", "lon", "tz", "date", "from", "to"];

  let metric = false;
  let chosen = null;     // {name, lat, lon, tz, cc} once a suggestion is picked
  let lastData = null;

  // ---- storage (may be blocked: private windows, previews) ----
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* per-viewer nicety only */ } }
  function recall(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  // ---- formatting ----
  function parseDay(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); }
  function fmtDay(iso) { const d = parseDay(iso); return MON[d.getMonth()] + " " + d.getDate(); }
  function longDay(iso) { const d = parseDay(iso); return DOW[d.getDay()] + " " + MON[d.getMonth()] + " " + d.getDate(); }
  function fmtTime(iso) { return new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }); }
  function hourLabel(h) { return h === 0 ? "12am" : h === 12 ? "noon" : h < 12 ? h + "am" : (h - 12) + "pm"; }
  function fmtClock(hhmm) {
    if (!hhmm) return "—";
    const [h, m] = hhmm.split(":").map(Number);
    return ((h + 11) % 12 + 1) + ":" + String(m).padStart(2, "0") + (h < 12 ? "am" : "pm");
  }
  function deg(f) { return f == null ? "—" : (metric ? Math.round((f - 32) * 5 / 9) : f) + "°"; }
  function speed(mph) { return mph == null ? "—" : metric ? Math.round(mph * 1.609) + " km/h" : mph + " mph"; }
  // Rain lives between 0 and about half an inch; two decimals (or tenths of
  // a mm) is the honest resolution.
  function rain(inch) {
    if (inch == null) return "—";
    return metric ? (inch * 25.4).toFixed(1) + " mm" : inch.toFixed(2).replace(/^0/, "") + "\"";
  }
  function isTrace(v) { return v != null && v < 0.005; }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function bold(text) { return el("b", null, text); }

  function setStatus(text, err) {
    const st = $("#status");
    st.textContent = text || "";
    st.classList.toggle("err", !!err);
  }

  // ---- units ----
  function applyUnits(cc) {
    const saved = recall(UNITS_KEY);
    metric = saved ? saved === "metric" : !IMPERIAL.has((cc || "").toUpperCase());
    syncUnitsButton();
  }
  function syncUnitsButton() {
    const b = $("#units");
    b.textContent = metric ? "Show °F" : "Show °C";
    b.setAttribute("aria-pressed", String(metric));
  }
  $("#units").addEventListener("click", () => {
    metric = !metric;
    store(UNITS_KEY, metric ? "metric" : "imperial");
    syncUnitsButton();
    if (lastData) show(lastData);
  });

  // ---- place search ----
  const placeInput = $("#place"), list = $("#place-list");
  let timer = null, seq = 0, options = [], active = -1;

  placeInput.addEventListener("input", () => {
    chosen = null;
    clearTimeout(timer);
    const q = placeInput.value.trim();
    if (q.length < 2) { closeList(); return; }
    timer = setTimeout(() => search(q), 250);
  });
  placeInput.addEventListener("keydown", e => {
    if (list.hidden || !options.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      [...list.children].forEach((li, i) => li.classList.toggle("active", i === active));
      placeInput.setAttribute("aria-activedescendant", "place-opt-" + active);
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault(); pick(active);
    } else if (e.key === "Escape") {
      closeList();
    }
  });
  placeInput.addEventListener("blur", () => setTimeout(closeList, 120));

  function search(q) {
    const mine = ++seq;
    fetch("/api/places?q=" + encodeURIComponent(q))
      .then(r => r.json().then(j => ({ ok: r.ok, j }), () => ({ ok: false, j: {} })))
      .then(({ ok, j }) => {
        if (mine !== seq) return;
        if (!ok) throw new Error(j.error || "place search failed");
        showList(j.results || []);
      })
      .catch(err => { if (mine === seq) { closeList(); setStatus(err.message, true); } });
  }
  function showList(results) {
    options = results; active = -1;
    list.innerHTML = "";
    results.forEach((r, i) => {
      const li = el("li", null, r.label);
      li.id = "place-opt-" + i;
      li.setAttribute("role", "option");
      li.addEventListener("mousedown", e => { e.preventDefault(); pick(i); });
      list.appendChild(li);
    });
    if (!results.length) list.appendChild(el("li", "empty", "No matching places"));
    list.hidden = false;
    placeInput.setAttribute("aria-expanded", "true");
  }
  function closeList() {
    list.hidden = true;
    placeInput.setAttribute("aria-expanded", "false");
    placeInput.removeAttribute("aria-activedescendant");
  }
  function pick(i) {
    const r = options[i];
    chosen = { name: r.label, lat: r.lat, lon: r.lon, tz: r.tz, cc: r.country_code || "" };
    placeInput.value = r.label;
    closeList();
    setStatus("");
  }

  // ---- form + URL ----
  $("#event-form").addEventListener("submit", e => {
    e.preventDefault();
    if (!chosen) { setStatus("Pick a place from the suggestions list.", true); placeInput.focus(); return; }
    const date = $("#date").value, from = $("#from").value, to = $("#to").value;
    if (!date || !from || !to) { setStatus("Enter the date and the event's start and end time.", true); return; }
    if (from >= to) { setStatus("The event must end after it starts, on the same day.", true); return; }
    const q = new URLSearchParams({ place: chosen.name, lat: chosen.lat, lon: chosen.lon, tz: chosen.tz,
                                    cc: chosen.cc, date, from, to });
    history.pushState(null, "", "?" + q.toString());
    load(q);
  });
  window.addEventListener("popstate", fromUrl);

  function fromUrl() {
    const q = new URLSearchParams(location.search);
    if (!NEED.every(k => q.get(k))) { $("#result").innerHTML = ""; lastData = null; return; }
    chosen = { name: q.get("place"), lat: +q.get("lat"), lon: +q.get("lon"), tz: q.get("tz"), cc: q.get("cc") || "" };
    placeInput.value = chosen.name;
    $("#date").value = q.get("date"); $("#from").value = q.get("from"); $("#to").value = q.get("to");
    load(q);
  }

  function load(q) {
    const api = new URLSearchParams({ name: q.get("place"), lat: q.get("lat"), lon: q.get("lon"), tz: q.get("tz"),
                                      cc: q.get("cc") || "", date: q.get("date"), from: q.get("from"), to: q.get("to") });
    applyUnits(q.get("cc"));
    lastData = null;
    $("#result").innerHTML = "";
    setStatus("Fetching forecasts… the first look at a place takes a few seconds.");
    fetch("/api/forecast?" + api.toString())
      .then(r => r.json().then(j => ({ ok: r.ok, j }), () => ({ ok: false, j: { error: "HTTP " + r.status } })))
      .then(({ ok, j }) => { if (!ok) throw new Error(j.error || "request failed"); lastData = j; show(j); })
      .catch(err => setStatus("Couldn't get the forecast: " + err.message, true));
  }

  // ---- result ----
  function show(data) {
    const result = $("#result");
    result.innerHTML = "";
    if (data.status === "too_far") {
      const box = el("div", "notice");
      box.appendChild(el("p", null, "Forecasts don't reach " + longDay(data.date) + " yet. They will on " +
        longDay(data.available_from) + ", so check back then."));
      result.appendChild(box);
      renderSources([]);
      setStatus(data.place.name);
      return;
    }
    try { render(data); } catch (e) { setStatus("Couldn't show the forecast (" + e.message + ").", true); }
  }

  function render(data) {
    const s = data.summary, v = data.verdict, date = data.date, w = data.window;
    const gold = new Set(w.hours);
    const node = $("#day-tpl").content.firstElementChild.cloneNode(true);
    const dt = parseDay(date);
    const windowText = fmtClock(w.from) + "–" + fmtClock(w.to);

    $(".eyebrow-tag", node).textContent = "Your event · " + windowText;
    $(".dow", node).textContent = DOW[dt.getDay()];
    $(".date", node).textContent = MON[dt.getMonth()] + " " + dt.getDate();
    $(".where", node).textContent = data.place.name;
    $(".hi", node).textContent = deg(s.hi);
    $(".lo", node).textContent = deg(s.lo);
    $(".cond", node).textContent = s.cond || "";
    const humEl = $(".hum", node), windEl = $(".wind", node);
    humEl.textContent = s.hum == null ? "—" : s.hum + "%";
    humEl.classList.toggle("na", s.hum == null);
    windEl.textContent = s.wind == null ? "—" : speed(s.wind) + (s.wdir ? " " + s.wdir : "");
    windEl.classList.toggle("na", s.wind == null);

    // headline chance + band
    const popEl = $(".pop-num", node);
    const band = $(".band", node), fill = $(".fill", node), tick = $(".tick", node);
    if (s.pop == null) {
      popEl.textContent = "No forecast yet"; popEl.classList.add("na");
      $(".pop-label", node).textContent = "";
      tick.style.display = "none";
    } else {
      popEl.textContent = s.pop + "%";
      band.style.left = (s.pop_min || 0) + "%";
      band.style.width = Math.max(0, (s.pop_max || 0) - (s.pop_min || 0)) + "%";
      tick.style.left = s.pop + "%";
      requestAnimationFrame(() => { fill.style.width = s.pop + "%"; });
    }
    const meta = $(".meta", node);
    if (s.pop != null) {
      const weighted = s.pop_plain != null && s.weights && Object.keys(s.weights).length > 1;
      meta.append(weighted ? "Skill-weighted across " : "Average of ",
        bold(s.pop_n + " source" + (s.pop_n === 1 ? "" : "s")));
      if (weighted && s.pop_plain !== s.pop) meta.append(" (plain average ", bold(s.pop_plain + "%"), ")");
      if (s.pop_n > 1) meta.append(" · they range from ", bold(s.pop_min + "%"), " to ", bold(s.pop_max + "%"));
    } else {
      meta.textContent = "Only some sources reach this far out so far; check back later.";
    }

    // how much, not just how likely
    if (s.amt != null) {
      const a = $(".amt-line", node);
      a.hidden = false;
      if (isTrace(s.amt)) a.append("Rainfall ", bold("a trace at most"));
      else a.append("Rainfall ", bold(rain(s.amt)), " expected");
      if (s.amt_max != null && !isTrace(s.amt_max) && s.amt_max > s.amt) a.append(" · wettest source ", bold(rain(s.amt_max)));
      if (s.amt_p90 != null && !isTrace(s.amt_p90)) a.append(" · 1-in-10 wet case ", bold(rain(s.amt_p90)));
      a.append(" · across ", bold(String(s.amt_n)), " source" + (s.amt_n === 1 ? "" : "s"));
    }

    // verdict for the event window
    const vb = $(".verdict", node);
    vb.classList.add(v.grade || "none");
    $(".verdict-label", vb).textContent = "Event · " + windowText;
    $(".verdict-text", vb).textContent = v.label;
    const fmt = { rain: x => x + "%", wind: speed, hum: x => x + "%", temp: deg };
    const label = { rain: "rain", wind: "wind", hum: "humidity", temp: "temp" };
    v.checks.forEach(c => {
      const li = el("li", c.grade);
      li.append(label[c.key] + " ", bold(fmt[c.key](c.value)));
      $(".factors", vb).appendChild(li);
    });
    if (v.window_amt != null) {
      const p = el("p", "sun");
      if (isTrace(v.window_amt)) p.append("Expected rainfall during the event: ", bold("none"), " (the chance above is of a passing shower, not a soaking).");
      else p.append("Expected rainfall during the event: ", bold(rain(v.window_amt)), ".");
      vb.appendChild(p);
    }
    if (data.sun && (data.sun.sunrise || data.sun.sunset)) {
      const p = el("p", "sun");
      p.append("Sunrise ", bold(fmtClock(data.sun.sunrise)), " · sunset ", bold(fmtClock(data.sun.sunset)));
      vb.appendChild(p);
    }
    if (v.checks.length) {
      vb.appendChild(el("p", "basis", v.basis === "hourly"
        ? "Rain is the highest hourly chance during the event; wind, humidity and temperature are averages over those hours."
        : "Based on the day's figures until hourly forecasts reach this date."));
    }

    // hourly chance bars, event hours in gold
    const hours = $(".hours", node);
    s.hourly.forEach(h => {
      const inWindow = gold.has(h.h);
      const nearWindow = inWindow || gold.has(h.h - 1) || gold.has(h.h + 1);
      const b = el("div", "h" + (h.pop == null ? " na" : "") + ((h.h < 6 || h.h >= 21) ? " night" : "") + (inWindow ? " gold" : ""));
      b.style.height = h.pop == null ? "2px" : Math.max(2, h.pop) + "%";
      b.title = hourLabel(h.h) + ": " + (h.pop == null ? "no hourly data" : h.pop + "% · " + h.n + " src" +
        (h.temp != null ? " · " + deg(h.temp) : "") + (h.hum != null ? " · " + h.hum + "% hum" : "") +
        (h.wind != null ? " · " + speed(h.wind) : "") + (h.amt != null ? " · " + rain(h.amt) + "/h" : ""));
      if (h.pop != null && (inWindow || (h.h % 3 === 0 && !nearWindow))) b.appendChild(el("span", "lbl", h.pop + "%"));
      hours.appendChild(b);
    });
    // rainfall strip on the same axis, scaled to the data (a fixed ceiling
    // draws sub-pixel bars on drizzle days); the caption states the scale
    const amtRow = $(".amounts", node);
    if (s.hourly.some(h => h.amt != null)) {
      const ceiling = Math.max(0.01, ...s.hourly.map(h => h.amt || 0));
      amtRow.hidden = false;
      s.hourly.forEach(h => {
        const a = el("div", "a" + (h.amt > 0.02 ? " wet" : "") + (gold.has(h.h) ? " gold" : ""));
        a.style.height = h.amt == null ? "0" : Math.max(2, Math.min(100, (h.amt / ceiling) * 100)) + "%";
        a.title = hourLabel(h.h) + ": " + (h.amt == null ? "no amount published" :
          (isTrace(h.amt) ? "no rain" : rain(h.amt) + " in the hour") + " · " + h.amt_n + " src");
        amtRow.appendChild(a);
      });
      const cap = $(".amt-cap", node);
      cap.hidden = false;
      cap.textContent = "Bars above: chance of rain. Below: how much; a full-height bar is " + rain(ceiling) + " in the hour.";
    } else {
      amtRow.remove();
    }
    if (s.hourly_available) {
      const cap = el("p", "window-cap");
      cap.append(el("span", "swatch"), "Your event, " + windowText);
      $(".hour-axis", node).after(cap);
    } else {
      hours.replaceWith(el("p", "meta", "No source has published hourly detail this far out yet."));
      $(".hour-axis", node).remove();
      [".amounts", ".amt-cap"].forEach(sel => { const x = $(sel, node); if (x) x.remove(); });
    }

    // per source
    const ul = $(".per-source", node);
    data.sources.forEach(src => {
      const d = src.daily && src.daily[date];
      const li = el("li");
      const n = el("span", "n", src.name);
      const wt = s.weights && s.weights[src.id];
      if (wt != null && wt !== 1) { const x = el("span", "w", "×" + wt); x.title = "weight in the headline average"; n.appendChild(x); }
      if (src.note) n.appendChild(el("small", null, src.note));
      if (src.ok && d && (d.hum != null || d.wind != null)) {
        n.appendChild(el("span", "vit", [d.hum != null ? d.hum + "% humidity" : "",
          d.wind != null ? speed(d.wind) + (d.wdir ? " " + d.wdir : "") : ""].filter(Boolean).join(" · ")));
      }
      if (src.ok && d && d.amt != null) {
        n.appendChild(el("span", "vit", (isTrace(d.amt) ? "no measurable rain" : rain(d.amt) + " of rain") +
          (d.amt_p90 != null && !isTrace(d.amt_p90) ? " · wettest runs " + rain(d.amt_p90) : "")));
      }
      const t = el("span", "t"), p = el("span", "p");
      if (src.status === "not_here") { li.className = "off"; p.textContent = "not available here"; p.className += " na"; }
      else if (!src.ok) { li.className = "off"; p.textContent = "unavailable"; p.className += " na"; }
      else if (!d) {
        li.className = "off"; p.className += " na";
        p.textContent = src.covers_from ? "covers this date from " + fmtDay(src.covers_from) : "not this far out";
      } else {
        t.textContent = d.hi != null ? deg(d.hi) + " / " + deg(d.lo) : "";
        if (d.pop == null) { p.textContent = "no rain %"; p.className += " na"; } else p.textContent = d.pop + "%";
      }
      li.append(n, t, p);
      ul.appendChild(li);
    });

    $("#result").appendChild(node);
    renderSources(data.sources);
    const okCount = data.sources.filter(x => x.ok).length;
    setStatus("Updated " + fmtTime(data.generated_at) + " · " + okCount + " of " + data.sources.length +
      " sources reporting · times are local to " + data.place.tz.replace(/_/g, " "));
  }

  function renderSources(sources) {
    const ul = $("#sources");
    ul.innerHTML = "";
    sources.forEach(src => {
      const li = el("li", src.ok ? "" : "off");
      const a = el("a", null, src.name);
      a.href = src.url || "#"; a.target = "_blank"; a.rel = "noopener";
      li.appendChild(a);
      const note = src.status === "not_here" ? "not available here" : !src.ok ? "unavailable right now" : "";
      if (note) li.appendChild(el("small", null, note));
      ul.appendChild(li);
    });
  }

  fromUrl();
})();
