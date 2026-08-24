(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
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
  function deg(v) { return v == null ? "—" : v + "°"; }
  function fmtDay(iso) { const d = parseDay(iso); return MON[d.getMonth()] + " " + d.getDate(); }

  function render(data) {
    const days = $("#days");
    days.innerHTML = "";
    const tpl = $("#day-tpl");
    const okCount = data.sources.filter(s => s.ok).length;

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
        tick.style.left = "calc(" + s.pop + "% - 1px)";
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

      // hourly
      const hours = $(".hours", node);
      s.hourly.forEach(h => {
        const b = document.createElement("div");
        b.className = "h" + (h.pop == null ? " na" : "") + ((h.h < 6 || h.h >= 21) ? " night" : "");
        b.style.height = h.pop == null ? "2px" : Math.max(2, h.pop) + "%";
        b.title = hourLabel(h.h) + ": " + (h.pop == null ? "no hourly data" : h.pop + "% · " + h.n + " src" + (h.temp != null ? " · " + h.temp + "°" : ""));
        if (h.pop != null && h.h % 3 === 0) {
          const l = document.createElement("span");
          l.className = "lbl"; l.textContent = h.pop;
          b.appendChild(l);
        }
        hours.appendChild(b);
      });
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
      node.id = "d" + date;
      if (location.hash === "#" + date) toggle(false);
      node.addEventListener("click", e => { if (!detail.contains(e.target)) toggle(); });
      node.addEventListener("keydown", e => {
        if ((e.key === "Enter" || e.key === " ") && !detail.contains(e.target)) { e.preventDefault(); toggle(); }
      });
      days.appendChild(node);
    });

    // sources list
    const ul = $("#sources");
    ul.innerHTML = "";
    data.sources.forEach(src => {
      const li = document.createElement("li");
      if (!src.ok) li.className = "off";
      li.innerHTML = '<a href="' + src.url + '" target="_blank" rel="noopener">' + src.name + "</a>" +
        (src.ok ? (src.coverage ? "<small>through " + fmtDay(src.coverage.split("→")[1].trim()) + "</small>" : "")
                : "<small>unavailable on last check</small>");
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
