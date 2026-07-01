"""Direct unit tests for MideaCloudClient.apply — the turbo/fan co-command fix
and the device-confirmed read-back. These exercise the real apply() body (the
e2e cloud_cycle tests mock the whole client away, so this is the only place the
fan=auto-under-turbo behavior and the state read-back are actually asserted).
"""
import pytest

from hvac.config import Config
from hvac.midea_cloud import MideaCloudClient, _MODE, _FAN


class FakeState:
    """Stand-in for midea_beautiful's AirConditionerState. `set_state` on the
    device writes these attrs; apply() reads them back as device-confirmed."""
    def __init__(self, **kw):
        self.indoor_temperature = kw.get("indoor_temperature", 23.0)   # C
        self.target_temperature = kw.get("target_temperature", 16.0)   # C
        self.mode = kw.get("mode", _MODE["cool"])
        self.fan_speed = kw.get("fan_speed", _FAN["auto"])
        self.running = kw.get("running", True)
        self.online = kw.get("online", True)
        self.turbo = kw.get("turbo", False)


class FakeDev:
    """Records the last set_state kwargs and lets a test script how the device
    responds (e.g. accept vs silently refuse turbo)."""
    def __init__(self, on_apply=None):
        self.state = FakeState()
        self.last_kwargs = None
        self._on_apply = on_apply

    def set_state(self, **kwargs):
        self.last_kwargs = kwargs
        # mirror the library: applying updates state to what the DEVICE reports.
        # by default reflect the command; a test can override via on_apply to
        # simulate a unit that drops turbo.
        if self._on_apply:
            self._on_apply(self.state, kwargs)
        else:
            for k in ("running", "mode", "fan_speed", "turbo"):
                if k in kwargs:
                    setattr(self.state, k, kwargs[k])
            if "target_temperature" in kwargs:
                self.state.target_temperature = kwargs["target_temperature"]


def _client(dev):
    c = MideaCloudClient(Config())
    c._ensure = lambda: None          # skip real cloud login
    c._dev = dev
    c._cloud = object()               # sentinel; passed through as kwargs["cloud"]
    return c


def test_turbo_sends_fan_auto_not_max():
    """The core fix: when turbo is requested, fan must be commanded AUTO (102),
    not max — the unit drops turbo when a manual fan speed is co-commanded."""
    dev = FakeDev()
    c = _client(dev)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=True)
    assert dev.last_kwargs["turbo"] is True
    assert dev.last_kwargs["fan_speed"] == _FAN["auto"]      # 102, NOT max(100)


def test_no_turbo_sends_requested_fan():
    dev = FakeDev()
    c = _client(dev)
    c.apply(68.0, mode="cool", power=True, fan_speed="high", turbo=False)
    assert dev.last_kwargs["turbo"] is False
    assert dev.last_kwargs["fan_speed"] == _FAN["high"]      # 80


def test_apply_returns_device_confirmed_state():
    """apply() returns what the DEVICE reports, not the intent."""
    dev = FakeDev()
    c = _client(dev)
    st = c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=True)
    assert st is not None
    assert st.turbo is True
    assert st.mode == "cool"
    assert st.power is True


def test_apply_reports_turbo_refusal():
    """Device silently refuses turbo -> read-back turbo stays False even though
    we requested it. This is exactly the 'dashboard showed turbo but it never
    turned on' scenario, now surfaced as device truth."""
    def refuse_turbo(state, kwargs):
        state.running = kwargs.get("running", state.running)
        state.mode = kwargs.get("mode", state.mode)
        state.fan_speed = kwargs.get("fan_speed", state.fan_speed)
        state.turbo = False                    # <- unit ignores the turbo bit
    dev = FakeDev(on_apply=refuse_turbo)
    c = _client(dev)
    st = c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=True)
    assert dev.last_kwargs["turbo"] is True     # we DID request it
    assert st.turbo is False                    # but the device reports it OFF


def test_cool_mode_sets_target_fan_mode_does_not():
    dev = FakeDev()
    c = _client(dev)
    c.apply(60.0, mode="cool", power=True, fan_speed="max", turbo=False)
    assert "target_temperature" in dev.last_kwargs
    dev2 = FakeDev()
    c2 = _client(dev2)
    c2.apply(60.0, mode="fan", power=True, fan_speed="max", turbo=False)
    assert "target_temperature" not in dev2.last_kwargs   # target would snap Duo back to COOL


def test_apply_readback_failure_returns_none():
    """If reading state back throws, apply returns None (caller treats as
    applied-but-unconfirmed) rather than masking the successful command."""
    class BoomDev:
        def __init__(self):
            self.last_kwargs = None
        def set_state(self, **kwargs):     # succeeds — command lands
            self.last_kwargs = kwargs
        @property
        def state(self):                   # but the read-back throws
            raise RuntimeError("cloud read-back timeout")
    dev = BoomDev()
    c = _client(dev)
    assert c.apply(60.0, mode="cool", power=True, turbo=False) is None
    assert dev.last_kwargs is not None     # the command still went out
