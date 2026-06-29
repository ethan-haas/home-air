"""Web dashboard: live status + history, read from the SQLite log.

Pure stdlib HTTP server (no Flask). The data layer (`status_payload`,
`history_payload`, `actions_payload`) is split from the HTTP layer so it can be
unit-tested without binding a socket. Endpoints:

    GET /                 -> the dashboard page (web/index.html)
    GET /api/status       -> current reading, band, health, 24h summary, params
    GET /api/history?hours=24 -> time series for the charts
    GET /api/actions?limit=50 -> recent setpoint changes / errors
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .schedule import ScheduleConfig, schedule_point
from .storage import Storage

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _hour_of(ts: float) -> float:
    lt = datetime.fromtimestamp(ts)
    return lt.hour + lt.minute / 60.0


def _band_for(row, sched: ScheduleConfig) -> tuple[float, float, float]:
    sp = schedule_point(_hour_of(row["ts"]), row["outdoor_temp"], None, sched)
    return sp.target, sp.low, sp.high


def status_payload(storage: Storage, sched: ScheduleConfig | None = None,
                   now: float | None = None) -> dict:
    sched = sched or ScheduleConfig()
    now = now or time.time()
    rows = storage.recent_readings(limit=2000)
    total = storage.count_readings()
    params = storage.latest_params()
    if not rows:
        return {"ok": True, "has_data": False, "total_readings": total,
                "params": params}

    last = rows[-1]
    tgt, low, high = _band_for(last, sched)
    te = last["t_ethan"]
    in_band = (te is not None and low <= te <= high)

    # 24h (or available) summary
    cutoff = now - 24 * 3600
    win = [r for r in rows if r["ts"] >= cutoff and r["t_ethan"] is not None]
    summary = {}
    if win:
        bands = [_band_for(r, sched) for r in win]
        ib = sum(1 for r, (t, lo, hi) in zip(win, bands)
                 if lo <= r["t_ethan"] <= hi)
        duty = [r["ac_running"] for r in win if r["ac_running"] is not None]
        temps = [r["t_ethan"] for r in win]
        summary = {
            "n": len(win),
            "in_band_pct": round(100 * ib / len(win), 1),
            "ac_on_pct": round(100 * sum(duty) / len(duty), 1) if duty else None,
            "min_ethan": round(min(temps), 1),
            "max_ethan": round(max(temps), 1),
            "avg_ethan": round(sum(temps) / len(temps), 1),
        }

    return {
        "ok": True, "has_data": True, "total_readings": total,
        "now": now,
        "current": {
            "ts": last["ts"], "age_s": round(now - last["ts"]),
            "t_ethan": te, "t_office": last["t_office"],
            "outdoor": last["outdoor_temp"], "humidity": last["outdoor_hum"],
            "target": tgt, "band_low": low, "band_high": high,
            "setpoint": last["setpoint_cmd"], "mode": last["mode"],
            "ac_running": last["ac_running"], "error": last["error"],
            "in_band": in_band,
            "fan": (last["fan"] if "fan" in last.keys() else None),
            "turbo": (last["turbo"] if "turbo" in last.keys() else None),
            "t_living": (last["t_living"] if "t_living" in last.keys() else None),
            "t_heather": (last["t_heather"] if "t_heather" in last.keys() else None),
            "indoor_hum": (last["indoor_hum"] if "indoor_hum" in last.keys() else None),
            "central_cool": (last["central_cool"] if "central_cool" in last.keys() else None),
        },
        "summary_24h": summary,
        "params": params,
    }


def history_payload(storage: Storage, hours: float = 24,
                    sched: ScheduleConfig | None = None,
                    now: float | None = None) -> dict:
    sched = sched or ScheduleConfig()
    now = now or time.time()
    cutoff = now - hours * 3600
    rows = [r for r in storage.recent_readings(limit=20000) if r["ts"] >= cutoff]
    pts = []
    for r in rows:
        tgt, low, high = _band_for(r, sched)
        k = r.keys()
        pts.append({
            "ts": r["ts"], "t_ethan": r["t_ethan"], "t_office": r["t_office"],
            "outdoor": r["outdoor_temp"], "target": tgt,
            "band_low": low, "band_high": high,
            "setpoint": r["setpoint_cmd"], "ac_running": r["ac_running"],
            "mode": r["mode"], "humidity": r["outdoor_hum"],
            "turbo": (r["turbo"] if "turbo" in k else None),
            "t_living": (r["t_living"] if "t_living" in k else None),
            "t_heather": (r["t_heather"] if "t_heather" in k else None),
        })
    return {"ok": True, "hours": hours, "points": pts}


def _period(hour: float, sched: ScheduleConfig) -> str:
    h = hour % 24
    if h >= sched.night_start_h or h < sched.night_end_h:
        return "night"
    if (sched.night_start_h - sched.precool_ramp_h) <= h < sched.night_start_h:
        return "evening"
    return "day"


# rough power model for the 12k BTU Midea Duo (it reports no real power):
# compressor cooling ~1.1 kW, turbo ~1.5 kW, FAN-only (free-cool) ~0.12 kW, idle ~0.05.
_KW_COOL, _KW_TURBO, _KW_FAN, _KW_IDLE = 1.1, 1.5, 0.12, 0.05
_COST_PER_KWH = 0.15            # $/kWh (US avg-ish; adjust per your utility)


def _row_kw(r) -> float:
    k = r.keys()
    if not r["ac_running"]:
        return _KW_IDLE
    if r["mode"] == "fan":               # free-cool: fan only, compressor OFF
        return _KW_FAN
    return _KW_TURBO if ("turbo" in k and r["turbo"]) else _KW_COOL


def analytics_payload(storage: Storage, sched: ScheduleConfig | None = None,
                      days: float = 7, now: float | None = None) -> dict:
    sched = sched or ScheduleConfig()
    now = now or time.time()
    rows = [r for r in storage.recent_readings(limit=50000)
            if r["ts"] >= now - days * 86400 and r["t_ethan"] is not None]
    if not rows:
        return {"ok": True, "has_data": False}

    # --- per-period comfort + energy ---
    periods: dict = {}
    for r in rows:
        p = _period(_hour_of(r["ts"]), sched)
        tgt, lo, hi = _band_for(r, sched)
        kk = r.keys()
        d = periods.setdefault(p, {"n": 0, "in": 0, "abserr": 0.0, "temp": 0.0,
                                   "acon": 0, "turbo": 0, "central": 0})
        d["n"] += 1
        d["in"] += 1 if lo <= r["t_ethan"] <= hi else 0
        d["abserr"] += abs(r["t_ethan"] - ((lo + hi) / 2))
        d["temp"] += r["t_ethan"]
        d["acon"] += 1 if r["ac_running"] else 0
        d["turbo"] += 1 if ("turbo" in kk and r["turbo"]) else 0
        d["central"] += 1 if ("central_cool" in kk and r["central_cool"]) else 0
    period_stats = {p: {
        "n": d["n"],
        "in_band_pct": round(100 * d["in"] / d["n"], 1),
        "mae": round(d["abserr"] / d["n"], 2),
        "avg_temp": round(d["temp"] / d["n"], 1),
        "ac_on_pct": round(100 * d["acon"] / d["n"], 1),
        "turbo_pct": round(100 * d["turbo"] / d["n"], 1),
        "central_ac_pct": round(100 * d["central"] / d["n"], 1),
    } for p, d in periods.items()}

    # --- correlations (what drives Ethan's room) ---
    corr = {}
    try:
        import numpy as np
        te = np.array([r["t_ethan"] for r in rows], float)
        for key, col in (("outdoor", "outdoor_temp"), ("office", "t_office"),
                         ("setpoint", "setpoint_cmd")):
            v = np.array([r[col] if r[col] is not None else np.nan for r in rows], float)
            m = ~np.isnan(v)
            if m.sum() > 10 and np.std(v[m]) > 0.1 and np.std(te[m]) > 0.1:
                corr[key] = round(float(np.corrcoef(te[m], v[m])[0, 1]), 2)
    except Exception:
        pass

    # --- 9pm goal tracker: temp at ~21:00 each day vs 68-70 goal ---
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(r["ts"])
        by_day[d.strftime("%Y-%m-%d")].append((d.hour + d.minute / 60.0, r["t_ethan"]))
    nights = []
    for day, pts in sorted(by_day.items()):
        near = min(pts, key=lambda x: abs(x[0] - 21.0), default=None)
        if near and abs(near[0] - 21.0) <= 1.0:
            t = round(near[1], 1)
            nights.append({"date": day, "temp_9pm": t,
                           "met_goal": sched.night_low <= t <= sched.night_high})

    # --- per-day summary + energy/cost + mode breakdown (compressor vs free-cool) ---
    day_agg = defaultdict(lambda: {"n": 0, "in": 0, "kwh": 0.0, "ac_s": 0.0,
                                   "min": 999.0, "max": -999.0})
    total_kwh = 0.0
    mode_s = {"cool": 0.0, "fan": 0.0, "off": 0.0}   # seconds in each mode
    gap_sum = 0.0; gap_n = 0                          # office->room gap (coupling health)
    srt = sorted(rows, key=lambda r: r["ts"])
    for i, r in enumerate(srt):
        dt_s = 0.0
        if i + 1 < len(srt):
            dt_s = min(600.0, max(0.0, srt[i + 1]["ts"] - r["ts"]))
        kwh = _row_kw(r) * dt_s / 3600.0
        total_kwh += kwh
        md = r["mode"] if r["mode"] in ("cool", "fan", "off") else (
            "cool" if r["ac_running"] else "off")
        # a 'fan' row with the unit NOT running is idle, not free-cooling — don't
        # credit it free-cool hours or compressor savings (it was charged idle
        # power by _row_kw, so counting it as fan would double-count).
        if md == "fan" and not r["ac_running"]:
            md = "off"
        mode_s[md] += dt_s
        if r["t_office"] is not None:
            gap_sum += (r["t_ethan"] - r["t_office"]); gap_n += 1
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        tgt, lo, hi = _band_for(r, sched)
        a = day_agg[day]
        a["n"] += 1
        a["in"] += 1 if lo <= r["t_ethan"] <= hi else 0
        a["kwh"] += kwh
        a["ac_s"] += dt_s if r["ac_running"] else 0.0
        a["min"] = min(a["min"], r["t_ethan"])
        a["max"] = max(a["max"], r["t_ethan"])
    daily = []
    for day, a in sorted(day_agg.items()):
        n9 = next((x for x in nights if x["date"] == day), None)
        daily.append({
            "date": day,
            "in_band_pct": round(100 * a["in"] / a["n"], 1),
            "min": round(a["min"], 1), "max": round(a["max"], 1),
            "ac_hours": round(a["ac_s"] / 3600.0, 1),
            "kwh": round(a["kwh"], 1), "cost": round(a["kwh"] * _COST_PER_KWH, 2),
            "temp_9pm": (n9["temp_9pm"] if n9 else None),
        })

    return {
        "ok": True, "has_data": True, "days": days, "n": len(rows),
        "periods": period_stats, "correlations": corr,
        "nights_9pm": nights[-14:],
        # count over the SAME last-14 slice the UI renders as chips, else the
        # header "(hit/nights met)" can exceed the visible chip count.
        "goal_summary": {"nights": len(nights[-14:]),
                         "hit": sum(1 for n in nights[-14:] if n["met_goal"]),
                         "night_low": sched.night_low, "night_high": sched.night_high,
                         "night_target": sched.night_target,
                         "day_low": sched.day_low, "day_high": sched.day_high},
        "daily": daily[-14:],
        "energy": {"kwh": round(total_kwh, 1),
                   "cost": round(total_kwh * _COST_PER_KWH, 2),
                   "cost_per_kwh": _COST_PER_KWH, "days": days},
        "modes": {k: round(v / 3600.0, 1) for k, v in mode_s.items()},  # hours
        "freecool": {
            "fan_hours": round(mode_s["fan"] / 3600.0, 1),
            # kWh saved by free-cooling (fan) instead of running the compressor
            "kwh_saved": round(mode_s["fan"] / 3600.0 * (_KW_COOL - _KW_FAN), 1),
            "cost_saved": round(mode_s["fan"] / 3600.0 * (_KW_COOL - _KW_FAN)
                                * _COST_PER_KWH, 2),
        },
        "office": {"avg_room_office_gap": round(gap_sum / gap_n, 1) if gap_n else None},
    }


def actions_payload(storage: Storage, limit: int = 50) -> dict:
    cur = storage.conn.execute(
        "SELECT ts, kind, detail FROM actions ORDER BY ts DESC LIMIT ?", (limit,))
    rows = [{"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]}
            for r in cur.fetchall()]
    return {"ok": True, "actions": rows}


def make_handler(db_path, sched: ScheduleConfig | None = None):
    sched = sched or ScheduleConfig()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):       # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str):
            if not path.exists():
                self._json({"ok": False, "error": "not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            st = Storage(db_path)
            try:
                if u.path in ("/", "/index.html"):
                    self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                elif u.path == "/api/status":
                    self._json(status_payload(st, sched))
                elif u.path == "/api/history":
                    hours = float(q.get("hours", ["24"])[0])
                    self._json(history_payload(st, hours, sched))
                elif u.path == "/api/analytics":
                    days = float(q.get("days", ["7"])[0])
                    self._json(analytics_payload(st, sched, days))
                elif u.path == "/api/actions":
                    limit = int(q.get("limit", ["50"])[0])
                    self._json(actions_payload(st, limit))
                else:
                    self._json({"ok": False, "error": "unknown route"}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            finally:
                st.close()

    return Handler


def serve(db_path, host: str = "0.0.0.0", port: int = 8787,
          sched: ScheduleConfig | None = None) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, sched))
    print(f"Home_Air dashboard: http://localhost:{port}  (db={db_path})")
    httpd.serve_forever()
