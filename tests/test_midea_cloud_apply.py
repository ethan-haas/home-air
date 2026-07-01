"""Direct unit tests for MideaCloudClient.apply — how this Duo's boost actually
works. Verified against the live device: the app's Turbo/Boost button drives the
`turbo_fan` wire attribute (data[8] bit5); the library's `turbo` (data[10] bit1)
is a different bit this unit ignores. So boost is READ off turbo_fan, and it is
only COMMANDED when the operator opts in (config midea_apply_turbo_fan) — else a
boost the user set by hand in the app must persist untouched.
"""
import pytest

from hvac.config import Config
from hvac.midea_cloud import MideaCloudClient, _MODE, _FAN


class FakeState:
    """Stand-in for midea_beautiful's AirConditionerState."""
    def __init__(self, **kw):
        self.indoor_temperature = kw.get("indoor_temperature", 23.0)   # C
        self.target_temperature = kw.get("target_temperature", 16.0)   # C
        self.mode = kw.get("mode", _MODE["cool"])
        self.fan_speed = kw.get("fan_speed", _FAN["auto"])
        self.running = kw.get("running", True)
        self.online = kw.get("online", True)
        self.turbo = kw.get("turbo", False)
        self.turbo_fan = kw.get("turbo_fan", False)


class FakeDev:
    def __init__(self, on_apply=None):
        self.state = FakeState()
        self.last_kwargs = None
        self._on_apply = on_apply

    def set_state(self, **kwargs):
        self.last_kwargs = kwargs
        if self._on_apply:
            self._on_apply(self.state, kwargs)
        else:
            for k in ("running", "mode", "fan_speed", "turbo", "turbo_fan"):
                if k in kwargs:
                    setattr(self.state, k, kwargs[k])
            if "target_temperature" in kwargs:
                self.state.target_temperature = kwargs["target_temperature"]


def _client(dev, apply_turbo_fan=False):
    cfg = Config()
    cfg.midea_apply_turbo_fan = apply_turbo_fan
    c = MideaCloudClient(cfg)
    c._ensure = lambda: None          # skip real cloud login
    c._dev = dev
    c._cloud = object()               # sentinel; passed through as kwargs["cloud"]
    return c


def test_turbo_request_keeps_requested_max_fan():
    """turbo_fan coexists with a manual fan (verified live: boost on + fan=100),
    so a boost decision must NOT downgrade the fan to auto."""
    dev = FakeDev()
    c = _client(dev)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=True)
    assert dev.last_kwargs["fan_speed"] == _FAN["max"]      # 100, kept


def test_no_turbo_sends_requested_fan():
    dev = FakeDev()
    c = _client(dev)
    c.apply(68.0, mode="cool", power=True, fan_speed="high", turbo=False)
    assert dev.last_kwargs["turbo"] is False
    assert dev.last_kwargs["fan_speed"] == _FAN["high"]      # 80


def test_boost_is_read_from_turbo_fan():
    """The app's boost = turbo_fan. Even when the library's `turbo` bit is False,
    a device reporting turbo_fan=True must surface as boost ON."""
    dev = FakeDev()
    dev.state.turbo = False
    dev.state.turbo_fan = True                 # app boost ON
    c = _client(dev)
    st = c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=False)
    assert st is not None
    assert st.turbo is True, "boost surfaced from turbo_fan"


def test_default_does_not_command_turbo_fan():
    """Default (opt-in off): apply must NOT send turbo_fan, so a boost the user
    set by hand in the app is never cleared by the controller."""
    dev = FakeDev()
    dev.state.turbo_fan = True                  # user turned boost on in the app
    c = _client(dev, apply_turbo_fan=False)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=False)
    assert "turbo_fan" not in dev.last_kwargs
    assert dev.state.turbo_fan is True          # manual boost preserved


def test_opt_in_commands_turbo_fan_from_intent():
    """With autonomous boost enabled, apply drives turbo_fan from the decision."""
    dev = FakeDev()
    c = _client(dev, apply_turbo_fan=True)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=True)
    assert dev.last_kwargs["turbo_fan"] is True
    st = c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=False)
    assert dev.last_kwargs["turbo_fan"] is False   # can also turn it off


def test_cool_mode_sets_target_fan_mode_does_not():
    dev = FakeDev()
    c = _client(dev)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=False)
    assert "target_temperature" in dev.last_kwargs
    dev2 = FakeDev()
    c2 = _client(dev2)
    c2.apply(60.0, mode="fan", power=True, fan_speed="max", turbo=False)
    assert "target_temperature" not in dev2.last_kwargs   # target snaps Duo back to COOL


def test_apply_readback_failure_returns_none():
    class BoomDev:
        def __init__(self):
            self.last_kwargs = None
        def set_state(self, **kwargs):
            self.last_kwargs = kwargs
        @property
        def state(self):
            raise RuntimeError("cloud read-back timeout")
    dev = BoomDev()
    c = _client(dev)
    assert c.apply(60.0, mode="cool", power=True, turbo=False) is None
    assert dev.last_kwargs is not None
