"""Forecast sources: fetch each one and normalise it to one schema.

Normalised per-source shape:
  daily:  {"YYYY-MM-DD": {"pop", "hi", "lo", "cond", "hum", "wind", "wdir", "amt", ...}}
  hourly: {"YYYY-MM-DD": {hour(int): {"pop", "temp", "hum", "wind", "wdir", "amt"}}}
Temperatures are °F, wind mph, rain inches; dates and hours are the place's local time.
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

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


def _local_date_hour(iso: str, tz):
    """ISO timestamp (with offset or Z) -> (local 'YYYY-MM-DD', hour) in `tz`."""
    s = iso.replace("Z", "+00:00")
    # TWC gives -0400 without the colon
    m = re.match(r"^(.*[+-]\d\d)(\d\d)$", s)
    if m and ":" not in s[-6:]:
        s = f"{m.group(1)}:{m.group(2)}"
    dt = datetime.fromisoformat(s).astimezone(tz)
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


# ----------------------------------------------------------------------------
# parsers (pure; unit-tested against fixtures)
# ----------------------------------------------------------------------------

def parse_twc_daily(d, tz):
    daily = {}
    dp = d["daypart"][0]
    n = len(d["validTimeLocal"])
    for i in range(n):
        date, _ = _local_date_hour(d["validTimeLocal"][i], tz)
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


def parse_twc_hourly(d, tz):
    hourly = _hourly_dict()
    for i, ts in enumerate(d["validTimeLocal"]):
        date, hour = _local_date_hour(ts, tz)
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


def parse_nws_daily(d, tz):
    daily = {}
    for p in d["properties"]["periods"]:
        date, _ = _local_date_hour(p["startTime"], tz)
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


def parse_nws_grid_qpf(grid, tz):
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
        start = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tz)
        for k in range(span):
            t = start + timedelta(hours=k)
            out[t.strftime("%Y-%m-%d")][t.hour] = per_hour
    return out


def parse_nws_hourly(d, tz, qpf=None):
    hourly = _hourly_dict()
    qpf = qpf or {}
    for p in d["properties"]["periods"]:
        date, hour = _local_date_hour(p["startTime"], tz)
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


def parse_metno(d, tz):
    """MET Norway publishes no precip probability — temperatures only.
    Hi/lo come from the next_6_hours max/min blocks (present for ~9 days;
    plain hourly points stop after ~2 days), so a day needs >=3 of its 4
    blocks to count."""
    daily = {}
    hourly = _hourly_dict()
    blocks = defaultdict(list)
    amounts = _hourly_dict()
    for ts in d["properties"]["timeseries"]:
        date, hour = _local_date_hour(ts["time"], tz)
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
            start = datetime.fromisoformat(ts["time"].replace("Z", "+00:00")).astimezone(tz)
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
    """Daily only: Foreca's hourly endpoint 404s for the location ids we tried, so the
    source feeds the day tiles and stays out of the hourly bars and the
    event verdict rather than inventing hours it does not have."""
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
# HTTP
# ----------------------------------------------------------------------------

class NotAvailableHere(Exception):
    """The source does not cover this place (e.g. NWS outside the US)."""


def http_get(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, headers, timeout):
    return json.loads(http_get(url, headers, timeout))


def cffi_json(url, timeout):
    # Foreca fronts its JSON with a TLS-fingerprint check; curl_cffi's Chrome
    # impersonation passes it, urllib does not.
    from curl_cffi import requests as cffi_requests
    r = cffi_requests.get(url, impersonate="chrome", timeout=timeout)
    r.raise_for_status()
    return r.json()


def have_curl_cffi():
    return importlib.util.find_spec("curl_cffi") is not None


@dataclass
class FetchContext:
    user_agent: str
    timeout: float = 20.0
    twc_key: str | None = None
    get_json: Callable | None = None          # (url, headers, timeout) -> obj; tests inject
    get_browser_json: Callable | None = None  # (url, timeout) -> obj; tests inject

    def json(self, url, headers=None):
        return (self.get_json or http_json)(url, headers or {"User-Agent": self.user_agent}, self.timeout)

    def browser_json(self, url):
        return (self.get_browser_json or cffi_json)(url, self.timeout)


# ----------------------------------------------------------------------------
# fetchers: fetch(place, ctx) -> (daily, hourly) or (daily, hourly, note)
# ----------------------------------------------------------------------------

NWS_COUNTRIES = frozenset({"US", "PR", "GU", "VI", "AS", "MP"})
OM_DAILY = "precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code"
OM_HOURLY = ("precipitation_probability,precipitation,temperature_2m,relative_humidity_2m,"
             "wind_speed_10m,wind_direction_10m")
ENS_HOURLY = "precipitation,temperature_2m,relative_humidity_2m,wind_speed_10m"


def fetch_twc(place, ctx):
    base = ("https://api.weather.com/v3/wx/forecast/{}?geocode={:.3f},{:.3f}"
            "&format=json&units=e&language=en-US&apiKey={}")
    hdr = {"User-Agent": BROWSER_UA}
    d = ctx.json(base.format("daily/15day", place.lat, place.lon, ctx.twc_key), hdr)
    h = ctx.json(base.format("hourly/15day", place.lat, place.lon, ctx.twc_key), hdr)
    return parse_twc_daily(d, place.zone), parse_twc_hourly(h, place.zone)


# A coastal point can sit in a model grid cell that is mostly water: that row
# then reports sea-surface temperature, flat day and night (the original site
# saw 70-73°F all day from ECMWF). A sea cell is flat on EVERY day, so the
# test is the largest daily swing; one flat day is ensemble-mean smoothing.
SEA_SWING_F = 4
INLAND_ELEVATION_M = 100
NEIGHBOURS = ((0, -0.25), (0, 0.25), (-0.25, 0), (0.25, 0))
LAND_NOTE = "nearest land grid cell used (this model's own cell here is over water)"
SEA_NOTE = "temperatures left out: this model's grid cell here is over water"
_CELL_OFFSETS = {}
_CELL_LOCK = threading.Lock()


def looks_like_sea(hourly):
    swings = []
    for hours in hourly.values():
        temps = [v["temp"] for v in hours.values() if v.get("temp") is not None]
        if len(temps) >= 18:
            swings.append(max(temps) - min(temps))
    return bool(swings) and max(swings) < SEA_SWING_F


def _without_temps(daily, hourly):
    for d in daily.values():
        for k in ("hi", "lo", "hum", "wind", "wdir"):
            d[k] = None
    for hours in hourly.values():
        for v in hours.values():
            for k in ("temp", "hum", "wind", "wdir"):
                v[k] = None
    return daily, hourly


def _shift(lat, lon, dlat, dlon):
    return max(-90.0, min(90.0, lat + dlat)), ((lon + dlon + 180) % 360) - 180


def land_cell(place, key, at):
    """Fetch via at(lat, lon) -> (daily, hourly, elevation), stepping off a
    sea grid cell when needed. Returns (daily, hourly, note)."""
    full_key = key + place.cache_key()
    with _CELL_LOCK:
        known = _CELL_OFFSETS.get(full_key, "unknown")
    if known is None:
        daily, hourly, _ = at(place.lat, place.lon)
        return (*_without_temps(daily, hourly), SEA_NOTE)
    if known != "unknown":
        daily, hourly, _ = at(*_shift(place.lat, place.lon, *known))
        return daily, hourly, (LAND_NOTE if known != (0, 0) else None)
    daily, hourly, elevation = at(place.lat, place.lon)
    if (elevation is not None and elevation >= INLAND_ELEVATION_M) or not looks_like_sea(hourly):
        with _CELL_LOCK:
            _CELL_OFFSETS[full_key] = (0, 0)
        return daily, hourly, None
    failed = False
    for off in NEIGHBOURS:
        try:
            d2, h2, _ = at(*_shift(place.lat, place.lon, *off))
        except Exception:  # noqa: BLE001 — a failed neighbour is just not the answer
            failed = True
            continue
        if not looks_like_sea(h2):
            with _CELL_LOCK:
                _CELL_OFFSETS[full_key] = off
            return d2, h2, LAND_NOTE
    if not failed:   # only remember "all water" when every neighbour answered
        with _CELL_LOCK:
            _CELL_OFFSETS[full_key] = None
    return (*_without_temps(daily, hourly), SEA_NOTE)


def _real_local_times(raw, tz):
    """Open-Meteo labels the whole series with ONE utc offset, the one in
    force when it answers, so every hour after a DST change is an hour off.
    Re-label each stamp through UTC into the place's real local time."""
    off = raw.get("utc_offset_seconds")
    times = (raw.get("hourly") or {}).get("time")
    if off is None or not times:
        return raw
    shift = timedelta(seconds=off)
    raw["hourly"]["time"] = [
        (datetime.fromisoformat(t) - shift).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        for t in times]
    return raw


def fetch_openmeteo(model):
    def fetch(place, ctx):
        def at(lat, lon):
            q = urllib.parse.urlencode({
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                "daily": OM_DAILY, "hourly": OM_HOURLY, "forecast_days": 16,
                "timezone": place.tz, "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph", "models": model})
            raw = _real_local_times(ctx.json("https://api.open-meteo.com/v1/forecast?" + q), place.zone)
            daily, hourly = parse_openmeteo(raw)
            return daily, hourly, raw.get("elevation")
        return land_cell(place, ("forecast", model), at)
    return fetch


def fetch_ensemble(model):
    def fetch(place, ctx):
        def at(lat, lon):
            q = urllib.parse.urlencode({
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                "hourly": ENS_HOURLY, "forecast_days": 16, "timezone": place.tz,
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "models": model})
            raw = _real_local_times(ctx.json("https://ensemble-api.open-meteo.com/v1/ensemble?" + q), place.zone)
            daily, hourly = parse_ensemble(raw)
            return daily, hourly, raw.get("elevation")
        return land_cell(place, ("ensemble", model), at)
    return fetch


def fetch_nws(place, ctx):
    if place.country_code and place.country_code not in NWS_COUNTRIES:
        raise NotAvailableHere("US forecasts only")
    try:
        pts = ctx.json(f"https://api.weather.gov/points/{place.lat:.4f},{place.lon:.4f}")["properties"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotAvailableHere("US forecasts only") from None
        raise
    d = ctx.json(pts["forecast"])
    h = ctx.json(pts["forecastHourly"])
    # the hourly product carries a probability but no amount; the raw gridpoint
    # is the only place NWS publishes quantitativePrecipitation
    try:
        qpf = parse_nws_grid_qpf(ctx.json(pts["forecastGridData"]), place.zone)
    except Exception:  # noqa: BLE001 — an amount is a bonus, never worth losing NWS over
        qpf = {}
    return parse_nws_daily(d, place.zone), parse_nws_hourly(h, place.zone, qpf)


def fetch_metno(place, ctx):
    # MET Norway's terms: at most 4 decimals, and an identifying User-Agent
    d = ctx.json("https://api.met.no/weatherapi/locationforecast/2.0/complete"
                 f"?lat={place.lat:.4f}&lon={place.lon:.4f}")
    return parse_metno(d, place.zone)


_FORECA_IDS = {}
_FORECA_LOCK = threading.Lock()


def foreca_location_id(place, ctx):
    key = place.cache_key()
    with _FORECA_LOCK:
        if key in _FORECA_IDS:
            return _FORECA_IDS[key]
    loc = ctx.browser_json(f"https://api.foreca.net/locations/{place.lon:.4f},{place.lat:.4f}.json?lang=en")
    lid = str(loc["id"])
    with _FORECA_LOCK:
        _FORECA_IDS[key] = lid
    return lid


def fetch_foreca(place, ctx):
    lid = foreca_location_id(place, ctx)
    return parse_foreca(ctx.browser_json(f"https://api.foreca.net/data/daily/{lid}.json?dataset=full&lang=en"))


# ----------------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    id: str
    name: str
    short: str
    note: str
    fetch: Callable
    link: Callable
    opt_in: str | None = None   # "twc" / "foreca": off unless the host turns it on


def _fixed(url):
    return lambda place: url


ENS_DOCS = "https://open-meteo.com/en/docs/ensemble-api"

# Order matters: summarise_day() takes the condition phrase from the first
# source that publishes one, deliberately weather.com -> Foreca -> NWS.
SOURCES = (
    Source("twc", "The Weather Channel", "weather.com", "Same forecast engine behind Weather Underground",
           fetch_twc, lambda p: f"https://weather.com/weather/tenday/l/{p.lat:.3f},{p.lon:.3f}", opt_in="twc"),
    Source("foreca", "Foreca", "Foreca", "Commercial forecaster behind MSN and Bing Weather — daily only",
           fetch_foreca, _fixed("https://www.foreca.com/"), opt_in="foreca"),
    Source("nws", "National Weather Service", "NWS / NOAA", "US only · 7-day forecast",
           fetch_nws, lambda p: f"https://forecast.weather.gov/MapClick.php?lat={p.lat:.4f}&lon={p.lon:.4f}"),
    Source("ecmwf_ens", "ECMWF ensemble (51 runs)", "ECMWF ENS",
           "Share of 51 model runs that produce measurable daytime rain",
           fetch_ensemble("ecmwf_ifs025"), _fixed(ENS_DOCS)),
    Source("gefs", "GEFS ensemble (31 runs)", "GEFS",
           "Share of 31 NOAA model runs that produce measurable daytime rain",
           fetch_ensemble("gfs_seamless"), _fixed(ENS_DOCS)),
    Source("icon_ens", "ICON ensemble (40 runs)", "ICON ENS",
           "Share of 40 German (DWD) model runs that produce measurable daytime rain",
           fetch_ensemble("icon_seamless"), _fixed(ENS_DOCS)),
    Source("ecmwf", "ECMWF (European model)", "ECMWF", "via Open-Meteo",
           fetch_openmeteo("ecmwf_ifs025"), _fixed("https://open-meteo.com/")),
    Source("gfs", "GFS (NOAA global model)", "GFS", "via Open-Meteo",
           fetch_openmeteo("gfs_seamless"), _fixed("https://open-meteo.com/")),
    # No "best_match" blend row: it resolves to one of the models above and
    # was a byte-identical duplicate that double-weighted it.
    Source("gem", "GEM (Environment Canada)", "GEM", "via Open-Meteo",
           fetch_openmeteo("gem_seamless"), _fixed("https://open-meteo.com/")),
    Source("metno", "MET Norway (yr.no)", "yr.no", "Temperatures only — publishes no rain probability",
           fetch_metno, _fixed("https://www.yr.no/")),
)


def enabled_sources(cfg):
    out = []
    for src in SOURCES:
        if src.opt_in == "twc" and not (cfg.enable_twc and cfg.twc_key):
            continue
        if src.opt_in == "foreca" and not (cfg.enable_foreca and have_curl_cffi()):
            continue
        out.append(src)
    return out
