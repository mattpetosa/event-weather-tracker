from zoneinfo import ZoneInfo

from eventweather import sources as c
from util import fx, NY, FIXTURE_DAYS

def _valid_day(d):
    assert d["pop"] is None or 0 <= d["pop"] <= 100
    assert d["hi"] is None or 0 <= d["hi"] <= 120
    assert d["lo"] is None or -20 <= d["lo"] <= 100


def test_twc_daily_covers_event_days_with_values():
    d = c.parse_twc_daily(fx("twc_daily.json"), NY)
    assert len(d) == 15
    for day in FIXTURE_DAYS:
        assert day in d
        _valid_day(d[day])
        assert d[day]["pop"] is not None and d[day]["hi"] > d[day]["lo"]
    assert d["2026-08-24"]["pop"] is not None  # today falls back to night part

def test_twc_hourly_local_dates_and_24_hours():
    h = c.parse_twc_hourly(fx("twc_hourly.json"), NY)
    assert set(h["2026-09-06"].keys()) == set(range(24))
    assert all(0 <= v["pop"] <= 100 for v in h["2026-09-06"].values())

def test_openmeteo_daily_and_hourly():
    d, h = c.parse_openmeteo(fx("openmeteo_ecmwf.json"))
    assert len(d) == 16 and "2026-09-07" in d
    _valid_day(d["2026-09-06"])
    assert d["2026-09-06"]["cond"] is not None
    assert len(h["2026-09-06"]) == 24

def test_nws_daily_merges_day_and_night_periods():
    d = c.parse_nws_daily(fx("nws_daily.json"), NY)
    day = d["2026-08-25"]
    assert day["hi"] == 82 and day["lo"] is not None and day["pop"] == 15
    assert day["cond"]
    # first partial day still has its night low
    assert d["2026-08-24"]["lo"] == 62

def test_nws_hourly():
    h = c.parse_nws_hourly(fx("nws_hourly.json"), NY)
    assert len(h["2026-08-25"]) == 24
    assert h["2026-08-24"][13]["pop"] == 9

def test_metno_temps_only_and_partial_days_dropped():
    d, h = c.parse_metno(fx("metno.json"), NY)
    assert "2026-08-25" in d and "2026-09-01" in d  # 6h blocks reach ~9 days out
    assert d["2026-08-25"]["pop"] is None
    assert d["2026-08-25"]["hi"] > d["2026-08-25"]["lo"]
    assert "2026-09-03" not in d  # only 1 block that day → dropped
    assert h["2026-08-25"][12]["temp"] is not None


def test_ensemble_probability_is_member_share():
    d, h = c.parse_ensemble(fx("ensemble_ecmwf.json"))
    assert d["2026-09-06"]["members"] == 51
    # hi/lo are the member-mean curve's extremes, so they sit inside the members' spread
    h6 = fx("ensemble_ecmwf.json")["hourly"]
    idx = [i for i, t in enumerate(h6["time"]) if t.startswith("2026-09-06")]
    member_max = max(h6[k][i] for k in h6 if k.startswith("temperature") for i in idx)
    assert d["2026-09-06"]["hi"] < member_max
    for day in FIXTURE_DAYS:
        assert day in d and 0 <= d[day]["pop"] <= 100
        assert d[day]["hi"] > d[day]["lo"]
    assert len(h["2026-09-06"]) == 24
    assert all(0 <= v["pop"] <= 100 for v in h["2026-09-06"].values())
    # a day's chance can never be below its wettest single hour
    assert d["2026-09-06"]["pop"] >= max(v["pop"] for hr, v in h["2026-09-06"].items() if hr in c.DAY_WINDOW)

def test_ensemble_gefs_and_partial_first_day_dropped():
    d, h = c.parse_ensemble(fx("ensemble_gefs.json"))
    assert d["2026-09-06"]["members"] == 31
    first = min(h)
    assert len(h[first]) == 24  # open-meteo pads today from midnight

def test_ensemble_synthetic_counts():
    hours = ["2026-09-06T%02d:00" % i for i in range(24)]
    base = {"time": hours}
    # member 1 rains at noon, member 2 rains at 3am (outside the daytime window), members 3-4 dry
    base["precipitation_member01"] = [1.0 if i == 12 else 0 for i in range(24)]
    base["precipitation_member02"] = [1.0 if i == 3 else 0 for i in range(24)]
    base["precipitation_member03"] = [0.1] * 24   # a spread-out 6h block: counts every hour, and the day
    base["precipitation_member04"] = [0.005] * 24 # trace below both bars: never counts
    d, h = c.parse_ensemble({"hourly": base})
    assert d["2026-09-06"]["pop"] == 50           # members 1 and 3
    assert d["2026-09-06"]["hi"] is None and h["2026-09-06"][12]["hum"] is None
    assert h["2026-09-06"][12]["pop"] == 50       # members 1 and 3
    assert h["2026-09-06"][3]["pop"] == 50        # members 2 and 3
    assert h["2026-09-06"][5]["pop"] == 25        # member 3 only


def test_wind_text_forms():
    assert c._wind_from_text("SW 7 mph") == (7, "SW")
    assert c._wind_from_text("5 to 10 mph") == (8, None)
    assert c._wind_from_text("W 8 mph") == (8, "W")
    assert c._wind_from_text(None) == (None, None)
    assert c._cardinal(242.7) == "SW" and c._cardinal(0) == "N" and c._cardinal(350) == "N" and c._cardinal(100) == "E"

def test_humidity_wind_per_source():
    d = c.parse_twc_daily(fx("twc_daily.json"), NY)
    assert 0 < d["2026-09-06"]["hum"] <= 100 and d["2026-09-06"]["wind"] >= 0 and d["2026-09-06"]["wdir"]
    h = c.parse_twc_hourly(fx("twc_hourly.json"), NY)
    assert h["2026-09-06"][12]["hum"] and h["2026-09-06"][12]["wdir"]
    nd = c.parse_nws_daily(fx("nws_daily.json"), NY)
    assert nd["2026-08-25"]["wind"] == 5 and nd["2026-08-25"]["wdir"] == "W"
    nh = c.parse_nws_hourly(fx("nws_hourly.json"), NY)
    assert nh["2026-08-24"][18]["hum"] == 54 and nh["2026-08-24"][18]["wind"] == 5
    _, mh = c.parse_metno(fx("metno.json"), NY)
    v = next(iter(mh["2026-08-25"].values()))
    assert 0 < v["hum"] <= 100 and v["wind"] >= 0 and v["wdir"] in c._CARDINALS
    _, om = c.parse_openmeteo(fx("openmeteo_ecmwf.json"))
    assert om["2026-09-06"][12]["hum"] and om["2026-09-06"][12]["wdir"]
    fd = c.parse_foreca_daily(fx("foreca_daily.json"))["2026-09-01"]
    assert fd["hum"] == 62 and fd["wind"] == 4 and fd["wdir"] == "SW"
    ed, eh = c.parse_ensemble(fx("ensemble_ecmwf.json"))
    assert eh["2026-09-06"][12]["hum"] and eh["2026-09-06"][12]["wind"] is not None

def test_fill_daily_from_hourly_uses_daytime_only():
    daily = {"2026-09-06": {"pop": 10, "hi": 80, "lo": 60, "cond": None}}
    hourly = {"2026-09-06": {h: {"hum": 90 if h < 6 else 50, "wind": 20 if h < 6 else 10, "wdir": "N" if h < 6 else "SW"} for h in range(24)}}
    c.fill_daily_from_hourly(daily, hourly)
    e = daily["2026-09-06"]
    assert e["hum"] == 50 and e["wind"] == 10 and e["wdir"] == "SW"
    # a published daily figure is kept
    daily = {"2026-09-06": {"pop": 10, "hi": 80, "lo": 60, "cond": None, "hum": 70, "wind": 3, "wdir": "E"}}
    c.fill_daily_from_hourly(daily, hourly)
    assert daily["2026-09-06"]["hum"] == 70 and daily["2026-09-06"]["wdir"] == "E"
    # too few hours → None, not a guess
    daily = {"2026-09-06": {"pop": 10, "hi": 80, "lo": 60, "cond": None}}
    c.fill_daily_from_hourly(daily, {"2026-09-06": {12: {"hum": 50, "wind": 1, "wdir": "S"}}})
    assert daily["2026-09-06"]["hum"] is None and daily["2026-09-06"]["wdir"] is None

def test_coverage_is_null_when_a_source_returns_no_days():
    """[] is truthy in JavaScript: an empty coverage list made the page call
    .split() on it and throw out of render(), blanking the whole forecast
    because one source came back empty. Must be None, like the failure
    branch already writes."""
    assert c.coverage_range({}) is None
    assert c.coverage_range({"2026-09-05": {}, "2026-09-07": {}}) == \
        "2026-09-05 → 2026-09-07"


def test_openmeteo_survives_a_short_weather_code_array():
    """GEM stops returning weather_code past day 10 while still returning
    temperatures. Indexing a padded [None] * 99 covered a missing array but
    not a short one, which raised IndexError and failed the whole source."""
    d = fx("openmeteo_ecmwf.json")
    n = len(d["daily"]["time"])
    assert n > 2
    d["daily"]["weather_code"] = d["daily"]["weather_code"][:2]
    parsed = c.parse_openmeteo(d)[0]
    dates = d["daily"]["time"]
    assert len(parsed) == n
    assert parsed[dates[-1]]["cond"] is None
    assert parsed[dates[2]]["cond"] is None
    assert parsed[dates[2]]["hi"] is not None, "the rest of the day survives"
    del d["daily"]["weather_code"]
    assert all(v["cond"] is None for v in c.parse_openmeteo(d)[0].values())


def test_openmeteo_daily_pop_is_daytime_window_not_24h_max():
    """Open-Meteo's precipitation_probability_max covers the whole 24 h,
    including overnight; the other sources publish a *daytime* chance. The
    daily figure must be the max over DAY_WINDOW so the rows are comparable."""
    d = fx("openmeteo_ecmwf.json")
    date = "2026-09-06"
    idx = [i for i, t in enumerate(d["hourly"]["time"]) if t.startswith(date)]
    for i in idx:
        d["hourly"]["precipitation_probability"][i] = 10
    night = [i for i in idx if int(d["hourly"]["time"][i][11:13]) not in c.DAY_WINDOW]
    d["hourly"]["precipitation_probability"][night[0]] = 90
    d["daily"]["precipitation_probability_max"][d["daily"]["time"].index(date)] = 90
    day_i = [i for i in idx if int(d["hourly"]["time"][i][11:13]) in c.DAY_WINDOW][0]
    d["hourly"]["precipitation_probability"][day_i] = 35
    assert c.parse_openmeteo(d)[0][date]["pop"] == 35


def test_twc_hourly_amount_in_inches():
    h = c.parse_twc_hourly(fx("twc_hourly.json"), NY)
    day = h["2026-09-06"]
    assert all("amt" in v for v in day.values())
    assert all(v["amt"] is None or v["amt"] >= 0 for v in day.values())


def test_openmeteo_hourly_amount_is_inches_and_optional():
    d, h = c.parse_openmeteo(fx("openmeteo_ecmwf_precip.json"))
    amts = [v["amt"] for v in h["2026-09-06"].values()]
    assert len(amts) == 24 and all(a is not None for a in amts)
    assert all(0 <= a < 5 for a in amts), "inches, not millimetres"
    assert d["2026-09-06"]["amt"] == round(sum(
        h["2026-09-06"][hr]["amt"] for hr in c.DAY_WINDOW), 3)
    # a model queried without the precipitation field must still parse
    d2, h2 = c.parse_openmeteo(fx("openmeteo_ecmwf.json"))
    assert all(v["amt"] is None for v in h2["2026-09-06"].values())
    assert d2["2026-09-06"]["amt"] is None


def test_ensemble_amount_is_member_mean_with_a_p90_day_total():
    d, h = c.parse_ensemble(fx("ensemble_ecmwf.json"))
    day = d["2026-09-06"]
    assert day["amt"] is not None and day["amt"] >= 0
    assert day["amt_p90"] >= day["amt"], "the wet tail sits above the mean"
    # hourly means sum to the daytime day total
    assert day["amt"] == round(sum(h["2026-09-06"][hr]["amt"] for hr in c.DAY_WINDOW), 3)


def test_nws_grid_qpf_is_spread_evenly_over_its_block():
    q = c.parse_nws_grid_qpf(fx("nws_grid.json"), NY)
    # 2026-09-01T18:00Z/PT6H = 7.874 mm over 2pm-8pm local -> 0.052 in/h
    assert q["2026-09-01"][14] == round(7.874 / 25.4 / 6, 3)
    assert q["2026-09-01"][14] == q["2026-09-01"][19]
    assert 15 not in q.get("2026-08-31", {}), "no hours invented outside the blocks"


def test_metno_hourly_amount():
    d, h = c.parse_metno(fx("metno.json"), NY)
    first = h["2026-08-25"]
    assert any(v.get("amt") is not None for v in first.values())
    assert all(v.get("amt") is None or v["amt"] >= 0 for v in first.values())


def test_foreca_daily_converts_its_metric_units():
    """Foreca serves metric no matter what unit params you send: temps in C,
    wind in m/s, rain in mm. Verified 2026-09-01 against Open-Meteo (30C=86F
    vs 88.6F) and by correlating the 13-day wind series (x2.237 -> 7.2 mph
    mean vs 8.3; as-if-mph would be 3.2, far too low)."""
    d = c.parse_foreca_daily(fx("foreca_daily.json"))
    day = d["2026-09-01"]        # raw: tmax 30, tmin 22, winds 2, windd 211, rain 8.71
    assert day["hi"] == 86 and day["lo"] == 72
    assert day["wind"] == 4 and day["wdir"] == "SW"
    assert day["amt"] == 0.343
    assert day["pop"] == 70 and day["hum"] == 62
    assert "rain" in day["cond"].lower()


def test_foreca_daily_covers_the_event_days():
    d = c.parse_foreca_daily(fx("foreca_daily.json"))
    for day in FIXTURE_DAYS:
        assert day in d
        _valid_day(d[day])


def test_foreca_returns_no_hourly():
    """Foreca's hourly endpoint 404s, so the source is daily-only: it feeds the
    day tiles but must not claim hours it does not have."""
    daily, hourly = c.parse_foreca(fx("foreca_daily.json"))
    assert daily and all(not hours for hours in hourly.values())


def test_icon_ensemble_parses_all_forty_members():
    daily, hourly = c.parse_ensemble(fx("ensemble_icon.json"))
    for day in FIXTURE_DAYS:
        assert daily[day]["members"] == 40
        assert 0 <= daily[day]["pop"] <= 100
        assert daily[day]["hi"] > daily[day]["lo"]
        assert daily[day]["amt"] is not None


def test_icon_ensemble_reads_a_land_cell_not_the_sst_cell():
    """ECMWF's 0.25 cell at the fixture point is open Atlantic: sea level, and flat
    70-73F sea-surface temps EVERY day. ICON's land mask calls the same cell
    land, so unlike ECMWF it needs no longitude nudge. A marine cell is flat
    on every day, so the guard is the warmest day's swing, not the coolest --
    the member mean legitimately flattens on days the members disagree about
    (Sept 6 spans just 3F across 40 runs that individually swing 7-11F)."""
    raw = fx("ensemble_icon.json")
    assert raw["elevation"] > 0                      # 13m: land, not sea surface
    daily, hourly = c.parse_ensemble(raw)
    swings = [daily[d]["hi"] - daily[d]["lo"] for d in FIXTURE_DAYS]
    assert max(swings) > 8


def test_local_date_hour_on_a_dst_change_day():
    # 2026-11-01: US clocks fall back at 2am, so 1am happens twice
    assert c._local_date_hour("2026-11-01T05:30:00Z", NY) == ("2026-11-01", 1)
    assert c._local_date_hour("2026-11-01T06:30:00Z", NY) == ("2026-11-01", 1)
    assert c._local_date_hour("2026-11-01T07:30:00Z", NY) == ("2026-11-01", 2)
    # Sydney springs forward 2am -> 3am on 2026-10-04: the date is already the 4th
    syd = ZoneInfo("Australia/Sydney")
    assert c._local_date_hour("2026-10-03T15:30:00Z", syd) == ("2026-10-04", 1)
    assert c._local_date_hour("2026-10-03T16:30:00Z", syd) == ("2026-10-04", 3)


def test_parsers_follow_the_zone_they_are_given():
    """The same NWS hourly feed read in UTC moves every hour by 4 (EDT)."""
    ny = c.parse_nws_hourly(fx("nws_hourly.json"), NY)
    utc = c.parse_nws_hourly(fx("nws_hourly.json"), ZoneInfo("UTC"))
    assert ny["2026-08-24"][13]["pop"] == utc["2026-08-24"][17]["pop"]
