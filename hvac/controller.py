"""The control algorithm.

Problem shape: the Midea AC lives in the *office*, but we want to hold *Ethan's
room* (a different, coupled room) at `target`. The Midea only knows its own
setpoint, which governs the office. So the office must run *colder* than the
target by an offset that grows with outdoor heat load. We can't measure that
offset a priori, so we (a) feed-forward a learned estimate and (b) close the
loop with PI feedback on the actual Ethan-room error.

    setpoint = target
               - feedforward_offset(outdoor)      # learned, open-loop
               - (Kp*error + Ki*integral)         # feedback, corrects model error

where  error = t_ethan - target   (positive = Ethan's room too warm).

Design choices that matter:
  * Feedforward does the heavy lifting so feedback gains stay gentle (stable on a
    slow thermal plant with long lag between office and Ethan's room).
  * Deadband freezes the integrator near target so sensor noise doesn't wander
    the setpoint.
  * Anti-windup: integral only accumulates when the output isn't saturated.
  * Min-command-gap + setpoint quantisation protect the compressor and avoid
    chattering commands.
  * `feedforward_offset` and the gains are the *learned* params (storage.py),
    so the same code gets better as `learn.py` refits them from real data.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ControllerParams:
    # Defaults below were fit by tools/tune.py against the simulator scenario
    # suite, CALIBRATED to real LAN data + gen-3 schedule (68-70 by 9pm).

    # feedforward: how far below target the office must sit
    base_offset_f: float = 4.23     # offset at the reference outdoor temp
    outdoor_ref_f: float = 77.17    # reference outdoor temp for base_offset
    outdoor_gain: float = 0.888     # extra office-offset per F of outdoor above ref
    outdoor_gain_cold: float = 0.2 # offset relief per F of outdoor below ref

    # feedback (PI on Ethan-room error)
    kp: float = 4.47
    ki: float = 0.00004              # per second (small: slow plant)
    integral_clamp_f: float = 2.01  # anti-windup bound on Ki*integral contribution

    # predictive feedforward from the weather forecast: the office->Ethan plant
    # lags ~30 min, so reacting to *current* outdoor temp always trails a heat
    # ramp. Blend in the forecast `forecast_lead_min` ahead to pre-cool.
    forecast_lead_min: float = 78.4
    forecast_weight: float = 1.0   # 0 = ignore forecast, 1 = use forecast only

    # behaviour
    deadband_f: float = 0.71
    band_f: float = 1.0

    # fan + boost (turbo) policy — drives the Midea's fan speed and turbo lever.
    # Quiet at night (Ethan asleep); ramp fan with cooling demand by day; engage
    # turbo only for a big daytime deficit (fast recovery, but loud + thirsty).
    boost_error_f: float = 2.5      # error above which to use turbo + MAX fan (day)
    high_fan_error_f: float = 1.5   # error above which to use HIGH fan (day)
    # efficiency: data shows the office is usually already cold while Ethan's room
    # stays hot (airflow-limited, not power-limited). Turbo only helps when the
    # OFFICE itself can't reach its setpoint; once the office is within this many
    # F of setpoint, turbo just burns power -> use MAX fan (airflow) instead.
    office_cold_margin_f: float = 2.0
    # economizer / free cooling: when it's colder OUTSIDE than the target, the
    # room's heat load is low and cold air is already available — circulate it
    # with the FAN (no compressor, ~50W vs ~1100W) instead of refrigerating.
    # Only run the compressor if no cold air source is on hand (office warm).
    econ_enabled: bool = True
    # free-cool HYSTERESIS (stops cool<->fan thrashing that never settled on the unit):
    econ_office_margin_f: float = 3.0   # ENTER free-cool when office <= target - this
    econ_exit_office_margin_f: float = 0.5  # EXIT when office warms to within this of target
    econ_max_error_f: float = 2.0       # enter free-cool only when room within this of target
    econ_exit_error_f: float = 3.0      # exit free-cool (run compressor) if room climbs past this
    # OUTDOOR GATE (data-driven): with the compressor off, the office's cold is a
    # depleting bank — it only stays cold (and the room only holds) when it's cold
    # enough OUTSIDE. Real logs: fan-mode room drift was +0.02 F/hr at outdoor<65
    # but +2.0 to +2.4 F/hr at outdoor 65-75. So free-cool ONLY when outdoor is
    # below this; above it the compressor is the only thing that pulls the room down.
    econ_max_outdoor_f: float = 65.0    # enter free-cool only when outdoor <= this
    econ_exit_outdoor_f: float = 67.0   # exit free-cool when outdoor warms past this (hysteresis)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ControllerParams":
        if not d:
            return cls()
        known = {k: d[k] for k in cls().__dict__ if k in d}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    setpoint_f: float          # what to command the Midea to
    mode: str                  # 'cool' | 'fan' | 'off'
    ac_should_run: bool
    error: float
    integral: float
    feedforward: float
    reason: str
    fan_speed: str = "auto"    # silent|low|medium|high|max|auto
    turbo: bool = False        # boost
    free_cool: bool = False    # compressor idle, fan circulating (cool mode, high sp)


class Controller:
    """Stateful PI + feedforward controller. State = the integral term."""

    def __init__(self, params: ControllerParams | None = None,
                 setpoint_min: float = 60.0, setpoint_max: float = 80.0,
                 step: float = 1.0):
        self.p = params or ControllerParams()
        self.smin = setpoint_min
        self.smax = setpoint_max
        self.step = step
        self.integral = 0.0          # accumulated error*dt (seconds)
        self.last_setpoint: Optional[float] = None
        self._free_cooling = False   # hysteretic free-cool state

    def reset(self) -> None:
        self.integral = 0.0
        self.last_setpoint = None
        self._free_cooling = False

    # ---- feedforward ----
    def feedforward_offset(self, outdoor_f: Optional[float]) -> float:
        p = self.p
        if outdoor_f is None:
            return p.base_offset_f
        if outdoor_f >= p.outdoor_ref_f:
            return p.base_offset_f + p.outdoor_gain * (outdoor_f - p.outdoor_ref_f)
        # cooler than reference: need a bit less offset, but never negative
        relief = p.outdoor_gain_cold * (p.outdoor_ref_f - outdoor_f)
        return max(0.0, p.base_offset_f - relief)

    def _quantize(self, s: float) -> float:
        s = max(self.smin, min(self.smax, s))
        return round(s / self.step) * self.step

    def _outdoor_effective(self, outdoor_f: Optional[float],
                           forecast_f: Optional[float]) -> Optional[float]:
        if outdoor_f is None:
            return forecast_f
        if forecast_f is None:
            return outdoor_f
        w = self.p.forecast_weight
        # bias toward the hotter of now/forecast so we pre-cool but never
        # pre-warm below the current load. The max() must apply on BOTH branches:
        # when the forecast is cooler than now (afternoon falling limb) blended
        # dips below current outdoor, which would relax the feedforward and
        # undercool while it's still hot out -> clamp to current outdoor.
        blended = (1 - w) * outdoor_f + w * forecast_f
        return max(outdoor_f, blended)

    def _fan_plan(self, error: float, ac_should_run: bool, sleep_mode: bool,
                  office_temp: Optional[float] = None,
                  setpoint: Optional[float] = None) -> tuple[str, bool]:
        """Pick fan speed + turbo. Escalate with the deficit at ANY time (turbo is
        OK day or night per the operator). EFFICIENCY: turbo only when the office
        is still warmer than its setpoint (compressor-limited); if the office is
        already cold, turbo wastes power (room is airflow-limited) -> MAX fan, no
        turbo, same room effect. Whisper only when in band at night."""
        p = self.p
        if not ac_should_run:
            return "auto", False
        if error > p.boost_error_f:
            office_cold = (office_temp is not None and setpoint is not None
                           and office_temp <= setpoint + p.office_cold_margin_f)
            return ("max", False) if office_cold else ("max", True)
        if error > p.high_fan_error_f:
            return "high", False
        if error > 0.3:
            return "medium", False
        # near/in band: quiet at night (asleep, comfortable), gentle auto by day
        return ("silent" if sleep_mode else "auto"), False

    def decide(self, t_ethan: float, target: float, outdoor_f: Optional[float],
               dt_s: float, outdoor_forecast_f: Optional[float] = None,
               sleep_mode: bool = False, office_temp: Optional[float] = None) -> Decision:
        p = self.p
        error = t_ethan - target  # >0 too hot
        outdoor_eff = self._outdoor_effective(outdoor_f, outdoor_forecast_f)

        ff = self.feedforward_offset(outdoor_eff)
        in_deadband = abs(error) <= p.deadband_f

        # --- conditional integration (anti-windup) ---
        # First check whether the *current* output is railed; if so, don't
        # integrate further in the direction that worsens the saturation.
        prov_fb = p.kp * error + p.ki * self.integral
        prov_setpoint = self._quantize(target - ff - prov_fb)
        sat_low = prov_setpoint <= self.smin    # can't cool any harder
        sat_high = prov_setpoint >= self.smax   # can't warm any further
        pushing_into_rail = (sat_low and error > 0) or (sat_high and error < 0)

        if not in_deadband and not pushing_into_rail:
            self.integral += error * dt_s
        # hard bound so Ki*integral can never exceed integral_clamp_f
        if p.ki > 0:
            imax = p.integral_clamp_f / p.ki
            self.integral = max(-imax, min(imax, self.integral))

        fb = p.kp * error + p.ki * self.integral
        raw = target - ff - fb            # lower setpoint => more cooling
        setpoint = self._quantize(raw)

        # --- mode selection: airflow-first, refrigerate-last (FAN_ONLY free-cool
        #     with HYSTERESIS so it doesn't thrash cool<->fan every cycle) ---
        cool_needed = error > -p.band_f
        # update the hysteretic free-cool state
        if self._free_cooling:
            # stay free-cooling until the office warms up, the room climbs away, or
            # it gets too warm outside to passively keep the office cold
            if (office_temp is None
                    or office_temp >= target - p.econ_exit_office_margin_f
                    or error >= p.econ_exit_error_f
                    or (outdoor_eff is not None
                        and outdoor_eff > p.econ_exit_outdoor_f)):
                self._free_cooling = False
        else:
            # enter free-cool only when the office is clearly cold, the room is near
            # target, AND it's cold enough outside to sustain it with the compressor
            # off (otherwise the office warms and the room drifts hot — see logs)
            if (p.econ_enabled and office_temp is not None
                    and office_temp <= target - p.econ_office_margin_f
                    and error <= p.econ_max_error_f
                    and outdoor_eff is not None
                    and outdoor_eff <= p.econ_max_outdoor_f):
                self._free_cooling = True

        if not cool_needed and (outdoor_f is None or outdoor_f <= target):
            mode = "off"
            ac_should_run = False
            self._free_cooling = False
            reason = "below band, cool outdoor: idle"
        elif self._free_cooling:
            # office already cold -> FAN_ONLY circulates it, compressor OFF
            mode = "fan"
            ac_should_run = True
            reason = "free-cool: fan circulates cold air, compressor off"
        else:
            mode = "cool"
            ac_should_run = True
            reason = ("cooling: recover (room far above target)"
                      if error > p.econ_max_error_f else "cooling: chill office")

        free_cool = (mode == "fan")
        fan_speed, turbo = self._fan_plan(error, ac_should_run, sleep_mode,
                                          office_temp=office_temp, setpoint=setpoint)
        if free_cool:
            fan_speed, turbo = "max", False    # circulate hard, no compressor/turbo
        self.last_setpoint = setpoint
        return Decision(
            setpoint_f=setpoint, mode=mode, ac_should_run=ac_should_run,
            error=error, integral=self.integral, feedforward=ff, reason=reason,
            fan_speed=fan_speed, turbo=turbo, free_cool=free_cool,
        )
