"""Fetch every enabled source for a place, cache the whole horizon, and
answer one date + event window from it."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import date as date_cls, datetime, timedelta, timezone

from . import summary as sm
from .sources import FetchContext, NotAvailableHere, coverage_range, fill_daily_from_hourly


class PastDate(ValueError):
    pass


class ForecastService:
    def __init__(self, cfg, sources, clock=time.time):
        self.cfg, self.sources, self.clock = cfg, list(sources), clock
        self.rules = sm.load_rules(cfg.rules_file)
        self._cache = OrderedDict()     # key -> (expires_at, generated_at, entries)
        self._inflight = {}             # key -> {"done": Event, "result": (generated, entries) | None}
        self._lock = threading.Lock()

    # -- public ---------------------------------------------------------------

    def today(self, place):
        return datetime.fromtimestamp(self.clock(), place.zone).date()

    def is_cached(self, place):
        with self._lock:
            hit = self._cache.get(self._key(place))
            return bool(hit and hit[0] > self.clock())

    def forecast(self, place, date, start, end):
        hours = sm.window_hours(start, end)
        today = self.today(place)
        state, available_from = sm.date_state(date, today)
        base = {"place": asdict(place), "date": date,
                "window": {"from": start, "to": end, "hours": hours}}
        if state == "past":
            raise PastDate("that date has already passed where the event is")
        if state == "too_far":
            return {**base, "status": "too_far", "available_from": available_from}
        generated_at, entries = self._load(place)
        out = [self._for_date(e, date, today) for e in entries]
        summary = sm.summarise_day(out, date, today, place.lat, place.lon, place.zone)
        return {**base, "status": "ok", "generated_at": generated_at, "sources": out,
                "summary": summary, "verdict": sm.verdict(summary, hours, self.rules),
                "sun": summary["sun"]}

    # -- cache ----------------------------------------------------------------

    def _key(self, place):
        # the zone and country change what is fetched (Open-Meteo's timezone=,
        # every parser's local hours, whether NWS is asked), so they are part
        # of the key: one odd link must not relabel the hours for everyone
        return place.cache_key() + (place.tz, place.country_code, tuple(s.id for s in self.sources))

    def _load(self, place):
        key = self._key(place)
        while True:
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] > self.clock():
                    self._cache.move_to_end(key)
                    return hit[1], hit[2]
                slot = self._inflight.get(key)
                owner = slot is None
                if owner:
                    slot = self._inflight[key] = {"done": threading.Event(), "result": None}
            if not owner:
                slot["done"].wait()
                if slot["result"] is not None:
                    return slot["result"]   # share the owner's answer, even an all-failed one
                continue                    # the owner raised: try ourselves
            try:
                entries = self._fetch_all(place)
                generated = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
                if any(e["ok"] for e in entries):   # never pin an all-failed answer for an hour
                    with self._lock:
                        self._cache[key] = (self.clock() + self.cfg.cache_ttl, generated, entries)
                        self._cache.move_to_end(key)
                        while len(self._cache) > self.cfg.cache_size:
                            self._cache.popitem(last=False)
                slot["result"] = (generated, entries)
                return generated, entries
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                slot["done"].set()

    # -- fetching -------------------------------------------------------------

    def _fetch_all(self, place):
        ctx = FetchContext(user_agent=self.cfg.user_agent(), timeout=self.cfg.source_timeout,
                           twc_key=self.cfg.twc_key)
        pool = ThreadPoolExecutor(max_workers=max(1, len(self.sources)))
        try:
            futs = {pool.submit(self._run_one, s, place, ctx): s for s in self.sources}
            done, _ = wait(futs, timeout=self.cfg.source_timeout)
            return [f.result() if f in done else self._entry(s, place, "unavailable", error="timed out")
                    for f, s in futs.items()]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _run_one(self, s, place, ctx):
        t0 = time.monotonic()
        try:
            res = s.fetch(place, ctx)
            daily, hourly = res[0], res[1]
            note = res[2] if len(res) > 2 else None
            fill_daily_from_hourly(daily, hourly)
            e = self._entry(s, place, "ok", note=note)
            e.update(daily_all=daily, coverage=coverage_range(daily),
                     hourly_all={d: dict(sorted(h.items())) for d, h in hourly.items() if h})
        except NotAvailableHere as exc:
            e = self._entry(s, place, "not_here", error=str(exc) or "not available here")
        except Exception as exc:  # noqa: BLE001 — one bad source must not sink the page
            e = self._entry(s, place, "unavailable", error=f"{type(exc).__name__}: {exc}"[:200])
        e["elapsed_s"] = round(time.monotonic() - t0, 1)
        return e

    @staticmethod
    def _entry(s, place, status, error=None, note=None):
        return {"id": s.id, "name": s.name, "short": s.short, "url": s.link(place),
                "note": " · ".join(x for x in (s.note, note) if x), "ok": status == "ok",
                "status": status, "error": error, "daily_all": {}, "hourly_all": {},
                "coverage": None, "elapsed_s": None}

    @staticmethod
    def _for_date(e, date, today):
        d = e["daily_all"].get(date)
        h = e["hourly_all"].get(date)
        covers_from = None
        if e["ok"] and d is None and e["daily_all"]:
            reach = (date_cls.fromisoformat(max(e["daily_all"])) - today).days
            covers_from = (date_cls.fromisoformat(date) - timedelta(days=max(reach, 0))).isoformat()
        out = {k: v for k, v in e.items() if k not in ("daily_all", "hourly_all")}
        out.update(daily={date: d} if d else {}, hourly={date: h} if h else {}, covers_from=covers_from)
        return out
