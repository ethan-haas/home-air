# Fresh-eyes review — cloud_cycle device-confirmed-state + dashboard fixes

Reviewed: full 1550-line diff (`.github/workflows/control.yml`, `docs/index.html`,
`web/index.html`, `hvac/{controller,dashboard,learn,midea_client,midea_cloud,
service,weather}.py`, `scripts/cloud_cycle.py`, new tests) against HEAD
(`28ea265`), plus the installed `midea_beautiful` vendor library source to
verify the core physical-device assumption.

Verification performed (not just reading):
- `python -m pytest -q` → **52 passed**, no failures.
- `node --test tests/*.mjs` → **23 passed** (jsdom not installed locally, so
  `dashboard_smoke.test.mjs` ran its static-analysis fallback branch; both
  test files still exercised real logic — `parseCsvLine`/`localHour`/`dayKey`
  extracted and executed via `new Function(...)`, plus regex assertions on
  `docs/index.html`).
- Read `C:\python314\Lib\site-packages\midea_beautiful\appliance.py` /
  `lan.py` to confirm `AirConditionerState.needs_refresh()` **unconditionally
  returns `True`**, and `apply()` calls `self.refresh(cloud)` in that case —
  confirming `hvac/midea_cloud.py`'s new "the library auto-refreshes after
  set_state, so `_dev.state` already holds confirmed values" claim is
  factually correct, not a guess.

## Findings

**MED** — `scripts/cloud_cycle.py:210-213`, the `applied` boolean only checks
`confirmed.power == dec.ac_should_run and (not dec.turbo or confirmed.turbo)`.
It does **not** check `mode` or `setpoint` even though the adjacent comment
and the manifest description ("applied now means confirmed-matches-intent")
imply a full match. In practice this is masked because the dashboard's
if/elif chain checks `s.mismatch` (which *does* include `mode`/`setpoint`)
**before** `s.applied===false`, so a real mode/setpoint mismatch still
surfaces a warning — just as `"mismatch"` rather than `"unconfirmed"`. Not a
runtime risk, just an imprecise variable semantics vs. the stated intent.
Suggest either widening `applied` to include mode/setpoint, or narrowing the
comment to "device power+turbo confirmed" so the two signals aren't
conflated.

**MED** — No unit test exercises `hvac/midea_cloud.py`'s `apply()` directly.
`tests/test_cloud_cycle_e2e.py` replaces the whole `MideaCloudClient` class
with `FakeMideaCloudClient`, so the single highest-consequence line in this
diff — `fan_name = "auto" if turbo else fan_speed` (forcing fan=auto instead
of the controller's chosen "max" whenever turbo is requested on the cloud
transport) — and the `_MODE_INV`/`_FAN_INV`/read-back construction logic are
never exercised by anything except manual/prod inspection. I independently
validated the *design* is sound (see `needs_refresh()` note above) and that
the LAN client already uses the same "skip explicit fan when turbo requested"
semantics (presumably already field-proven there), but a wrong assumption
about the *cloud* relay specifically would only be caught by watching the
live dashboard's new `confirmed.turbo`/`mismatch` fields after this deploys,
not by CI. Recommend adding a small unit test that stubs `_dev`/`_cloud` on
`MideaCloudClient` directly and asserts the `set_state(**kwargs)` payload and
the returned `MideaState` for a turbo-requested call. Not a blocker — the
change is self-diagnosing (a wrong assumption shows up immediately as a
`mismatch: ["turbo"]` warning on the dashboard, not a silent failure) — but
should be the first thing checked after the next hot-day boost event.

**LOW** — `hvac/service.py:87-99` (`_maybe_learn`): previously `_last_learn`
was advanced unconditionally before attempting `learn()`; now it only
advances when `learn()` returns non-`None`. If `learn()` *raises* (vs.
returning `None` for insufficient data) on a persistent fault (e.g. a broken
DB), it will now retry every service cycle (~2 min) forever instead of
backing off for a day, producing repeated `error: learn: ...` log lines /
recompute. Does not touch actuation and does not affect the cloud cron path
at all (`cloud_cycle.py` never calls `learn()`), so no live-AC risk — cosmetic
log-spam risk only in the LAN service.

**LOW** — `hvac/weather.py:85`, the fallback-branch fix (`temps[min(j, ...)]`
replacing the inconsistent `round(frac)` with the already-computed `j =
idx + int(frac)`) has no dedicated regression test (pre-existing gap, not
introduced by this diff). Still index-clamped, so no crash risk either way.

## Traced happy path + failure paths (scripts/cloud_cycle.py)

- **All-sensors-fail**: `t_ethan` resolves via `read_full()` → `read_ethan_temp()`
  → `last_ethan` (carried forward) → still `None` → early-`return` branch
  writes a fully-populated, correctly-typed `status.json`/`history.csv` row
  (no f-string `:.1f` applied to `None` anywhere in that branch) and exits
  before reaching the one `f"ethan={t_ethan:.1f}..."` print that *would*
  crash on `None` — that print is unreachable in this branch. Confirmed via
  `test_ecobee_outage_no_prior_state` and `test_all_sensors_fail_gracefully`
  (both pass).
- **Ecobee outage WITH a carried-forward reading**: `t_ethan = last_ethan`
  (non-`None`) flows through the normal decide/actuate path — the AC is
  **not** left uncommanded; confirmed by `test_ecobee_outage_with_prior_state`
  (`applied is True`, a real setpoint sent using the stale-but-present temp).
- **`midea.apply()` raises**: `confirmed` stays `None`, `applied` stays
  `False`, `last_mode`/`last_fan`/`last_setpoint` are left un-updated (same as
  pre-diff behavior — those assignments are downstream of the failing call,
  inside the same `try`), `unconfirmed=True`, `mismatch=[]`, and all
  `show_*` values fall back to intent. No crash; `err` is set and surfaces on
  the dashboard via `cmdwarn`.
- **`apply()` returns `None`** (read-back parse failed but no exception):
  `applied=True` (documented as "applied-but-unconfirmed"), `unconfirmed=True`,
  `confirmed{}` all `None`, top-level `setpoint/mode/fan/turbo/ac_running`
  fall back to intent — verified by `test_readback_fails_apply_returns_none`.

## Caller-compatibility check

`hvac/service.py:Service.cycle()` calls `self.midea.apply(...)` at line 181-183
and **discards the return value** entirely — it never assigned or inspected
the old `None` return, so `MideaCloudClient.apply()` now returning
`Optional[MideaState]` is a non-breaking change for that caller.
`MideaClient.apply()` (LAN, `hvac/midea_client.py`) still implicitly returns
`None` (unchanged) since `_aapply()` has no return statement. Both transports
remain interface-compatible with `Service.cycle()`.

## Dashboard against the OLD (currently-live) status.json

The next cron run doesn't land for up to ~90 min after this push (GitHub cron
throttling, per the existing comment in the code), so `docs/index.html`'s new
JS will run against the OLD-schema `status.json` for a while. Verified every
new key access (`s.cmd`, `s.confirmed`, `s.unconfirmed`, `s.mismatch`) is
`&&`-guarded, never a bare property/bracket access — confirmed both by
reading the code and by the passing `dashboard_smoke.test.mjs` static-analysis
assertions (`s\.mismatch\s*&&`, `s\.cmd\s*&&`, etc., all matched). No blank-out
risk. `parseCsvLine` also degrades correctly to plain comma-split on
old-format unquoted rows.

## `.github/workflows/control.yml` rebase change

`git push || (git pull --rebase --autostash origin main && git push)`.
Confirmed the repo's actual branch is `main` (matches `origin main` in the
new command), the `concurrency` group already prevents overlapping runs (so
non-fast-forward should only occur from this diff's own commits landing
between checkout and push), and the retry is bounded (single retry, no loop).
A genuine rebase conflict would fail loud (job goes red) rather than silently
corrupt state — acceptable given `concurrency.cancel-in-progress: false`
already serializes runs.

## Dead-field removal

`ControllerParams.night_max_fan` removed from `hvac/controller.py`; grepped
the whole repo — zero other references except a stale planning note in
`FIX_PLAN.md`. `ControllerParams.from_dict()` already tolerates unknown/extra
keys, so any already-committed `state/model_params.json` containing the old
key round-trips safely.

## Verdict: **APPROVE-WITH-NITS**

Safe to push. No BLOCKER or HIGH-severity runtime-crash risk found on the
live cloud-cron path; all None-handling, f-string formatting, and
mismatch/setpoint math were traced and are guarded. The two MED items above
are real but non-blocking: (1) `applied`'s narrower-than-documented scope is
masked by the dashboard's check-ordering, and (2) the single riskiest
physical-behavior change (cloud turbo → fan=auto) lacks a direct unit test,
though it is well-reasoned, mirrors already-used LAN semantics, is validated
against the installed vendor library's actual refresh behavior, and is
self-diagnosing via the new `mismatch`/`confirmed` fields it also introduces.
Recommend, as a fast follow (not a gate): add a `MideaCloudClient.apply()`
unit test with a stubbed `_dev`, and tighten or re-document the `applied`
boolean.
