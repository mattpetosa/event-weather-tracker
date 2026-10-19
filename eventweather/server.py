"""HTTP front door: the static page, place search and the forecast API."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import place as place_mod
from . import summary as sm
from .forecast import PastDate
from .sources import http_json

WWW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "www")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".json": "application/json", ".webmanifest": "application/manifest+json"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PLACES_TTL = 86400
PLACES_MAX = 1000


class BadRequest(Exception):
    pass


class RateLimiter:
    """Sliding-window counter per key. Memory is bounded: past max_keys the
    idle keys are dropped (and if that is not enough, all of them)."""

    def __init__(self, clock=time.monotonic, max_keys=10000):
        self.clock, self.max_keys = clock, max_keys
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, limit, window):
        now = self.clock()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._prune(now)
                q = self._hits[key] = deque()
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(q[0] + window - now) + 1)
            q.append(now)
            return True, 0

    def _prune(self, now, horizon=3600):
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - horizon]:
            del self._hits[k]
        if len(self._hits) >= self.max_keys:
            self._hits.clear()


class App:
    def __init__(self, cfg, service, www_dir, search=None, clock=time.monotonic):
        self.cfg, self.service, self.clock = cfg, service, clock
        self.www = os.path.realpath(www_dir)
        self.search = search or self._search_upstream
        self.limiter = RateLimiter(clock)
        self._places = {}
        self._places_lock = threading.Lock()

    def _search_upstream(self, q):
        return place_mod.search(
            q, lambda url: http_json(url, {"User-Agent": self.cfg.user_agent()}, self.cfg.source_timeout))

    def client_ip(self, handler):
        if self.cfg.trust_proxy:
            # the proxy appends the address it saw, so the last entry is the
            # one a visitor can't forge
            parts = [p.strip() for p in handler.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            if parts:
                return parts[-1]
        return handler.client_address[0]

    def places(self, qs, ip):
        q = " ".join(qs.get("q", [""])[0].split())
        if not 2 <= len(q) <= 100:
            raise BadRequest("type at least 2 characters")
        key = q.lower()
        with self._places_lock:
            hit = self._places.get(key)
            if hit and hit[0] > self.clock():
                return 200, {"results": hit[1]}, {}
        ok, retry = self.limiter.allow((ip, "places"), self.cfg.search_limit, 60)
        if not ok:
            return 429, {"error": "Too many searches; wait a moment."}, {"Retry-After": str(retry)}
        try:
            results = self.search(q)
        except Exception:  # noqa: BLE001
            return 502, {"error": "Place search is unavailable right now. Try again in a minute."}, {}
        with self._places_lock:
            if len(self._places) >= PLACES_MAX:
                self._places.clear()
            self._places[key] = (self.clock() + PLACES_TTL, results)
        return 200, {"results": results}, {}

    def forecast(self, qs, ip):
        def g(k):
            return qs.get(k, [""])[0].strip()
        try:
            p = place_mod.from_params(g("name"), g("lat"), g("lon"), g("tz"), g("cc"))
        except ValueError as e:
            raise BadRequest(str(e)) from None
        date = g("date")
        if not DATE_RE.fullmatch(date):
            raise BadRequest("date must be YYYY-MM-DD")
        try:
            date_cls.fromisoformat(date)
            sm.parse_window(g("from"), g("to"))
        except ValueError as e:
            raise BadRequest(str(e)) from None
        state, _ = sm.date_state(date, self.service.today(p))
        if state == "ok" and not self.service.is_cached(p):
            ok, retry = self.limiter.allow((ip, "forecast"), self.cfg.rate_limit, 3600)
            if not ok:
                return 429, {"error": f"Too many new forecasts from your address; try again in "
                                      f"{retry // 60 + 1} min."}, {"Retry-After": str(retry)}
        try:
            return 200, self.service.forecast(p, date, g("from"), g("to")), {}
        except PastDate as e:
            raise BadRequest(str(e)) from None


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EventWeather"
        sys_version = ""

        def do_GET(self):
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            try:
                if parts.path == "/api/places":
                    self._json(*app.places(qs, app.client_ip(self)))
                elif parts.path == "/api/forecast":
                    self._json(*app.forecast(qs, app.client_ip(self)))
                elif parts.path.startswith("/api/"):
                    self._json(404, {"error": "not found"}, {})
                else:
                    self._static(parts.path)
            except BadRequest as e:
                self._json(400, {"error": str(e)}, {})
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": "Something went wrong."}, {})

        def _send(self, code, ctype, data, extra):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, body, extra):
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._send(code, "application/json; charset=utf-8", data, {"Cache-Control": "no-store", **extra})

        def _static(self, path):
            rel = unquote(path).lstrip("/") or "index.html"
            if "\x00" in rel:
                return self._json(404, {"error": "not found"}, {})
            full = os.path.realpath(os.path.join(app.www, rel))
            if not full.startswith(app.www + os.sep) or not os.path.isfile(full):
                return self._json(404, {"error": "not found"}, {})
            with open(full, "rb") as f:
                data = f.read()
            ctype = TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            self._send(200, ctype, data, {"Cache-Control": "no-cache"})

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    return Handler


def make_server(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
