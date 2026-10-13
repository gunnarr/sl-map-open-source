# AGENTS.md – guide for AI coding agents

This file is for AI agents (Claude Code, Codex, Cursor, Copilot, Gemini …) working in this repo, and for humans who
like a short version. `CLAUDE.md` points here. The human-facing docs are [README.md](README.md) (Swedish) and
[README.en.md](README.en.md).

**What this is:** SL Live Map – a real-time map of Stockholm's public transport. `server.py` (Flask) fetches
Trafiklab's GTFS-RT feed (protobuf), turns it into compact JSON and caches it; `map.html` is one page with all
JavaScript and CSS inline that draws the vehicles on Leaflet and polls every two seconds. No build step, no
framework, no bundler. Keep it that way.

## Set up and check your work

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/ -q        # ~450 tests in seconds, no keys, no network – run after every change
npm install --no-audit --no-fund           # once, for the browser test
node map-behaviour-test.mjs                 # drives the real map.html in headless Chrome, no network needed
```

The browser test uses Google Chrome if it is installed on macOS, otherwise `CHROME_PATH`, otherwise Puppeteer's own
Chrome. If npm blocked Puppeteer's download (an `install-scripts … blocked` warning), run
`npx puppeteer browsers install chrome` or set `CHROME_PATH`. CI runs Python 3.13 and Node 22.

Both must pass before you hand anything back. The tests mock every HTTP call, and a socket guard in
`tests/conftest.py` fails any test that tries to reach the network – don't loosen it, mock instead.

### Running the real map (needs keys from the human)

The live map needs Trafiklab keys. **You cannot create them for the human, and you must not ask them to paste keys
into the chat.** Ask them to follow "Skaffa nycklar hos Trafiklab" in the README and put the keys in `.env` themselves
(`cp .env.example .env`). Never print, log, commit or quote the contents of `.env`.

```bash
venv/bin/python3 build_trip_lookup.py --save-zip sl.zip    # downloads static data ONCE (quota: 50/month on Bronze)
venv/bin/python3 build_stations.py --gtfs-zip sl.zip       # reuse the zip – never download again for these
venv/bin/python3 build_geojson.py --gtfs-zip sl.zip
VEHICLE_CACHE_SECONDS=60 venv/bin/python3 server.py        # http://localhost:8080
```

Mind the quotas: Bronze is 30,000 realtime calls a month. Keep `VEHICLE_CACHE_SECONDS=60` while developing, don't
poll `/api/vehicles` in loops, and don't leave the map open in a headless browser longer than you need. The
generated files (`regions/sl/trip_lookup.json`, `stations.json`, `*_lines.geojson`, `sl.zip`) are gitignored –
never commit them.

Without keys you can still start the server, and these answers are **expected**, not bugs:
`/api/vehicles` gives 200 with `{"error": "TRAFIKLAB_KEY not configured", "vehicles": []}`, `/health` gives 503 (no
fresh data), and `/stations.json` and `/api/*-lines` give 404 until the builders have run. The page still loads, and
`map-behaviour-test.mjs` feeds it fake vehicles. The dev server listens on 127.0.0.1 only; set `HOST=0.0.0.0` to
open it from a phone on the same network.

Links in the menu to other pages (Om, Nyheter …) and the sharing tags in `map.html` point at sl-map.gunnar.se's own
pages, which aren't in this repo – `site-config.js` hides the links, and a 404 for `about.html` locally is expected.

### Checking a change to map.html

1. `node map-behaviour-test.mjs` and the pytest suite.
2. Look at it: phone width (390 px) and desktop, light and dark mode (the theme toggle top right, stored in
   `localStorage.theme`), and screensaver mode (`/?screensaver`).
3. No errors in the browser console.

## Where things are

| File | What |
|---|---|
| `server.py` | All backend. Vehicle fetch + cache, delta updates, line shapes, health, static files. |
| `map.html` | The whole frontend, inline. |
| `site-config.js` | Everything deployment-specific the page needs: tiles, analytics, app banner, menu links. |
| `regions/sl/config.json` | Region data: names, metro groups, line colours, tram names, map centre. |
| `build_trip_lookup.py`, `build_stations.py`, `build_geojson.py` | Offline builders from Trafiklab's static GTFS. |
| `tests/` | pytest. Fixtures `app` and `client` in `tests/conftest.py`. |
| `map-behaviour-test.mjs` | Browser test with scripted API responses. |

Landmarks to search for (names are stable, line numbers aren't):

- **server.py:** `_fetch_with_lock` (per-key lock, stale-while-revalidate cache), `_do_fetch_vehicle_positions`
  (protobuf → JSON), `_compute_vehicle_diff` (the `?since=` delta), `_load_trip_lookup`, `_load_region_config`,
  `api_vehicles`, `health`, `static_files` (allowlist of servable file types).
- **map.html:** `init`, `fetchVehicles` / `startPolling` / `stopPolling` (polling, paused when the tab is hidden),
  `updateVehicles` (diffing markers), `animateMarker` / `runAnims` (requestAnimationFrame movement),
  `predictPosition`, `snapToTrack` (rail vehicles follow the track shapes), `createDotIcon` / `getIconTier` (marker
  looks per zoom), `buildPopup`, `makeTileLayer` / `setTheme`, `loadStations`, `loadMetroRoutes`.

## API (other clients depend on it)

| Endpoint | Returns |
|---|---|
| `GET /api/vehicles` | `{timestamp, vehicles: [...]}` – every vehicle with a known line. |
| `GET /api/vehicles?since=<timestamp>` | Only changed, added and removed vehicles since that snapshot. |
| `GET /api/metro-lines`, `/api/train-lines`, `/api/tram-lines` | GeoJSON track shapes. |
| `GET /stations.json` | Metro, tram and train stations. |
| `GET /api/version`, `GET /health` | Version (for auto-reload) and health. |

Native apps decode these. `tests/test_api_contract.py` freezes the shape: you may **add** fields, but don't rename,
remove or retype one, and never edit the contract test to make a change pass.

## Rules

- **The browser never talks to Trafiklab.** The key stays on the server; every visitor shares one cached upstream
  answer. Don't add upstream calls, shorten the cache or fetch per visitor.
- **No new dependencies** (pip or npm) and no build step without an issue where the maintainer agreed first.
- **Deployment values belong in `site-config.js`, never in `map.html`** – tests enforce it.
- **UI text is Swedish.** Code comments and commit messages in English.
- **Small diffs.** Don't reformat, reorder or "modernise" code you weren't asked to touch; it buries the real change.
- **Tests with every fix.** A bug fix comes with a test that fails without it.
- Don't touch `LICENSE`, and don't add licence headers.
- Respect the performance work in `map.html` (icon cache, popup guard, batched station rendering, paused polling).
  Mobile load time matters more than features.

## GTFS-RT quirks you will hit

- Use `vehicle.id`, not `entity.id` – the entity id changes on every fetch.
- Vehicles without `trip_id` are hidden: the line comes from `trip_lookup.json` via the trip id.
- Trams never report a bearing; it is calculated from movement (`getBearing`).
- Speed is unreliable. Update intervals differ a lot per mode (buses every few seconds, trams up to a minute).
- Some boat lines share numbers with metro lines; `metro_groups` in `regions/<id>/config.json` sorts that out.
- Trafiklab's support forum (support.trafiklab.se) often explains data oddities before you go looking for a bug.

## Common tasks

- **Other map tiles:** edit `site-config.js` (presets in its comments). Nothing in `map.html`.
- **Another region:** copy `regions/sl/` to `regions/<operator>/`, edit `config.json`, run the builders with
  `--operator <operator>`, set `REGION=<operator>`. The page is built for Stockholm, so put a copy of `map.html` in
  the region folder and adjust it – the server prefers it.
- **New field in `/api/vehicles`:** add it in `_do_fetch_vehicle_positions`, add it to `_DELTA_FIELDS` if it can change, add it
  to `tests/test_api_contract.py`, and keep the payload small – it's sent every two seconds.

## Pull requests

Read [CONTRIBUTING.md](CONTRIBUTING.md) first – it says what is likely to be declined. Open an issue before a bigger
change. In the PR: what changed and why, how it was tested (paste the test summary), screenshots for visual changes,
and that an AI agent helped. The human submitting is responsible for every line: they should have read and run it.
