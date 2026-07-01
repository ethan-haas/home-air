"""End-to-end tests for scripts/cloud_cycle.py — device-confirmed-state contract."""

import json
import csv
import time
import sys
from pathlib import Path
from unittest.mock import Mock, MagicMock

import pytest

# Add the parent dir to path so we can import scripts.cloud_cycle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvac.midea_client import MideaState


class FakeEcobeeClient:
    """Minimal ecobee mock returning sensor readings."""
    def __init__(self, cfg, temps_ethan=None, temps_living=None, temps_heather=None,
                 humidity=None, should_fail_full=False, should_fail_ethan=False):
        self.cfg = cfg
        self.temps_ethan = temps_ethan or [72.0]
        self.temps_living = temps_living or [72.0]
        self.temps_heather = temps_heather or [72.0]
        self.humidity = humidity or 45.0
        self.should_fail_full = should_fail_full
        self.should_fail_ethan = should_fail_ethan
        self._call_count = 0

    def read_full(self):
        if self.should_fail_full:
            raise RuntimeError("simulated ecobee read_full failure")
        ethan = self.temps_ethan[min(self._call_count, len(self.temps_ethan) - 1)]
        return {
            "ethan": ethan,
            "living": self.temps_living[min(self._call_count, len(self.temps_living) - 1)],
            "heather": self.temps_heather[min(self._call_count, len(self.temps_heather) - 1)],
            "indoor_hum": self.humidity,
            "central_cool": None,
        }

    def read_ethan_temp(self):
        if self.should_fail_ethan:
            raise RuntimeError("simulated ecobee read_ethan_temp failure")
        result = self.temps_ethan[min(self._call_count, len(self.temps_ethan) - 1)]
        self._call_count += 1
        return result


class FakeMideaCloudClient:
    """Minimal Midea cloud mock."""
    def __init__(self, cfg, device_accepts=True, device_turbo_result=None,
                 setpoint_offset=0.0):
        self.cfg = cfg
        self.device_accepts = device_accepts
        self.device_turbo_result = device_turbo_result
        self.setpoint_offset = setpoint_offset   # F/C round-trip drift to simulate
        self.last_apply_call = None

    def refresh(self):
        """Simulate reading current device state."""
        return MideaState(
            indoor_temp_f=72.0,
            target_f=72.0,
            power=False,
            mode="cool",
            online=True,
            fan_speed="auto",
            turbo=False,
        )

    def apply(self, setpoint_f, mode, power, fan_speed=None, turbo=False):
        """Simulate applying a command and reading back confirmed state."""
        self.last_apply_call = {
            "setpoint_f": setpoint_f,
            "mode": mode,
            "power": power,
            "fan_speed": fan_speed,
            "turbo": turbo,
        }
        if self.device_accepts is None:
            # Simulate read-back failure
            return None

        # Device confirms the command (or deliberately refuses it)
        confirmed_turbo = self.device_turbo_result if self.device_turbo_result is not None else (turbo if self.device_accepts else False)

        return MideaState(
            indoor_temp_f=72.0,
            target_f=setpoint_f + self.setpoint_offset,
            power=power if self.device_accepts else False,
            mode=mode if self.device_accepts else "cool",
            online=True,
            fan_speed=fan_speed if self.device_accepts else "auto",
            turbo=confirmed_turbo,
        )


class FakeWeather:
    """Simple weather object."""
    def __init__(self, temp_f=75.0, forecast_temp_f=76.0, humidity=50.0):
        self.temp_f = temp_f
        self.forecast_temp_f = forecast_temp_f
        self.humidity = humidity


@pytest.fixture
def cloud_cycle_env(tmp_path, monkeypatch):
    """Set up a clean environment for cloud_cycle tests."""
    # Redirect state files to tmp_path
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Monkeypatch the module-level paths in scripts.cloud_cycle
    import scripts.cloud_cycle
    monkeypatch.setattr(scripts.cloud_cycle, "STATE_DIR", state_dir)
    monkeypatch.setattr(scripts.cloud_cycle, "STATE_FILE", state_dir / "controller_state.json")
    monkeypatch.setattr(scripts.cloud_cycle, "PARAMS_FILE", state_dir / "model_params.json")
    monkeypatch.setattr(scripts.cloud_cycle, "HISTORY_FILE", state_dir / "history.csv")
    monkeypatch.setattr(scripts.cloud_cycle, "STATUS_FILE", state_dir / "status.json")

    # Mock config
    mock_config = Mock()
    mock_config.load.return_value = mock_config
    mock_config.midea_transport = "cloud"
    mock_config.setpoint_min_f = 60.0
    mock_config.setpoint_max_f = 82.0
    mock_config.setpoint_step_f = 1.0
    mock_config.interval_s = 120
    mock_config.min_command_gap_s = 300
    mock_config.latitude = 40.1
    mock_config.longitude = -82.9
    mock_config.target_f = 72.0
    # default fixture models a turbo-CAPABLE unit so the confirmed-state contract
    # tests exercise the turbo path; the suppression test overrides this to False.
    mock_config.midea_turbo_supported = True

    monkeypatch.setattr("scripts.cloud_cycle.Config", mock_config)

    return {
        "state_dir": state_dir,
        "config": mock_config,
    }


def test_happy_path_device_accepts_turbo(cloud_cycle_env, monkeypatch):
    """Happy path: Ethan's temp is hot (76F), controller decides turbo, device accepts.
    Assert: applied=True, mismatch=[], turbo confirmed, cmd.turbo=True."""
    state_dir = cloud_cycle_env["state_dir"]

    # Hot day, so turbo should be considered
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[76.0])
    fake_midea = FakeMideaCloudClient(None, device_accepts=True, device_turbo_result=True)
    fake_weather = FakeWeather(temp_f=90.0, forecast_temp_f=92.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    # Patch at the source modules where cloud_cycle imports from
    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)

    # Simulate a day-time run (e.g., 2pm = hour 14)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    # Read status.json
    status_file = state_dir / "status.json"
    assert status_file.exists(), "status.json was not created"
    status = json.loads(status_file.read_text())

    # Check core assertions
    assert status["applied"] is True, "device should have accepted the command"
    assert status["mismatch"] == [], f"mismatch should be empty, got {status['mismatch']}"
    assert status["unconfirmed"] is False, "should be confirmed"
    assert status["cmd"]["turbo"] is True, "intent was to send turbo"
    assert status["confirmed"]["turbo"] is True, "device confirmed turbo"
    assert status["turbo"] is True, "top-level turbo shows confirmed value"


def test_device_refuses_turbo(cloud_cycle_env, monkeypatch):
    """Device REFUSES turbo: intent turbo=True, confirmed turbo=False.
    Assert: mismatch=['turbo'], applied=False, cmd.turbo=True, confirmed.turbo=False,
    top-level turbo=False (device truth)."""
    state_dir = cloud_cycle_env["state_dir"]

    # Hot day -> controller wants turbo
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[76.0])
    # Device accepts command but drops turbo
    fake_midea = FakeMideaCloudClient(None, device_accepts=True, device_turbo_result=False)
    fake_weather = FakeWeather(temp_f=90.0, forecast_temp_f=92.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    status_file = state_dir / "status.json"
    status = json.loads(status_file.read_text())

    assert "turbo" in status["mismatch"], "turbo should be in mismatch list"
    assert status["applied"] is False, "device did not accept intent (turbo mismatch)"
    assert status["cmd"]["turbo"] is True, "we intended turbo"
    assert status["confirmed"]["turbo"] is False, "device refused turbo"
    assert status["turbo"] is False, "top-level turbo shows device truth (False)"


def test_readback_fails_apply_returns_none(cloud_cycle_env, monkeypatch):
    """Midea apply returns None (read-back failed).
    Assert: unconfirmed=True, applied=True (unconfirmed-but-no-exception),
    confirmed fields are all None, top-level values fall back to intent."""
    state_dir = cloud_cycle_env["state_dir"]

    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[72.0])
    # apply() returns None = read-back failed
    fake_midea = FakeMideaCloudClient(None, device_accepts=None)
    fake_weather = FakeWeather(temp_f=75.0, forecast_temp_f=76.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    status_file = state_dir / "status.json"
    status = json.loads(status_file.read_text())

    assert status["unconfirmed"] is True, "should be unconfirmed (read-back failed)"
    assert status["applied"] is True, "applied=True when confirmed is None (no exception)"
    assert status["confirmed"]["turbo"] is None, "confirmed fields should be None"
    assert status["confirmed"]["fan"] is None
    assert status["confirmed"]["setpoint"] is None
    # Top-level values fall back to intent
    assert status["turbo"] == status["cmd"]["turbo"], "top-level turbo falls back to intent"
    assert status["fan"] == status["cmd"]["fan"], "top-level fan falls back to intent"


def test_ecobee_outage_no_prior_state(cloud_cycle_env, monkeypatch):
    """Ecobee total outage + no prior state: read_full and read_ethan_temp both fail.
    Assert: main() does NOT raise, writes status.json with error set, applied=False,
    no actuation happens."""
    state_dir = cloud_cycle_env["state_dir"]

    # Both methods fail
    fake_ecobee = FakeEcobeeClient(None, should_fail_full=True, should_fail_ethan=True)
    fake_midea = FakeMideaCloudClient(None)
    fake_weather = FakeWeather(temp_f=75.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)

    from scripts.cloud_cycle import main
    # Should NOT raise
    main()

    status_file = state_dir / "status.json"
    assert status_file.exists(), "status.json should be written even on outage"
    status = json.loads(status_file.read_text())

    assert status["error"] is not None, "should have an error message"
    assert status["applied"] is False, "should not have applied a command"
    assert status["t_ethan"] is None, "Ethan temp should be None"

    # Verify a history row was written (even with error)
    history_file = state_dir / "history.csv"
    assert history_file.exists(), "history.csv should exist"
    lines = history_file.read_text().split('\n')
    assert len(lines) >= 2, "history should have header + at least 1 data row"


def test_ecobee_outage_with_prior_state(cloud_cycle_env, monkeypatch):
    """Ecobee outage WITH prior last_ethan in state: carry forward and make decision.
    Assert: a decision is made (setpoint is sent), status.json shows the applied command,
    no error is set (graceful fallback, not failure)."""
    state_dir = cloud_cycle_env["state_dir"]

    # Pre-seed state with last_ethan
    prior_state = {
        "integral": 0.0,
        "last_setpoint": 72.0,
        "last_mode": "cool",
        "last_fan": ["auto", 0],
        "last_cmd_time": time.time() - 400,  # old enough to allow a new command
        "last_office": 72.0,
        "last_ethan": 74.0,  # carried-forward temp
        "updated": time.time(),
    }
    state_file = state_dir / "controller_state.json"
    state_file.write_text(json.dumps(prior_state, indent=2))

    # Both ecobee methods fail; Midea works
    fake_ecobee = FakeEcobeeClient(None, should_fail_full=True, should_fail_ethan=True)
    fake_midea = FakeMideaCloudClient(None, device_accepts=True)
    fake_weather = FakeWeather(temp_f=75.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    status_file = state_dir / "status.json"
    status = json.loads(status_file.read_text())

    # A decision was made (using carried-forward last_ethan)
    assert status["applied"] is True, "should have applied a command using carried-forward temp"
    # error should be None when we successfully make a decision with carried-forward data
    assert status["error"] is None or "no Ethan temp" not in status.get("error", ""), \
        "should gracefully use carried-forward temp, not fail"

    # Verify last_ethan was carried forward in state
    new_state = json.loads(state_file.read_text())
    assert new_state["last_ethan"] == 74.0, "last_ethan should be carried forward"


def test_setpoint_logged_respects_gap_gate(cloud_cycle_env, monkeypatch):
    """Gap-gated setpoint: when min_command_gap blocks a setpoint change,
    the LOGGED setpoint should be the held value (send_sp), not the desired dec.setpoint_f.
    Assert: CSV setpoint column matches send_sp (the gap-held value)."""
    state_dir = cloud_cycle_env["state_dir"]

    # Pre-seed state with a recent command to trigger the gap-gate
    now = time.time()
    prior_state = {
        "integral": 0.0,
        "last_setpoint": 72.0,
        "last_mode": "cool",
        "last_fan": ["auto", 0],
        "last_cmd_time": now - 100,  # recent, within the 300s gap
        "last_office": 72.0,
        "last_ethan": 70.0,
        "updated": now,
    }
    state_file = state_dir / "controller_state.json"
    state_file.write_text(json.dumps(prior_state, indent=2))

    # Controller will want a different setpoint (cooler on a hot day)
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[76.0])
    fake_midea = FakeMideaCloudClient(None, device_accepts=True)
    fake_weather = FakeWeather(temp_f=90.0, forecast_temp_f=92.0)

    def get_weather_mock(lat, lon, lead_min):
        return fake_weather

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_mock)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    # Read CSV and check the setpoint column
    history_file = state_dir / "history.csv"
    assert history_file.exists()

    with open(history_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) >= 1, "history should have at least one data row"
    last_row = rows[-1]

    # The setpoint in the CSV should be send_sp (gap-held value, 72.0)
    logged_setpoint = float(last_row["setpoint"]) if last_row["setpoint"] else None
    assert logged_setpoint == 72.0, \
        f"CSV setpoint should be gap-held value (72.0), got {logged_setpoint}"

    # Verify that status.json's cmd.setpoint is also the send_sp
    status_file = state_dir / "status.json"
    status = json.loads(status_file.read_text())
    assert status["cmd"]["setpoint"] == 72.0, "cmd.setpoint should be send_sp (72.0)"


def test_all_sensors_fail_gracefully(cloud_cycle_env, monkeypatch):
    """All external services fail: ecobee, midea, weather.
    Assert: main() completes cleanly, status.json written with error, no crash."""
    state_dir = cloud_cycle_env["state_dir"]

    fake_ecobee = FakeEcobeeClient(None, should_fail_full=True, should_fail_ethan=True)
    fake_midea = FakeMideaCloudClient(None)

    def get_weather_fails(lat, lon, lead_min):
        raise RuntimeError("weather service down")

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", get_weather_fails)

    from scripts.cloud_cycle import main
    # Should complete without raising
    main()

    status_file = state_dir / "status.json"
    assert status_file.exists()
    status = json.loads(status_file.read_text())

    # status.json should reflect the failure state
    assert status["error"] is not None, "should have error message"
    assert status["t_ethan"] is None, "no Ethan temp available"
    assert status["outdoor"] is None, "no outdoor temp available"


def test_turbo_suppressed_when_unsupported(cloud_cycle_env, monkeypatch):
    """Gen-2 REFINE (grounded in a live run: this Duo refuses turbo over cloud).
    With midea_turbo_supported=False, a hot-room boost decision must be
    suppressed on the cloud path: no phantom turbo requested, MAX fan held, and
    no 'turbo' mismatch on the dashboard."""
    cloud_cycle_env["config"].midea_turbo_supported = False   # this unit can't turbo
    state_dir = cloud_cycle_env["state_dir"]

    # Hot day -> controller WOULD choose turbo, but the cloud path must suppress it
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[76.0])
    # Faithful device: reports back exactly what it was commanded
    fake_midea = FakeMideaCloudClient(None, device_accepts=True)
    fake_weather = FakeWeather(temp_f=90.0, forecast_temp_f=92.0)

    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", lambda lat, lon, lead_min: fake_weather)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)

    from scripts.cloud_cycle import main
    main()

    status = json.loads((state_dir / "status.json").read_text())
    assert status["cmd"]["turbo"] is False, "turbo suppressed on unsupported cloud unit"
    assert status["cmd"]["fan"] == "max", "MAX fan held for airflow instead of turbo"
    assert "turbo" not in status["mismatch"], "no phantom turbo -> no turbo mismatch"
    assert status["turbo"] is False


def test_setpoint_cquantum_not_flagged(cloud_cycle_env, monkeypatch):
    """A ~0.9F F/C round-trip drift (60.0F -> 16C -> 60.8F) must NOT show as a
    setpoint mismatch, else the dashboard cries wolf every cycle."""
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[72.0])
    fake_midea = FakeMideaCloudClient(None, device_accepts=True, setpoint_offset=0.8)
    fake_weather = FakeWeather(temp_f=80.0, forecast_temp_f=82.0)
    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", lambda lat, lon, lead_min: fake_weather)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)
    from scripts.cloud_cycle import main
    main()
    status = json.loads((cloud_cycle_env["state_dir"] / "status.json").read_text())
    assert "setpoint" not in status["mismatch"], "0.8F C-quantum drift is not a real mismatch"


def test_setpoint_large_drift_flagged(cloud_cycle_env, monkeypatch):
    """A genuine setpoint divergence (>1F) still flags."""
    fake_ecobee = FakeEcobeeClient(None, temps_ethan=[72.0])
    fake_midea = FakeMideaCloudClient(None, device_accepts=True, setpoint_offset=2.0)
    fake_weather = FakeWeather(temp_f=80.0, forecast_temp_f=82.0)
    monkeypatch.setattr("hvac.ecobee_client.EcobeeClient", lambda cfg: fake_ecobee)
    monkeypatch.setattr("hvac.midea_cloud.MideaCloudClient", lambda cfg: fake_midea)
    monkeypatch.setattr("hvac.weather.get_weather", lambda lat, lon, lead_min: fake_weather)
    monkeypatch.setattr("scripts.cloud_cycle.local_hour", lambda now: 14.0)
    from scripts.cloud_cycle import main
    main()
    status = json.loads((cloud_cycle_env["state_dir"] / "status.json").read_text())
    assert "setpoint" in status["mismatch"], "2F divergence is a real mismatch"
