# Wedding Weather Tracker

Multi-source rain outlook for one weekend, built for phones, live at https://weather.mhpwebserver.com. Pulls weather.com, Foreca, NWS, ECMWF/GFS/GEM and the ECMWF+GEFS+ICON ensembles hourly, averages them with skill-based weights, and shows a ceremony-window verdict for the key day. Fork it: change `LOCATION`, `EVENT_DAYS`, `KEY_DAY` and `KEY_WINDOW` for your own event.

Consensus rain outlook for one weekend, built for phones. Every hour `collect.py`
pulls the Red Bank forecast from ten sources, averages the daily chance of rain
(and the hourly chance where a source publishes it), and writes `data/latest.json`.
`www/` is a static page that reads that file. There is no backend.

## Sources
| id | Source | How | Reach |
|---|---|---|---|
| twc | The Weather Channel (weather.com; same engine as Wunderground) | `api.weather.com` v3 with the public key embedded in wunderground.com | 15 days daily + hourly |
| foreca | Foreca | daily JSON from `api.foreca.net`, fetched with `curl_cffi` (Chrome TLS fingerprint). Serves metric whatever unit params you send: °C, m/s, mm — convert on the way in | 13 days, **daily only** (the hourly endpoint 404s) |
| ecmwf_ens / gefs / icon_ens | ECMWF ensemble (51 runs), GEFS ensemble (31 runs), ICON ensemble (40 runs) | Open-Meteo ensemble API; rain % = share of members with ≥0.254 mm over 6am–8pm (hourly: ≥0.05 mm/h, because 6-hourly blocks are spread evenly across hours past ~day 6); temps = member mean | 16 days (GEFS needs `gfs_seamless`; `gfs025` is None past 10 days) |
| nws | National Weather Service | `api.weather.gov` gridpoints PHI | 7 days daily + hourly |
| ecmwf / gfs / gem | ECMWF, GFS, GEM (no best_match blend — it resolves to GFS here and double-counted it; daily rain % is the daytime-window max, not the 24 h max) | Open-Meteo `models=` | 16 days (GEM temps 10 days; ECMWF queried at lon -74.15 because its cell at -74.06 is ocean) |
| metno | MET Norway | `api.met.no` locationforecast | ~9 days, temperatures only |

Each source also yields humidity, wind speed (mph) and direction: the source's own daily figure where it publishes one (TWC dayparts, NWS wind text, Foreca's daily fields), otherwise the 6am–8pm average of its hourly data (`fill_daily_from_hourly`). Tiles show the cross-source average; direction is the most common compass point.

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

## Headline weighting

The tile headline is a **skill-weighted** mean, not a plain average (`source_weight()` in `collect.py`; the plain average is kept as `pop_plain`). Weights by lead time: ECMWF ensemble ×2, GEFS ×1.5, NWS ×1.5 (×2 inside 3 days), weather.com ×1.5, ECMWF/GFS ×1, ICON ensemble ×1, GEM ×0.75, Foreca ×1 inside a week and ×0.5 beyond. The ICON ensemble is deliberately held below ECMWF ENS and GEFS: a third ensemble should add a view, not tilt the headline wet on member count alone. The same weights apply to the hourly bars and the ceremony verdict. The min–max band is still the raw per-source range.

Weights are **per field**. Open-Meteo has no probability to publish from a deterministic run, so it derives `precipitation_probability` from that model's own ensemble — which means the `ecmwf` row's rain chance restates `ecmwf_ens`, and `gfs`'s restates `gefs`. Counted as full votes, one model family carried ×3 of the rain headline. For rain (`field="pop"`) those two rows drop to ×0.25; for temperature, humidity, wind and condition they keep full weight, because those really are an independent deterministic run. `GEM` is not discounted — it has no ensemble row on the page to duplicate.

## Rainfall amounts

Every source publishes an amount as well as a chance, and a 60% chance of 0.02" is a very different afternoon from 60% of half an inch. Amounts are stored in inches per hour (`amt` on each hourly entry, summed over `DAY_WINDOW` for the daily figure) and drawn as a strip hanging below the hourly chance bars, on a shared scale across the three days that the caption always states.

Sources differ in what they give: weather.com has a native hourly `qpf`; Open-Meteo carries `precipitation`; the ensembles use the **member mean** (the conventional QPF, and the only summary that adds up across hours) plus `amt_p90`, the 90th-percentile member day total — the wet tail. NWS publishes `quantitativePrecipitation` only on the raw gridpoint, in 6-hour blocks that reach ~3.5 days out, and met.no's blocks reach ~9; both are spread evenly across their hours, the same convention the ensembles already use once their data goes 6-hourly. Foreca publishes a daily `rain` total in mm, but no hourly breakdown.
