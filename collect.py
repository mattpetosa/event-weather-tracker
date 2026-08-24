#!/usr/bin/env python3
"""Red Bank, NJ multi-source forecast collector for weather.mhpwebserver.com.

Fetches every reachable forecast source, normalises them to one schema,
averages them per day (and per hour), and writes data/latest.json for the
static front end. Each source is isolated: a failure is recorded on that
source's entry and never aborts the run.

Normalised per-source shape:
  daily:  {"YYYY-MM-DD": {"pop": int|None, "hi": int|None, "lo": int|None, "cond": str|None}}
  hourly: {"YYYY-MM-DD": {hour(int): {"pop": int|None, "temp": int|None}}}
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
TZ = ZoneInfo("America/New_York")

LOCATION = {"name": "Red Bank, NJ", "lat": 40.347, "lon": -74.064}
EVENT_DAYS = ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"]
KEY_DAY = "2026-09-06"

UA_CONTACT = "weather.mhpwebserver.com (16355926+mattpetosa@users.noreply.github.com)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Public key embedded in wunderground.com's own front end (TWC's consumer API).
TWC_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
ACCU_LOCATION_KEY = "339525"  # Red Bank, NJ 07701

# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def _int(v):
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _f_from_c(c):
    return None if c is None else _int(c * 9 / 5 + 32)


def _local_date_hour(iso: str):
    """ISO timestamp (with offset or Z) -> (local 'YYYY-MM-DD', hour)."""
    s = iso.replace("Z", "+00:00")
    # TWC gives -0400 without the colon
    m = re.match(r"^(.*[+-]\d\d)(\d\d)$", s)
    if m and ":" not in s[-6:]:
        s = f"{m.group(1)}:{m.group(2)}"
    dt = datetime.fromisoformat(s).astimezone(TZ)
    return dt.strftime("%Y-%m-%d"), dt.hour


def _hourly_dict():
    return defaultdict(dict)


def http_get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_CONTACT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, headers=None):
    return json.loads(http_get(url, headers))

# ----------------------------------------------------------------------------
# parsers (pure; unit-tested against fixtures)
# ----------------------------------------------------------------------------

def parse_twc_daily(d):
    daily = {}
    dp = d["daypart"][0]
    n = len(d["validTimeLocal"])
    for i in range(n):
        date, _ = _local_date_hour(d["validTimeLocal"][i])
        day_i, night_i = 2 * i, 2 * i + 1
        pop = dp["precipChance"][day_i]
        cond = dp["wxPhraseLong"][day_i]
        if pop is None:  # today's daytime part expires after ~3pm local
            pop = dp["precipChance"][night_i]
            cond = dp["wxPhraseLong"][night_i]
        daily[date] = {
            "pop": _int(pop),
            "hi": _int(d["calendarDayTemperatureMax"][i]),
            "lo": _int(d["calendarDayTemperatureMin"][i]),
            "cond": cond,
        }
    return daily


def parse_twc_hourly(d):
    hourly = _hourly_dict()
    for i, ts in enumerate(d["validTimeLocal"]):
        date, hour = _local_date_hour(ts)
        hourly[date][hour] = {"pop": _int(d["precipChance"][i]),
                              "temp": _int(d["temperature"][i])}
    return hourly


def parse_openmeteo(d):
    daily, hourly = {}, _hourly_dict()
    dl = d.get("daily", {})
    for i, date in enumerate(dl.get("time", [])):
        pops = dl.get("precipitation_probability_max") or []
        daily[date] = {
            "pop": _int(pops[i]) if i < len(pops) else None,
            "hi": _int(dl["temperature_2m_max"][i]),
            "lo": _int(dl["temperature_2m_min"][i]),
            "cond": WMO_CODES.get(_int((dl.get("weather_code") or [None] * 99)[i])),
        }
    hl = d.get("hourly", {})
    pops = hl.get("precipitation_probability") or []
    for i, ts in enumerate(hl.get("time", [])):
        date, hour = ts[:10], int(ts[11:13])
        hourly[date][hour] = {"pop": _int(pops[i]) if i < len(pops) else None,
                              "temp": _int(hl["temperature_2m"][i])}
    return daily, hourly


WMO_CODES = {0: "Sunny", 1: "Mostly Sunny", 2: "Partly Cloudy", 3: "Cloudy",
             45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
             61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 66: "Freezing Rain",
             67: "Freezing Rain", 71: "Snow", 73: "Snow", 75: "Snow", 77: "Snow",
             80: "Showers", 81: "Showers", 82: "Heavy Showers", 85: "Snow Showers",
             86: "Snow Showers", 95: "Thunderstorms", 96: "T-Storms w/ Hail",
             99: "T-Storms w/ Hail"}


def parse_nws_daily(d):
    daily = {}
    for p in d["properties"]["periods"]:
        date, _ = _local_date_hour(p["startTime"])
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        entry = daily.setdefault(date, {"pop": None, "hi": None, "lo": None, "cond": None})
        if p["isDaytime"]:
            entry["hi"] = _int(p["temperature"])
            entry["pop"] = _int(pop)
            entry["cond"] = p.get("shortForecast")
        else:
            entry["lo"] = _int(p["temperature"])
            if entry["pop"] is None:
                entry["pop"] = _int(pop)
            if entry["cond"] is None:
                entry["cond"] = p.get("shortForecast")
    return daily


def parse_nws_hourly(d):
    hourly = _hourly_dict()
    for p in d["properties"]["periods"]:
        date, hour = _local_date_hour(p["startTime"])
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        hourly[date][hour] = {"pop": _int(pop), "temp": _int(p["temperature"])}
    return hourly


def parse_metno(d):
    """MET Norway publishes no precip probability — temperatures only.
    Hi/lo come from the next_6_hours max/min blocks (present for ~9 days;
    plain hourly points stop after ~2 days), so a day needs >=3 of its 4
    blocks to count."""
    daily = {}
    hourly = _hourly_dict()
    blocks = defaultdict(list)
    for ts in d["properties"]["timeseries"]:
        date, hour = _local_date_hour(ts["time"])
        data = ts["data"]
        t = data["instant"]["details"].get("air_temperature")
        if t is not None:
            hourly[date][hour] = {"pop": None, "temp": _f_from_c(t)}
        six = (data.get("next_6_hours") or {}).get("details") or {}
        if "air_temperature_max" in six and "air_temperature_min" in six:
            # a 6h block starting at 18:00+ local mostly belongs to the night → still that date
            blocks[date].append((six["air_temperature_max"], six["air_temperature_min"]))
    for date, bl in blocks.items():
        if len(bl) >= 3:
            daily[date] = {"pop": None, "hi": _f_from_c(max(b[0] for b in bl)),
                           "lo": _f_from_c(min(b[1] for b in bl)), "cond": None}
    return daily, hourly


_ACCU_CARD = re.compile(
    r'<a class="daily-forecast-card[^"]*" href="[^"]*">(.*?)</a>', re.S)
_ACCU_DATE = re.compile(r'class="module-header sub date">\s*(\d+)/(\d+)\s*<')
_ACCU_HI = re.compile(r'class="high">\s*(-?\d+)')
_ACCU_LO = re.compile(r'class="low">\s*/\s*(-?\d+)')
_ACCU_POP = re.compile(r'class="precip">.*?(\d+)%', re.S)
_ACCU_PHRASE = re.compile(r'class="phrase">([^<]*)<')


def parse_accuweather_daily(page: str, year_hint: int | None = None):
    """Server-rendered 15-day cards. Year is inferred from the header
    'August 24 - September 7' range crossing Dec->Jan if needed."""
    daily = {}
    year = year_hint or datetime.now(TZ).year
    prev_month = None
    for m in _ACCU_CARD.finditer(page):
        card = m.group(1)
        dm = _ACCU_DATE.search(card)
        if not dm:
            continue
        month, day = int(dm.group(1)), int(dm.group(2))
        if prev_month and month < prev_month:
            year += 1
        prev_month = month
        date = f"{year:04d}-{month:02d}-{day:02d}"
        hi, lo, pop = _ACCU_HI.search(card), _ACCU_LO.search(card), _ACCU_POP.search(card)
        tail = page[m.end():m.end() + 2500]
        ph = _ACCU_PHRASE.search(tail)
        daily[date] = {
            "pop": _int(pop.group(1)) if pop else None,
            "hi": _int(hi.group(1)) if hi else None,
            "lo": _int(lo.group(1)) if lo else None,
            "cond": html.unescape(ph.group(1).strip()) if ph else None,
        }
    return daily


_ACCU_HOUR = re.compile(
    r'<div id="(\d{9,11})" data-qa="\d+" class="accordion-item hour"(.*?)(?=<div id="\d{9,11}" data-qa=|<!-- end hourly|$)', re.S)
_ACCU_HTEMP = re.compile(r'class="temp[^"]*">\s*(-?\d+)')


def parse_accuweather_hourly(page: str):
    hourly = _hourly_dict()
    for m in _ACCU_HOUR.finditer(page):
        epoch = int(m.group(1))
        body = m.group(2)
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(TZ)
        pop = _ACCU_POP.search(body)
        temp = _ACCU_HTEMP.search(body)
        hourly[dt.strftime("%Y-%m-%d")][dt.hour] = {
            "pop": _int(pop.group(1)) if pop else None,
            "temp": _int(temp.group(1)) if temp else None,
        }
    return hourly

POP_THRESHOLD_MM = 0.254   # 0.01 inch — the NWS "measurable precipitation" bar, applied to the day's total
HOURLY_THRESHOLD_MM = 0.05 # per hour. Beyond ~day 6 the ensembles are 6-hourly and Open-Meteo spreads each
                           # block's total evenly over its hours, so 0.05 mm/h ≈ measurable rain in the block.
DAY_WINDOW = range(6, 21)  # 6am–8pm local: the daytime span the other sources' daily figure covers


def parse_ensemble(d):
    """Open-Meteo ensemble response: hourly precipitation (and temperature) per
    member. Rain chance = share of members that produce measurable rain —
    daily over the daytime window, hourly within that hour. Temperatures are
    the member mean."""
    h = d["hourly"]
    members = sorted(k for k in h if k.startswith("precipitation"))
    temps = sorted(k for k in h if k.startswith("temperature_2m"))
    if not members:
        raise ValueError("no ensemble members in response")
    by_day = defaultdict(list)  # date -> [(hour, [precip per member], [temp per member])]
    for i, ts in enumerate(h["time"]):
        date, hour = ts[:10], int(ts[11:13])
        by_day[date].append((hour, [h[m][i] for m in members], [h[t][i] for t in temps]))
    daily, hourly = {}, _hourly_dict()
    n = len(members)
    for date, rows in by_day.items():
        day_totals = [0.0] * n
        t_hi, t_lo = [], []
        valid_hours = 0
        for hour, precs, tmps in rows:
            if any(p is None for p in precs):
                continue
            valid_hours += 1
            wet = sum(1 for p in precs if p >= HOURLY_THRESHOLD_MM)
            tv = [t for t in tmps if t is not None]
            mean_t = sum(tv) / len(tv) if tv else None  # member mean, not the extreme member
            hourly[date][hour] = {"pop": round(100 * wet / n), "temp": _int(mean_t)}
            if hour in DAY_WINDOW:
                for k, p in enumerate(precs):
                    day_totals[k] += p
            if mean_t is not None:
                t_hi.append(mean_t); t_lo.append(mean_t)
        if valid_hours < 20:  # the run's final day is usually cut short a few hours
            continue
        wet_days = sum(1 for tot in day_totals if tot >= POP_THRESHOLD_MM)
        daily[date] = {"pop": round(100 * wet_days / n),
                       "hi": _int(max(t_hi)) if t_hi else None,
                       "lo": _int(min(t_lo)) if t_lo else None,
                       "cond": None, "members": n}
    return daily, hourly


# ----------------------------------------------------------------------------
# fetchers — each returns (daily, hourly)
# ----------------------------------------------------------------------------

def fetch_twc():
    base = "https://api.weather.com/v3/wx/forecast/{}?geocode={},{}&format=json&units=e&language=en-US&apiKey={}"
    hdr = {"User-Agent": BROWSER_UA}
    d = http_json(base.format("daily/15day", LOCATION["lat"], LOCATION["lon"], TWC_KEY), hdr)
    h = http_json(base.format("hourly/15day", LOCATION["lat"], LOCATION["lon"], TWC_KEY), hdr)
    return parse_twc_daily(d), parse_twc_hourly(h)


def fetch_openmeteo(model, lon=None):
    """lon override: ECMWF's 0.25° cell containing Red Bank (40.25,-74.0) is
    open Atlantic, so it returns sea-surface-flat temps (70-73° all day).
    Nudging to -74.15 lands in the 40.25,-74.25 cell ~10 mi inland."""
    def _f():
        q = urllib.parse.urlencode({
            "latitude": LOCATION["lat"], "longitude": lon or LOCATION["lon"],
            "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code",
            "hourly": "precipitation_probability,temperature_2m",
            "forecast_days": 16, "timezone": "America/New_York",
            "temperature_unit": "fahrenheit", "models": model})
        return parse_openmeteo(http_json("https://api.open-meteo.com/v1/forecast?" + q))
    return _f


def fetch_ensemble(model, lon=None):
    def _f():
        q = urllib.parse.urlencode({
            "latitude": LOCATION["lat"], "longitude": lon or LOCATION["lon"],
            "hourly": "precipitation,temperature_2m", "forecast_days": 16,
            "timezone": "America/New_York", "temperature_unit": "fahrenheit", "models": model})
        return parse_ensemble(http_json("https://ensemble-api.open-meteo.com/v1/ensemble?" + q))
    return _f


def fetch_nws():
    pts = http_json(f"https://api.weather.gov/points/{LOCATION['lat']},{LOCATION['lon']}")["properties"]
    d = http_json(pts["forecast"])
    h = http_json(pts["forecastHourly"])
    return parse_nws_daily(d), parse_nws_hourly(h)


def fetch_metno():
    d = http_json(f"https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={LOCATION['lat']}&lon={LOCATION['lon']}")
    return parse_metno(d)


def fetch_accuweather():
    # Akamai rejects curl/urllib TLS fingerprints; curl_cffi impersonates Chrome.
    from curl_cffi import requests as cffi_requests
    s = cffi_requests.Session(impersonate="chrome")
    base = f"https://www.accuweather.com/en/us/red-bank/07701"
    r = s.get(f"{base}/daily-weather-forecast/{ACCU_LOCATION_KEY}", timeout=30)
    r.raise_for_status()
    daily = parse_accuweather_daily(r.text)
    hourly = _hourly_dict()
    for day in range(1, 5):  # AccuWeather only serves 4 days of hourly without premium
        try:
            rh = s.get(f"{base}/hourly-weather-forecast/{ACCU_LOCATION_KEY}?day={day}", timeout=30)
            if rh.status_code == 200 and "Hourly Weather" in rh.text:
                for date, hours in parse_accuweather_hourly(rh.text).items():
                    hourly[date].update(hours)
            time.sleep(1.5)
        except Exception:  # noqa: BLE001 — hourly is best-effort
            pass
    return daily, hourly


SOURCES = [
    {"id": "twc", "name": "The Weather Channel", "short": "weather.com",
     "url": "https://weather.com/weather/tenday/l/40.347,-74.064", "fetch": fetch_twc,
     "note": "Same forecast engine behind Weather Underground"},
    {"id": "accuweather", "name": "AccuWeather", "short": "AccuWeather",
     "url": "https://www.accuweather.com/en/us/red-bank/07701/daily-weather-forecast/339525",
     "fetch": fetch_accuweather, "note": "Hourly detail only within 4 days"},
    {"id": "nws", "name": "National Weather Service", "short": "NWS / NOAA",
     "url": "https://forecast.weather.gov/MapClick.php?lat=40.347&lon=-74.064", "fetch": fetch_nws,
     "note": "7-day forecast — appears once the day is within a week"},
    {"id": "ecmwf_ens", "name": "ECMWF ensemble (51 runs)", "short": "ECMWF ENS",
     "url": "https://open-meteo.com/en/docs/ensemble-api", "fetch": fetch_ensemble("ecmwf_ifs025", lon=-74.15),
     "note": "Share of 51 model runs that produce measurable daytime rain"},
    {"id": "gefs", "name": "GEFS ensemble (31 runs)", "short": "GEFS",
     "url": "https://open-meteo.com/en/docs/ensemble-api", "fetch": fetch_ensemble("gfs_seamless"),
     "note": "Share of 31 NOAA model runs that produce measurable daytime rain"},
    {"id": "ecmwf", "name": "ECMWF (European model)", "short": "ECMWF",
     "url": "https://open-meteo.com/", "fetch": fetch_openmeteo("ecmwf_ifs025", lon=-74.15),
     "note": "via Open-Meteo, nearest land grid cell"},
    {"id": "gfs", "name": "GFS (NOAA global model)", "short": "GFS",
     "url": "https://open-meteo.com/", "fetch": fetch_openmeteo("gfs_seamless"), "note": "via Open-Meteo"},
    {"id": "gem", "name": "GEM (Environment Canada)", "short": "GEM",
     "url": "https://open-meteo.com/", "fetch": fetch_openmeteo("gem_seamless"), "note": "via Open-Meteo"},
    {"id": "openmeteo", "name": "Open-Meteo blend", "short": "Open-Meteo",
     "url": "https://open-meteo.com/", "fetch": fetch_openmeteo("best_match"), "note": "Best-match model blend"},
    {"id": "metno", "name": "MET Norway (yr.no)", "short": "yr.no",
     "url": "https://www.yr.no/", "fetch": fetch_metno, "note": "Temperatures only — publishes no rain probability"},
]

# ----------------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------------

def _avg(vals):
    vals = [v for v in vals if v is not None]
    return (round(sum(vals) / len(vals)), len(vals)) if vals else (None, 0)


def summarise(sources, days=EVENT_DAYS):
    summary = {}
    for date in days:
        pops, his, los, conds = [], [], [], []
        per_hour = defaultdict(list)
        per_hour_temp = defaultdict(list)
        for s in sources:
            if not s.get("ok"):
                continue
            d = s["daily"].get(date)
            if d:
                pops.append(d["pop"]); his.append(d["hi"]); los.append(d["lo"])
                if d.get("cond"):
                    conds.append(d["cond"])
            for h, v in (s["hourly"].get(date) or {}).items():
                per_hour[int(h)].append(v.get("pop"))
                per_hour_temp[int(h)].append(v.get("temp"))
        pop, pop_n = _avg(pops)
        hi, _ = _avg(his)
        lo, _ = _avg(los)
        hourly = []
        for h in range(24):
            p, n = _avg(per_hour.get(h, []))
            t, _ = _avg(per_hour_temp.get(h, []))
            hourly.append({"h": h, "pop": p, "n": n, "temp": t})
        valid = [v for v in pops if v is not None]
        summary[date] = {
            "pop": pop, "pop_n": pop_n,
            "pop_min": min(valid) if valid else None,
            "pop_max": max(valid) if valid else None,
            "hi": hi, "lo": lo,
            # first source in priority order with a human-written phrase; the
            # four Open-Meteo model rows would otherwise outvote weather.com/AccuWeather/NWS
            "cond": conds[0] if conds else None,
            "hourly": hourly,
            "hourly_available": any(x["n"] for x in hourly),
        }
    return summary


def collect(sources=SOURCES, days=EVENT_DAYS, verbose=True):
    out_sources = []
    for src in sources:
        entry = {k: src[k] for k in ("id", "name", "short", "url", "note")}
        t0 = time.time()
        try:
            daily, hourly = src["fetch"]()
            entry.update({
                "ok": True, "error": None,
                "daily": {d: daily[d] for d in days if d in daily},
                "hourly": {d: {str(h): v for h, v in sorted(hourly[d].items())}
                           for d in days if d in hourly and hourly[d]},
                "coverage": sorted(daily.keys())[-1:] and f"{min(daily)} → {max(daily)}",
            })
        except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
            entry.update({"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                          "daily": {}, "hourly": {}, "coverage": None})
        entry["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["elapsed_s"] = round(time.time() - t0, 1)
        if verbose:
            status = "ok " if entry["ok"] else "ERR"
            got = ", ".join(f"{d[5:]}={entry['daily'][d]['pop']}%" for d in days if d in entry["daily"]) or "no event days yet"
            print(f"[{status}] {src['name']:<28} {entry['elapsed_s']:>5}s  {entry['error'] or got}")
        out_sources.append(entry)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "location": LOCATION,
        "days": days,
        "key_day": KEY_DAY,
        "sources": out_sources,
        "summary": summarise(out_sources, days),
    }


def write(result):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M")
    with open(os.path.join(HISTORY_DIR, f"{stamp}.json"), "w") as f:
        json.dump(result, f, separators=(",", ":"))
    # trend: one point per snapshot, tiny enough to ship to the browser
    trend = []
    for name in sorted(os.listdir(HISTORY_DIR)):
        try:
            with open(os.path.join(HISTORY_DIR, name)) as f:
                snap = json.load(f)
            trend.append({"at": snap["generated_at"],
                          "pop": {d: snap["summary"][d]["pop"] for d in snap["days"]}})
        except Exception:  # noqa: BLE001
            continue
    result["trend"] = trend[-400:]
    tmp = os.path.join(DATA_DIR, "latest.json.tmp")
    with open(tmp, "w") as f:
        json.dump(result, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(DATA_DIR, "latest.json"))


if __name__ == "__main__":
    res = collect()
    write(res)
    ok = sum(1 for s in res["sources"] if s["ok"])
    print(f"\n{ok}/{len(res['sources'])} sources ok. Summary:")
    for d in res["days"]:
        s = res["summary"][d]
        print(f"  {d}: rain {s['pop']}% (n={s['pop_n']}, {s['pop_min']}–{s['pop_max']})  {s['lo']}–{s['hi']}°F  {s['cond']}")
    sys.exit(0 if ok else 1)
