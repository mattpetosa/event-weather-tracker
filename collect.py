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
FORECA_LOCATION_ID = "105103159"  # Red Bank, Monmouth, NJ (lat/lon matches LOCATION)

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


MM_PER_INCH = 25.4


def _inches(mm):
    """Millimetres -> inches, rounded to the thousandth (0.001 in ~= 0.03 mm,
    finer than any source's own resolution). None passes through."""
    return None if mm is None else round(mm / MM_PER_INCH, 3)


def _percentile(vals, q):
    """Nearest-rank percentile over a list of numbers."""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))]


def fill_daily_from_hourly(daily, hourly):
    """Give every daily entry hum/wind/wdir and a daytime rain total, deriving
    from the daytime hours when the source did not publish a daily figure
    itself. Idempotent: only fills values that are still None/absent."""
    for date, entry in daily.items():
        hrs = [v for h, v in (hourly.get(date) or {}).items() if int(h) in DAY_WINDOW]
        hums = [v.get("hum") for v in hrs if v.get("hum") is not None]
        winds = [v.get("wind") for v in hrs if v.get("wind") is not None]
        if entry.get("amt") is None:
            amts = [v.get("amt") for v in hrs if v.get("amt") is not None]
            # a partial day would silently understate the total, so require
            # most of the daytime window before publishing a number
            entry["amt"] = round(sum(amts), 3) if len(amts) >= 12 else None
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
        qpf = d.get("qpf") or []          # TWC publishes qpf in inches already
        hourly[date][hour] = {"pop": _int(d["precipChance"][i]),
                              "temp": _int(d["temperature"][i]),
                              "hum": _int(d["relativeHumidity"][i]),
                              "wind": _int(d["windSpeed"][i]),
                              "wdir": d["windDirectionCardinal"][i],
                              "amt": round(qpf[i], 3) if i < len(qpf) and qpf[i] is not None else None}
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
    # included, while TWC/Foreca/NWS and the ensembles publish a
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
    # Absent for any model queried without it, and for models that publish no
    # precipitation at all — an amount-less source still parses.
    precip = hl.get("precipitation") or []
    for i, ts in enumerate(hl.get("time", [])):
        date, hour = ts[:10], int(ts[11:13])
        hourly[date][hour] = {"pop": _int(pops[i]) if i < len(pops) else None,
                              "temp": _int(hl["temperature_2m"][i]),
                              "hum": _int(hums[i]) if i < len(hums) else None,
                              "wind": _int(winds[i]) if i < len(winds) else None,
                              "wdir": _cardinal(dirs[i]) if i < len(dirs) else None,
                              "amt": _inches(precip[i]) if i < len(precip) else None}
    return fill_daily_from_hourly(daily, hourly), hourly


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


_ISO_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?)?$")


def _duration_hours(dur):
    """ISO-8601 duration as NWS writes it in a gridpoint validTime ('PT6H',
    'P1DT6H'). Returns None for anything finer than an hour, which the
    quantitativePrecipitation series never uses."""
    m = _ISO_DUR.match(dur)
    if not m:
        return None
    days, hours = m.group(1), m.group(2)
    total = int(days or 0) * 24 + int(hours or 0)
    return total or None


def parse_nws_grid_qpf(grid):
    """NWS gridpoint quantitativePrecipitation -> {date: {hour: inches}}.

    The series comes in multi-hour blocks (usually PT6H) with the block's
    total. Spread it evenly across the block's hours, the same convention the
    ensembles already use once their data goes 6-hourly, so an hourly amount
    means the same thing on every row of the chart."""
    out = _hourly_dict()
    series = (grid.get("properties", {}).get("quantitativePrecipitation") or {})
    for block in series.get("values", []):
        stamp, _, dur = block["validTime"].partition("/")
        span = _duration_hours(dur)
        value = block.get("value")
        if not span or value is None:
            continue
        per_hour = _inches(value / span)
        start = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ)
        for k in range(span):
            t = start + timedelta(hours=k)
            out[t.strftime("%Y-%m-%d")][t.hour] = per_hour
    return out


def parse_nws_hourly(d, qpf=None):
    hourly = _hourly_dict()
    qpf = qpf or {}
    for p in d["properties"]["periods"]:
        date, hour = _local_date_hour(p["startTime"])
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        wind, _ = _wind_from_text(p.get("windSpeed"))
        hourly[date][hour] = {"pop": _int(pop), "temp": _int(p["temperature"]),
                              "hum": _int((p.get("relativeHumidity") or {}).get("value")),
                              "wind": wind, "wdir": p.get("windDirection"),
                              "amt": (qpf.get(date) or {}).get(hour)}
    # the gridpoint QPF runs ~7 days, past where the hourly periods stop
    for date, hours in qpf.items():
        for hour, amt in hours.items():
            hourly[date].setdefault(hour, {"pop": None, "temp": None, "hum": None,
                                           "wind": None, "wdir": None, "amt": amt})
    return hourly


def parse_metno(d):
    """MET Norway publishes no precip probability — temperatures only.
    Hi/lo come from the next_6_hours max/min blocks (present for ~9 days;
    plain hourly points stop after ~2 days), so a day needs >=3 of its 4
    blocks to count."""
    daily = {}
    hourly = _hourly_dict()
    blocks = defaultdict(list)
    amounts = _hourly_dict()
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
        one = (data.get("next_1_hours") or {}).get("details") or {}
        six = (data.get("next_6_hours") or {}).get("details") or {}
        # 1-hourly amounts run out after ~2 days; spread the 6h blocks after
        # that, the same convention as the ensembles and the NWS gridpoint
        if "precipitation_amount" in one:
            amounts[date][hour] = _inches(one["precipitation_amount"])
        elif "precipitation_amount" in six:
            per_hour = _inches(six["precipitation_amount"] / 6)
            start = datetime.fromisoformat(ts["time"].replace("Z", "+00:00")).astimezone(TZ)
            for k in range(6):
                at = start + timedelta(hours=k)
                amounts[at.strftime("%Y-%m-%d")].setdefault(at.hour, per_hour)
        if "air_temperature_max" in six and "air_temperature_min" in six:
            # a 6h block starting at 18:00+ local mostly belongs to the night → still that date
            blocks[date].append((six["air_temperature_max"], six["air_temperature_min"]))
    for date, hours in amounts.items():
        for hour, amt in hours.items():
            entry = hourly[date].get(hour)
            if entry is None:
                hourly[date][hour] = {"pop": None, "temp": None, "hum": None,
                                      "wind": None, "wdir": None, "amt": amt}
            else:
                entry["amt"] = amt
    for date, bl in blocks.items():
        if len(bl) >= 3:
            daily[date] = {"pop": None, "hi": _f_from_c(max(b[0] for b in bl)),
                           "lo": _f_from_c(min(b[1] for b in bl)), "cond": None}
    return fill_daily_from_hourly(daily, hourly), hourly


MPH_PER_MS = 2.236936


def parse_foreca_daily(d):
    """Foreca's daily JSON. It serves metric regardless of the unit params you
    send (tempunit=F/units=us are all silently ignored), so temps arrive in C,
    wind in m/s and rain in mm. Verified 2026-09-01: tmax 30 vs Open-Meteo's
    88.6F for the same day, and the 13-day wind series x2.237 averages 7.2 mph
    against Open-Meteo's 8.3 -- read as mph it would average 3.2, far too low."""
    daily = {}
    for row in d.get("data") or []:
        date = row.get("date")
        if not date:
            continue
        daily[date] = {
            "pop": _int(row.get("rainp")),
            "hi": _f_from_c(row.get("tmax")), "lo": _f_from_c(row.get("tmin")),
            "cond": (row.get("symbtxt") or None),
            "hum": _int(row.get("rhum")),
            "wind": _int(row["winds"] * MPH_PER_MS) if row.get("winds") is not None else None,
            "wdir": _cardinal(row.get("windd")),
            "amt": _inches(row.get("rain")),
        }
    return daily


def parse_foreca(d):
    """Daily only: Foreca's hourly endpoint 404s for this location id, so the
    source feeds the day tiles and stays out of the hourly bars and the
    ceremony verdict rather than inventing hours it does not have."""
    return parse_foreca_daily(d), _hourly_dict()


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
                                  "hum": _int(hum), "wind": _int(wind), "wdir": None,
                                  # ensemble-mean rainfall: the conventional QPF, and the
                                  # only summary that adds up correctly across hours
                                  "amt": _inches(sum(precs) / n)}
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
                       "cond": None, "members": n,
                       # the mean is what you should expect, the p90 is the wet
                       # tail — a mean of 0.02" with a p90 of 0.3" is a very
                       # different afternoon from one where both are 0.02".
                       "amt_p90": _inches(_percentile(day_totals, 0.9))}
    # daily amt is the sum of the hourly means over the daytime window
    return fill_daily_from_hourly(daily, hourly), hourly


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
            "hourly": "precipitation_probability,precipitation,temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
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
    # the hourly product carries a probability but no amount; the raw gridpoint
    # is the only place NWS publishes quantitativePrecipitation
    try:
        qpf = parse_nws_grid_qpf(http_json(pts["forecastGridData"]))
    except Exception:  # noqa: BLE001 — an amount is a bonus, never worth losing NWS over
        qpf = {}
    return parse_nws_daily(d), parse_nws_hourly(h, qpf)


def fetch_metno():
    d = http_json(f"https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={LOCATION['lat']}&lon={LOCATION['lon']}")
    return parse_metno(d)


def fetch_foreca():
    # Foreca fronts its JSON with the same Akamai-style TLS check AccuWeather
    # used, so the Chrome impersonation curl_cffi gives us is still needed.
    from curl_cffi import requests as cffi_requests
    r = cffi_requests.get(
        f"https://api.foreca.net/data/daily/{FORECA_LOCATION_ID}.json?dataset=full&lang=en",
        impersonate="chrome", timeout=30)
    r.raise_for_status()
    return parse_foreca(r.json())


SOURCES = [
    {"id": "twc", "name": "The Weather Channel", "short": "weather.com",
     "url": "https://weather.com/weather/tenday/l/40.347,-74.064", "fetch": fetch_twc,
     "note": "Same forecast engine behind Weather Underground"},
    # Foreca sits in the slot AccuWeather held: summarise() takes the condition
    # phrase from the first source in this list that publishes one, and that
    # order is deliberately weather.com -> commercial -> NWS.
    {"id": "foreca", "name": "Foreca", "short": "Foreca",
     "url": "https://www.foreca.com/United-States/New-Jersey/Red-Bank",
     "fetch": fetch_foreca, "note": "Commercial forecaster behind MSN and Bing Weather \u2014 daily only"},
    {"id": "nws", "name": "National Weather Service", "short": "NWS / NOAA",
     "url": "https://forecast.weather.gov/MapClick.php?lat=40.347&lon=-74.064", "fetch": fetch_nws,
     "note": "7-day forecast — appears once the day is within a week"},
    {"id": "ecmwf_ens", "name": "ECMWF ensemble (51 runs)", "short": "ECMWF ENS",
     "url": "https://open-meteo.com/en/docs/ensemble-api", "fetch": fetch_ensemble("ecmwf_ifs025", lon=-74.15),
     "note": "Share of 51 model runs that produce measurable daytime rain"},
    {"id": "gefs", "name": "GEFS ensemble (31 runs)", "short": "GEFS",
     "url": "https://open-meteo.com/en/docs/ensemble-api", "fetch": fetch_ensemble("gfs_seamless"),
     "note": "Share of 31 NOAA model runs that produce measurable daytime rain"},
    # No lon override, unlike ECMWF: ICON's land mask calls the 40.25,-74.0
    # cell land (elevation 13m), so it is not the sea-surface cell ECMWF hits.
    {"id": "icon_ens", "name": "ICON ensemble (40 runs)", "short": "ICON ENS",
     "url": "https://open-meteo.com/en/docs/ensemble-api", "fetch": fetch_ensemble("icon_seamless"),
     "note": "Share of 40 German (DWD) model runs that produce measurable daytime rain"},
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


# Open-Meteo does not publish a probability from a deterministic run — there
# is none to publish — it derives precipitation_probability from that model's
# own ensemble. So the `ecmwf` row's rain chance is a restatement of
# `ecmwf_ens`, and `gfs`'s is a restatement of `gefs`: counting both as full
# votes let one model family carry ×3 of the rain headline. Their
# temperatures, humidity, wind and condition ARE the deterministic run and
# stay independent, so the discount is applied per field, not per source.
POP_TWINS = {"ecmwf": "ecmwf_ens", "gfs": "gefs"}
TWIN_POP_WEIGHT = 0.25  # a token vote: a different threshold and window over the same ensemble


def _wavg_f(pairs, places=3):
    """Weighted mean that keeps its decimals — rainfall totals live well below
    1, so the integer rounding _wavg does would flatten every one of them."""
    pairs = [(v, w) for v, w in pairs if v is not None]
    if not pairs:
        return None, 0
    tot = sum(w for _, w in pairs)
    return round(sum(v * w for v, w in pairs) / tot, places), len(pairs)


def source_weight(source_id, lead_days, field="temp"):
    """How much a source counts in the headline average, by how far out the
    day is, and for which field. Sources are not equally skilful: at 1–2 weeks
    the ensembles (ECMWF ENS especially) are the best estimate on the page, a
    single deterministic GFS/GEM run is close to noise, and a commercial
    15-day (Foreca) is weak past about a week. NWS is human-adjusted and its trust
    ramps up as the day approaches. Unknown ids count as a plain 1.0.

    field="pop" (or "amt") additionally discounts the deterministic rows whose
    precipitation figures are derived from an ensemble already on the page —
    see POP_TWINS."""
    if lead_days is None:
        lead_days = 7
    if field in ("pop", "amt") and source_id in POP_TWINS:
        return TWIN_POP_WEIGHT
    if source_id == "ecmwf_ens":
        return 2.0
    if source_id == "gefs":
        return 1.5
    if source_id == "nws":
        return 2.0 if lead_days <= 3 else 1.5
    if source_id == "twc":
        return 1.5
    if source_id == "icon_ens":
        # a third independent ensemble, but held below ECMWF ENS and GEFS so
        # that adding it does not tilt the headline wet on member count alone
        return 1.0
    if source_id == "foreca":
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
        amts, p90s = [], []
        weights = {}
        per_hour = defaultdict(list)
        per_hour_temp = defaultdict(list)
        per_hour_hum = defaultdict(list)
        per_hour_wind = defaultdict(list)
        per_hour_amt = defaultdict(list)
        for s in sources:
            if not s.get("ok"):
                continue
            # precipitation is weighted separately: the deterministic rows get
            # their rain figures from an ensemble that is already on the page
            w = source_weight(s.get("id"), lead)
            wp = source_weight(s.get("id"), lead, "pop")
            d = s["daily"].get(date)
            if d:
                weights[s.get("id") or f"source{len(weights)}"] = wp
                pops.append((d["pop"], wp)); his.append((d["hi"], w)); los.append((d["lo"], w))
                if d.get("cond"):
                    conds.append(d["cond"])
                hums.append((d.get("hum"), w)); winds.append((d.get("wind"), w)); wdirs.append(d.get("wdir"))
                amts.append((d.get("amt"), wp)); p90s.append((d.get("amt_p90"), wp))
            for h, v in (s["hourly"].get(date) or {}).items():
                per_hour[int(h)].append((v.get("pop"), wp))
                per_hour_temp[int(h)].append((v.get("temp"), w))
                per_hour_hum[int(h)].append((v.get("hum"), w))
                per_hour_wind[int(h)].append((v.get("wind"), w))
                per_hour_amt[int(h)].append((v.get("amt"), wp))
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
            am, am_n = _wavg_f(per_hour_amt.get(h, []))
            hourly.append({"h": h, "pop": p, "n": n, "temp": t, "hum": hu, "wind": wi,
                           "amt": am, "amt_n": am_n})
        valid = [v for v, _ in pops if v is not None]
        valid_amt = [v for v, _ in amts if v is not None]
        amt, amt_n = _wavg_f(amts)
        summary[date] = {
            "pop": pop, "pop_n": pop_n, "pop_plain": pop_plain,
            "pop_min": min(valid) if valid else None,
            "pop_max": max(valid) if valid else None,
            "lead_days": lead, "weights": weights,
            "hi": hi, "lo": lo,
            # how much, not just how likely — a 60% chance of 0.02" is a
            # different afternoon from a 60% chance of half an inch
            "amt": amt, "amt_n": amt_n,
            "amt_max": max(valid_amt) if valid_amt else None,
            "amt_p90": _wavg_f(p90s)[0],
            "hum": _wavg(hums)[0], "hum_n": _wavg(hums)[1],
            "wind": _wavg(winds)[0], "wind_n": _wavg(winds)[1], "wdir": _mode(wdirs),
            # first source in priority order with a human-written phrase; the
            # five Open-Meteo model rows would otherwise outvote weather.com/Foreca/NWS
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
