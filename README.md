# Home_Air — schedule-driven cooling for Ethan's room

A free, self-hosted control loop that holds **Ethan's room** on a time-of-day
comfort schedule by driving the **office Midea Duo** portable AC, using
**Ethan's-room temperature from an ecobee SmartSensor** plus **outdoor weather**.
It logs every cycle to SQLite and **learns the house's thermal response over
time** to improve its own setpoints.

## Comfort schedule + cost optimisation (`hvac/schedule.py`)

- **10pm–9am: firm 70°F** (sleep). The target ramps 72→70 over 9–10pm so the
  room actually *reaches* 70 by 10pm despite thermal lag, not just starts cooling
  then.
- **9am–10pm: 72–74°F band.** Within the band the controller **rides the cool
  edge (72) when it's mild outside** (cooling is cheap/efficient → maximise
  coolness) and **drifts to the warm edge (74) when it's hot** (cooling is
  expensive → minimise runtime). It **pre-cools toward 72 when the forecast shows
  a heat ramp coming**, banking cheap cold to shave the expensive peak.
- **Optional time-of-use** (`ScheduleConfig.tou`): ride 74 during a utility peak
  window and pre-cool to 72 just before it.

This is the "optimal vs weather, minimise electricity cost while maximising
coolness" requirement: cheap coolness is bought, expensive coolness is skipped.

## The hard part (and why this isn't a thermostat)

The Midea AC is in the **office**; the sensor we care about is in **Ethan's
room** next door. The Midea only knows its own setpoint, which governs the
*office*. To pull Ethan's room to 70°F the office must run **colder** than 70 by
an offset that grows with outdoor heat — and there's a ~30-minute lag between
cooling the office and Ethan's room responding. So a naive "set Midea to 70"
fails. The controller solves this with three layers:

```
setpoint = target
           − feedforward_offset(outdoor)     # learned open-loop: how much colder the office must run
           − (Kp·error + Ki·∫error)          # PI feedback on the actual Ethan-room error
```

plus **weather-forecast pre-cooling** (lowers the setpoint ahead of a heat ramp,
since reacting to current outdoor temp always trails), a **deadband** so sensor
noise doesn't wander the setpoint, **anti-windup**, **setpoint clamping/quantising**,
and **compressor min-cycle protection**.

## Architecture

```
hvac/
  config.py        target, sensor/thermostat IDs, Midea creds, bounds, location
  ecobee_client.py reads Ethan's-room temp (sensor rs2:101:1); token-refresh aware
  midea_client.py  drives the Midea over LAN via msmart-ng (+ MockMidea)
  weather.py       Open-Meteo current temp + forecast (free, no key)
  controller.py    the algorithm: feedforward + PI + forecast pre-cool
  storage.py       SQLite: readings log + versioned learned params
  learn.py         offline: refit the site-specific feedforward from logged data
  simulator.py     2-room RC thermal model (office+Ethan+outdoor) — the test rig
  score.py         closed-loop sim + the mechanical metric
  service.py       live loop: read → decide → actuate → log
  sim_harness.py   run the real service loop against the simulator (no hardware)
scripts/
  run_service.py     entry point (--dry-run / --sim / --once)
  discover_midea.py  one-time Midea token/key fetch
tools/
  tune.py            autoresearch: search controller params to maximise the metric
tests/               pytest suite (controller, simulator, storage/learn, integration)
```

## Quick start

```bash
pip install -r requirements.txt

# 1. Prove it works with zero hardware (runs the real control loop on the sim):
python scripts/run_service.py --sim --cycles 30

# 2. See the algorithm's quality score on the scenario suite:
python -m hvac.score          # prints METRIC (~85; higher=better)

# 3. Configure real devices:
cp .env.example .env
#   ecobee: see "ecobee auth" below
python scripts/discover_midea.py --account you@email --password 'pw'   # → MIDEA_* into .env

# 4. Dry-run against the real sensor, but don't touch the AC:
python scripts/run_service.py --dry-run --once

# 5. Go live:
python scripts/run_service.py
```

Run it under Task Scheduler / a service manager for 24/7 operation (the loop
sleeps `interval_s` between cycles; default 120s).

## Dashboard

A zero-dependency web dashboard (stdlib HTTP server + Chart.js from CDN) shows
live status and history straight from the SQLite log:

```bash
python scripts/run_dashboard.py            # serve the live DB at http://localhost:8787
python scripts/run_dashboard.py --demo     # seed ~24h of simulated data first, then serve
python scripts/run_dashboard.py --port 9000 --db data/home_air.db
```

Shows: current Ethan-room temp vs the active target band, AC setpoint/mode,
outdoor temp, 24h in-band % and AC duty, min/max range, the learned model
params, a temperature+band history chart (6h/24h/3d/7d), a setpoint+cooling-state
chart, and a recent-actions log. Auto-refreshes every 30s. It binds `0.0.0.0`
so you can view it from your phone on the home network — it's read-only, but
unauthenticated, so keep it on the LAN.

## 24/7 in the cloud — no home device (GitHub Actions)

Run the whole loop from GitHub's cloud, controlling the Midea over the
**MSmartHome cloud relay** (how the phone app does it from anywhere) — nothing
runs on your home network.

- `hvac/midea_cloud.py` controls the AC via `midea-beautiful-air` (MSmartHome
  cloud), selected with `MIDEA_TRANSPORT=cloud`.
- `scripts/cloud_cycle.py` runs one stateless cycle: read ecobee + weather →
  scheduled decision → command the AC via cloud → persist controller state +
  `state/history.csv` + `state/status.json` back to the repo.
- `.github/workflows/control.yml` runs it every 15 min (cron), pinned to
  `TZ=America/New_York` so the night/day schedule is house-local.

**Deploy:**
1. Push this folder to a GitHub repo (private recommended).
2. Repo → Settings → Secrets and variables → Actions → add:
   `ECOBEE_ACCOUNT`, `ECOBEE_PASSWORD`, `ECOBEE_THERMOSTAT_ID`, `MIDEA_ACCOUNT`,
   `MIDEA_PASSWORD`, `MIDEA_ID`, `HA_LAT`, `HA_LON` (your own values).
3. Actions tab → enable workflows → run "Home_Air control loop" once
   (workflow_dispatch) to verify, then it self-runs every 15 min.

**Midea login cap (error 65027):** Midea limits concurrent logins per account.
The workflow logs in once per run and uses a `concurrency` group so runs don't
overlap — fine in steady state. If you hammer logins (many manual runs + the
phone app at once) you'll hit 65027; wait for sessions to expire or sign out
extra devices in the MSmartHome app.

LAN mode (`MIDEA_TRANSPORT=lan`, default) still works when running on the home
network and is lower-latency; cloud mode is for off-LAN 24/7 hosting.

## How it improves itself

Every cycle is logged to `data/home_air.db`. Periodically (e.g. nightly) run:

```bash
python -m hvac.learn
```

`learn.py` fits `t_ethan ≈ a0 + a1·setpoint + a2·outdoor` over real cooling data,
inverts it to the setpoint that holds the target, and writes refreshed
**feedforward** params (the site-specific part) as a new versioned row. The
controller loads the latest params each cycle, so it gets better as data
accumulates — **without touching the feedback gains**, which stay fixed for
stability (tuned once against the simulator via `tools/tune.py`).

## The metric

`python -m hvac.score` runs the controller against four outdoor scenarios
(hot day, mild day, heat wave, afternoon heat spike) and reports:

```
score = 100·in_band − 8·MAE − 14·overshoot − 4·undershoot − 6·energy
```

scoped to cooling-relevant time (a cooling-only system can't hold the band when
it's colder outside than the band, so those minutes aren't counted against it).
Too-hot is weighted hardest — the failure the user feels — and energy is charged
(optionally TOU-weighted) so the optimiser trades a little warmth for less
runtime when cooling is expensive. Current tuned defaults score **~91**; the day
band makes even a 100°F+ heat wave comfortable (ride 74 at peak) where a fixed
70°F target was physically impossible.

## ecobee auth (registration is closed)

ecobee **no longer accepts new developer-API registrations**, so the classic
"register an app → API key → OAuth PIN" flow is likely unavailable. Use the
web-app credentials instead — all visible in the browser, none are a dev key:

1. Log in at https://www.ecobee.com, open DevTools (F12) → **Network**.
2. Find the request to `auth.ecobee.com/oauth/token` (the web app's Auth0 login).
3. From it copy into `.env`:
   - `ECOBEE_TOKEN` — the `access_token` (the `Bearer ...` value)
   - `ECOBEE_REFRESH_TOKEN` — the `refresh_token` in the token response

The client_id defaults to the ecobee web app's public id (verified live), and
refresh hits Auth0 (`auth.ecobee.com/oauth/token`), so `ecobee_client.py`
auto-renews the access token when it expires (API error code 14) — the ~1-hour
expiry stops mattering for the always-on service.

**Verified live (2026-06-19):** reads succeed against `api.ecobee.com/1/thermostat`
with this Auth0 token; Ethan's sensor `rs2:101:1` returned the same value the app
shows. We only ever **read** the sensor (the actuator is the Midea), so read
scope is sufficient.

API reference: https://www.ecobee.com/home/developer/api/introduction/index.shtml

## Hardware reference

- ecobee Smart Thermostat Enhanced (id via `ECOBEE_THERMOSTAT_ID` secret); a remote SmartSensor in the bedroom.
- Midea Duo 12,000 BTU portable AC (model `MAP14AS1TWT-C`), Wi-Fi/Matter, in the office.
- House coordinates configured via `HA_LAT` / `HA_LON` secrets (Open-Meteo weather).
```
