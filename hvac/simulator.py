"""Two-room RC thermal model — the local test rig.

Models the real situation: a Midea AC in the OFFICE, and ETHAN'S ROOM coupled to
the office (through a doorway) and to the outdoors. The AC's own thermostat
modulates cooling toward the *office* setpoint; our controller only gets to pick
that setpoint, using Ethan's-room temperature as feedback. This is what makes the
problem non-trivial and is exactly what the controller must master.

Energy balance per minute (degrees F, time in minutes):

    dT_office = k_oo*(T_out - T_office) + k_c*(T_ethan - T_office) - AC_cool
    dT_ethan  = k_eo*(T_out - T_ethan) + k_c*(T_office - T_ethan) + internal

AC_cool (the unit's inverter) ramps with how far the office is above setpoint:
    m       = clip((T_office - setpoint)/throttle_band, 0, 1)
    AC_cool = m * cap_cool      (0 when the AC is commanded off)

The model is deterministic; an optional `noise` adds sensor jitter so tests can
check robustness. Defaults are tuned so the office->Ethan steady-state offset is
several degrees and grows with outdoor heat — the regime the controller learns.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Plant:
    # Defaults CALIBRATED to 26h of real LAN data (gen-2 fit, 2026-06-20):
    #   - Ethan's room correlates with OUTDOOR at 0.92 but with the office at only
    #     0.16 -> the office AC has weak cross-room authority; outdoor dominates.
    #   - Midday with office driven to its floor (~62F), Ethan's room still sits
    #     ~74F at outdoor ~81F -> matches k_eo>>k_c below.
    #   - Office reaches its own setpoint easily (not the bottleneck) -> cap_cool
    #     stays strong; the limit is k_c (office->Ethan coupling).
    k_oo: float = 0.010     # office <-> outdoor coupling (1/min)
    k_eo: float = 0.020     # Ethan-room <-> outdoor coupling (1/min) — DOMINANT
    k_c: float = 0.012      # office <-> Ethan-room coupling — WEAK (real measure)
    internal: float = 0.020 # internal heat gain in Ethan's room (F/min)
    cap_cool: float = 0.50  # max AC cooling rate, 12k BTU oversized office (F/min)
    throttle_band: float = 3.0  # office-above-setpoint span for full modulation


@dataclass
class SimState:
    t_office: float = 76.0
    t_ethan: float = 76.0


def ac_cooling(t_office: float, setpoint: float, running: bool, plant: Plant) -> float:
    if not running:
        return 0.0
    m = (t_office - setpoint) / plant.throttle_band
    m = max(0.0, min(1.0, m))
    return m * plant.cap_cool


def step(state: SimState, outdoor: float, setpoint: float, running: bool,
         plant: Plant, dt_min: float = 1.0) -> SimState:
    """Advance the plant one tick. Uses small sub-steps for numerical stability."""
    n = max(1, int(math.ceil(dt_min / 1.0)))
    h = dt_min / n
    to, te = state.t_office, state.t_ethan
    for _ in range(n):
        cool = ac_cooling(to, setpoint, running, plant)
        dto = plant.k_oo * (outdoor - to) + plant.k_c * (te - to) - cool
        dte = plant.k_eo * (outdoor - te) + plant.k_c * (to - te) + plant.internal
        to += dto * h
        te += dte * h
    return SimState(t_office=to, t_ethan=te)


# ---- outdoor scenarios -------------------------------------------------------

def diurnal(hours: float, lo: float, hi: float, peak_hour: float = 16.0) -> float:
    """Sinusoidal outdoor temp over a day: min near sunrise, max mid-afternoon."""
    mid = (hi + lo) / 2.0
    amp = (hi - lo) / 2.0
    # phase so that maximum lands at peak_hour
    return mid + amp * math.cos((2 * math.pi / 24.0) * (hours - peak_hour))


@dataclass
class Scenario:
    name: str
    minutes: int                       # total sim length
    outdoor: Callable[[float], float]  # f(hours) -> outdoor F
    start: SimState = field(default_factory=lambda: SimState(76.0, 76.0))
    occupied: Optional[Callable[[float], bool]] = None  # f(hours)->occupied

    def outdoor_at(self, minute: float) -> float:
        return self.outdoor(minute / 60.0)

    def is_occupied(self, minute: float) -> bool:
        if self.occupied is None:
            return True
        return self.occupied(minute / 60.0)
