import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from eventweather import sources as s
from eventweather.config import Config
from eventweather.place import Place
from util import fx

NJ_PLACE = Place("Fixture point", 40.347, -74.064, "America/New_York", "US")
LONDON = Place("London", 51.5085, -0.1257, "Europe/London", "GB")


class Recorder:
    """A fake ctx.get_json: answers by URL substring, records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        for needle, resp in self.routes.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError("unexpected URL " + url)


def ctx(routes=None, browser=None, **kw):
    return s.FetchContext(user_agent="ewt-test (ops@example.org)", timeout=7,
                          get_json=Recorder(routes or {}), get_browser_json=browser, **kw)


def test_default_registry_is_off_for_the_unofficial_sources():
    ids = [x.id for x in s.enabled_sources(Config())]
    assert ids == ["nws", "ecmwf_ens", "gefs", "icon_ens", "ecmwf", "gfs", "gem", "metno"]


def test_twc_needs_the_flag_and_a_key(monkeypatch):
    assert "twc" not in [x.id for x in s.enabled_sources(Config(enable_twc=True))]
    assert "twc" in [x.id for x in s.enabled_sources(Config(enable_twc=True, twc_key="k"))]


def test_foreca_needs_the_flag_and_curl_cffi(monkeypatch):
    monkeypatch.setattr(s, "have_curl_cffi", lambda: False)
    assert "foreca" not in [x.id for x in s.enabled_sources(Config(enable_foreca=True))]
    monkeypatch.setattr(s, "have_curl_cffi", lambda: True)
    ids = [x.id for x in s.enabled_sources(Config(enable_foreca=True, enable_twc=True, twc_key="k"))]
    assert ids[:3] == ["twc", "foreca", "nws"], "condition-phrase priority order"


def test_best_match_blend_is_not_a_source():
    """best_match resolved to GFS at the fixture point and double-counted it."""
    assert "openmeteo" not in {x.id for x in s.SOURCES}


def test_openmeteo_asks_in_the_place_zone_and_coordinates():
    c = ctx({"api.open-meteo.com/v1/forecast": fx("openmeteo_ecmwf.json")})
    daily, hourly, *_ = s.fetch_openmeteo("gem_seamless")(LONDON, c)
    url, headers, timeout = c.get_json.calls[0]
    q = parse_qs(urlsplit(url).query)
    assert q["timezone"] == ["Europe/London"] and q["models"] == ["gem_seamless"]
    assert q["latitude"] == ["51.5085"] and q["longitude"] == ["-0.1257"]
    assert headers["User-Agent"].startswith("ewt-test") and timeout == 7
    assert daily


def test_ensemble_asks_in_the_place_zone():
    c = ctx({"ensemble-api.open-meteo.com": fx("ensemble_icon.json")})
    s.fetch_ensemble("icon_seamless")(LONDON, c)
    q = parse_qs(urlsplit(c.get_json.calls[0][0]).query)
    assert q["timezone"] == ["Europe/London"] and q["models"] == ["icon_seamless"]


def test_nws_is_skipped_outside_the_us_without_a_request():
    c = ctx()
    with pytest.raises(s.NotAvailableHere):
        s.fetch_nws(LONDON, c)
    assert c.get_json.calls == []


def test_nws_404_means_not_available_here():
    unknown = Place("Somewhere", 10, 10, "UTC")       # no country code: try it
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    with pytest.raises(s.NotAvailableHere):
        s.fetch_nws(unknown, ctx({"api.weather.gov/points": err}))


def test_nws_happy_path_reads_all_three_products():
    pts = {"properties": {"forecast": "https://api.weather.gov/F", "forecastHourly": "https://api.weather.gov/H",
                          "forecastGridData": "https://api.weather.gov/G"}}
    c = ctx({"/points/40.3470,-74.0640": pts, "/F": fx("nws_daily.json"),
             "/H": fx("nws_hourly.json"), "/G": fx("nws_grid.json")})
    daily, hourly = s.fetch_nws(NJ_PLACE, c)
    assert daily["2026-08-25"]["hi"] == 82 and hourly["2026-08-24"][13]["pop"] == 9
    assert all(h["User-Agent"].startswith("ewt-test") for _, h, _ in c.get_json.calls)


def test_metno_rounds_to_four_decimals_and_sends_the_contact():
    c = ctx({"api.met.no": fx("metno.json")})
    s.fetch_metno(Place("p", 40.347123, -74.064987, "America/New_York"), c)
    url, headers, _ = c.get_json.calls[0]
    assert "lat=40.3471&lon=-74.0650" in url and "ops@example.org" in headers["User-Agent"]


def test_twc_uses_the_key_and_a_browser_user_agent():
    c = ctx({"daily/15day": fx("twc_daily.json"), "hourly/15day": fx("twc_hourly.json")}, twc_key="KEY")
    daily, hourly = s.fetch_twc(NJ_PLACE, c)
    url, headers, _ = c.get_json.calls[0]
    assert "geocode=40.347,-74.064" in url and "apiKey=KEY" in url
    assert headers["User-Agent"] == s.BROWSER_UA and daily


def test_foreca_finds_its_location_id_once_per_place():
    loc = fx("foreca_location.json")
    calls = []

    def browser(url, timeout):
        calls.append(url)
        return loc if "/locations/" in url else fx("foreca_daily.json")
    c = ctx(browser=browser)
    s.fetch_foreca(NJ_PLACE, c)
    s.fetch_foreca(NJ_PLACE, c)
    assert calls[0] == "https://api.foreca.net/locations/-74.0640,40.3470.json?lang=en"
    assert sum("/locations/" in u for u in calls) == 1
    assert f"/data/daily/{loc['id']}.json" in calls[1]


def test_links_are_built_for_the_place():
    by_id = {x.id: x for x in s.SOURCES}
    assert by_id["twc"].link(NJ_PLACE) == "https://weather.com/weather/tenday/l/40.347,-74.064"
    assert by_id["nws"].link(NJ_PLACE) == "https://forecast.weather.gov/MapClick.php?lat=40.3470&lon=-74.0640"
    assert by_id["metno"].link(NJ_PLACE) == "https://www.yr.no/"
