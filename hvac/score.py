"""Closed-loop simulation + the mechanical metric (schedule-aware).

`run_closed_loop` drives the Controller against the Plant, computing the target
each tick from the time-of-day schedule (night 70, day band 72–74, pre-cool
ramps, weather/cost-aware target). `score_scenario` scores comfort against the
per-hour band and charges for energy (optionally TOU-weighted). `evaluate`
averages over the standard scenario suite — what the autoresearch loop maximises.

Metric (higher = better):

    score = 100·in_band                 # fraction of cooling-relevant time inside the hour's band
            −  8·dev                     # mean distance outside the band
            − 14·too_hot                 # mean degrees above the band high (comfort failure)
            −  4·too_cold                # mean degrees below band low (overcooling = wasted power)
            −  9·energy_cost             # mean compressor duty × price(hour)  (electricity bill)

Energy weight is higher than before because the brief explicitly asks to
minimise electricity cost; the day band gives the controller room to trade a
little warmth for a lot less runtime when cooling is expensive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .controller import Controller, ControllerParams
from .schedule import ScheduleConfig, schedule_point, price_at
from .simulator import Plant, Scenario, SimState, step, ac_cooling, diurnal


CONTROL_TICK_MIN = 5
WARMUP_MIN = 90          # skip the initial transient (room may start off-target)


@dataclass
class TracePoint:
    minute: int
    hour: float
    outdoor: float
    t_office: float
    t_ethan: float
    target: float
    low: float
    high: float
    price: float
    setpoint: float
    running: bool
    duty: float
    occupied: bool


def run_closed_loop(scenario: Scenario, params: ControllerParams,
                    sched: Optional[ScheduleConfig] = None,
                    plant: Optional[Plant] = None,
                    setpoint_min: float = 60.0, setpoint_max: float = 80.0,
                    step_f: float = 1.0, start_hour: float = 0.0) -> list[TracePoint]:
    sched = sched or ScheduleConfig()
    plant = plant or Plant()
    ctrl = Controller(params, setpoint_min, setpoint_max, step_f)
    state = SimState(scenario.start.t_office, scenario.start.t_ethan)
    trace: list[TracePoint] = []
    setpoint = sched.day_low
    running = True

    for minute in range(scenario.minutes):
        hour = start_hour + minute / 60.0
        outdoor = scenario.outdoor_at(minute)
        if minute % CONTROL_TICK_MIN == 0:
            lead = params.forecast_lead_min
            forecast = scenario.outdoor_at(minute + lead) if lead else None
            sp_pt = schedule_point(hour, outdoor, forecast, sched)
            # pass office temp + sleep flag so the metric exercises the SAME
            # control path as the live service (economizer/free-cool + outdoor
            # gate + night fan policy are all gated on these — without them the
            # whole economizer branch is dead under the metric)
            dec = ctrl.decide(state.t_ethan, sp_pt.target, outdoor,
                              dt_s=CONTROL_TICK_MIN * 60,
                              outdoor_forecast_f=forecast,
                              sleep_mode=sp_pt.night,
                              office_temp=state.t_office)
            setpoint = dec.setpoint_f
            running = dec.ac_should_run
        pt = schedule_point(hour, outdoor, None, sched)
        duty = ac_cooling(state.t_office, setpoint, running, plant) / max(plant.cap_cool, 1e-9)
        trace.append(TracePoint(
            minute=minute, hour=hour % 24, outdoor=outdoor,
            t_office=state.t_office, t_ethan=state.t_ethan,
            target=pt.target, low=pt.low, high=pt.high, price=pt.price,
            setpoint=setpoint, running=running, duty=duty,
            occupied=scenario.is_occupied(minute)))
        state = step(state, outdoor, setpoint, running, plant, dt_min=1.0)
    return trace


def score_scenario(trace: list[TracePoint]) -> dict:
    pts = [p for p in trace if p.minute >= WARMUP_MIN and p.occupied]
    if not pts:
        return {"score": -999.0, "in_band": 0, "dev": 0, "too_hot": 0,
                "too_cold": 0, "energy_cost": 0, "n": 0}
    n = len(pts)
    too_hot = sum(max(0.0, p.t_ethan - p.high) for p in pts) / n
    energy_cost = sum(p.duty * p.price for p in pts) / n
    # cold/comfort terms scoped to cooling-relevant time (can't hold a band when
    # it's colder outside than the band — no heat)
    cool_pts = [p for p in pts if p.outdoor >= p.low]
    if cool_pts:
        m = len(cool_pts)
        in_band = sum(1 for p in cool_pts if p.low <= p.t_ethan <= p.high) / m
        too_cold = sum(max(0.0, p.low - p.t_ethan) for p in cool_pts) / m
        dev = sum(max(0.0, p.t_ethan - p.high, p.low - p.t_ethan)
                  for p in cool_pts) / m
    else:
        in_band = too_cold = dev = 0.0
    score = (100 * in_band - 8 * dev - 14 * too_hot
             - 4 * too_cold - 9 * energy_cost)
    return {"score": score, "in_band": in_band, "dev": dev, "too_hot": too_hot,
            "too_cold": too_cold, "energy_cost": energy_cost, "n": n}


def standard_scenarios() -> list[Scenario]:
    day = 24 * 60
    # start near a plausible overnight indoor temp (~71) so warmup is short
    return [
        Scenario("hot_summer_day", day, lambda h: diurnal(h, 74, 95), SimState(71, 71)),
        Scenario("mild_day", day, lambda h: diurnal(h, 62, 80), SimState(71, 71)),
        Scenario("heat_wave", day, lambda h: diurnal(h, 82, 102), SimState(72, 73)),
        Scenario("heat_spike", day,
                 lambda h: diurnal(h, 72, 90) + (8 if 14 <= h <= 17 else 0),
                 SimState(71, 71)),
    ]


def evaluate(params: ControllerParams, sched: Optional[ScheduleConfig] = None,
             plant: Optional[Plant] = None, verbose: bool = False) -> dict:
    results = {}
    total = 0.0
    for sc in standard_scenarios():
        trace = run_closed_loop(sc, params, sched, plant, start_hour=0.0)
        s = score_scenario(trace)
        results[sc.name] = s
        total += s["score"]
        if verbose:
            print(f"  {sc.name:16s} score={s['score']:7.2f} "
                  f"in_band={s['in_band']*100:5.1f}% hot={s['too_hot']:.2f} "
                  f"cold={s['too_cold']:.2f} energy={s['energy_cost']:.2f}")
    mean = total / len(results)
    return {"metric": mean, "scenarios": results}


def main() -> None:
    from .storage import Storage
    from .config import DB_PATH
    learned = None
    try:
        st = Storage(DB_PATH)
        learned = st.latest_params()
        st.close()
    except Exception:
        pass
    params = ControllerParams.from_dict(learned)
    src = "learned" if learned else "default"
    print(f"Scoring controller params ({src}) against the comfort schedule:")
    res = evaluate(params, verbose=True)
    print(f"METRIC={res['metric']:.4f}")


if __name__ == "__main__":
    main()
