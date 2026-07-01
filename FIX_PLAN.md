# Home_Air fix plan (orchestrate gen-1)

Baseline HEAD: 92da83e · pytest baseline: 45 passed

## Root cause: "dashboard shows turbo but turbo was never on"
`scripts/cloud_cycle.py` writes the controller **decision** (`dec.turbo`) into
`state/status.json`, which the Pages dashboard renders as a **BOOST** badge —
regardless of whether the unit accepted the command. Two compounding faults:

1. **Cloud transport never engages turbo.** `midea_cloud.apply` co-sends
   `fan_speed=max` AND `turbo=True`. The unit drops turbo when a manual fan speed
   is co-commanded (LAN path already knows this: "turbo overrides fan").
2. **No read-back.** After `apply()`, the midea library auto-refreshes
   (`needs_refresh()==True`) so `_dev.state.turbo/fan/target/running` already hold
   **device-confirmed** values — but `cloud_cycle` throws them away.

Fix = command turbo correctly (fan=auto when turbo) AND report the read-back
confirmed state to the dashboard, with an intent≠confirmed mismatch badge.

## Batch A — turbo truth + apply correctness
- `hvac/midea_cloud.py::apply` → return a confirmed `MideaState` (built from
  `_dev.state` after `set_state`). When `turbo=True`, send `fan_speed="auto"`
  (let turbo drive airflow); mirror LAN semantics.
- `hvac/midea_client.py:144` (LAN) → when `turbo` requested but `supports_turbo`
  falsy, fall back to **max fan** (never leave fan unset); when supported, turbo
  drives, skip manual fan. Return confirmed state (already does via `_state`).
- `scripts/cloud_cycle.py` → capture `confirmed = midea.apply(...)`; write to
  `status.json`: `confirmed{turbo,fan,setpoint,mode,running,online}`, `cmd{...}`
  (intended), `applied` = confirmed-matches-intent, `unconfirmed` bool,
  `mismatch` list. Log the gap-gated `send_sp` (not `dec.setpoint_f`).
- Reliability: guard `read_ethan_temp()` in the except; carry forward
  `last_ethan` from state; if no temp at all, persist state + status(error) and
  skip actuation instead of crashing the run.

## Batch B — dashboards show truth
- `docs/index.html` (Pages): render **confirmed** turbo/fan/setpoint; BOOST badge
  only when confirmed; if `unconfirmed`/`mismatch`/`applied==0`/`error`, show a
  warning ("requested BOOST — device reports OFF"/"command not confirmed").
  Also fix: savings `×PRICE` (was 6.7× high); hour/day analytics in
  `America/New_York`; proper CSV parse (quoted error commas); "(window)"→"(all
  history)"; 9pm band from row `low/high` not hard-coded 69–71.
- `web/index.html` + `hvac/dashboard.py`: `F` null/NaN guard; remove dead code;
  surface `applied`/`error` awareness on the BOOST badge where available.

## Batch C — minor correctness
- `hvac/service.py`: log refresh failures (no bare `except:pass`); set
  `_last_learn` only after a successful persisted fit.
- `hvac/weather.py`: tail forecast branch — use `int(frac)` + `j` index (match
  main branch).
- `hvac/learn.py`: also export `state/model_params.json` so the cloud path can
  actually read learned params.
- `hvac/controller.py`: remove dead `night_max_fan` (operator comment says turbo
  is OK day/night; field is unreferenced) — no control-law change.

## Batch D — tests / e2e
- e2e `cloud_cycle` full run (mock ecobee+midea+weather): status.json has
  confirmed fields; mismatch flagged when device reports turbo off; carry-forward
  on ecobee failure; no crash when all sensors fail.
- turbo apply parity (cloud fan=auto under turbo; LAN max-fan fallback).
- node JS tests for dashboard pure funcs (savings×PRICE, NY-tz hour, CSV parse).

## Verify gate
`python -m pytest -q` green (≥45) + new tests + `node tests/dashboard.test.mjs`.
