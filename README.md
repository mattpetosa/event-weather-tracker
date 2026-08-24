# weather.mhpwebserver.com — Red Bank, NJ · Sept 4–7, 2026

Consensus rain outlook for one weekend, built for phones. Every hour `collect.py`
pulls the Red Bank forecast from eight sources, averages the daily chance of rain
(and the hourly chance where a source publishes it), and writes `data/latest.json`.
`www/` is a static page that reads that file. There is no backend.

## Sources
| id | Source | How | Reach |
|---|---|---|---|
| twc | The Weather Channel (weather.com; same engine as Wunderground) | `api.weather.com` v3 with the public key embedded in wunderground.com | 15 days daily + hourly |
| accuweather | AccuWeather | server-rendered daily page + hourly pages, fetched with `curl_cffi` (Chrome TLS fingerprint — plain curl gets an Akamai 403) | 15 days daily, 4 days hourly |
| nws | National Weather Service | `api.weather.gov` gridpoints PHI | 7 days daily + hourly |
| ecmwf / gfs / gem / openmeteo | ECMWF, GFS, GEM, Open-Meteo blend | Open-Meteo `models=` | 16 days (GEM temps 10 days; ECMWF queried at lon -74.15 because its cell at -74.06 is ocean) |
| metno | MET Norway | `api.met.no` locationforecast | ~9 days, temperatures only |

A source failing is recorded on its own entry (`ok: false`) and never blocks the run.

## Run / test
    .venv/bin/python -m pytest -q tests     # parsers against fixtures in tests/fixtures
    .venv/bin/python collect.py             # live collection → data/latest.json (+ data/history/)
    cd www && python3 -m http.server 8799   # local preview (www/data → ../data symlink)

## Deploy (this box)
    sudo cp deploy/weather.mhpwebserver.com.conf /etc/nginx/sites-available/ && sudo ln -s ../sites-available/weather.mhpwebserver.com.conf /etc/nginx/sites-enabled/
    sudo cp deploy/weather-collect.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now weather-collect.timer
    sudo nginx -t && sudo systemctl reload nginx

Hash links open a day expanded, e.g. `https://weather.mhpwebserver.com/#2026-09-06`.
