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
    assert d["2026-08-24"] == {"pop": 25, "hi": 81, "lo": 62, "cond": "Partly sunny"}
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
    assert d["hourly"][12] == {"h": 12, "pop": 20, "n": 2, "temp": 78}
    assert d["hourly"][0]["pop"] is None
    assert s["2026-09-07"]["pop"] is None and s["2026-09-07"]["hourly_available"] is False
