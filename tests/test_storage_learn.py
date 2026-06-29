import math

from hvac.storage import Storage, Reading
from hvac.controller import ControllerParams
from hvac.learn import fit_params


def test_storage_roundtrip(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.log_reading(Reading(ts=1.0, t_ethan=70.0, outdoor_temp=85.0,
                           setpoint_cmd=65.0, ac_running=1, target=70.0))
    assert st.count_readings() == 1
    st.save_params({"base_offset_f": 5.0}, source="manual")
    assert st.latest_params()["base_offset_f"] == 5.0
    st.close()


def test_latest_params_returns_newest(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.save_params({"kp": 1.0}, source="default")
    st.save_params({"kp": 2.0}, source="learn")
    assert st.latest_params()["kp"] == 2.0
    st.close()


def test_learn_recovers_synthetic_offset(tmp_path):
    """From SUCCESSFUL-HOLD points (room == target), learn must recover the true
    office offset(outdoor) — positive slope, sane base. (The old naive regression
    returned the wrong sign on closed-loop data; this method avoids that.)"""
    st = Storage(tmp_path / "t.db")
    import random
    rng = random.Random(0)
    a_true, b_true, target = 2.0, 0.08, 70.0   # offset = 2 + 0.08*outdoor held target
    ts = 0.0
    for _ in range(400):
        out = rng.uniform(78, 100)
        sp = target - (a_true + b_true * out)            # the setpoint that holds target
        te = target + rng.uniform(-0.4, 0.4)             # room actually AT target
        ts += 60
        st.log_reading(Reading(ts=ts, t_ethan=te, outdoor_temp=out,
                               setpoint_cmd=sp, ac_running=1, target=target))
    new = fit_params(st.recent_readings(), ControllerParams())
    assert new is not None
    assert new.outdoor_gain > 0.03                       # positive slope recovered
    assert abs(new.outdoor_gain - b_true) < 0.06         # close to true slope
    assert 1.0 <= new.base_offset_f <= 12.0
    st.close()


def test_learn_insufficient_data_returns_none(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.log_reading(Reading(ts=1.0, t_ethan=70.0, outdoor_temp=85.0,
                           setpoint_cmd=65.0, ac_running=1))
    assert fit_params(st.recent_readings(), ControllerParams()) is None
    st.close()
