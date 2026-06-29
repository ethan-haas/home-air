"""Time-of-day comfort schedule + cost-aware target selection.

Requirements:
  * 70°F in Ethan's room from 10pm to 9am (firm — sleep).
  * 72–74°F band during the day (9am–10pm).
  * Be optimal w.r.t. weather; minimise electricity cost while maximising
    coolness.

How "optimal / minimise cost while maximising coolness" is realised:
  * Cooling is cheapest/most efficient when it's cooler OUTSIDE (smaller ΔT, AC
    pulls more heat per watt). So during the day band we ride the COOL edge (72,
    more coolness) when outdoors is mild, and drift toward the WARM edge (74,
    less runtime) when outdoors is hot — buying the cheap coolness, skipping the
    expensive coolness.
  * Pre-cool: if the forecast shows a heat ramp coming while it's still cooler
    now, aim at the cool edge now to bank cold mass cheaply and shave the
    expensive afternoon peak (the controller's forecast feedforward does the
    rest).
  * Pre-night ramp: start easing the target down from 72→70 over 9–10pm so the
    room actually REACHES 70 by 10pm despite thermal lag, instead of starting to
    cool at 10pm and arriving late.
  * Optional time-of-use (TOU) peak window: if a utility peak rate applies, ride
    the warm edge during peak and pre-cool to the cool edge just before it.

Returns, per moment, a single `target` (for the controller) plus the comfort
`band` [low, high] (for scoring and mode logic) and an electricity `price`
weight (for the cost metric).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SchedulePoint:
    target: float       # effective setpoint goal for the controller
    low: float          # comfort band low edge
    high: float         # comfort band high edge
    price: float        # relative electricity price weight at this hour
    night: bool = False # sleep window -> keep the AC quiet (no turbo, low fan)


@dataclass
class ScheduleConfig:
    # GOAL: room at 70F BY 9pm and held overnight; 72-74F by day (weather-aware).
    night_target: float = 70.0      # firm night target — hold 70
    night_low: float = 69.0         # comfort band edges for scoring (+/-1)
    night_high: float = 71.0
    night_start_h: float = 21.0     # 9pm — room should be 70 by now
    night_end_h: float = 9.0        # 9am
    day_low: float = 72.0
    day_high: float = 74.0
    # evening pre-cool: start this many hours before 9pm and drive HARD toward 70
    # so the room actually reaches 70 by 9pm despite slow cooling.
    precool_ramp_h: float = 4.0     # 5pm -> 9pm aggressive glide
    # outdoor band for cost mapping: at/below efficient_f aim cool, at/above warm
    efficient_f: float = 78.0
    expensive_f: float = 95.0
    precool_gap_f: float = 6.0      # forecast-now gap that triggers day pre-cool
    # optional time-of-use peak (set tou=True to enable)
    tou: bool = False
    peak_start_h: float = 15.0
    peak_end_h: float = 19.0
    peak_price: float = 1.6
    offpeak_price: float = 1.0


def _is_night(h: float, c: ScheduleConfig) -> bool:
    # night wraps midnight: [night_start, 24) U [0, night_end)
    if c.night_start_h <= c.night_end_h:
        return c.night_start_h <= h < c.night_end_h
    return h >= c.night_start_h or h < c.night_end_h


def price_at(h: float, c: ScheduleConfig) -> float:
    if not c.tou:
        return 1.0
    h %= 24
    return c.peak_price if c.peak_start_h <= h < c.peak_end_h else c.offpeak_price


def schedule_point(hour: float, outdoor: float | None = None,
                   forecast: float | None = None,
                   c: ScheduleConfig | None = None) -> SchedulePoint:
    c = c or ScheduleConfig()
    h = hour % 24.0
    price = price_at(h, c)
    o = outdoor if outdoor is not None else c.efficient_f
    # cost/efficiency factor: 1 when it's cool & cheap to cool outside, 0 when hot
    cool = (c.expensive_f - o) / max(1e-6, c.expensive_f - c.efficient_f)
    cool = max(0.0, min(1.0, cool))

    # --- NIGHT (9pm-9am, asleep): hold a firm 70. Quiet once in band. ---
    if _is_night(h, c):
        return SchedulePoint(c.night_target, c.night_low, c.night_high,
                             price, night=True)

    # --- EVENING PRE-COOL (5pm-9pm, awake): drive HARD toward 70 so the room is
    #     70 BY 9pm despite slow cooling. Aggressive (night=False -> turbo ok). ---
    ramp_start = c.night_start_h - c.precool_ramp_h
    if ramp_start <= h < c.night_start_h:
        frac = (h - ramp_start) / c.precool_ramp_h            # 0..1
        t = c.day_low + (c.night_target - c.day_low) * frac   # 72 -> 70 glide
        # lenient band during the transition (don't penalize the glide)
        return SchedulePoint(t, c.night_low, c.day_high, price, night=False)

    # --- DAY (9am-5pm): cost-aware band 72-74. Hot out -> 74 (save energy);
    #     mild out -> 72 (cheap coolness). ---
    low, high = c.day_low, c.day_high
    warm = 1.0 - cool
    t = low + (high - low) * warm

    # TOU: during peak ride warm to avoid expensive cooling
    if c.tou and c.peak_start_h <= h < c.peak_end_h:
        t = high
    # pre-cool: a hot ramp is coming while it's cooler now -> bank cheap cold
    elif forecast is not None and (forecast - o) > c.precool_gap_f:
        t = low
    # TOU pre-peak: pre-cool to cool edge in the hour before peak
    elif c.tou and (c.peak_start_h - 1.0) <= h < c.peak_start_h:
        t = low

    return SchedulePoint(t, low, high, price)
