import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from eventweather.config import Config
from eventweather.forecast import ForecastService
from eventweather.server import WWW_DIR, App, RateLimiter, make_server
from eventweather.sources import Source

NOW = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc).timestamp()
DATES = [f"2026-10-{d}" for d in range(19, 27)]


def fake_fetch(place, ctx):
    daily = {d: {"pop": 20, "hi": 70, "lo": 55, "cond": "Sunny"} for d in DATES}
    hourly = {d: {h: {"pop": 20, "temp": 68, "hum": 60, "wind": 7, "wdir": "W", "amt": 0.0}
                  for h in range(24)} for d in DATES}
    return daily, hourly


def boot(search=None, **cfg):
    cfg = Config(**cfg)
    svc = ForecastService(cfg, [Source("a", "A", "a", "", fake_fetch, lambda p: "https://example.org")],
                          clock=lambda: NOW)
    app = App(cfg, svc, WWW_DIR, search=search or (lambda q: [{"label": q, "lat": 1, "lon": 2, "tz": "UTC"}]))
    httpd = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def server():
    httpd = boot()
    yield httpd
    httpd.shutdown()


def get(httpd, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def fc(**over):
    q = {"name": "Test", "lat": "40.71", "lon": "-74.01", "tz": "America/New_York", "cc": "US",
         "date": "2026-10-24", "from": "16:30", "to": "18:30"}
    q.update(over)
    from urllib.parse import urlencode
    return "/api/forecast?" + urlencode(q)


def test_index_and_security_headers(server):
    status, h, body = get(server, "/")
    assert status == 200 and h["Content-Type"].startswith("text/html")
    assert "script-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "same-origin"
    assert b"Event Weather Tracker" in body


def test_static_types(server):
    assert get(server, "/app.js")[1]["Content-Type"].startswith("text/javascript")
    assert get(server, "/style.css")[1]["Content-Type"].startswith("text/css")


@pytest.mark.parametrize("path", ["/../README.md", "/%2e%2e/README.md", "/..%2fREADME.md",
                                  "/nope.html", "/%00", "/data/latest.json"])
def test_static_never_leaves_www(server, path):
    assert get(server, path)[0] == 404


def test_places(server):
    status, h, body = get(server, "/api/places?q=London")
    assert status == 200 and json.loads(body)["results"][0]["label"] == "London"
    assert h["Cache-Control"] == "no-store"
    assert get(server, "/api/places?q=a")[0] == 400


def test_places_upstream_failure_is_502():
    def broken(q):
        raise OSError("down")
    httpd = boot(search=broken)
    try:
        status, _, body = get(httpd, "/api/places?q=London")
        assert status == 502 and "unavailable" in json.loads(body)["error"]
    finally:
        httpd.shutdown()


def test_forecast_ok_and_name_round_trips(server):
    status, _, body = get(server, fc(name="<b>Zürich</b> & São Paulo"))
    data = json.loads(body)
    assert status == 200 and data["status"] == "ok"
    assert data["place"]["name"] == "<b>Zürich</b> & São Paulo"
    assert data["window"]["hours"] == [16, 17, 18]


@pytest.mark.parametrize("over", [{"lat": "95"}, {"lon": "x"}, {"tz": "Nowhere/City"}, {"date": "2026-10-18"},
                                  {"date": "2026-13-01"}, {"date": "20261024"}, {"from": "18:30", "to": "16:30"},
                                  {"from": "4pm"}, {"cc": "USA"}])
def test_forecast_bad_input_is_400(server, over):
    status, _, body = get(server, fc(**over))
    assert status == 400 and json.loads(body)["error"]


def test_too_far_is_200_with_status(server):
    status, _, body = get(server, fc(date="2026-11-10"))
    assert status == 200 and json.loads(body)["status"] == "too_far"


def test_rate_limit_counts_only_new_places():
    httpd = boot(rate_limit=1)
    try:
        assert get(httpd, fc())[0] == 200
        assert get(httpd, fc(date="2026-10-25"))[0] == 200            # same place: cached, free
        assert get(httpd, fc(date="2026-11-10", lat="10"))[0] == 200  # too far: no fetch, free
        status, h, _ = get(httpd, fc(lat="41.5"))                     # new place: over the limit
        assert status == 429 and int(h["Retry-After"]) > 0
    finally:
        httpd.shutdown()


def test_forwarded_for_only_when_trusted():
    for trust, expect in ((False, 429), (True, 200)):
        httpd = boot(rate_limit=1, trust_proxy=trust)
        try:
            get(httpd, fc(lat="30"), {"X-Forwarded-For": "203.0.113.1"})
            status = get(httpd, fc(lat="31"), {"X-Forwarded-For": "203.0.113.2"})[0]
            assert status == expect
        finally:
            httpd.shutdown()


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert rl.allow("k", 2, 60) == (True, 0) and rl.allow("k", 2, 60)[0]
    ok, retry = rl.allow("k", 2, 60)
    assert not ok and 0 < retry <= 61
    t[0] = 61
    assert rl.allow("k", 2, 60)[0]


def test_unknown_api_path_is_404(server):
    assert get(server, "/api/nope")[0] == 404


def test_idle_connections_are_dropped():
    """A client that sends half a request must not hold a thread forever."""
    import socket
    import time as _time
    cfg = Config()
    svc = ForecastService(cfg, [], clock=lambda: NOW)
    app = App(cfg, svc, WWW_DIR)
    assert app.conn_timeout == 30
    app.conn_timeout = 0.5
    httpd = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")      # never finish the headers
        t0 = _time.monotonic()
        assert s.recv(1024) == b""                       # server hangs up
        assert _time.monotonic() - t0 < 3
        s.close()
    finally:
        httpd.shutdown()
