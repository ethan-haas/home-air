"""Offline autoresearch: search ControllerParams to maximise the sim metric.

This is the in-loop optimiser. It does a random search seeded by the current
defaults, then a local coordinate refinement around the best find. Run:

    python -m tools.tune --iters 400

Prints the best params + metric. Does NOT write anything; copy the winner into
ControllerParams defaults (or persist via storage) once you trust it.
"""
from __future__ import annotations

import argparse
import random
from dataclasses import replace

from hvac.controller import ControllerParams
from hvac.score import evaluate

# search ranges (name -> (lo, hi))
RANGES = {
    "base_offset_f": (2.0, 8.0),
    "outdoor_ref_f": (74.0, 84.0),
    "outdoor_gain": (0.05, 1.0),
    "outdoor_gain_cold": (0.0, 0.2),
    "kp": (0.2, 5.0),
    "ki": (0.0, 0.03),
    "integral_clamp_f": (2.0, 10.0),
    "forecast_lead_min": (0.0, 90.0),
    "forecast_weight": (0.0, 1.0),
    "deadband_f": (0.2, 0.8),
}


def sample(rng: random.Random) -> ControllerParams:
    vals = {k: rng.uniform(lo, hi) for k, (lo, hi) in RANGES.items()}
    return ControllerParams(**vals)


def metric(p: ControllerParams) -> float:
    return evaluate(p)["metric"]


def coordinate_refine(best: ControllerParams, best_m: float,
                      rng: random.Random, rounds: int = 6) -> tuple[ControllerParams, float]:
    for _ in range(rounds):
        improved = False
        for k, (lo, hi) in RANGES.items():
            cur = getattr(best, k)
            span = (hi - lo)
            for delta in (0.5, 0.2, 0.08, -0.08, -0.2, -0.5):
                cand_val = min(hi, max(lo, cur + delta * span * 0.25))
                cand = replace(best, **{k: cand_val})
                m = metric(cand)
                if m > best_m + 1e-6:
                    best, best_m = cand, m
                    improved = True
        if not improved:
            break
    return best, best_m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    best = ControllerParams()
    best_m = metric(best)
    print(f"start default metric={best_m:.4f}")

    for i in range(args.iters):
        cand = sample(rng)
        m = metric(cand)
        if m > best_m:
            best, best_m = cand, m
            print(f"  iter {i:4d} new best metric={best_m:.4f}")

    best, best_m = coordinate_refine(best, best_m, rng)
    print(f"\nBEST metric={best_m:.4f}")
    print("params =", best.to_dict())
    print("\nper-scenario:")
    evaluate(best, verbose=True)


if __name__ == "__main__":
    main()
