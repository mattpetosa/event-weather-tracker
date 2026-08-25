(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const KEY_WINDOW = { from: 16, to: 18, label: "Ceremony & cocktail hour, 4:30–6:30pm" };
  // Thresholds for a seated, dressed-up crowd in late-afternoon sun: [good max, watch max]
  const RULES = {
    rain: { good: 20, watch: 35 },
    wind: { good: 8, watch: 12 },   // waterfront lawn at the Molly Pitcher Inn: exposed to the river breeze
    hum:  { good: 65, watch: 70 },
  };
  function grade(v, r) { return v == null ? null : v <= r.good ? "good" : v <= r.watch ? "watch" : "concern"; }
  function gradeTemp(t) { return t == null ? null : (t >= 68 && t <= 78) ? "good" : (t >= 62 && t <= 82) ? "watch" : "concern"; }
  function avgOf(rows, k) { const v = rows.map(r => r[k]).filter(x => x != null); return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : null; }
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function parseDay(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  function hourLabel(h) {
    if (h === 0) return "12am";
    if (h === 12) return "noon";
    return h < 12 ? h + "am" : (h - 12) + "pm";
  }
  function fmtClock(hhmm) {
    if (!hhmm) return "—";
    const [h, m] = hhmm.split(":").map(Number);
    return ((h + 11) % 12 + 1) + ":" + String(m).padStart(2, "0") + (h < 12 ? "am" : "pm");
  }
  function deg(v) { return v == null ? "—" : v + "°"; }
  function fmtDay(iso) { const d = parseDay(iso); return MON[d.getMonth()] + " " + d.getDate(); }

  // The far end of a source's "YYYY-MM-DD → YYYY-MM-DD" coverage range, or
  // "" for anything that isn't one. collect.py writes null when a source
  // came back with no days at all, and this is not the place to find out
  // the hard way that it wrote something else.
  function coverageEnd(coverage) {
    if (typeof coverage !== "string") return "";
    const end = coverage.split("→")[1];
    return end ? end.trim() : "";
  }

  function render(data) {
    const days = $("#days");
    days.innerHTML = "";
    const tpl = $("#day-tpl");
    const okCount = data.sources.filter(s => s.ok).length;
    let deepLinked = null;

    data.days.forEach(date => {
      const s = data.summary[date];
      const node = tpl.content.firstElementChild.cloneNode(true);
      const dt = parseDay(date);
      const isKey = date === data.key_day;
      if (isKey) {
        node.classList.add("key");
        const tag = document.createElement("span");
        tag.className = "eyebrow-tag";
        tag.textContent = "The big day";
        node.prepend(tag);
      }
      $(".dow", node).textContent = DOW[dt.getDay()];
      $(".date", node).textContent = MON[dt.getMonth()] + " " + dt.getDate();
      $(".hi", node).textContent = deg(s.hi);
      $(".lo", node).textContent = deg(s.lo);
      $(".cond", node).textContent = s.cond || "";
      const humEl = $(".hum", node), windEl = $(".wind", node);
      if (s.hum == null) { humEl.textContent = "—"; humEl.classList.add("na"); }
      else humEl.textContent = s.hum + "%";
      if (s.wind == null) { windEl.textContent = "—"; windEl.classList.add("na"); }
      else windEl.textContent = s.wind + " mph" + (s.wdir ? " " + s.wdir : "");

      const popEl = $(".pop-num", node);
      if (s.pop == null) {
        popEl.textContent = "No forecast yet";
        popEl.classList.add("na");
        $(".pop-label", node).textContent = "";
      } else {
        popEl.textContent = s.pop + "%";
      }
      const band = $(".band", node), fill = $(".fill", node), tick = $(".tick", node);
      if (s.pop != null) {
        band.style.left = (s.pop_min || 0) + "%";
        band.style.width = Math.max(0, (s.pop_max || 0) - (s.pop_min || 0)) + "%";
        tick.style.left = s.pop + "%";
        requestAnimationFrame(() => { fill.style.width = s.pop + "%"; });
      } else {
        tick.style.display = "none";
      }
      const meta = $(".meta", node);
      if (s.pop != null) {
        meta.innerHTML = "Average of <b>" + s.pop_n + " source" + (s.pop_n === 1 ? "" : "s") + "</b>" +
          (s.pop_n > 1 ? " · they range from <b>" + s.pop_min + "%</b> to <b>" + s.pop_max + "%</b>" : "") +
          " · tap for hourly";
      } else {
        meta.textContent = "Forecasts reach this far out only from some sources — check back later.";
      }

      // ceremony verdict
      if (isKey) {
        const win = s.hourly.filter(h => h.h >= KEY_WINDOW.from && h.h <= KEY_WINDOW.to && h.pop != null);
        const lead = s.hourly.filter(h => h.h >= 14 && h.h <= KEY_WINDOW.to && h.pop != null);
        const hourlyBasis = win.length > 0;
        const f = hourlyBasis
          ? { rain: Math.max(...lead.map(h => h.pop)), wind: avgOf(win, "wind") ?? s.wind, hum: avgOf(win, "hum") ?? s.hum, temp: avgOf(win, "temp") ?? s.hi }
          : { rain: s.pop, wind: s.wind, hum: s.hum, temp: s.hi };
        const g = { rain: grade(f.rain, RULES.rain), wind: grade(f.wind, RULES.wind), hum: grade(f.hum, RULES.hum), temp: gradeTemp(f.temp) };
        // An easterly comes straight up the Navesink at the lawn — gustier and cooler than the town number.
        const onshore = /^E|^NE|^SE/.test(s.wdir || "");
        if (onshore && g.wind === "good") g.wind = "watch";
        const names = { rain: "rain", wind: "wind", hum: "humidity", temp: "temperature" };
        const concerns = Object.keys(g).filter(k => g[k] === "concern").map(k => names[k]);
        const watches = Object.keys(g).filter(k => g[k] === "watch").map(k => names[k]);
        const known = Object.values(g).filter(Boolean).length;
        let text;
        if (!known) text = "No forecast for the ceremony window yet";
        else if (concerns.length) text = "Concern: " + concerns.join(", ");
        else if (watches.length) text = "Good — keep an eye on " + watches.join(" and ") + (onshore && watches.includes("wind") ? " (onshore)" : "");
        else text = "Ideal so far";
        const v = $(".verdict", node);
        v.hidden = false;
        $(".verdict-label", v).textContent = "Ceremony · 4:30–6:30pm";
        $(".verdict-text", v).textContent = text;
        const ul = $(".factors", v);
        [["rain", f.rain == null ? null : f.rain + "%", "rain"],
         ["wind", f.wind == null ? null : f.wind + " mph" + (s.wdir ? " " + s.wdir : ""), "wind"],
         ["hum", f.hum == null ? null : f.hum + "%", "humidity"],
         ["temp", f.temp == null ? null : f.temp + "°", "temp"]].forEach(([k, val, label]) => {
          if (val == null) return;
          const li = document.createElement("li");
          li.className = g[k];
          li.innerHTML = label + " <b>" + val + "</b>";
          ul.appendChild(li);
        });
        if (s.sun && s.sun.sunset) {
          const sunEl = $(".sun", v);
          sunEl.hidden = false;
          sunEl.innerHTML = "Sunset <b>" + fmtClock(s.sun.sunset) + "</b> · golden hour <b>" +
            fmtClock(s.sun.golden_start) + "–" + fmtClock(s.sun.sunset) + "</b> — starts as cocktail hour winds down, the window for river portraits.";
          v.appendChild(sunEl);
        }
        if (known) {
          const basis = document.createElement("p");
          basis.className = "basis";
          basis.textContent = hourlyBasis
            ? "Rain is the peak chance from 2pm through the ceremony; wind, humidity and temperature are the 4–6pm average. Wind limits are tightened for the riverside lawn, and an easterly off the bay counts as a watch."
            : "Based on the day's averages until hourly forecasts reach this date.";
          v.appendChild(basis);
        }
      }

      // hourly
      const hours = $(".hours", node);
      s.hourly.forEach(h => {
        const b = document.createElement("div");
        const inWindow = isKey && h.h >= KEY_WINDOW.from && h.h <= KEY_WINDOW.to;
        b.className = "h" + (h.pop == null ? " na" : "") + ((h.h < 6 || h.h >= 21) ? " night" : "") + (inWindow ? " gold" : "");
        b.style.height = h.pop == null ? "2px" : Math.max(2, h.pop) + "%";
        b.title = hourLabel(h.h) + ": " + (h.pop == null ? "no hourly data" : h.pop + "% · " + h.n + " src" + (h.temp != null ? " · " + h.temp + "°" : "") + (h.hum != null ? " · " + h.hum + "% hum" : "") + (h.wind != null ? " · " + h.wind + " mph" : ""));
        const nearWindow = isKey && h.h >= KEY_WINDOW.from - 1 && h.h <= KEY_WINDOW.to + 1;
        if (h.pop != null && (inWindow || (h.h % 3 === 0 && !nearWindow))) {
          const l = document.createElement("span");
          l.className = "lbl"; l.textContent = h.pop + "%";
          b.appendChild(l);
        }
        hours.appendChild(b);
      });
      if (isKey && s.hourly_available) {
        const cap = document.createElement("p");
        cap.className = "window-cap";
        cap.innerHTML = '<span class="swatch"></span>' + KEY_WINDOW.label;
        $(".hour-axis", node).after(cap);
      }
      if (!s.hourly_available) {
        const p = document.createElement("p");
        p.className = "meta"; p.textContent = "No source has published hourly detail this far out yet.";
        hours.replaceWith(p);
        $(".hour-axis", node).remove();
      }

      // per-source
      const ul = $(".per-source", node);
      data.sources.forEach(src => {
        const li = document.createElement("li");
        const d = src.daily && src.daily[date];
        const n = document.createElement("span"); n.className = "n";
        n.innerHTML = src.name + (src.note ? "<small>" + src.note + "</small>" : "");
        if (src.ok && d && (d.hum != null || d.wind != null)) {
          const vit = document.createElement("span"); vit.className = "vit";
          vit.textContent = (d.hum != null ? d.hum + "% humidity" : "") + (d.hum != null && d.wind != null ? " · " : "") +
            (d.wind != null ? d.wind + " mph" + (d.wdir ? " " + d.wdir : "") : "");
          n.appendChild(vit);
        }
        const t = document.createElement("span"); t.className = "t";
        const p = document.createElement("span"); p.className = "p";
        if (!src.ok) { li.className = "off"; t.textContent = ""; p.textContent = "unavailable"; p.className += " na"; }
        else if (!d) { li.className = "off"; t.textContent = ""; p.textContent = "not this far out"; p.className += " na"; }
        else {
          t.textContent = d.hi != null ? deg(d.hi) + " / " + deg(d.lo) : "";
          if (d.pop == null) { p.textContent = "no rain %"; p.className += " na"; }
          else p.textContent = d.pop + "%";
        }
        li.append(n, t, p);
        ul.appendChild(li);
      });

      // expand/collapse
      const detail = $(".detail", node);
      const toggle = (scroll) => {
        const open = detail.hidden;
        detail.hidden = !open;
        node.setAttribute("aria-expanded", String(open));
        if (open && scroll !== false) requestAnimationFrame(() => node.scrollIntoView({ block: "nearest", behavior: "smooth" }));
      };
      // The element id is "d2026-09-06" while the shareable hash is
      // "#2026-09-06", so the browser's own anchor jump never fires — the
      // day opened where it was and stayed off-screen on a phone. Noted
      // here and scrolled to once, after every day has been appended.
      node.id = "d" + date;
      if (location.hash === "#" + date) { toggle(false); deepLinked = node; }
      node.addEventListener("click", e => { if (!detail.contains(e.target)) toggle(); });
      node.addEventListener("keydown", e => {
        if ((e.key === "Enter" || e.key === " ") && !detail.contains(e.target)) { e.preventDefault(); toggle(); }
      });
      days.appendChild(node);
    });
    if (deepLinked) {
      requestAnimationFrame(() => deepLinked.scrollIntoView({ block: "start" }));
    }

    // sources list
    const ul = $("#sources");
    ul.innerHTML = "";
    data.sources.forEach(src => {
      const li = document.createElement("li");
      if (!src.ok) li.className = "off";
      // Built as nodes, not markup: every other field on this page goes
      // through esc(), and this one line was the exception — src.url and
      // src.name interpolated raw into innerHTML.
      const a = document.createElement("a");
      a.href = src.url || "#";
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = src.name;
      li.appendChild(a);
      const small = document.createElement("small");
      // coverage may be absent or malformed — a source can report ok with
      // nothing in it. Read the end of the range defensively rather than
      // letting one blank source throw out of render() and blank the page.
      const through = coverageEnd(src.coverage);
      if (!src.ok) small.textContent = "unavailable on last check";
      else if (through) small.textContent = "through " + fmtDay(through);
      if (small.textContent) li.appendChild(small);
      ul.appendChild(li);
    });

    $("#status").textContent = "Updated " + fmtTime(data.generated_at) + " · " + okCount + " of " + data.sources.length + " sources reporting";
    $("#generated").textContent = "Forecasts refresh hourly. Chance of rain is each source's daytime figure; temperatures are the average high and low.";
  }

  fetch("data/latest.json?t=" + Math.floor(Date.now() / 300000), { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(data => { try { render(data); } catch (e) { throw new Error("render failed: " + e.message); } })
    .catch(err => {
      const st = $("#status");
      st.textContent = "Couldn't load the forecast data (" + err.message + "). Try again in a minute.";
      st.classList.add("err");
    });
})();
