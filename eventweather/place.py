"""Where the event is: a small Place record and the place search."""
from __future__ import annotations

import math
import re
import urllib.parse
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search?"
NAME_MAX = 120


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    tz: str
    country_code: str | None = None
    elevation: float | None = None

    @property
    def zone(self):
        return ZoneInfo(self.tz)

    def cache_key(self):
        """About 1 km: close enough that every source answers the same."""
        return (round(self.lat, 2), round(self.lon, 2))


def _label(r):
    parts = []
    for p in (r.get("name"), r.get("admin1"), r.get("country_code")):
        if p and p not in parts:
            parts.append(p)
    return ", ".join(parts)


def parse_geocode(d):
    out = []
    for r in d.get("results") or []:
        if r.get("latitude") is None or r.get("longitude") is None or not r.get("timezone"):
            continue
        out.append({"label": _label(r),
                    "lat": round(float(r["latitude"]), 4), "lon": round(float(r["longitude"]), 4),
                    "tz": r["timezone"], "country_code": r.get("country_code"),
                    "elevation": r.get("elevation")})
    return out


def search(q, get_json, count=8):
    url = GEOCODE_URL + urllib.parse.urlencode(
        {"name": q, "count": count, "language": "en", "format": "json"})
    return parse_geocode(get_json(url))


def from_params(name, lat, lon, tz, cc=""):
    """Build a Place from request parameters, or raise ValueError."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("lat and lon must be numbers") from None
    if not (math.isfinite(lat_f) and -90 <= lat_f <= 90):
        raise ValueError("lat must be between -90 and 90")
    if not (math.isfinite(lon_f) and -180 <= lon_f <= 180):
        raise ValueError("lon must be between -180 and 180")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("unknown time zone") from None
    cc = (cc or "").strip().upper()
    if cc and not re.fullmatch(r"[A-Z]{2}", cc):
        raise ValueError("cc must be a two-letter country code")
    name = " ".join((name or "").split())[:NAME_MAX] or f"{lat_f:.3f}, {lon_f:.3f}"
    return Place(name=name, lat=round(lat_f, 4), lon=round(lon_f, 4), tz=tz, country_code=cc or None)
