import threading
import time
from datetime import datetime, timezone

import pytest

from eventweather.config import Config
from eventweather.forecast import ForecastService, PastDate
from eventweather.place import Place
from eventweather.sources import NotAvailableHere, Source

NOW = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc).timestamp()   # noon in New York
NYC = Place("New York", 40.71, -74.01, "America/New_York", "US")


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def day_data(dates, pop=30, hi=70):
    daily = {d: {"pop": pop, "hi": hi, "lo": hi - 15, "cond": None} for d in dates}
    hourly = {d: {h: {"pop": pop, "temp": hi - 5, "hum": 60, "wind": 8, "wdir": "W", "amt": 0.0}
                  for h in range(24)} for d in dates}
    return daily, hourly


def src(id_, fetch):
    return Source(id_, id_.upper(), id_, "note", fetch, lambda p: "https://example.org/" + id_)


def counting(dates, counter, delay=0.0, **kw):
    def fetch(place, ctx):
        counter.append(place.name)
        if delay:
            time.sleep(delay)
        return day_data(dates, **kw)
    return fetch


DATES = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"]


def service(sources, clock=None, **cfg):
    return ForecastService(Config(**cfg), sources, clock=clock or Clock())


def test_ok_response_shape():
    calls = []
    svc = service([src("a", counting(DATES, calls))])
    r = svc.forecast(NYC, "2026-10-24", "16:30", "18:30")
    assert r["status"] == "ok" and r["date"] == "2026-10-24"
    assert r["window"] == {"from": "16:30", "to": "18:30", "hours": [16, 17, 18]}
    assert r["place"]["name"] == "New York" and r["place"]["tz"] == "America/New_York"
    (a,) = r["sources"]
    assert a["status"] == "ok" and a["ok"] and a["url"] == "https://example.org/a"
    assert set(a["daily"]) == {"2026-10-24"} and set(a["hourly"]) == {"2026-10-24"}
    assert "daily_all" not in a and a["covers_from"] is None
    assert r["summary"]["pop"] == 30 and r["summary"]["lead_days"] == 5
    assert r["verdict"]["basis"] == "hourly" and r["sun"]["sunset"]


def test_cache_hit_makes_no_fetch_until_ttl():
    calls, clock = [], Clock()
    svc = service([src("a", counting(DATES, calls))], clock=clock, cache_ttl=3600)
    svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert svc.is_cached(NYC)
    svc.forecast(NYC, "2026-10-22", "09:00", "10:00")          # other date + window, same place
    assert len(calls) == 1
    clock.t += 3601
    assert not svc.is_cached(NYC)
    svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert len(calls) == 2


def test_nearby_points_share_the_cache():
    calls = []
    svc = service([src("a", counting(DATES, calls))])
    svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    svc.forecast(Place("Near", 40.712, -74.008, "America/New_York", "US"), "2026-10-20", "12:00", "13:00")
    assert len(calls) == 1


def test_concurrent_requests_share_one_fetch():
    calls = []
    svc = service([src("a", counting(DATES, calls, delay=0.3))])
    threads = [threading.Thread(target=svc.forecast, args=(NYC, "2026-10-20", "12:00", "13:00")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_a_failing_source_is_unavailable_and_left_out():
    def boom(place, ctx):
        raise RuntimeError("upstream 503")
    svc = service([src("a", counting(DATES, [], pop=10)), src("b", boom)])
    r = svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    b = r["sources"][1]
    assert b["status"] == "unavailable" and not b["ok"] and "upstream 503" in b["error"]
    assert r["summary"]["pop"] == 10 and r["summary"]["pop_n"] == 1


def test_not_available_here_is_not_an_error():
    def nope(place, ctx):
        raise NotAvailableHere("US forecasts only")
    r = service([src("nws", nope)]).forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert r["sources"][0]["status"] == "not_here" and r["sources"][0]["error"] == "US forecasts only"


def test_slow_source_times_out_without_holding_the_page():
    svc = service([src("a", counting(DATES, [])), src("slow", counting(DATES, [], delay=3))],
                  source_timeout=0.3)
    t0 = time.monotonic()
    r = svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert time.monotonic() - t0 < 1.5
    assert r["sources"][1]["status"] == "unavailable" and r["sources"][1]["error"] == "timed out"
    assert r["sources"][0]["status"] == "ok"


def test_all_sources_failing_is_not_cached():
    def boom(place, ctx):
        raise RuntimeError("down")
    svc = service([src("a", boom)])
    svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert not svc.is_cached(NYC)


def test_too_far_returns_when_without_fetching():
    calls = []
    r = service([src("a", counting(DATES, calls))]).forecast(NYC, "2026-11-04", "12:00", "13:00")
    assert r["status"] == "too_far" and r["available_from"] == "2026-10-20" and calls == []


def test_past_date_is_refused():
    with pytest.raises(PastDate):
        service([]).forecast(NYC, "2026-10-18", "12:00", "13:00")


def test_today_is_the_place_local_date():
    # 20:00 UTC on Oct 19 is 09:00 on Oct 20 in Auckland
    clock = Clock(datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc).timestamp())
    akl = Place("Auckland", -36.85, 174.76, "Pacific/Auckland", "NZ")
    svc = service([src("a", counting(["2026-10-20"], []))], clock=clock)
    assert str(svc.today(akl)) == "2026-10-20"
    with pytest.raises(PastDate):
        svc.forecast(akl, "2026-10-19", "12:00", "13:00")
    assert svc.forecast(akl, "2026-10-20", "12:00", "13:00")["summary"]["lead_days"] == 0


def test_covers_from_for_a_source_that_has_not_reached_the_date():
    short = DATES[:4]                                    # reaches today + 3
    r = service([src("a", counting(short, []))]).forecast(NYC, "2026-10-30", "12:00", "13:00")
    assert r["sources"][0]["covers_from"] == "2026-10-27"
    assert r["sources"][0]["daily"] == {}


def test_bad_window_is_a_value_error():
    with pytest.raises(ValueError):
        service([]).forecast(NYC, "2026-10-20", "13:00", "12:00")


def test_cache_is_bounded():
    svc = service([src("a", counting(DATES, []))], cache_size=2)
    places = [Place(f"p{i}", 40 + i, -74, "America/New_York") for i in range(3)]
    for p in places:
        svc.forecast(p, "2026-10-20", "12:00", "13:00")
    assert not svc.is_cached(places[0]) and svc.is_cached(places[2])


def test_note_from_the_fetcher_is_added_to_the_source_note():
    def with_note(place, ctx):
        d, h = day_data(DATES)
        return d, h, "nearest land grid cell used"
    r = service([src("a", with_note)]).forecast(NYC, "2026-10-20", "12:00", "13:00")
    assert r["sources"][0]["note"] == "note · nearest land grid cell used"


def test_cache_key_includes_the_zone_and_country():
    """Same coordinates, different zone: a crafted link must not relabel
    everyone else's hours (or skip NWS) for an hour."""
    calls = []
    svc = service([src("a", counting(DATES, calls))])
    svc.forecast(Place("x", 40.71, -74.01, "Asia/Tokyo", "JP"), "2026-10-20", "12:00", "13:00")
    svc.forecast(NYC, "2026-10-20", "12:00", "13:00")
    svc.forecast(Place("x", 40.71, -74.01, "America/New_York", "FR"), "2026-10-20", "12:00", "13:00")
    assert len(calls) == 3


def test_waiters_share_a_failed_fetch_instead_of_queueing():
    calls = []

    def slow_boom(place, ctx):
        calls.append(1)
        time.sleep(0.3)
        raise RuntimeError("down")
    svc = service([src("a", slow_boom)])
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        svc.forecast(NYC, "2026-10-20", "12:00", "13:00"))) for _ in range(4)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and time.monotonic() - t0 < 1.0
    assert all(r["sources"][0]["status"] == "unavailable" for r in results)
    assert not svc.is_cached(NYC)
