"""Shared helpers for the tests. The fixtures are real responses captured in
Aug-Sep 2026 for a coastal New Jersey point (40.347, -74.064)."""
import json
import os
from datetime import date
from zoneinfo import ZoneInfo

FX = os.path.join(os.path.dirname(__file__), "fixtures")
NY = ZoneInfo("America/New_York")
NJ = {"lat": 40.347, "lon": -74.064, "tz": NY}
FIXTURE_DAYS = ["2026-09-05", "2026-09-06", "2026-09-07"]
TODAY = date(2026, 9, 1)


def fx(name):
    with open(os.path.join(FX, name)) as f:
        return json.load(f) if name.endswith(".json") else f.read()
