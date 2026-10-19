from urllib.parse import parse_qs, urlsplit

from eventweather import sources as s
from eventweather.place import Place

COAST = Place("Coast", 40.347, -74.064, "America/New_York", "US")
DAYS = ("2026-10-20", "2026-10-21", "2026-10-22")


def om_payload(swing, elevation=5.0):
    times, temps = [], []
    for d in DAYS:
        for h in range(24):
            times.append(f"{d}T{h:02d}:00")
            temps.append(60 + swing * (1 - abs(h - 14) / 14))
    return {"elevation": elevation,
            "daily": {"time": list(DAYS), "temperature_2m_max": [60 + swing] * 3,
                      "temperature_2m_min": [60] * 3, "precipitation_probability_max": [40] * 3},
            "hourly": {"time": times, "temperature_2m": temps,
                       "precipitation_probability": [40] * len(times), "precipitation": [0.5] * len(times),
                       "relative_humidity_2m": [80] * len(times), "wind_speed_10m": [9] * len(times)}}


class ByCell:
    """Fake get_json answering per (lat, lon) in the query string."""
    def __init__(self, cells, default):
        self.cells, self.default, self.calls = cells, default, []

    def __call__(self, url, headers, timeout):
        q = parse_qs(urlsplit(url).query)
        key = (q["latitude"][0], q["longitude"][0])
        self.calls.append(key)
        return self.cells.get(key, self.default)


def ctx(fake):
    return s.FetchContext(user_agent="t", get_json=fake)


def test_looks_like_sea():
    flat = s.parse_openmeteo(om_payload(2))[1]
    land = s.parse_openmeteo(om_payload(12))[1]
    assert s.looks_like_sea(flat) and not s.looks_like_sea(land)
    assert not s.looks_like_sea({}), "no data is not evidence of sea"


def test_one_flat_day_among_normal_days_is_not_sea():
    p = om_payload(12)
    for i in range(24):                      # flatten only the first day
        p["hourly"]["temperature_2m"][i] = 61
    assert not s.looks_like_sea(s.parse_openmeteo(p)[1])


def test_land_cell_is_used_as_is_with_no_extra_requests():
    fake = ByCell({}, om_payload(12))
    daily, hourly, note = s.fetch_openmeteo("ecmwf_ifs025")(COAST, ctx(fake))
    assert note is None and len(fake.calls) == 1


def test_sea_cell_retries_the_neighbours_and_takes_the_first_land_one():
    fake = ByCell({("40.3470", "-74.3140"): om_payload(2),      # west: also sea
                   ("40.3470", "-73.8140"): om_payload(12)},    # east: land
                  om_payload(2))
    daily, hourly, note = s.fetch_openmeteo("ecmwf_ifs025")(COAST, ctx(fake))
    assert fake.calls == [("40.3470", "-74.0640"), ("40.3470", "-74.3140"), ("40.3470", "-73.8140")]
    assert note == s.LAND_NOTE and daily["2026-10-21"]["hi"] == 72
    # remembered: the next fetch goes straight to the land cell
    fake.calls.clear()
    s.fetch_openmeteo("ecmwf_ifs025")(COAST, ctx(fake))
    assert fake.calls == [("40.3470", "-73.8140")]


def test_all_sea_keeps_rain_and_drops_temperatures():
    fake = ByCell({}, om_payload(2))
    daily, hourly, note = s.fetch_openmeteo("gem_seamless")(COAST, ctx(fake))
    assert note == s.SEA_NOTE and len(fake.calls) == 5
    d = daily["2026-10-21"]
    assert d["pop"] == 40 and d["amt"] is not None
    assert d["hi"] is None and d["lo"] is None and d["hum"] is None and d["wind"] is None
    h = hourly["2026-10-21"][12]
    assert h["pop"] == 40 and h["temp"] is None and h["hum"] is None and h["wind"] is None


def test_high_ground_skips_the_check():
    fake = ByCell({}, om_payload(2, elevation=1500))
    daily, hourly, note = s.fetch_openmeteo("gfs_seamless")(COAST, ctx(fake))
    assert note is None and len(fake.calls) == 1 and daily["2026-10-21"]["hi"] == 62


def test_neighbours_wrap_the_date_line():
    edge = Place("Edge", 10, 179.9, "Pacific/Fiji", "FJ")
    fake = ByCell({}, om_payload(2))
    s.fetch_openmeteo("gfs_seamless")(edge, ctx(fake))
    assert ("10.0000", "-179.8500") in fake.calls


def test_ensembles_get_the_same_check():
    ens = {"elevation": 3.0, "hourly": {"time": [f"{d}T{h:02d}:00" for d in DAYS for h in range(24)]}}
    n = len(ens["hourly"]["time"])
    ens["hourly"]["precipitation_member01"] = [0.0] * n
    ens["hourly"]["temperature_2m_member01"] = [70.0] * n
    fake = ByCell({}, ens)
    _, _, note = s.fetch_ensemble("ecmwf_ifs025")(COAST, ctx(fake))
    assert note == s.SEA_NOTE
