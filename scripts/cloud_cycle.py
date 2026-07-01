#!/usr/bin/env python
"""One stateless control cycle for the cloud (GitHub Actions cron).

Runs entirely off-LAN: reads Ethan's temp from ecobee, outdoor from Open-Meteo,
computes the scheduled target + decision, and commands the Midea via the
MSmartHome CLOUD relay. Controller state (integral, last command) and history
persist to files in the repo so the next scheduled run continues seamlessly.

Designed to be run by .github/workflows/control.yml every ~10-15 min, with
credentials supplied as GitHub Actions secrets (env vars). After it runs, the
workflow commits the updated state/ files back to the repo.

  python scripts/cloud_cycle.py
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvac.config import Config
from hvac.controller import Controller, ControllerParams
from hvac.schedule import ScheduleConfig, schedule_point

HOME = Path(__file__).resolve().parent.parent
STATE_DIR = HOME / "state"
STATE_FILE = STATE_DIR / "controller_state.json"
PARAMS_FILE = STATE_DIR / "model_params.json"      # learned params (optional)
HISTORY_FILE = STATE_DIR / "history.csv"
STATUS_FILE = STATE_DIR / "status.json"


def load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def local_hour(now: float) -> float:
    # The whole comfort schedule is hour-of-day driven, but GitHub Actions runners
    # are UTC -> machine-local time would shift the night/precool windows by the
    # UTC offset. Pin to Ohio wall-clock explicitly.
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
        return dt.hour + dt.minute / 60.0
    except Exception:
        lt = time.localtime(now)
        return lt.tm_hour + lt.tm_min / 60.0


def main() -> None:
    cfg = Config.load()
    cfg.midea_transport = "cloud"          # this entry point is cloud-only
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()

    # --- params (learned, if learn.py has run and committed) + state ---
    params = ControllerParams.from_dict(load_json(PARAMS_FILE, {}))
    st = load_json(STATE_FILE, {})
    ctrl = Controller(params, cfg.setpoint_min_f, cfg.setpoint_max_f,
                      cfg.setpoint_step_f)
    ctrl.integral = float(st.get("integral", 0.0))
    last_setpoint = st.get("last_setpoint")
    last_mode = st.get("last_mode")
    last_fan = tuple(st["last_fan"]) if st.get("last_fan") else None
    last_cmd_time = float(st.get("last_cmd_time", 0.0))
    last_office = st.get("last_office")        # office temp from the prior run
    last_ethan = st.get("last_ethan")          # Ethan's temp from the prior run

    # --- read sensors (cloud APIs, work anywhere) ---
    from hvac.ecobee_client import EcobeeClient
    ec = EcobeeClient(cfg)
    t_living = t_heather = indoor_hum = central_cool = None
    t_ethan = None
    try:
        ctx = ec.read_full()
        t_ethan = ctx.get("ethan")
        t_living = ctx.get("living")
        t_heather = ctx.get("heather")
        indoor_hum = ctx.get("indoor_hum")
        central_cool = ctx.get("central_cool")
        if t_ethan is None:
            t_ethan = ec.read_ethan_temp()
    except Exception as e:
        print(f"ecobee read_full failed: {e}")
        try:
            t_ethan = ec.read_ethan_temp()
        except Exception as e2:
            print(f"ecobee read_ethan_temp failed: {e2}")
            t_ethan = None

    if t_ethan is None:
        # persistent ecobee outage: carry the last known reading forward so a
        # single flaky poll doesn't crash the whole run.
        t_ethan = last_ethan
    else:
        last_ethan = t_ethan

    if t_ethan is None:
        # never had a reading at all (fresh state + dead ecobee) -> nothing to
        # control against. Skip actuation entirely, persist what we have, and
        # exit cleanly (no traceback, no missing history row).
        err = "no Ethan temp available (ecobee down, no carried-forward value)"
        print(err)
        STATE_FILE.write_text(json.dumps({
            "integral": ctrl.integral, "last_setpoint": last_setpoint,
            "last_mode": last_mode, "last_fan": list(last_fan) if last_fan else None,
            "last_cmd_time": last_cmd_time, "interval_s": cfg.interval_s,
            "last_office": last_office, "last_ethan": last_ethan, "updated": now,
        }, indent=2), encoding="utf-8")
        HEADER = ["ts", "t_ethan", "t_office", "outdoor", "humidity", "target",
                  "band_low", "band_high", "setpoint", "mode", "fan", "turbo",
                  "ac_running", "applied", "indoor_hum", "central_cool",
                  "t_living", "t_heather", "error"]
        fresh = True
        if HISTORY_FILE.exists():
            try:
                first = HISTORY_FILE.open(encoding="utf-8").readline().strip()
                fresh = first != ",".join(HEADER)
            except Exception:
                fresh = True
        mode_w = "w" if fresh else "a"
        with HISTORY_FILE.open(mode_w, newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            if fresh:
                wr.writerow(HEADER)
            wr.writerow([round(now), None, last_office, None, None, None,
                         None, None, last_setpoint, last_mode,
                         last_fan[0] if last_fan else None,
                         int(last_fan[1]) if last_fan else 0, 0, 0,
                         None, "", None, None, err])
        STATUS_FILE.write_text(json.dumps({
            "updated": now, "t_ethan": None, "t_office": last_office,
            "outdoor": None, "humidity": None,
            "target": None, "band": [None, None],
            "setpoint": last_setpoint, "mode": last_mode,
            "fan": last_fan[0] if last_fan else None,
            "turbo": bool(last_fan[1]) if last_fan else False,
            "ac_running": False,
            "indoor_hum": None, "central_cool": None,
            "t_living": None, "t_heather": None, "in_band": None,
            "cmd": {"setpoint": last_setpoint, "mode": last_mode,
                    "fan": last_fan[0] if last_fan else None,
                    "turbo": bool(last_fan[1]) if last_fan else False,
                    "running": False},
            "confirmed": {"turbo": None, "fan": None, "setpoint": None,
                          "mode": None, "running": None, "online": None},
            "unconfirmed": True, "mismatch": [],
            "applied": False, "error": err,
        }, indent=2), encoding="utf-8")
        return

    outdoor = forecast = humidity = None
    try:
        from hvac.weather import get_weather
        w = get_weather(cfg.latitude, cfg.longitude,
                        lead_min=params.forecast_lead_min)
        outdoor, forecast, humidity = w.temp_f, w.forecast_temp_f, w.humidity
    except Exception as e:
        print(f"weather unavailable: {e}")

    # read the unit's current office temp BEFORE deciding — the economizer /
    # free-cool branch and the turbo-suppression logic are gated on office_temp,
    # so without it the cloud path could only ever pick cool/off (no free-cool).
    from hvac.midea_cloud import MideaCloudClient
    midea = MideaCloudClient(cfg)
    office_temp = last_office
    try:
        ms = midea.refresh()
        if getattr(ms, "indoor_temp_f", None) is not None:
            office_temp = ms.indoor_temp_f
    except Exception as e:
        print(f"midea refresh failed (using last_office={last_office}): {e}")

    # --- decide ---
    sched = ScheduleConfig(night_target=cfg.target_f)
    sp = schedule_point(local_hour(now), outdoor, forecast, sched)
    # integrate against ACTUAL elapsed time since the last cron run (~10-15 min),
    # not a fixed 120s. Cap only true outages (interval_s is the LAN-loop 120s and
    # is unrelated to the cron cadence, so 4*interval would clamp every normal run).
    prev = float(st.get("updated", now))
    dt = max(1.0, min(now - prev, 3600.0)) if prev < now else cfg.interval_s
    dec = ctrl.decide(t_ethan, sp.target, outdoor, dt_s=dt,
                      outdoor_forecast_f=forecast, sleep_mode=sp.night,
                      office_temp=office_temp)

    # This unit refuses turbo over the cloud relay (verified live: requested
    # turbo -> device confirmed turbo off). Requesting it just swaps MAX fan for
    # AUTO with no boost, so on the cloud path suppress unsupported turbo and
    # keep MAX fan — same airflow during recovery, and no phantom BOOST/mismatch
    # on the dashboard. Override with HA_MIDEA_TURBO=on for a unit that honors it.
    if dec.turbo and not cfg.midea_turbo_supported:
        dec.turbo = False
        dec.fan_speed = "max"

    # --- actuate via cloud ---
    # Re-assert the full command every run (like the LAN service) so the unit's
    # onboard schedule/eco can't override us; only the setpoint is gap-gated.
    fan_state = [dec.fan_speed, dec.turbo]
    gap_ok = (now - last_cmd_time) >= cfg.min_command_gap_s
    sp_changed = (last_setpoint != dec.setpoint_f)
    send_sp = dec.setpoint_f if (last_setpoint is None or gap_ok) else last_setpoint
    applied = False
    err = None
    confirmed = None
    try:
        confirmed = midea.apply(send_sp, dec.mode, dec.ac_should_run,
                                fan_speed=dec.fan_speed, turbo=dec.turbo)
        last_mode, last_fan = dec.mode, tuple(fan_state)
        if last_setpoint is None or gap_ok:
            last_setpoint = send_sp
            if sp_changed:
                last_cmd_time = now
    except Exception as e:
        err = str(e)
        print(f"midea apply failed: {e}")

    unconfirmed = confirmed is None
    if confirmed is not None:
        conf_turbo, conf_fan = confirmed.turbo, confirmed.fan_speed
        conf_setpoint, conf_mode = confirmed.target_f, confirmed.mode
        conf_running, conf_online = confirmed.power, confirmed.online
    else:
        conf_turbo = conf_fan = conf_setpoint = conf_mode = conf_running = conf_online = None

    mismatch = []
    if confirmed is not None:
        if bool(conf_turbo) != bool(dec.turbo):
            mismatch.append("turbo")
        if conf_mode != dec.mode:
            mismatch.append("mode")
        if bool(conf_running) != bool(dec.ac_should_run):
            mismatch.append("running")
        if conf_setpoint is not None and abs(conf_setpoint - send_sp) > 0.6:
            mismatch.append("setpoint")

    # "applied" = the call succeeded AND the device confirms our full intent
    # (no mismatch on turbo/mode/running/setpoint). A failed read-back
    # (unconfirmed) can't be checked, so it counts as applied-but-unconfirmed.
    # Derived from `mismatch` so the two can never disagree.
    applied = (err is None) and (unconfirmed or not mismatch)

    # values the dashboard's existing top-level keys show: CONFIRMED when we
    # have it, else fall back to intent so a read-back failure doesn't blank
    # the dashboard out.
    show_turbo = conf_turbo if confirmed is not None else dec.turbo
    show_fan = conf_fan if confirmed is not None else dec.fan_speed
    show_setpoint = conf_setpoint if (confirmed is not None and conf_setpoint is not None) else send_sp
    show_mode = conf_mode if confirmed is not None else dec.mode
    show_running = conf_running if confirmed is not None else dec.ac_should_run

    # --- persist state ---
    STATE_FILE.write_text(json.dumps({
        "integral": ctrl.integral, "last_setpoint": last_setpoint,
        "last_mode": last_mode, "last_fan": list(last_fan) if last_fan else None,
        "last_cmd_time": last_cmd_time, "interval_s": cfg.interval_s,
        "last_office": office_temp, "last_ethan": last_ethan, "updated": now,
    }, indent=2), encoding="utf-8")

    HEADER = ["ts", "t_ethan", "t_office", "outdoor", "humidity", "target",
              "band_low", "band_high", "setpoint", "mode", "fan", "turbo",
              "ac_running", "applied", "indoor_hum", "central_cool",
              "t_living", "t_heather", "error"]
    # start fresh if the file is missing or on the OLD (shorter) schema, so the
    # dashboard always reads a single consistent header.
    fresh = True
    if HISTORY_FILE.exists():
        try:
            first = HISTORY_FILE.open(encoding="utf-8").readline().strip()
            fresh = first != ",".join(HEADER)
        except Exception:
            fresh = True
    mode_w = "w" if fresh else "a"
    with HISTORY_FILE.open(mode_w, newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        if fresh:
            wr.writerow(HEADER)
        # log the ACTUALLY-COMMANDED setpoint (send_sp), not dec.setpoint_f --
        # the gap-gate may hold the old setpoint.
        wr.writerow([round(now), t_ethan, office_temp, outdoor, humidity, sp.target,
                     sp.low, sp.high, send_sp, dec.mode, dec.fan_speed,
                     int(dec.turbo), int(dec.ac_should_run), int(applied),
                     indoor_hum, (int(central_cool) if central_cool is not None else ""),
                     t_living, t_heather, err or ""])

    STATUS_FILE.write_text(json.dumps({
        "updated": now, "t_ethan": t_ethan, "t_office": office_temp,
        "outdoor": outdoor, "humidity": humidity,
        "target": sp.target, "band": [sp.low, sp.high],
        # top-level keys show CONFIRMED-device-truth when available, else intent
        "setpoint": show_setpoint, "mode": show_mode, "fan": show_fan,
        "turbo": show_turbo, "ac_running": show_running,
        "indoor_hum": indoor_hum, "central_cool": central_cool,
        "t_living": t_living, "t_heather": t_heather,
        "in_band": (sp.low <= t_ethan <= sp.high) if t_ethan is not None else None,
        "cmd": {"setpoint": send_sp, "mode": dec.mode, "fan": dec.fan_speed,
                "turbo": dec.turbo, "running": dec.ac_should_run},
        "confirmed": {"turbo": conf_turbo, "fan": conf_fan,
                      "setpoint": conf_setpoint, "mode": conf_mode,
                      "running": conf_running, "online": conf_online},
        "unconfirmed": unconfirmed, "mismatch": mismatch,
        "applied": applied, "error": err,
    }, indent=2), encoding="utf-8")

    print(f"ethan={t_ethan:.1f}F target={sp.target:.1f} out={outdoor} "
          f"sp={send_sp:.0f} {dec.mode} fan={dec.fan_speed}"
          f"{' +turbo' if dec.turbo else ''} "
          f"{'APPLIED' if applied else 'hold'}"
          f"{' UNCONFIRMED' if unconfirmed else ''}"
          f"{' MISMATCH:' + ','.join(mismatch) if mismatch else ''}")


if __name__ == "__main__":
    main()
