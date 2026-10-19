import json
from datetime import date

import pytest

from eventweather import summary as sm


def summary_with(hours=None, **day):
    hourly = [{"h": h, "pop": None, "n": 0, "temp": None, "hum": None, "wind": None, "amt": None, "amt_n": 0}
              for h in range(24)]
    for h, vals in (hours or {}).items():
        hourly[h].update(vals)
    base = {"pop": None, "hi": None, "wind": None, "hum": None}
    base.update(day)
    base["hourly"] = hourly
    return base


@pytest.mark.parametrize("start,end,hours", [
    ("16:30", "18:30", [16, 17, 18]),
    ("16:00", "18:00", [16, 17]),
    ("16:45", "17:15", [16, 17]),
    ("00:00", "23:59", list(range(24))),
    ("09:00", "09:15", [9]),
])
def test_window_hours(start, end, hours):
    assert sm.window_hours(start, end) == hours


@pytest.mark.parametrize("start,end", [("18:00", "16:00"), ("16:00", "16:00"), ("24:00", "24:30"),
                                       ("4pm", "6pm"), ("", "18:00"), ("16:60", "18:00")])
def test_bad_windows_are_refused(start, end):
    with pytest.raises(ValueError):
        sm.parse_window(start, end)


def test_date_states():
    today = date(2026, 10, 19)
    assert sm.date_state("2026-10-18", today) == ("past", None)
    assert sm.date_state("2026-10-19", today) == ("ok", None)
    assert sm.date_state("2026-11-03", today) == ("ok", None)          # today + 15
    assert sm.date_state("2026-11-04", today) == ("too_far", "2026-10-20")


def test_all_good_on_hourly_basis():
    s = summary_with({16: {"pop": 10, "wind": 6, "hum": 50, "temp": 72, "amt": 0.0},
                      17: {"pop": 15, "wind": 8, "hum": 55, "temp": 70, "amt": 0.0}})
    v = sm.verdict(s, [16, 17])
    assert v["grade"] == "good" and v["label"] == "Looks good" and v["basis"] == "hourly"
    assert {c["key"]: c["value"] for c in v["checks"]} == {"rain": 15, "wind": 7, "hum": 52, "temp": 71}
    assert v["window_amt"] == 0.0 and v["no_data"] == []


@pytest.mark.parametrize("pop,grade", [(20, "good"), (21, "watch"), (35, "watch"), (36, "concern")])
def test_rain_is_the_peak_hour(pop, grade):
    s = summary_with({16: {"pop": 5}, 17: {"pop": pop}})
    assert {c["key"]: c["grade"] for c in sm.verdict(s, [16, 17])["checks"]}["rain"] == grade


@pytest.mark.parametrize("t,grade", [(65, "good"), (80, "good"), (64, "watch"), (85, "watch"),
                                     (55, "watch"), (54, "concern"), (86, "concern")])
def test_temperature_ranges(t, grade):
    s = summary_with({12: {"pop": 0, "temp": t}})
    assert {c["key"]: c["grade"] for c in sm.verdict(s, [12])["checks"]}["temp"] == grade


def test_worst_check_wins_and_labels_name_them():
    s = summary_with({12: {"pop": 50, "wind": 12, "hum": 70, "temp": 72}})
    v = sm.verdict(s, [12])
    assert v["grade"] == "concern" and v["label"] == "Concern: rain"
    s = summary_with({12: {"pop": 10, "wind": 12, "hum": 70, "temp": 72}})
    assert sm.verdict(s, [12])["label"] == "Keep an eye on: wind, humidity"


def test_daily_fallback_when_hourly_has_not_reached_the_date():
    s = summary_with(pop=30, hi=88, wind=4, hum=60)
    v = sm.verdict(s, [16, 17])
    assert v["basis"] == "daily" and v["window_amt"] is None
    assert {c["key"]: c["value"] for c in v["checks"]} == {"rain": 30, "wind": 4, "hum": 60, "temp": 88}
    assert v["grade"] == "concern"


def test_hourly_gaps_fall_back_per_field_to_the_day():
    s = summary_with({16: {"pop": 10}}, wind=9, hum=50, hi=70)
    v = sm.verdict(s, [16])
    assert {c["key"]: c["value"] for c in v["checks"]} == {"rain": 10, "wind": 9, "hum": 50, "temp": 70}


def test_no_data_at_all():
    v = sm.verdict(summary_with(), [16])
    assert v["grade"] is None and v["checks"] == []
    assert v["label"] == "No forecast for the event window yet"
    assert v["no_data"] == ["rain", "wind", "humidity", "temperature"]


def test_rules_file_overrides(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"wind": {"good": 5}, "temp": {"good": [60, 70]}}))
    rules = sm.load_rules(str(p))
    assert rules["wind"] == {"good": 5, "watch": 15} and rules["temp"]["good"] == [60, 70]
    assert sm.DEFAULT_RULES["wind"]["good"] == 10, "defaults untouched"
    s = summary_with({12: {"pop": 0, "wind": 6, "temp": 75}})
    grades = {c["key"]: c["grade"] for c in sm.verdict(s, [12], rules)["checks"]}
    assert grades["wind"] == "watch" and grades["temp"] == "watch"


@pytest.mark.parametrize("body", ['{"snow": {"good": 1}}', '{"wind": {"great": 1}}', '{"wind": {"good": "x"}}',
                                  '{"temp": {"good": [80, 60]}}', '[1]', 'not json'])
def test_bad_rules_files_are_refused(tmp_path, body):
    p = tmp_path / "rules.json"
    p.write_text(body)
    with pytest.raises(ValueError):
        sm.load_rules(str(p))
