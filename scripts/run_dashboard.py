#!/usr/bin/env python
"""Run the Home_Air dashboard.

  python scripts/run_dashboard.py                 # serve the live DB
  python scripts/run_dashboard.py --port 9000
  python scripts/run_dashboard.py --demo          # seed a day of sim data first

Open http://localhost:8787 . Auto-refreshes every 30s.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvac.config import Config, DB_PATH
from hvac.dashboard import serve
from hvac.storage import Storage


def seed_demo(db_path) -> None:
    """Populate the DB with ~24h of simulated readings so the UI has history."""
    from hvac.controller import ControllerParams
    from hvac.midea_client import MockMidea
    from hvac.simulator import Plant
    from hvac.sim_harness import SimWorld, SimEcobee, SimWeather
    from hvac.service import Service

    cfg = Config()
    cfg.interval_s = 120
    cfg.min_command_gap_s = 0
    storage = Storage(db_path)
    midea = MockMidea(cfg, indoor_f=75.0)
    world = SimWorld(Plant(), midea, start_hour=0.0, start_ethan=72.0,
                     interval_min=10.0)
    # backdate the clock so timestamps span the last 24h up to now
    base = time.time() - 24 * 3600
    step = 600  # 10 min
    tick = {"t": base}

    def clock():
        v = tick["t"]; tick["t"] += step; return v

    svc = Service(cfg, midea, SimEcobee(world), SimWeather(world), storage,
                  clock=clock, hour_fn=lambda: (world.minute / 60.0) % 24)
    for _ in range(144):  # 144 * 10min = 24h
        svc.cycle()
        world.advance()
    print(f"seeded {storage.count_readings()} demo readings into {db_path}")
    storage.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--demo", action="store_true",
                    help="seed ~24h of simulated data before serving")
    args = ap.parse_args()
    if args.demo:
        seed_demo(args.db)
    serve(args.db, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
