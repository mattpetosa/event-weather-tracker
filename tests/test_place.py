import pytest

from eventweather import place as pl
from util import fx


def test_parse_geocode_labels_and_fields():
    res = pl.parse_geocode(fx("geocode_search.json"))
    first = res[0]
    assert first["label"] == "London, England, GB"
    assert first["tz"] == "Europe/London" and first["country_code"] == "GB"
    assert abs(first["lat"] - 51.5085) < 0.01 and abs(first["lon"] + 0.1257) < 0.01


def test_parse_geocode_skips_results_without_a_time_zone_and_dedups_label_parts():
    d = {"results": [
        {"name": "Nowhere", "latitude": 1, "longitude": 2},
        {"name": "Singapore", "admin1": "Singapore", "country_code": "SG",
         "latitude": 1.29, "longitude": 103.85, "timezone": "Asia/Singapore"},
        {"name": "Tiny", "country_code": "FR", "latitude": 45, "longitude": 3, "timezone": "Europe/Paris"},
    ]}
    res = pl.parse_geocode(d)
    assert [r["label"] for r in res] == ["Singapore, SG", "Tiny, FR"]


def test_parse_geocode_no_results():
    assert pl.parse_geocode({}) == [] and pl.parse_geocode({"results": None}) == []


def test_search_builds_the_query():
    seen = []
    res = pl.search("São Paulo", lambda url: seen.append(url) or {"results": []})
    assert res == []
    assert seen[0].startswith("https://geocoding-api.open-meteo.com/v1/search?")
    assert "name=S%C3%A3o+Paulo" in seen[0] and "count=8" in seen[0]


def test_from_params_valid_and_normalised():
    p = pl.from_params("  Asbury   Park, NJ ", "40.34705", "-74.06431", "America/New_York", "us")
    assert p == pl.Place("Asbury Park, NJ", 40.3471, -74.0643, "America/New_York", "US")
    assert p.cache_key() == (40.35, -74.06)
    assert p.zone.key == "America/New_York"


def test_from_params_defaults_the_name_to_coordinates():
    assert pl.from_params("", "51.5", "-0.12", "Europe/London").name == "51.500, -0.120"


def test_from_params_keeps_unicode_and_markup_as_text():
    assert pl.from_params("<b>Zürich</b>", "47.37", "8.54", "Europe/Zurich").name == "<b>Zürich</b>"


@pytest.mark.parametrize("lat,lon,tz,cc", [
    ("91", "0", "UTC", ""), ("0", "181", "UTC", ""), ("nan", "0", "UTC", ""),
    ("x", "0", "UTC", ""), ("0", "0", "Mars/Base", ""), ("0", "0", "../../etc/passwd", ""),
    ("0", "0", "", ""), ("0", "0", "UTC", "usa"),
])
def test_from_params_rejects_bad_input(lat, lon, tz, cc):
    with pytest.raises(ValueError):
        pl.from_params("x", lat, lon, tz, cc)


def test_from_params_truncates_long_names():
    assert len(pl.from_params("a" * 500, "0", "0", "UTC").name) == 120
