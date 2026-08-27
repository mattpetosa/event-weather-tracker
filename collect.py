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
from datetime import datetime, timedelta, timezone, date as date_cls
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
TZ = ZoneInfo("America/New_York")

LOCATION = {"name": "Red Bank, NJ", "lat": 40.347, "lon": -74.064}
EVENT_DAYS = ["2026-09-05", "2026-09-06", "2026-09-07"]
KEY_DAY = "2026-09-06"

UA_CONTACT = os.environ.get("WEATHER_UA_CONTACT", "wedding-weather-tracker (https://github.com/mattpetosa/wedding-weather-tracker)")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Public key embedded in wunderground.com's own front end (TWC's consumer API).
# Public consumer key that wunderground.com's own front end embeds (not a
# secret; visible in any browser's network tab). Override with TWC_API_KEY.
TWC_KEY = os.environ.get("TWC_API_KEY", "e1f10a1e78da46f5b10a1e78da96f525")
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


_CARDINALS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _cardinal(deg):
    if deg is None:
        return None
    return _CARDINALS[int((float(deg) + 22.5) // 45) % 8]


_WIND_TEXT = re.compile(r'(?:([NSEW]{1,3})\s+)?(\d+)(?:\s*to\s*(\d+))?\s*mph', re.I)


def _wind_from_text(text):
    """'SW 7 mph' / '5 to 10 mph' / '7 mph' -> (mph, cardinal|None)."""
    if not text:
        return None, None
    m = _WIND_TEXT.search(text)
    if not m:
        return None, None
    lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
    return round((lo + hi) / 2), (m.group(1).upper() if m.group(1) else None)


def _mode(vals):
    vals = [v for v in vals if v]
    return max(set(vals), key=vals.count) if vals else None


def fill_daily_from_hourly(daily, hourly):
    """Give every daily entry hum/wind/wdir, deriving from the daytime hours
    when the source did not publish a daily figure itself."""
    for date, entry in daily.items():
        hrs = [v for h, v in (hourly.get(date) or {}).items() if int(h) in DAY_WINDOW]
        hums = [v.get("hum") for v in hrs if v.get("hum") is not None]
        winds = [v.get("wind") for v in hrs if v.get("wind") is not None]
        if entry.get("hum") is None:
            entry["hum"] = _int(sum(hums) / len(hums)) if len(hums) >= 6 else None
        if entry.get("wind") is None:
            entry["wind"] = _int(sum(winds) / len(winds)) if len(winds) >= 6 else None
        if entry.get("wdir") is None:
            entry["wdir"] = _mode([v.get("wdir") for v in hrs]) if len(hrs) >= 6 else None
    return daily


def _local_date_hour(iso: str):
    """ISO timestamp (with offset or Z) -> (local 'YYYY-MM-DD', hour)."""
    s = iso.replace("Z", "+00:00")
    # TWC gives -0400 without the colon
    m = re.match(r"^(.*[+-]\d\d)(\d\d)$", s)
    if m and ":" not in s[-6:]:
        s = f"{m.group(1)}:{m.group(2)}"
    dt = datetime.fromisoformat(s).astimezone(TZ)
    return dt.strftime("%Y-%m-%d"), dt.hour


def coverage_range(daily):
    """"first → last" for what a source returned, or None if it returned nothing.

    None, not []. The old expression was `sorted(daily)[-1:] and f"..."`,
    which yields the empty list for an empty forecast — and [] is *truthy*
    in JavaScript, so the page called .split() on it and threw out of
    render(), blanking the entire forecast because one source came back
    with no days. The failure path already wrote None; this agrees with it.
    """
    return f"{min(daily)} → {max(daily)}" if daily else None


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
        part = day_i if dp["relativeHumidity"][day_i] is not None else night_i
        daily[date] = {
            "pop": _int(pop),
            "hi": _int(d["calendarDayTemperatureMax"][i]),
            "lo": _int(d["calendarDayTemperatureMin"][i]),
            "cond": cond,
            "hum": _int(dp["relativeHumidity"][part]),
            "wind": _int(dp["windSpeed"][part]),
            "wdir": dp["windDirectionCardinal"][part],
        }
    return daily


def parse_twc_hourly(d):
    hourly = _hourly_dict()
    for i, ts in enumerate(d["validTimeLocal"]):
        date, hour = _local_date_hour(ts)
        hourly[date][hour] = {"pop": _int(d["precipChance"][i]),
                              "temp": _int(d["temperature"][i]),
                              "hum": _int(d["relativeHumidity"][i]),
                              "wind": _int(d["windSpeed"][i]),
                              "wdir": d["windDirectionCardinal"][i]}
    return hourly


def parse_openmeteo(d):
    daily, hourly = {}, _hourly_dict()
    dl = d.get("daily", {})
    pops = dl.get("precipitation_probability_max") or []
    # Length-checked like pops, not padded: `[None] * 99` stood in for a
    # missing weather_code array and happened to work only while no source
    # returned more than 99 days — and it did nothing at all for the other
    # case, a weather_code array present but shorter than time (GEM returns
    # exactly that past day 10), which raised IndexError and failed the
    # whole source.
    codes = dl.get("weather_code") or []
    for i, date in enumerate(dl.get("time", [])):
        daily[date] = {
            "pop": _int(pops[i]) if i < len(pops) else None,
            "hi": _int(dl["temperature_2m_max"][i]),
            "lo": _int(dl["temperature_2m_min"][i]),
            "cond": WMO_CODES.get(_int(codes[i])) if i < len(codes) else None,
        }
    hl = d.get("hourly", {})
    pops = hl.get("precipitation_probability") or []
    hums = hl.get("relative_humidity_2m") or []
    # Daily pop: precipitation_probability_max is the 24-hour max, overnight
    # included, while TWC/AccuWeather/NWS and the ensembles publish a
    # *daytime* chance. Re-derive it as the max over DAY_WINDOW from the
    # hourly array so the rows are comparable (fall back to the 24 h figure
    # if the day has no hourly data).
    day_max = defaultdict(lambda: None)
    for i, ts in enumerate(hl.get("time", [])):
        if i < len(pops) and pops[i] is not None and int(ts[11:13]) in DAY_WINDOW:
            cur = day_max[ts[:10]]
            day_max[ts[:10]] = pops[i] if cur is None else max(cur, pops[i])
    for date, v in daily.items():
        if day_max.get(date) is not None:
            v["pop"] = _int(day_max[date])
    winds = hl.get("wind_speed_10m") or []
    dirs = hl.get("wind_direction_10m") or []
    for i, ts in enumerate(hl.get("time", [])):
        date, hour = ts[:10], int(ts[11:13])
        hourly[date][hour] = {"pop": _int(pops[i]) if i < len(pops) else None,
                              "temp": _int(hl["temperature_2m"][i]),
                              "hum": _int(hums[i]) if i < len(hums) else None,
                              "wind": _int(winds[i]) if i < len(winds) else None,
                              "wdir": _cardinal(dirs[i]) if i < len(dirs) else None}
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
        entry = daily.setdefault(date, {"pop": None, "hi": None, "lo": None, "cond": None,
                                        "hum": None, "wind": None, "wdir": None})
        if p["isDaytime"]:
            entry["hi"] = _int(p["temperature"])
            entry["pop"] = _int(pop)
            entry["cond"] = p.get("shortForecast")
            entry["wind"], entry["wdir"] = _wind_from_text(p.get("windSpeed"))
            entry["wdir"] = entry["wdir"] or p.get("windDirection")
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
        wind, _ = _wind_from_text(p.get("windSpeed"))
        hourly[date][hour] = {"pop": _int(pop), "temp": _int(p["temperature"]),
                              "hum": _int((p.get("relativeHumidity") or {}).get("value")),
                              "wind": wind, "wdir": p.get("windDirection")}
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
        inst = data["instant"]["details"]
        t = inst.get("air_temperature")
        if t is not None:
            ws = inst.get("wind_speed")
            hourly[date][hour] = {"pop": None, "temp": _f_from_c(t),
                                  "hum": _int(inst.get("relative_humidity")),
                                  "wind": _int(ws * 2.23694) if ws is not None else None,
                                  "wdir": _cardinal(inst.get("wind_from_direction"))}
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
_ACCU_WIND = re.compile(r'Wind<span class="value">([^<]+)<')
_ACCU_HUM = re.compile(r'Humidity<span class="value">\s*(\d+)%')


def parse_accuweather_daily(page: str, year_hint: int | None = None):
    """Server-rendered 15-day cards. Year is inferred from the header
    'August 24 - September 7' range crossing Dec->Jan if needed."""
    daily = {}
    year = year_hint or datetime.now(TZ).year
    prev_month = None
    cards = list(_ACCU_CARD.finditer(page))
    for i, m in enumerate(cards):
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
        # The phrase and wind sit in the expandable panel that follows the
        # card, so the search has to run past the card's own </a>. It must
        # stop at the NEXT card, though: a fixed 2500-char window ran
        # straight into the following day, so a card whose panel omitted the
        # wind (or was shorter than the window) silently inherited tomorrow's
        # figure and reported it as today's.
        stop = cards[i + 1].start() if i + 1 < len(cards) else len(page)
        tail = page[m.end():min(m.end() + 2500, stop)]
        ph = _ACCU_PHRASE.search(tail)
        wm = _ACCU_WIND.search(tail)
        wind, wdir = _wind_from_text(html.unescape(wm.group(1)) if wm else None)
        daily[date] = {
            "pop": _int(pop.group(1)) if pop else None,
            "hi": _int(hi.group(1)) if hi else None,
            "lo": _int(lo.group(1)) if lo else None,
            "cond": html.unescape(ph.group(1).strip()) if ph else None,
            "hum": None, "wind": wind, "wdir": wdir,
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
        hm = _ACCU_HUM.search(body)
        wm = _ACCU_WIND.search(body)
        wind, wdir = _wind_from_text(html.unescape(wm.group(1)) if wm else None)
        hourly[dt.strftime("%Y-%m-%d")][dt.hour] = {
            "pop": _int(pop.group(1)) if pop else None,
            "temp": _int(temp.group(1)) if temp else None,
            "hum": _int(hm.group(1)) if hm else None,
            "wind": wind, "wdir": wdir,
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
    hums = sorted(k for k in h if k.startswith("relative_humidity_2m"))
    winds = sorted(k for k in h if k.startswith("wind_speed_10m"))

    def _mean(keys, i):
        vals = [h[k][i] for k in keys if h[k][i] is not None]
        return sum(vals) / len(vals) if vals else None
    if not members:
        raise ValueError("no ensemble members in response")
    by_day = defaultdict(list)  # date -> [(hour, [precip per member], [temp per member])]
    for i, ts in enumerate(h["time"]):
        date, hour = ts[:10], int(ts[11:13])
        by_day[date].append((hour, [h[m][i] for m in members], [h[t][i] for t in temps],
                             _mean(hums, i), _mean(winds, i)))
    daily, hourly = {}, _hourly_dict()
    n = len(members)
    for date, rows in by_day.items():
        day_totals = [0.0] * n
        t_hi, t_lo = [], []
        valid_hours = 0
        for hour, precs, tmps, hum, wind in rows:
            if any(p is None for p in precs):
                continue
            valid_hours += 1
            wet = sum(1 for p in precs if p >= HOURLY_THRESHOLD_MM)
            tv = [t for t in tmps if t is not None]
            mean_t = sum(tv) / len(tv) if tv else None  # member mean, not the extreme member
            hourly[date][hour] = {"pop": round(100 * wet / n), "temp": _int(mean_t),
                                  "hum": _int(hum), "wind": _int(wind), "wdir": None}
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
            "hourly": "precipitation_probability,temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
            "forecast_days": 16, "timezone": "America/New_York",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "models": model})
        return parse_openmeteo(http_json("https://api.open-meteo.com/v1/forecast?" + q))
    return _f


def fetch_ensemble(model, lon=None):
    def _f():
        q = urllib.parse.urlencode({
            "latitude": LOCATION["lat"], "longitude": lon or LOCATION["lon"],
            "hourly": "precipitation,temperature_2m,relative_humidity_2m,wind_speed_10m", "forecast_days": 16,
            "timezone": "America/New_York", "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph", "models": model})
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
        except Exception:  # noqa: BLE001 — hourly is best-effort
            pass
        finally:
            # In a finally, not at the end of the try: a request that raised
            # skipped the pause entirely, so the four hourly pages went out
            # back to back precisely when Akamai was already unhappy with us
            # — the moment the courtesy gap matters most.
            if day < 4:
                time.sleep(1.5)
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
    # No "best_match" blend row: for this location it resolves to GFS and was
    # a byte-identical duplicate, double-weighting GFS in every average.
    {"id": "gem", "name": "GEM (Environment Canada)", "short": "GEM",
     "url": "https://open-meteo.com/", "fetch": fetch_openmeteo("gem_seamless"), "note": "via Open-Meteo"},
    {"id": "metno", "name": "MET Norway (yr.no)", "short": "yr.no",
     "url": "https://www.yr.no/", "fetch": fetch_metno, "note": "Temperatures only — publishes no rain probability"},
]

# ----------------------------------------------------------------------------
# sun times (NOAA solar equations; accurate to a minute or so)
# ----------------------------------------------------------------------------
import math


def _sun_event_utc_hours(date, lat, lon, elev_deg, rising):
    """UTC decimal hour when the sun's centre is at elev_deg on `date`
    (-0.833 = standard sunrise/sunset incl. refraction, 6 = golden hour)."""
    y, m, d = (int(x) for x in date.split("-"))
    n = datetime(y, m, d).timetuple().tm_yday
    for guess in (12.0 - lon / 15.0,):
        for _ in range(3):  # iterate: fractional year depends on the time of day
            gamma = 2 * math.pi / 365 * (n - 1 + (guess - 12) / 24)
            eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                               - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
            decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
                    - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
                    - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
            lat_r = math.radians(lat)
            cos_ha = ((math.cos(math.radians(90 - elev_deg)) / (math.cos(lat_r) * math.cos(decl)))
                      - math.tan(lat_r) * math.tan(decl))
            if abs(cos_ha) > 1:
                return None
            ha = math.degrees(math.acos(cos_ha)) * (1 if rising else -1)
            guess = (720 - 4 * (lon + ha) - eqtime) / 60.0
        return guess
    return None


def sun_times(date, lat=LOCATION["lat"], lon=LOCATION["lon"]):
    def local(h):
        if h is None:
            return None
        base = datetime(*(int(x) for x in date.split("-")), tzinfo=timezone.utc)
        return (base + timedelta(hours=h)).astimezone(TZ).strftime("%H:%M")
    return {
        "sunrise": local(_sun_event_utc_hours(date, lat, lon, -0.833, True)),
        "sunset": local(_sun_event_utc_hours(date, lat, lon, -0.833, False)),
        "golden_start": local(_sun_event_utc_hours(date, lat, lon, 6, False)),
    }


# ----------------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------------

def _avg(vals):
    """Plain mean over the non-None values, with the count."""
    vals = [v for v in vals if v is not None]
    return (round(sum(vals) / len(vals)), len(vals)) if vals else (None, 0)


def _wavg(pairs):
    """Weighted mean over (value, weight) pairs, skipping None values.
    Returns (mean, n_sources) — n counts sources, not weight."""
    pairs = [(v, w) for v, w in pairs if v is not None]
    if not pairs:
        return None, 0
    tot = sum(w for _, w in pairs)
    return round(sum(v * w for v, w in pairs) / tot), len(pairs)


def source_weight(source_id, lead_days):
    """How much a source counts in the headline average, by how far out the
    day is. Sources are not equally skilful: at 1–2 weeks the ensembles
    (ECMWF ENS especially) are the best estimate on the page, a single
    deterministic GFS/GEM run is close to noise, and AccuWeather's 15-day is
    weak past about a week. NWS is human-adjusted and its trust ramps up as
    the day approaches. Unknown ids count as a plain 1.0."""
    if lead_days is None:
        lead_days = 7
    if source_id == "ecmwf_ens":
        return 2.0
    if source_id == "gefs":
        return 1.5
    if source_id == "nws":
        return 2.0 if lead_days <= 3 else 1.5
    if source_id == "twc":
        return 1.5
    if source_id == "accuweather":
        return 1.0 if lead_days <= 7 else 0.5
    if source_id in ("ecmwf", "gfs"):
        return 1.0
    if source_id == "gem":
        return 0.75
    return 1.0


def summarise(sources, days=EVENT_DAYS, today=None):
    today_d = date_cls.fromisoformat(today) if today else datetime.now(TZ).date()
    summary = {}
    for date in days:
        lead = (date_cls.fromisoformat(date) - today_d).days
        pops, his, los, conds, hums, winds, wdirs = [], [], [], [], [], [], []
        weights = {}
        per_hour = defaultdict(list)
        per_hour_temp = defaultdict(list)
        per_hour_hum = defaultdict(list)
        per_hour_wind = defaultdict(list)
        for s in sources:
            if not s.get("ok"):
                continue
            w = source_weight(s.get("id"), lead)
            d = s["daily"].get(date)
            if d:
                weights[s.get("id") or f"source{len(weights)}"] = w
                pops.append((d["pop"], w)); his.append((d["hi"], w)); los.append((d["lo"], w))
                if d.get("cond"):
                    conds.append(d["cond"])
                hums.append((d.get("hum"), w)); winds.append((d.get("wind"), w)); wdirs.append(d.get("wdir"))
            for h, v in (s["hourly"].get(date) or {}).items():
                per_hour[int(h)].append((v.get("pop"), w))
                per_hour_temp[int(h)].append((v.get("temp"), w))
                per_hour_hum[int(h)].append((v.get("hum"), w))
                per_hour_wind[int(h)].append((v.get("wind"), w))
        pop, pop_n = _wavg(pops)
        pop_plain, _ = _avg([v for v, _ in pops])
        hi, _ = _wavg(his)
        lo, _ = _wavg(los)
        hourly = []
        for h in range(24):
            p, n = _wavg(per_hour.get(h, []))
            t, _ = _wavg(per_hour_temp.get(h, []))
            hu, _ = _wavg(per_hour_hum.get(h, []))
            wi, _ = _wavg(per_hour_wind.get(h, []))
            hourly.append({"h": h, "pop": p, "n": n, "temp": t, "hum": hu, "wind": wi})
        valid = [v for v, _ in pops if v is not None]
        summary[date] = {
            "pop": pop, "pop_n": pop_n, "pop_plain": pop_plain,
            "pop_min": min(valid) if valid else None,
            "pop_max": max(valid) if valid else None,
            "lead_days": lead, "weights": weights,
            "hi": hi, "lo": lo,
            "hum": _wavg(hums)[0], "hum_n": _wavg(hums)[1],
            "wind": _wavg(winds)[0], "wind_n": _wavg(winds)[1], "wdir": _mode(wdirs),
            # first source in priority order with a human-written phrase; the
            # four Open-Meteo model rows would otherwise outvote weather.com/AccuWeather/NWS
            "cond": conds[0] if conds else None,
            "hourly": hourly,
            "hourly_available": any(x["n"] for x in hourly),
            "sun": sun_times(date),
        }
    return summary


def collect(sources=SOURCES, days=EVENT_DAYS, verbose=True):
    out_sources = []
    for src in sources:
        entry = {k: src[k] for k in ("id", "name", "short", "url", "note")}
        t0 = time.time()
        try:
            daily, hourly = src["fetch"]()
            fill_daily_from_hourly(daily, hourly)
            entry.update({
                "ok": True, "error": None,
                "daily": {d: daily[d] for d in days if d in daily},
                "hourly": {d: {str(h): v for h, v in sorted(hourly[d].items())}
                           for d in days if d in hourly and hourly[d]},
                "coverage": coverage_range(daily),
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


# How many snapshots of history ride along in latest.json. Nothing in www/
# reads `trend` today; it is kept because it is the only record of how the
# forecast moved, and dropping it would quietly discard that.
TREND_POINTS = 400


def write(result):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M")
    with open(os.path.join(HISTORY_DIR, f"{stamp}.json"), "w") as f:
        json.dump(result, f, separators=(",", ":"))
    # trend: one point per snapshot, tiny enough to ship to the browser.
    # Only the newest TREND_POINTS files are opened. This used to read and
    # parse every file in the history directory on every run and then throw
    # all but the last 400 away — one snapshot an hour means the discarded
    # work grows without limit while the answer never changes. History file
    # names are the sortable stamp `YYYYMMDD-HHMM`, so the tail of the
    # sorted listing is exactly the newest ones.
    trend = []
    for name in sorted(os.listdir(HISTORY_DIR))[-TREND_POINTS:]:
        try:
            with open(os.path.join(HISTORY_DIR, name)) as f:
                snap = json.load(f)
            trend.append({"at": snap["generated_at"],
                          "pop": {d: snap["summary"][d]["pop"] for d in snap["days"]}})
        except Exception:  # noqa: BLE001
            continue
    result["trend"] = trend
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
        print(f"  {d}: rain {s['pop']}% (n={s['pop_n']}, {s['pop_min']}–{s['pop_max']})  {s['lo']}–{s['hi']}°F  "
              f"hum {s['hum']}% (n={s['hum_n']})  wind {s['wind']} mph {s['wdir'] or ''} (n={s['wind_n']})  {s['cond']}")
    sys.exit(0 if ok else 1)
