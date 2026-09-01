import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect as c

FX = os.path.join(os.path.dirname(__file__), "fixtures")
def fx(name):
    with open(os.path.join(FX, name)) as f:
        return json.load(f) if name.endswith(".json") else f.read()

def _valid_day(d):
    assert d["pop"] is None or 0 <= d["pop"] <= 100
    assert d["hi"] is None or 0 <= d["hi"] <= 120
    assert d["lo"] is None or -20 <= d["lo"] <= 100


def test_twc_daily_covers_event_days_with_values():
    d = c.parse_twc_daily(fx("twc_daily.json"))
    assert len(d) == 15
    for day in c.EVENT_DAYS:
        assert day in d
        _valid_day(d[day])
        assert d[day]["pop"] is not None and d[day]["hi"] > d[day]["lo"]
    assert d["2026-08-24"]["pop"] is not None  # today falls back to night part

def test_twc_hourly_local_dates_and_24_hours():
    h = c.parse_twc_hourly(fx("twc_hourly.json"))
    assert set(h["2026-09-06"].keys()) == set(range(24))
    assert all(0 <= v["pop"] <= 100 for v in h["2026-09-06"].values())

def test_openmeteo_daily_and_hourly():
    d, h = c.parse_openmeteo(fx("openmeteo_ecmwf.json"))
    assert len(d) == 16 and "2026-09-07" in d
    _valid_day(d["2026-09-06"])
    assert d["2026-09-06"]["cond"] is not None
    assert len(h["2026-09-06"]) == 24

def test_nws_daily_merges_day_and_night_periods():
    d = c.parse_nws_daily(fx("nws_daily.json"))
    day = d["2026-08-25"]
    assert day["hi"] == 82 and day["lo"] is not None and day["pop"] == 15
    assert day["cond"]
    # first partial day still has its night low
    assert d["2026-08-24"]["lo"] == 62

def test_nws_hourly():
    h = c.parse_nws_hourly(fx("nws_hourly.json"))
    assert len(h["2026-08-25"]) == 24
    assert h["2026-08-24"][13]["pop"] == 9

def test_metno_temps_only_and_partial_days_dropped():
    d, h = c.parse_metno(fx("metno.json"))
    assert "2026-08-25" in d and "2026-09-01" in d  # 6h blocks reach ~9 days out
    assert d["2026-08-25"]["pop"] is None
    assert d["2026-08-25"]["hi"] > d["2026-08-25"]["lo"]
    assert "2026-09-03" not in d  # only 1 block that day → dropped
    assert h["2026-08-25"][12]["temp"] is not None

def test_accuweather_daily_cards():
    d = c.parse_accuweather_daily(fx("accuweather_daily.html"), year_hint=2026)
    assert len(d) == 15
    assert d["2026-08-24"] == {"pop": 25, "hi": 81, "lo": 62, "cond": "Partly sunny", "hum": None, "wind": 7, "wdir": "SW"}
    assert d["2026-08-25"]["pop"] == 3
    for day in c.EVENT_DAYS:
        assert day in d and d[day]["pop"] is not None, day

def test_accuweather_year_rollover():
    page = fx("accuweather_daily.html").replace('sub date">8/', 'sub date">12/').replace('sub date">9/', 'sub date">1/')
    d = c.parse_accuweather_daily(page, year_hint=2026)
    assert "2027-01-07" in d

def test_accuweather_hourly_epoch_ids():
    h = c.parse_accuweather_hourly(fx("accuweather_hourly_day2.html"))
    assert "2026-08-25" in h
    day = h["2026-08-25"]
    assert len(day) >= 20
    assert all(v["pop"] is not None for v in day.values())
    assert all(v["temp"] is not None for v in day.values())

def test_summary_averages_only_available_values():
    srcs = [
        {"ok": True, "daily": {"2026-09-06": {"pop": 20, "hi": 80, "lo": 60, "cond": "Sunny"}},
         "hourly": {"2026-09-06": {"12": {"pop": 10, "temp": 78}}}},
        {"ok": True, "daily": {"2026-09-06": {"pop": 40, "hi": 84, "lo": None, "cond": "Drizzle"}},
         "hourly": {"2026-09-06": {"12": {"pop": 30, "temp": None}}}},
        {"ok": True, "daily": {"2026-09-06": {"pop": None, "hi": 82, "lo": 62, "cond": "Drizzle"}}, "hourly": {}},
        {"ok": False, "daily": {"2026-09-06": {"pop": 99, "hi": 99, "lo": 99, "cond": "X"}}, "hourly": {}},
    ]
    s = c.summarise(srcs, ["2026-09-06", "2026-09-07"])
    d = s["2026-09-06"]
    assert d["pop"] == 30 and d["pop_n"] == 2 and d["pop_min"] == 20 and d["pop_max"] == 40
    assert d["hi"] == 82 and d["lo"] == 61 and d["cond"] == "Sunny"
    assert d["hourly"][12] == {"h": 12, "pop": 20, "n": 2, "temp": 78, "hum": None,
                               "wind": None, "amt": None, "amt_n": 0}
    assert d["hourly"][0]["pop"] is None
    assert s["2026-09-07"]["pop"] is None and s["2026-09-07"]["hourly_available"] is False


def test_ensemble_probability_is_member_share():
    d, h = c.parse_ensemble(fx("ensemble_ecmwf.json"))
    assert d["2026-09-06"]["members"] == 51
    # hi/lo are the member-mean curve's extremes, so they sit inside the members' spread
    h6 = fx("ensemble_ecmwf.json")["hourly"]
    idx = [i for i, t in enumerate(h6["time"]) if t.startswith("2026-09-06")]
    member_max = max(h6[k][i] for k in h6 if k.startswith("temperature") for i in idx)
    assert d["2026-09-06"]["hi"] < member_max
    for day in c.EVENT_DAYS:
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
    d = c.parse_twc_daily(fx("twc_daily.json"))
    assert 0 < d["2026-09-06"]["hum"] <= 100 and d["2026-09-06"]["wind"] >= 0 and d["2026-09-06"]["wdir"]
    h = c.parse_twc_hourly(fx("twc_hourly.json"))
    assert h["2026-09-06"][12]["hum"] and h["2026-09-06"][12]["wdir"]
    nd = c.parse_nws_daily(fx("nws_daily.json"))
    assert nd["2026-08-25"]["wind"] == 5 and nd["2026-08-25"]["wdir"] == "W"
    nh = c.parse_nws_hourly(fx("nws_hourly.json"))
    assert nh["2026-08-24"][18]["hum"] == 54 and nh["2026-08-24"][18]["wind"] == 5
    _, mh = c.parse_metno(fx("metno.json"))
    v = next(iter(mh["2026-08-25"].values()))
    assert 0 < v["hum"] <= 100 and v["wind"] >= 0 and v["wdir"] in c._CARDINALS
    _, om = c.parse_openmeteo(fx("openmeteo_ecmwf.json"))
    assert om["2026-09-06"][12]["hum"] and om["2026-09-06"][12]["wdir"]
    ah = c.parse_accuweather_hourly(fx("accuweather_hourly_day2.html"))
    v = next(iter(ah["2026-08-25"].values()))
    assert v["hum"] and v["wind"] == 7 and v["wdir"] == "W"
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

def test_summary_humidity_wind():
    srcs = [
        {"ok": True, "daily": {"2026-09-06": {"pop": 20, "hi": 80, "lo": 60, "cond": "Sunny", "hum": 60, "wind": 8, "wdir": "SW"}}, "hourly": {}},
        {"ok": True, "daily": {"2026-09-06": {"pop": 30, "hi": 80, "lo": 60, "cond": None, "hum": 70, "wind": None, "wdir": "SW"}}, "hourly": {}},
        {"ok": True, "daily": {"2026-09-06": {"pop": 30, "hi": 80, "lo": 60, "cond": None, "hum": None, "wind": 12, "wdir": "W"}}, "hourly": {}},
    ]
    s = c.summarise(srcs, ["2026-09-06"])["2026-09-06"]
    assert s["hum"] == 65 and s["hum_n"] == 2 and s["wind"] == 10 and s["wind_n"] == 2 and s["wdir"] == "SW"


def test_sun_times_red_bank_sept_6():
    st = c.sun_times("2026-09-06")
    # timeanddate.com for Red Bank, NJ on 2026-09-06: sunrise ~6:27am, sunset ~7:20pm EDT
    assert st["sunrise"] in ("06:26", "06:27", "06:28")
    assert st["sunset"] in ("19:19", "19:20", "19:21")
    h, m = map(int, st["golden_start"].split(":"))
    assert 18 * 60 + 20 <= h * 60 + m <= 18 * 60 + 45   # ~40-55 min before sunset in September


def test_coverage_is_null_when_a_source_returns_no_days():
    """[] is truthy in JavaScript: an empty coverage list made the page call
    .split() on it and throw out of render(), blanking the whole forecast
    because one source came back empty. Must be None, like the failure
    branch already writes."""
    assert c.coverage_range({}) is None
    assert c.coverage_range({"2026-09-05": {}, "2026-09-07": {}}) == \
        "2026-09-05 → 2026-09-07"


def test_accuweather_card_does_not_inherit_the_next_day_s_wind():
    """The phrase and wind live in the panel after the card, so the search
    has to run past the card's </a> — but a fixed 2500-char window ran into
    the following day (real panels are 570–1120 chars apart). A day whose
    panel omitted the wind quietly reported tomorrow's."""
    page = fx("accuweather_daily.html")
    cards = list(c._ACCU_CARD.finditer(page))
    panel = page[cards[0].end():cards[1].start()]
    stripped = (page[:cards[0].end()]
                + c._ACCU_WIND.sub("", panel, count=1)
                + page[cards[1].start():])
    d = c.parse_accuweather_daily(stripped)
    assert d["2026-08-24"]["wind"] is None
    assert d["2026-08-24"]["wdir"] is None
    assert d["2026-08-25"]["wind"] == 8 and d["2026-08-25"]["wdir"] == "W"


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


def test_best_match_blend_is_not_a_source():
    """best_match resolves to GFS for this location, so listing it doubled
    GFS's weight in every average."""
    assert "openmeteo" not in {s["id"] for s in c.SOURCES}


def test_source_weight_by_lead_time():
    # ECMWF ensemble is the most skilful thing on the page at 1–2 weeks out
    assert c.source_weight("ecmwf_ens", 11) > c.source_weight("gfs", 11)
    # AccuWeather's 15-day is weak past a week; fine inside it
    assert c.source_weight("accuweather", 11) < c.source_weight("accuweather", 3)
    # NWS earns more trust as the day approaches
    assert c.source_weight("nws", 2) > c.source_weight("nws", 6)
    # unknown / test sources count as a plain 1.0
    assert c.source_weight(None, 5) == 1.0 and c.source_weight("bogus", 5) == 1.0


def test_summary_headline_is_weighted_and_plain_average_kept():
    srcs = [
        {"id": "ecmwf_ens", "ok": True, "daily": {"2026-09-06": {"pop": 40, "hi": 80, "lo": 60}},
         "hourly": {"2026-09-06": {"12": {"pop": 40, "temp": 80}}}},
        {"id": "accuweather", "ok": True, "daily": {"2026-09-06": {"pop": 0, "hi": 80, "lo": 60}},
         "hourly": {"2026-09-06": {"12": {"pop": 0, "temp": 80}}}},
    ]
    s = c.summarise(srcs, ["2026-09-06"], today="2026-08-26")["2026-09-06"]
    assert s["pop_plain"] == 20
    assert s["pop"] > 20, "headline leans toward the ensemble"
    assert s["hourly"][12]["pop"] > 20
    assert s["pop_n"] == 2 and s["pop_min"] == 0 and s["pop_max"] == 40
    assert s["weights"]["ecmwf_ens"] > s["weights"]["accuweather"]


# ---------------------------------------------------------------------------
# rain amount ("how much", not just "how likely") and family de-duplication
# ---------------------------------------------------------------------------

def test_source_weight_pop_dedups_the_ensemble_twins():
    """Open-Meteo derives precipitation_probability from a model's ensemble,
    so the deterministic ECMWF/GFS rows re-state their own ensemble row's
    opinion. They must not count as independent votes for rain."""
    assert c.source_weight("ecmwf", 5, "pop") < c.source_weight("ecmwf", 5, "temp")
    assert c.source_weight("gfs", 5, "pop") < c.source_weight("gfs", 5, "temp")
    # the ensembles themselves and the independent sources are untouched
    for sid in ("ecmwf_ens", "gefs", "nws", "twc", "accuweather", "gem"):
        assert c.source_weight(sid, 5, "pop") == c.source_weight(sid, 5, "temp")
    # GEM has no ensemble row on the page, so it is duplicating nothing
    assert c.source_weight("gem", 5, "pop") == 0.75
    # the ECMWF family still outweighs a lone deterministic run
    assert c.source_weight("ecmwf_ens", 5, "pop") > c.source_weight("ecmwf", 5, "pop")
    # default field keeps the old single-argument behaviour
    assert c.source_weight("ecmwf", 5) == c.source_weight("ecmwf", 5, "temp")


def test_summary_pop_dedup_does_not_touch_temperature():
    """The det rows keep full weight for temps — those are a real independent
    run — while their rain vote is discounted."""
    srcs = [
        {"id": "ecmwf_ens", "ok": True, "daily": {"2026-09-06": {"pop": 60, "hi": 70, "lo": 60}}, "hourly": {}},
        {"id": "ecmwf", "ok": True, "daily": {"2026-09-06": {"pop": 60, "hi": 70, "lo": 60}}, "hourly": {}},
        {"id": "gem", "ok": True, "daily": {"2026-09-06": {"pop": 0, "hi": 90, "lo": 60}}, "hourly": {}},
    ]
    s = c.summarise(srcs, ["2026-09-06"], today="2026-09-01")["2026-09-06"]
    # pop: ecmwf_ens 2.0 + ecmwf 0.25 vs gem 0.75  ->  60*2.25/3.0 = 45
    assert s["pop"] == 45
    # hi: ecmwf_ens 2.0 + ecmwf 1.0 vs gem 0.75  ->  (70*3 + 90*.75)/3.75 = 74
    assert s["hi"] == 74
    # the published weights describe the headline, which is the rain number
    assert s["weights"]["ecmwf"] == 0.25


def test_twc_hourly_amount_in_inches():
    h = c.parse_twc_hourly(fx("twc_hourly.json"))
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
    q = c.parse_nws_grid_qpf(fx("nws_grid.json"))
    # 2026-09-01T18:00Z/PT6H = 7.874 mm over 2pm-8pm local -> 0.052 in/h
    assert q["2026-09-01"][14] == round(7.874 / 25.4 / 6, 3)
    assert q["2026-09-01"][14] == q["2026-09-01"][19]
    assert 15 not in q.get("2026-08-31", {}), "no hours invented outside the blocks"


def test_metno_hourly_amount():
    d, h = c.parse_metno(fx("metno.json"))
    first = h["2026-08-25"]
    assert any(v.get("amt") is not None for v in first.values())
    assert all(v.get("amt") is None or v["amt"] >= 0 for v in first.values())


def test_summary_amount_is_weighted_hourly_and_daily():
    srcs = [
        {"id": "ecmwf_ens", "ok": True,
         "daily": {"2026-09-06": {"pop": 60, "hi": 70, "lo": 60, "amt": 0.4, "amt_p90": 1.0}},
         "hourly": {"2026-09-06": {"12": {"pop": 60, "temp": 70, "amt": 0.4}}}},
        {"id": "gem", "ok": True,
         "daily": {"2026-09-06": {"pop": 0, "hi": 70, "lo": 60, "amt": 0.0}},
         "hourly": {"2026-09-06": {"12": {"pop": 0, "temp": 70, "amt": 0.0}}}},
    ]
    s = c.summarise(srcs, ["2026-09-06"], today="2026-09-01")["2026-09-06"]
    # 0.4*2.0 / (2.0+0.75) = 0.291
    assert s["amt"] == 0.291
    assert s["amt_max"] == 0.4, "wettest source, for the range"
    assert s["amt_p90"] == 1.0
    assert s["hourly"][12]["amt"] == 0.291
    assert s["amt_n"] == 2


def test_summary_amount_absent_when_no_source_publishes_one():
    srcs = [{"id": "accuweather", "ok": True,
             "daily": {"2026-09-06": {"pop": 70, "hi": 75, "lo": 65}}, "hourly": {}}]
    s = c.summarise(srcs, ["2026-09-06"], today="2026-09-01")["2026-09-06"]
    assert s["amt"] is None and s["amt_n"] == 0 and s["amt_p90"] is None
