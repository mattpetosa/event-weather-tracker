# Event Weather Tracker

Will the weather hold for your event? Pick a place, a date and the hours that matter. The page
asks every major forecast for that day, combines them with skill-based weights, colours your event's
hours gold, and gives a verdict for just those hours: **Looks good**, **Keep an eye on: wind**, or
**Concern: rain**. Send the link to guests; it opens straight to the answer.

It's a small Python server with no required dependencies, made for hosting your own copy.

## Run it

**Plain Python** (3.11 or newer):

    git clone https://github.com/mattpetosa/event-weather-tracker
    cd event-weather-tracker
    EWT_CONTACT="you@example.org" python3 -m eventweather

Then open http://127.0.0.1:8080. On Windows, also run `pip install tzdata` if Python can't find
time zones.

**Docker:**

    docker compose up -d

Edit `compose.yaml` first to set `EWT_CONTACT`.

**Behind nginx with systemd:** see `deploy/event-weather-tracker.service` and
`deploy/nginx.example.conf`. Set `EWT_TRUST_PROXY=1` when a proxy sits in front, so rate limits
apply per visitor rather than to the proxy.

## Settings

All settings are environment variables.

| Variable | Default | What it does |
|---|---|---|
| `EWT_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Address to listen on |
| `EWT_PORT` | `8080` | Port |
| `EWT_CONTACT` | *(none)* | An email or URL for the User-Agent header. NWS and MET Norway ask every app to identify itself; set this. |
| `EWT_ENABLE_TWC` | `0` | Turn on weather.com (also needs `TWC_API_KEY`) |
| `TWC_API_KEY` | *(none)* | weather.com API key |
| `EWT_ENABLE_FORECA` | `0` | Turn on Foreca (needs `pip install -r requirements-foreca.txt`, or build Docker with `WITH_FORECA=1`) |
| `EWT_CACHE_TTL` | `3600` | Seconds to keep a place's forecasts |
| `EWT_CACHE_SIZE` | `100` | Places kept in memory (about 1 MB each) |
| `EWT_SOURCE_TIMEOUT` | `20` | Seconds before a slow source is marked unavailable |
| `EWT_RATE_LIMIT` | `30` | New (uncached) forecasts per visitor per hour |
| `EWT_SEARCH_LIMIT` | `60` | Place searches per visitor per minute |
| `EWT_TRUST_PROXY` | `0` | Read the visitor's address from `X-Forwarded-For` |
| `EWT_RULES_FILE` | *(none)* | JSON file overriding the verdict thresholds (below) |

### Verdict thresholds

Rain is the highest hourly chance during the event. Wind, humidity and temperature are averages over
the event's hours. Until hourly forecasts reach the date, the day's figures are used. Defaults:

```json
{
  "rain": {"good": 20, "watch": 35},
  "wind": {"good": 10, "watch": 15},
  "hum":  {"good": 65, "watch": 75},
  "temp": {"good": [65, 80], "watch": [55, 85]}
}
```

Rain and humidity are %, wind is mph, temperature is °F. A value at or under `good` is good, at or
under `watch` is worth watching, and anything above is a concern. For temperature, `good` and `watch`
are ranges. Put only the values you want to change in your `EWT_RULES_FILE`.

## Sources

| Source | Default | Notes |
|---|---|---|
| ECMWF, GEFS and ICON ensembles | on | Open-Meteo ensemble API. Rain % = share of model runs with measurable daytime rain |
| ECMWF, GFS, GEM | on | Open-Meteo forecast API |
| National Weather Service | on | US places only; reaches about 7 days |
| MET Norway (yr.no) | on | Temperatures only; publishes no rain chance |
| weather.com | **off** | Unofficial endpoint; needs `EWT_ENABLE_TWC=1` and `TWC_API_KEY` |
| Foreca | **off** | Unofficial endpoint, daily figures only; needs `EWT_ENABLE_FORECA=1` and `curl_cffi` |

**weather.com and Foreca use unofficial endpoints.** They aren't public APIs, can change or break
without notice, and their terms may not allow this use. That's why they are off by default; turning
them on is your call.

**Open-Meteo** is free for non-commercial use (see https://open-meteo.com/en/terms). An uncached
forecast costs about six Open-Meteo calls, a few more for coastal places (see below), and each place
is cached for an hour. Commercial hosts need an Open-Meteo plan.

Forecasts reach about 16 days ahead. A later date shows when it comes into range.

## How the number is made

- **Skill weights.** The headline is a weighted average: ECMWF ensemble ×2, GEFS ×1.5, NWS ×1.5
  (×2 inside 3 days), weather.com ×1.5, ECMWF/GFS ×1, ICON ensemble ×1, GEM ×0.75, Foreca ×1 inside
  a week and ×0.5 beyond. The page also shows the plain average and the lowest-to-highest range.
- **No double counting.** Open-Meteo derives the deterministic ECMWF and GFS rain chance from those
  models' own ensembles. For rain those two rows count ×0.25; their temperatures keep full weight.
- **Daytime chance.** Each source's daily rain chance is its daytime (6am–8pm) figure, so rows are
  comparable.
- **Rain amounts.** Amounts sit beside chances: a 60% chance of 0.02" is a different afternoon from
  60% of half an inch. Ensembles use the member mean, plus a 1-in-10 wet case.
- **Coastal places.** A model's grid cell can be mostly water, which gives flat sea-surface
  temperatures. When a row's temperatures are flat on every day, the server tries the neighbouring
  cells and uses the nearest land one. If there is none, it keeps that row's rain and leaves out its
  temperatures.
- **Time zones.** All times are local to the event, using the time zone the place search returns.

## API

- `GET /api/places?q=london` returns `{"results": [{"label", "lat", "lon", "tz", "country_code", "elevation"}]}`
- `GET /api/forecast?name=&lat=&lon=&tz=&cc=&date=YYYY-MM-DD&from=HH:MM&to=HH:MM` returns the summary, the
  verdict, the sunrise/sunset times and every source's figures for that day.
  - The response has `status: "too_far"` with `available_from` when the date is beyond the horizon.
  - The API returns 400 for bad input and 429 when rate-limited.

## Development

    python3 -m venv .venv && .venv/bin/pip install pytest
    .venv/bin/python -m pytest -q

Tests run offline against captured responses in `tests/fixtures/`.

## License

MIT
