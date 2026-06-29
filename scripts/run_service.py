#!/usr/bin/env python
"""Entry point for the live control service.

Examples:
  python scripts/run_service.py --dry-run --once      # no hardware, one cycle
  python scripts/run_service.py --sim                 # run against the simulator
  python scripts/run_service.py                       # real ecobee + real Midea
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvac.config import Config, DB_PATH
from hvac.service import Service
from hvac.storage import Storage


def build_weather_fn(cfg: Config):
    from hvac.weather import get_weather
    from hvac.controller import ControllerParams
    lead = ControllerParams().forecast_lead_min

    def fn():
        try:
            return get_weather(cfg.latitude, cfg.longitude, lead_min=lead)
        except Exception:
            return None
    return fn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="use MockMidea (no hardware writes)")
    ap.add_argument("--sim", action="store_true",
                    help="run fully simulated (mock ecobee+midea+weather)")
    ap.add_argument("--once", action="store_true", help="run a single cycle")
    ap.add_argument("--cycles", type=int, default=None)
    args = ap.parse_args()

    cfg = Config.load()
    storage = Storage(DB_PATH)

    if args.sim:
        from hvac.sim_harness import SimWorld, SimEcobee, SimWeather
        from hvac.midea_client import MockMidea
        from hvac.simulator import Plant
        midea = MockMidea(cfg)
        world = SimWorld(Plant(), midea, start_hour=12.0, start_ethan=76.0)
        svc = Service(cfg, midea, SimEcobee(world), SimWeather(world), storage,
                      hour_fn=lambda: (world.minute / 60.0) % 24)
        n = args.cycles or (1 if args.once else 60)
        for _ in range(n):
            res = svc.cycle()
            print(f"h={world.minute/60%24:4.1f} ethan={res.t_ethan:5.1f}F "
                  f"target={res.target:4.1f} out={res.outdoor:5.1f} "
                  f"sp={res.setpoint:.0f} {res.mode} "
                  f"{'APPLIED' if res.applied else 'hold'}")
            world.advance()
        return
    else:
        from hvac.ecobee_client import EcobeeClient
        ecobee = EcobeeClient(cfg)
        if args.dry_run:
            from hvac.midea_client import MockMidea
            midea = MockMidea(cfg)
        elif cfg.midea_transport == "cloud":
            from hvac.midea_cloud import MideaCloudClient
            midea = MideaCloudClient(cfg)
        else:
            from hvac.midea_client import MideaClient
            midea = MideaClient(cfg)
        weather_fn = build_weather_fn(cfg)
        svc = Service(cfg, midea, ecobee, weather_fn, storage)

    cycles = 1 if args.once else args.cycles
    svc.run(max_cycles=cycles)


if __name__ == "__main__":
    main()
