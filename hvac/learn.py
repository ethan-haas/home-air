"""Offline learning: refit the feedforward from real logged data.

The controller splits responsibilities:
  * Feedback gains (kp, ki, anti-windup) govern STABILITY — tuned once against
    the simulator; they don't depend on the specific house.
  * The feedforward OFFSET — how far below target the office must run, and how
    that grows with outdoor temp — is SITE-specific (insulation, door, sun) and
    is exactly what we can learn from data.

Method: at steady operation the room obeys (approximately)

    t_ethan ~= a0 + a1*setpoint + a2*outdoor

Fit a0,a1,a2 by least squares over logged cooling samples, then invert to get
the setpoint that holds t_ethan == target:

    S*(outdoor) = (target - a0 - a2*outdoor) / a1
    offset(outdoor) = target - S*(outdoor) = c0 + c1*outdoor

which maps straight onto ControllerParams.base_offset_f / outdoor_gain. The
gains are left untouched. New params are versioned into the model_params table;
the controller always loads the latest, so the system improves as data grows.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np

from .controller import ControllerParams
from .storage import Storage

# repo-root/state/model_params.json — the same file scripts/cloud_cycle.py reads
# (PARAMS_FILE there) so the cloud control path can pick up learned params too.
HOME = Path(__file__).resolve().parent.parent
STATE_DIR = HOME / "state"
PARAMS_FILE = STATE_DIR / "model_params.json"

MIN_SAMPLES = 60          # need a meaningful amount of cooling data
OFFSET_LO, OFFSET_HI = 1.0, 12.0
# upper bound must sit ABOVE the calibrated default outdoor_gain (0.888) or the
# very first learn run can only clip the slope DOWN, silently weakening the
# tuned hot-weather feedforward.
GAIN_LO, GAIN_HI = 0.0, 1.5


# "successfully holding target" = |room - target| <= this. Kept tight: the
# commanded setpoint still contains the PI feedback term (kp*error), so a loose
# tolerance bakes feedback into the learned open-loop feedforward.
HOLD_TOL_F = 0.5


def fit_params(rows, base: ControllerParams) -> Optional[ControllerParams]:
    """Learn the site feedforward offset from SUCCESSFUL-HOLD points only.

    The naive room ~ setpoint + outdoor regression is invalid here: it's
    closed-loop data (the controller lowers the setpoint *because* the room is
    hot), so setpoint and room are spuriously anti-correlated and the fit returns
    a wrong (even negative) gain. Instead, use only moments where the room was
    actually held AT target while running — there, `target - setpoint` is the
    office offset that genuinely worked at that outdoor temp. Fit
    offset(outdoor) = a + b*(outdoor); that IS the feedforward, confound-free.
    """
    O, OFF = [], []
    for r in rows:
        sp = r["setpoint_cmd"]; te = r["t_ethan"]; out = r["outdoor_temp"]
        tgt = r["target"]; running = r["ac_running"]
        keys = r.keys()
        mode = r["mode"] if "mode" in keys else None
        if None in (sp, te, out, tgt) or not running:
            continue
        # exclude free-cool/fan (and off) rows: there ac_running==1 but the
        # compressor is off and setpoint_cmd is a never-applied cool number, so
        # `tgt - sp` is not a real office offset and would corrupt the fit.
        # (None mode = legacy/synthetic compressor-cool row -> keep.)
        if mode in ("fan", "off"):
            continue
        # exclude rows where the HOUSE central AC was co-cooling the room — the
        # Midea didn't earn that offset alone, so it over-credits the unit.
        if "central_cool" in keys and r["central_cool"]:
            continue
        if abs(te - tgt) > HOLD_TOL_F:        # only when the room was held at target
            continue
        O.append(out); OFF.append(tgt - sp)   # offset that held target at this outdoor
    if len(OFF) < MIN_SAMPLES:
        return None
    O = np.array(O, float); OFF = np.array(OFF, float)
    ref = base.outdoor_ref_f

    if np.std(O) < 1.5:
        # not enough outdoor spread to fit a slope -> just update the base offset,
        # keep the existing outdoor_gain
        base_offset = float(np.clip(np.median(OFF), OFFSET_LO, OFFSET_HI))
        return replace(base, base_offset_f=base_offset)

    slope, intercept = np.polyfit(O, OFF, 1)   # offset = intercept + slope*outdoor
    gain = float(np.clip(slope, GAIN_LO, GAIN_HI))
    base_offset = float(np.clip(intercept + slope * ref, OFFSET_LO, OFFSET_HI))
    return replace(base, base_offset_f=base_offset, outdoor_gain=gain)


def _export_params_json(params: ControllerParams,
                        path: Path = PARAMS_FILE) -> None:
    """Write learned params as JSON so the cloud path (scripts/cloud_cycle.py,
    which has no SQLite state) can also pick them up. Round-trips through
    ControllerParams.from_dict(json.load(...)) — same dict shape as to_dict()."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(params.to_dict(), indent=2))
    except Exception:
        pass    # best-effort: SQLite (st.save_params) remains the source of truth


def learn(db_path, verbose: bool = True) -> Optional[dict]:
    st = Storage(db_path)
    try:
        base = ControllerParams.from_dict(st.latest_params())
        rows = st.recent_readings(limit=20000)
        new = fit_params(rows, base)
        if new is None:
            if verbose:
                print(f"learn: insufficient/ill-conditioned data "
                      f"({len(rows)} rows); keeping current params")
            return None
        n = sum(1 for r in rows if r["ac_running"] and r["setpoint_cmd"] is not None)
        st.save_params(new.to_dict(), source="learn", n_samples=n)
        _export_params_json(new)
        if verbose:
            print(f"learn: refit from {n} cooling samples -> "
                  f"base_offset_f={new.base_offset_f:.2f} "
                  f"outdoor_gain={new.outdoor_gain:.3f}")
        return new.to_dict()
    finally:
        st.close()


def main() -> None:
    from .config import DB_PATH
    learn(DB_PATH)


if __name__ == "__main__":
    main()
