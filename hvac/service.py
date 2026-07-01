"""Live control service: read -> decide -> actuate -> log, every interval.

Dependencies are injected so the same loop runs against real hardware, a mock,
or the simulator (used by the integration test). Each cycle:

  1. read Ethan's-room temp from ecobee
  2. read outdoor temp + forecast from Open-Meteo
  3. load the latest learned params and decide a setpoint
  4. command the Midea (respecting the compressor min-command-gap)
  5. log everything to SQLite (the data learn.py later trains on)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config
from .controller import Controller, ControllerParams
from .schedule import ScheduleConfig, schedule_point
from .storage import Storage, Reading


def _local_hour() -> float:
    lt = time.localtime()
    return lt.tm_hour + lt.tm_min / 60.0


@dataclass
class CycleResult:
    t_ethan: float
    outdoor: Optional[float]
    target: float
    setpoint: float
    mode: str
    applied: bool
    reason: str
    fan_speed: str = "auto"
    turbo: bool = False


class Service:
    def __init__(self, cfg: Config, midea, ecobee, weather_fn: Callable,
                 storage: Optional[Storage] = None,
                 clock: Callable[[], float] = time.time,
                 sched: Optional[ScheduleConfig] = None,
                 hour_fn: Optional[Callable[[], float]] = None):
        self.cfg = cfg
        self.midea = midea
        self.ecobee = ecobee
        self.weather_fn = weather_fn           # () -> Weather-like or None
        self.storage = storage or Storage(cfg.log_path.replace("service.log",
                                          "home_air.db"))
        self.clock = clock
        self.sched = sched or ScheduleConfig(night_target=cfg.target_f)
        # local hour-of-day source (overridable for tests/sim)
        self.hour_fn = hour_fn or (lambda: _local_hour())
        self._ctrl = Controller(self._load_params(),
                                cfg.setpoint_min_f, cfg.setpoint_max_f,
                                cfg.setpoint_step_f)
        self._last_cmd_time = 0.0
        self._last_setpoint: Optional[float] = None
        self._last_mode: Optional[str] = None
        self._last_fan: Optional[tuple] = None
        self._last_office: Optional[float] = None   # office temp from prior cycle
        self._last_ethan: Optional[float] = None     # last good ecobee read (carry-forward)
        self._last_ctx: tuple = (None, None, None, None)  # living/heather/hum/central
        self.learn_interval_s = 86400          # auto-refit the feedforward daily
        # Seed from the LAST ACTUAL learn (persisted in model_params), not process
        # start — else frequent restarts keep resetting the 24h clock and learn
        # never fires (the self-improvement loop dies). 0 => learn ASAP on startup.
        self._last_learn = self._last_learn_ts()

    def _last_learn_ts(self) -> float:
        """Timestamp of the most recent committed learn, or 0 if none."""
        try:
            row = self.storage.conn.execute(
                "SELECT MAX(ts) t FROM model_params WHERE source='learn'").fetchone()
            return float(row["t"]) if row and row["t"] is not None else 0.0
        except Exception:
            return 0.0

    def _maybe_learn(self, now: float) -> None:
        """Daily: refit the site feedforward from accumulated data (robust hold-
        point method). The controller reloads params each cycle, so improvements
        apply automatically — the system gets better as data grows."""
        if now - self._last_learn < self.learn_interval_s:
            return
        try:
            from .learn import learn
            res = learn(self.path_for_learn(), verbose=False)
            self.storage.log_action("learn", f"refit feedforward: {res}")
            if res is not None:
                # advance the 24h clock only after a successful, persisted fit;
                # if data was insufficient (res is None), retry next cycle instead
                # of blocking for a full day.
                self._last_learn = now
        except Exception as e:
            self.storage.log_action("error", f"learn: {e}")

    def path_for_learn(self):
        return self.storage.path

    def _load_params(self) -> ControllerParams:
        try:
            return ControllerParams.from_dict(self.storage.latest_params())
        except Exception:
            return ControllerParams()

    def cycle(self) -> CycleResult:
        now = self.clock()
        # 1. room temps + whole-house context (all in one ecobee call).
        #    ecobee's cloud occasionally returns a transient 'code=3 Processing
        #    error'; rather than abort the whole cycle (losing control + spamming
        #    errors), carry forward the last good reading — the room barely moves
        #    in one interval. Only the very first cycle (no history) can fail hard.
        t_living = t_heather = indoor_hum = central_cool = None
        try:
            if hasattr(self.ecobee, "read_full"):
                ctx = self.ecobee.read_full()
                t_ethan = ctx.get("ethan")
                t_living = ctx.get("living")
                t_heather = ctx.get("heather")
                indoor_hum = ctx.get("indoor_hum")
                central_cool = ctx.get("central_cool")
                if t_ethan is None:
                    t_ethan = self.ecobee.read_ethan_temp()
            else:
                t_ethan = self.ecobee.read_ethan_temp()
            self._last_ethan = t_ethan
            self._last_ctx = (t_living, t_heather, indoor_hum, central_cool)
        except Exception as e:
            if getattr(self, "_last_ethan", None) is None:
                raise                       # first cycle, nothing to fall back to
            t_ethan = self._last_ethan
            t_living, t_heather, indoor_hum, central_cool = self._last_ctx
            self.storage.log_action("warn", f"ecobee stale, using last: {e}")
        # 2. weather
        outdoor = forecast = humidity = None
        try:
            w = self.weather_fn()
            if w is not None:
                outdoor = w.temp_f
                forecast = getattr(w, "forecast_temp_f", None)
                humidity = getattr(w, "humidity", None)
        except Exception as e:
            self.storage.log_action("error", f"weather: {e}")

        # 3. decide (reload params so learning takes effect without restart)
        self._ctrl.p = self._load_params()
        # time-of-day schedule -> effective target (night 70 / day band 72-74,
        # weather/cost-aware), then the controller closes the loop on it
        sp = schedule_point(self.hour_fn(), outdoor, forecast, self.sched)
        dt = self.cfg.interval_s
        dec = self._ctrl.decide(t_ethan, sp.target, outdoor,
                                dt_s=dt, outdoor_forecast_f=forecast,
                                sleep_mode=sp.night, office_temp=self._last_office)

        # 4. actuate. ONLY the setpoint is compressor-protected (gated by the
        #    min-command-gap). MODE + FAN + turbo change the airflow, not the
        #    compressor load, so apply them EVERY cycle — otherwise the gap
        #    throttles free-cool/fan switches and the unit gets stuck (e.g. cool +
        #    silent fan while we think it's free-cool + max). Re-assert all each
        #    cycle so the AC's onboard schedule/eco can't override us.
        gap_ok = (now - self._last_cmd_time) >= self.cfg.min_command_gap_s
        if self._last_setpoint is None or (gap_ok and self._last_setpoint != dec.setpoint_f):
            self._last_setpoint = dec.setpoint_f
            self._last_cmd_time = now              # gate only true setpoint changes
        fan_state = (dec.fan_speed, dec.turbo)
        if (self._last_mode, self._last_fan) != (dec.mode, fan_state):
            self.storage.log_action(
                "setpoint", f"sp={self._last_setpoint} mode={dec.mode} "
                            f"fan={dec.fan_speed} turbo={dec.turbo} "
                            f"err={dec.error:.2f} ff={dec.feedforward:.2f}")
        self._last_mode = dec.mode                 # mode/fan: apply immediately
        self._last_fan = fan_state
        # re-assert the full command every cycle
        applied = False
        try:
            fan_i, turbo_i = self._last_fan
            self.midea.apply(self._last_setpoint, self._last_mode,
                             dec.ac_should_run, fan_speed=fan_i, turbo=turbo_i)
            applied = True
        except Exception as e:
            self.storage.log_action("error", f"apply: {e}")

        # 5. log
        ms = None
        try:
            ms = self.midea.refresh()
            if getattr(ms, "indoor_temp_f", None) is not None:
                self._last_office = ms.indoor_temp_f   # feed next cycle's efficiency logic
        except Exception as e:
            print(f"midea refresh failed: {e}")
        self.storage.log_reading(Reading(
            ts=now, t_ethan=t_ethan,
            t_office=getattr(ms, "indoor_temp_f", None),
            outdoor_temp=outdoor, outdoor_hum=humidity,
            # log the APPLIED setpoint (gap-gated), not the freshly-computed one:
            # during ramps the unit is still on _last_setpoint, and learn.py reads
            # setpoint_cmd as the offset the AC actually held at.
            target=sp.target, setpoint_cmd=self._last_setpoint,
            ac_running=int(dec.ac_should_run), mode=dec.mode,
            error=dec.error, integral=dec.integral,
            note=f"{dec.reason} | want {dec.mode}/{dec.fan_speed} "
                 f"got {getattr(ms,'mode',None)}/{getattr(ms,'fan_speed',None)}",
            forecast_temp=forecast, feedforward=dec.feedforward,
            fan=dec.fan_speed, turbo=int(dec.turbo),
            t_living=t_living, t_heather=t_heather,
            indoor_hum=indoor_hum, central_cool=central_cool,
        ))
        self._maybe_learn(now)
        return CycleResult(t_ethan, outdoor, sp.target, dec.setpoint_f,
                           dec.mode, applied, dec.reason,
                           dec.fan_speed, dec.turbo)

    def run(self, max_cycles: Optional[int] = None) -> None:
        self.storage.log_action("startup", str(self.cfg.to_dict()))
        try:
            self.midea.connect()
        except Exception as e:
            self.storage.log_action("error", f"midea connect: {e}")
        n = 0
        while max_cycles is None or n < max_cycles:
            try:
                res = self.cycle()
                print(f"[{time.strftime('%H:%M:%S')}] ethan={res.t_ethan:.1f}F "
                      f"target={res.target:.1f} out={res.outdoor} "
                      f"sp={res.setpoint:.0f} {res.mode} fan={res.fan_speed}"
                      f"{' +turbo' if res.turbo else ''} "
                      f"{'APPLIED' if res.applied else 'hold'} :: {res.reason}")
            except Exception as e:
                self.storage.log_action("error", f"cycle: {e}")
                print(f"cycle error: {e}")
            n += 1
            if max_cycles is None or n < max_cycles:
                time.sleep(self.cfg.interval_s)
