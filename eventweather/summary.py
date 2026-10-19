"""One day's consensus from many forecasts: skill weights, the summary and
sunrise/sunset. (The event-window verdict joins this file in a later step.)"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .sources import _mode

# ----------------------------------------------------------------------------
# sun times (NOAA solar equations; accurate to a minute or so)
# ----------------------------------------------------------------------------


def _sun_event_utc_hours(date, lat, lon, elev_deg, rising):
    """UTC decimal hour when the sun's centre is at elev_deg on `date`
    (-0.833 = standard sunrise/sunset incl. refraction). None when the sun
    never reaches that elevation that day (polar day or night). The result
    can fall outside 0-24; adding it to the date's UTC midnight still lands
    on the right moment."""
    y, m, d = (int(x) for x in date.split("-"))
    n = datetime(y, m, d).timetuple().tm_yday
    guess = 12.0 - lon / 15.0
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


def sun_times(date, lat, lon, tz):
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz

    def local(h):
        if h is None:
            return None
        base = datetime(*(int(x) for x in date.split("-")), tzinfo=timezone.utc)
        return (base + timedelta(hours=h)).astimezone(zone).strftime("%H:%M")
    return {
        "sunrise": local(_sun_event_utc_hours(date, lat, lon, -0.833, True)),
        "sunset": local(_sun_event_utc_hours(date, lat, lon, -0.833, False)),
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



def summarise_day(sources, date, today, lat, lon, tz):
    """Weighted consensus for one local date. `today` is the place's local
    date (a datetime.date), so lead time is measured where the event is.
    Sources with ok=False are ignored; hourly keys may be int or str."""
    lead = (date_cls.fromisoformat(date) - today).days
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
            pops.append((d.get("pop"), wp)); his.append((d.get("hi"), w)); los.append((d.get("lo"), w))
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
    hum, hum_n = _wavg(hums)
    wind, wind_n = _wavg(winds)
    return {
        "pop": pop, "pop_n": pop_n, "pop_plain": pop_plain,
        "pop_min": min(valid) if valid else None,
        "pop_max": max(valid) if valid else None,
        "lead_days": lead, "weights": weights,
        "hi": _wavg(his)[0], "lo": _wavg(los)[0],
        # how much, not just how likely: a 60% chance of 0.02" is a
        # different afternoon from a 60% chance of half an inch
        "amt": amt, "amt_n": amt_n,
        "amt_max": max(valid_amt) if valid_amt else None,
        "amt_p90": _wavg_f(p90s)[0],
        "hum": hum, "hum_n": hum_n,
        "wind": wind, "wind_n": wind_n, "wdir": _mode(wdirs),
        # first source in priority order with a human-written phrase; the
        # Open-Meteo model rows would otherwise outvote weather.com/Foreca/NWS
        "cond": conds[0] if conds else None,
        "hourly": hourly,
        "hourly_available": any(x["n"] for x in hourly),
        "sun": sun_times(date, lat, lon, tz),
    }
