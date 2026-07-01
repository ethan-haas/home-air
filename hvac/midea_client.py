"""Midea client — drives the office Midea Duo portable AC over the LAN.

Uses msmart-ng for local (cloud-free) control once a token/key have been
obtained one time (see scripts/discover_midea.py). Midea works in Celsius
internally, so setpoints are converted from F and rounded to the unit's 0.5C
grid and clamped to its reported min/max.

`MockMidea` mirrors the interface with no hardware, for tests, dry-runs, and the
simulator-backed service. Both expose the same small synchronous surface:

    connect()                 -> establish/refresh the session
    refresh() -> MideaState   -> read indoor temp + current settings
    apply(setpoint_f, mode, power)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .config import Config


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def round_half(x: float) -> float:
    return round(x * 2.0) / 2.0


@dataclass
class MideaState:
    indoor_temp_f: Optional[float]
    target_f: Optional[float]
    power: bool
    mode: str
    online: bool = True
    fan_speed: Optional[str] = None
    turbo: bool = False


class MideaClient:
    """Real device control via msmart-ng (async wrapped in a sync facade)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._dev = None
        self._loop = None       # persistent loop: keeps the device transport alive

    def _run(self, coro):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _build(self):
        from msmart.device import AirConditioner as AC
        if not (self.cfg.midea_host and self.cfg.midea_id
                and self.cfg.midea_token and self.cfg.midea_key):
            raise RuntimeError(
                "Midea not configured: need MIDEA_HOST, MIDEA_ID, MIDEA_TOKEN, "
                "MIDEA_KEY (run scripts/discover_midea.py once)")
        return AC(self.cfg.midea_host, int(self.cfg.midea_id), 6444)

    async def _aconnect(self, attempts: int = 8, backoff_s: float = 2.0):
        # try the primary token/key, then the alternate (endian) candidate.
        # The V3 LAN handshake is flaky (the device serves one connection and may
        # reset mid-handshake), so retry each candidate with backoff. Wrong-key
        # errors short-circuit to the next candidate; transport resets retry.
        candidates = [(self.cfg.midea_token, self.cfg.midea_key)]
        if self.cfg.midea_token_alt and self.cfg.midea_key_alt:
            candidates.append((self.cfg.midea_token_alt, self.cfg.midea_key_alt))
        last = None
        for attempt in range(attempts):
            for tok, key in candidates:
                dev = self._build()
                try:
                    await dev.authenticate(tok, key)
                    self._dev = dev
                    return dev
                except Exception as e:
                    last = e
                    name = type(e).__name__
                    if "Authentication" in name:
                        continue          # wrong key: try the other candidate now
            if attempt < attempts - 1:
                await asyncio.sleep(backoff_s)
        raise RuntimeError(f"Midea authenticate failed after {attempts} attempts: {last}")

    async def _arefresh(self) -> MideaState:
        if self._dev is None:
            await self._aconnect()
        await self._dev.refresh()
        return self._state()

    @staticmethod
    def _fan_enum(name: str):
        from msmart.device import AirConditioner as AC
        return {
            "silent": AC.FanSpeed.SILENT, "low": AC.FanSpeed.LOW,
            "medium": AC.FanSpeed.MEDIUM, "high": AC.FanSpeed.HIGH,
            "max": AC.FanSpeed.MAX, "auto": AC.FanSpeed.AUTO,
        }.get(name, AC.FanSpeed.AUTO)

    async def _aapply(self, setpoint_f: float, mode: str, power: bool,
                      fan_speed: str = "auto", turbo: bool = False):
        from msmart.device import AirConditioner as AC
        if self._dev is None:
            await self._aconnect()
        dev = self._dev
        dev.power_state = power
        try:
            dev.fahrenheit = True        # unit displays F (16C shows as 60F floor)
        except Exception:
            pass
        # kill the unit's energy-save / sleep curves that override our setpoint;
        # silence the beep since we re-assert every cycle
        for attr in ("eco", "sleep", "beep"):
            try:
                setattr(dev, attr, False)
            except Exception:
                pass
        # louver: only on motorized units (this Duo's vane is manual -> default off)
        try:
            if getattr(self.cfg, "midea_set_swing", False):
              dev.swing_mode = AC.SwingMode.OFF
              pos = max(1, min(5, int(getattr(self.cfg, "midea_swing_pos", 1))))
              dev.vertical_swing_angle = {
                1: AC.SwingAngle.POS_1, 2: AC.SwingAngle.POS_2,
                3: AC.SwingAngle.POS_3, 4: AC.SwingAngle.POS_4,
                5: AC.SwingAngle.POS_5,
            }[pos]
        except Exception:
            pass
        if mode == "cool":
            dev.operational_mode = AC.OperationalMode.COOL
        elif mode == "fan":
            dev.operational_mode = AC.OperationalMode.FAN_ONLY
        # turbo (boost) overrides fan; when off, set the requested fan speed.
        # If turbo is requested but the unit doesn't support it, fall back to
        # MAX fan so the fan is never left unset (silent) during the biggest
        # deficit.
        supports = getattr(dev, "supports_turbo", False)
        if supports:
            dev.turbo = bool(turbo)
        if not (turbo and supports):
            dev.fan_speed = self._fan_enum("max" if turbo else fan_speed)
        # set the target ONLY in cool mode — setting a target while commanding
        # FAN_ONLY makes this unit snap back to COOL (target implies temp control),
        # which is why fan mode never stuck from the running service.
        if mode != "fan":
            c = f_to_c(setpoint_f)
            lo = getattr(dev, "min_target_temperature", 16) or 16
            hi = getattr(dev, "max_target_temperature", 30) or 30
            dev.target_temperature = max(lo, min(hi, round_half(c)))
        await dev.apply()

    def _state(self) -> MideaState:
        from msmart.device import AirConditioner as AC
        dev = self._dev
        tt = getattr(dev, "target_temperature", None)
        it = getattr(dev, "indoor_temperature", None)
        mode = getattr(dev, "operational_mode", None)
        mode_s = {AC.OperationalMode.COOL: "cool",
                  AC.OperationalMode.FAN_ONLY: "fan"}.get(mode, str(mode))
        fan = getattr(dev, "fan_speed", None)
        fan_name = getattr(fan, "name", str(fan)).lower() if fan is not None else None
        return MideaState(
            indoor_temp_f=c_to_f(it) if it is not None else None,
            target_f=c_to_f(tt) if tt is not None else None,
            power=bool(getattr(dev, "power_state", False)),
            mode=mode_s,
            online=bool(getattr(dev, "online", True)),
            fan_speed=fan_name,
            turbo=bool(getattr(dev, "turbo", False)),
        )

    # ---- sync facade (all on the one persistent loop) ----
    def connect(self):
        self._run(self._aconnect())

    def refresh(self) -> MideaState:
        try:
            return self._run(self._arefresh())
        except Exception:
            self._dev = None                 # force re-auth next call
            return self._run(self._arefresh())

    def apply(self, setpoint_f: float, mode: str = "cool", power: bool = True,
              fan_speed: str = "auto", turbo: bool = False):
        # Force a FRESH connection each apply: on a long-lived session this unit
        # silently drops MODE changes (cool->fan never landed), while a fresh
        # authenticated session applies them reliably (proven standalone).
        self._dev = None
        try:
            self._run(self._aapply(setpoint_f, mode, power, fan_speed, turbo))
        except Exception:
            self._dev = None                 # reconnect once on error
            self._run(self._aapply(setpoint_f, mode, power, fan_speed, turbo))


class MockMidea:
    """In-memory stand-in. Optionally backed by a simulator Plant so the live
    service can be exercised end-to-end with no hardware."""

    def __init__(self, cfg: Optional[Config] = None, indoor_f: float = 74.0):
        self.cfg = cfg
        self.state = MideaState(indoor_temp_f=indoor_f, target_f=70.0,
                                power=True, mode="cool", online=True,
                                fan_speed="auto", turbo=False)
        self.applied: list[tuple] = []

    def connect(self):
        return None

    def refresh(self) -> MideaState:
        return self.state

    def apply(self, setpoint_f: float, mode: str = "cool", power: bool = True,
              fan_speed: str = "auto", turbo: bool = False):
        self.state.target_f = setpoint_f
        self.state.mode = mode
        self.state.power = power
        self.state.fan_speed = fan_speed
        self.state.turbo = turbo
        self.applied.append((setpoint_f, mode, power, fan_speed, turbo))
