import time

from hvac.storage import Storage, Reading
from hvac.dashboard import status_payload, history_payload, actions_payload


def _seed(st, now):
    for i in range(30):
        ts = now - (30 - i) * 300
        st.log_reading(Reading(
            ts=ts, t_ethan=70.0 + (i % 3), t_office=66.0,
            outdoor_temp=85.0, outdoor_hum=40.0, target=70.0,
            setpoint_cmd=64.0, ac_running=1, mode="cool",
            error=0.5, integral=10.0, note="test"))
    st.log_action("setpoint", "sp=64 mode=cool")
    st.save_params({"base_offset_f": 5.1, "kp": 2.4}, source="learn", n_samples=120)


def test_status_no_data(tmp_path):
    st = Storage(tmp_path / "d.db")
    s = status_payload(st)
    assert s["ok"] and s["has_data"] is False
    st.close()


def test_status_payload(tmp_path):
    now = time.time()
    st = Storage(tmp_path / "d.db")
    _seed(st, now)
    s = status_payload(st, now=now)
    assert s["has_data"] is True
    cur = s["current"]
    assert cur["t_ethan"] is not None
    assert cur["band_low"] < cur["band_high"]
    assert "in_band" in cur
    assert s["summary_24h"]["n"] == 30
    assert 0 <= s["summary_24h"]["in_band_pct"] <= 100
    assert s["params"]["base_offset_f"] == 5.1
    st.close()


def test_history_payload_windowing(tmp_path):
    now = time.time()
    st = Storage(tmp_path / "d.db")
    _seed(st, now)
    # only last 0.5h -> at most 6 of the 5-min-spaced points
    h = history_payload(st, hours=0.5, now=now)
    assert h["ok"]
    assert 0 < len(h["points"]) <= 7
    p = h["points"][0]
    for k in ("ts", "t_ethan", "outdoor", "target", "band_low", "band_high"):
        assert k in p
    st.close()


def test_actions_payload(tmp_path):
    st = Storage(tmp_path / "d.db")
    _seed(st, time.time())
    a = actions_payload(st, limit=10)
    assert a["actions"] and a["actions"][0]["kind"] == "setpoint"
    st.close()


def test_analytics_payload(tmp_path):
    import time
    from hvac.dashboard import analytics_payload
    now = time.time()
    st = Storage(tmp_path / "a.db")
    # seed a night reading (~9pm) + a day reading
    for hours_ago, temp in [(0.1, 69.0), (12.0, 73.0)]:
        st.log_reading(Reading(ts=now - hours_ago*3600, t_ethan=temp,
                               t_office=64.0, outdoor_temp=82.0, target=70.0,
                               setpoint_cmd=62.0, ac_running=1, mode="cool",
                               turbo=1, t_living=72.0, t_heather=71.0))
    a = analytics_payload(st, days=2, now=now)
    assert a["has_data"] is True
    assert "periods" in a and "correlations" in a
    assert "goal_summary" in a
    st.close()


def test_storage_migration_adds_columns(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    # simulate an OLD db without the new columns
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, ts REAL, t_ethan REAL)")
    c.commit(); c.close()
    st = Storage(p)   # should migrate
    cols = {r["name"] for r in st.conn.execute("PRAGMA table_info(readings)")}
    assert {"turbo", "fan", "t_living", "t_heather", "forecast_temp"} <= cols
    st.close()
