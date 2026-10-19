from datetime import date

from eventweather import summary as sm
from util import NJ, TODAY

D = "2026-09-06"


def day(srcs, date_=D, today=TODAY):
    return sm.summarise_day(srcs, date_, today, **NJ)


def test_summary_averages_only_available_values():
    srcs = [
        {"ok": True, "daily": {D: {"pop": 20, "hi": 80, "lo": 60, "cond": "Sunny"}},
         "hourly": {D: {"12": {"pop": 10, "temp": 78}}}},
        {"ok": True, "daily": {D: {"pop": 40, "hi": 84, "lo": None, "cond": "Drizzle"}},
         "hourly": {D: {"12": {"pop": 30, "temp": None}}}},
        {"ok": True, "daily": {D: {"pop": None, "hi": 82, "lo": 62, "cond": "Drizzle"}}, "hourly": {}},
        {"ok": False, "daily": {D: {"pop": 99, "hi": 99, "lo": 99, "cond": "X"}}, "hourly": {}},
    ]
    d = day(srcs)
    assert d["pop"] == 30 and d["pop_n"] == 2 and d["pop_min"] == 20 and d["pop_max"] == 40
    assert d["hi"] == 82 and d["lo"] == 61 and d["cond"] == "Sunny"
    assert d["hourly"][12] == {"h": 12, "pop": 20, "n": 2, "temp": 78, "hum": None,
                               "wind": None, "amt": None, "amt_n": 0}
    assert d["hourly"][0]["pop"] is None
    other = day(srcs, "2026-09-07")
    assert other["pop"] is None and other["hourly_available"] is False


def test_hourly_keys_may_be_ints():
    srcs = [{"ok": True, "daily": {}, "hourly": {D: {12: {"pop": 40, "temp": 70}}}}]
    assert day(srcs)["hourly"][12]["pop"] == 40


def test_lead_days_come_from_the_today_given():
    assert day([], today=date(2026, 9, 1))["lead_days"] == 5
    assert day([], today=date(2026, 9, 6))["lead_days"] == 0


def test_summary_humidity_wind():
    srcs = [
        {"ok": True, "daily": {D: {"pop": 20, "hi": 80, "lo": 60, "cond": "Sunny", "hum": 60, "wind": 8, "wdir": "SW"}}, "hourly": {}},
        {"ok": True, "daily": {D: {"pop": 30, "hi": 80, "lo": 60, "cond": None, "hum": 70, "wind": None, "wdir": "SW"}}, "hourly": {}},
        {"ok": True, "daily": {D: {"pop": 30, "hi": 80, "lo": 60, "cond": None, "hum": None, "wind": 12, "wdir": "W"}}, "hourly": {}},
    ]
    s = day(srcs)
    assert s["hum"] == 65 and s["hum_n"] == 2 and s["wind"] == 10 and s["wind_n"] == 2 and s["wdir"] == "SW"


def test_sun_times_new_jersey_sept_6():
    st = sm.sun_times(D, 40.347, -74.064, "America/New_York")
    # timeanddate.com for that point on 2026-09-06: sunrise ~6:27am, sunset ~7:20pm EDT
    assert st["sunrise"] in ("06:26", "06:27", "06:28")
    assert st["sunset"] in ("19:19", "19:20", "19:21")
    assert set(st) == {"sunrise", "sunset"}


def test_sun_times_far_from_utc_land_on_the_local_date():
    akl = sm.sun_times("2026-10-20", -36.85, 174.76, "Pacific/Auckland")
    assert "06:2" <= akl["sunrise"] <= "06:40" and "19:30" <= akl["sunset"] <= "19:50"
    hnl = sm.sun_times("2026-10-20", 21.31, -157.86, "Pacific/Honolulu")
    assert "06:20" <= hnl["sunrise"] <= "06:35" and "17:55" <= hnl["sunset"] <= "18:10"


def test_sun_times_polar_day_and_night_are_none():
    for d in ("2026-06-21", "2026-12-21"):
        assert sm.sun_times(d, 78.22, 15.65, "Arctic/Longyearbyen") == {"sunrise": None, "sunset": None}


def test_source_weight_by_lead_time():
    assert sm.source_weight("ecmwf_ens", 11) > sm.source_weight("gfs", 11)
    assert sm.source_weight("foreca", 11) < sm.source_weight("foreca", 3)
    assert sm.source_weight("nws", 2) > sm.source_weight("nws", 6)
    assert sm.source_weight(None, 5) == 1.0 and sm.source_weight("bogus", 5) == 1.0


def test_summary_headline_is_weighted_and_plain_average_kept():
    srcs = [
        {"id": "ecmwf_ens", "ok": True, "daily": {D: {"pop": 40, "hi": 80, "lo": 60}},
         "hourly": {D: {"12": {"pop": 40, "temp": 80}}}},
        {"id": "foreca", "ok": True, "daily": {D: {"pop": 0, "hi": 80, "lo": 60}},
         "hourly": {D: {"12": {"pop": 0, "temp": 80}}}},
    ]
    s = day(srcs, today=date(2026, 8, 26))
    assert s["pop_plain"] == 20
    assert s["pop"] > 20, "headline leans toward the ensemble"
    assert s["hourly"][12]["pop"] > 20
    assert s["pop_n"] == 2 and s["pop_min"] == 0 and s["pop_max"] == 40
    assert s["weights"]["ecmwf_ens"] > s["weights"]["foreca"]


def test_source_weight_pop_dedups_the_ensemble_twins():
    assert sm.source_weight("ecmwf", 5, "pop") < sm.source_weight("ecmwf", 5, "temp")
    assert sm.source_weight("gfs", 5, "pop") < sm.source_weight("gfs", 5, "temp")
    for sid in ("ecmwf_ens", "gefs", "icon_ens", "nws", "twc", "foreca", "gem"):
        assert sm.source_weight(sid, 5, "pop") == sm.source_weight(sid, 5, "temp")
    assert sm.source_weight("gem", 5, "pop") == 0.75
    assert sm.source_weight("ecmwf_ens", 5, "pop") > sm.source_weight("ecmwf", 5, "pop")
    assert sm.source_weight("ecmwf", 5) == sm.source_weight("ecmwf", 5, "temp")


def test_summary_pop_dedup_does_not_touch_temperature():
    srcs = [
        {"id": "ecmwf_ens", "ok": True, "daily": {D: {"pop": 60, "hi": 70, "lo": 60}}, "hourly": {}},
        {"id": "ecmwf", "ok": True, "daily": {D: {"pop": 60, "hi": 70, "lo": 60}}, "hourly": {}},
        {"id": "gem", "ok": True, "daily": {D: {"pop": 0, "hi": 90, "lo": 60}}, "hourly": {}},
    ]
    s = day(srcs)
    assert s["pop"] == 45      # 60*2.25/3.0
    assert s["hi"] == 74       # (70*3 + 90*.75)/3.75
    assert s["weights"]["ecmwf"] == 0.25


def test_summary_amount_is_weighted_hourly_and_daily():
    srcs = [
        {"id": "ecmwf_ens", "ok": True,
         "daily": {D: {"pop": 60, "hi": 70, "lo": 60, "amt": 0.4, "amt_p90": 1.0}},
         "hourly": {D: {"12": {"pop": 60, "temp": 70, "amt": 0.4}}}},
        {"id": "gem", "ok": True,
         "daily": {D: {"pop": 0, "hi": 70, "lo": 60, "amt": 0.0}},
         "hourly": {D: {"12": {"pop": 0, "temp": 70, "amt": 0.0}}}},
    ]
    s = day(srcs)
    assert s["amt"] == 0.291 and s["amt_max"] == 0.4 and s["amt_p90"] == 1.0
    assert s["hourly"][12]["amt"] == 0.291 and s["amt_n"] == 2


def test_summary_amount_absent_when_no_source_publishes_one():
    srcs = [{"id": "foreca", "ok": True, "daily": {D: {"pop": 70, "hi": 75, "lo": 65}}, "hourly": {}}]
    s = day(srcs)
    assert s["amt"] is None and s["amt_n"] == 0 and s["amt_p90"] is None


def test_new_sources_are_weighted_below_the_established_ensembles():
    assert sm.source_weight("icon_ens", 5) < sm.source_weight("gefs", 5)
    assert sm.source_weight("icon_ens", 5) > sm.source_weight("gem", 5)
    assert sm.source_weight("foreca", 11) < sm.source_weight("foreca", 3)
