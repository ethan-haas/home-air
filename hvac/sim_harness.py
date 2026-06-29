"""Glue that lets the live Service run against the thermal simulator.

Provides drop-in replacements for the ecobee reader and the weather function
that read from / advance a shared simulated world. Used by `run_service --sim`
and by the closed-loop integration test, so the *exact* production control path
(Service.cycle) is exercised end-to-end without hardware.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .simulator import Plant, SimState, step, diurnal
from .controller import ControllerParams


@dataclass
class _W:
    temp_f: float
    humidity: Optional[float]
    forecast_temp_f: Optional[float]


class SimWorld:
    """Shared simulated environment: a clock, an outdoor profile, and the plant."""

    def __init__(self, plant: Plant, midea, start_hour: float = 12.0,
                 start_ethan: float = 76.0, lo: float = 74.0, hi: float = 95.0,
                 interval_min: float = 2.0,
                 lead_min: float = ControllerParams().forecast_lead_min):
        self.plant = plant
        self.midea = midea
        self.minute = start_hour * 60.0
        self.state = SimState(start_ethan, start_ethan)
        self.lo, self.hi = lo, hi
        self.interval_min = interval_min
        self.lead_min = lead_min

    def outdoor_now(self) -> float:
        return diurnal(self.minute / 60.0, self.lo, self.hi)

    def outdoor_ahead(self) -> float:
        return diurnal((self.minute + self.lead_min) / 60.0, self.lo, self.hi)

    def advance(self) -> None:
        """Step the plant one control interval using the Midea's current command."""
        sp = self.midea.state.target_f if self.midea.state.target_f else 70.0
        running = self.midea.state.power
        self.state = step(self.state, self.outdoor_now(), sp, running,
                          self.plant, dt_min=self.interval_min)
        # feed the stepped office temp back into the mock unit so the service's
        # economizer/free-cool + turbo-suppression (gated on office_temp) actually
        # run in --sim and integration tests — else _last_office is stuck at 74.
        self.midea.state.indoor_temp_f = self.state.t_office
        self.minute += self.interval_min


class SimEcobee:
    def __init__(self, world: SimWorld):
        self.world = world

    def read_ethan_temp(self) -> float:
        return self.world.state.t_ethan


class SimWeather:
    def __init__(self, world: SimWorld):
        self.world = world

    def __call__(self) -> _W:
        return _W(temp_f=self.world.outdoor_now(), humidity=45.0,
                  forecast_temp_f=self.world.outdoor_ahead())
