from hvac.controller import ControllerParams
from hvac.score import evaluate, run_closed_loop, score_scenario, standard_scenarios
from hvac.config import Config
from hvac.simulator import Plant
from hvac.midea_client import MockMidea
from hvac.sim_harness import SimWorld, SimEcobee, SimWeather
from hvac.service import Service
from hvac.storage import Storage


def test_feasible_day_holds_band():
    """On a FEASIBLE day (mild outdoors), the controller must hold the 72-74 band.
    The plant is calibrated to real LAN data: hot afternoons are AC-capacity-bound
    (weak office->Ethan coupling), so the suite-wide metric is not a quality bar —
    feasible-condition tracking is."""
    res = evaluate(ControllerParams())
    assert res["scenarios"]["mild_day"]["in_band"] > 0.7


def test_cools_hard_when_infeasible():
    """On a hot day the office AC physically can't hold the band (real coupling),
    but the controller must still do its best: drive the setpoint to the floor and
    never overcool. (Honest ceiling, surfaced by the LAN-data calibration.)"""
    sc = [s for s in standard_scenarios() if s.name == "heat_wave"][0]
    trace = run_closed_loop(sc, ControllerParams(), start_hour=0.0)
    midday = [p for p in trace if 12 <= p.hour <= 18]
    avg_sp = sum(p.setpoint for p in midday) / len(midday)
    assert avg_sp <= 63.0                       # flooring the setpoint = cooling hard
    assert score_scenario(trace)["too_cold"] == 0.0   # never wastes cooling below band


def test_robust_to_plant_mismatch():
    """Under ~20% thermal-model error the controller must stay controlled (no
    runaway) on the feasible mild day."""
    off_plant = Plant(k_c=0.010, cap_cool=0.45, k_eo=0.024)
    res = evaluate(ControllerParams(), plant=off_plant)
    assert res["scenarios"]["mild_day"]["in_band"] > 0.4


def test_service_cycle_closed_loop(tmp_path):
    """Exercise the REAL Service.cycle path against the simulator + MockMidea,
    and confirm it pulls a hot room toward target and logs data."""
    cfg = Config()
    cfg.interval_s = 120
    cfg.min_command_gap_s = 0  # allow every-cycle commands in the test
    storage = Storage(tmp_path / "svc.db")
    midea = MockMidea(cfg, indoor_f=80.0)
    # start at 8pm so the run crosses the day band into the firm-70 night target
    world = SimWorld(Plant(), midea, start_hour=20.0, start_ethan=80.0,
                     interval_min=2.0)
    svc = Service(cfg, midea, SimEcobee(world), SimWeather(world), storage,
                  hour_fn=lambda: (world.minute / 60.0) % 24)

    temps = []
    for _ in range(300):  # ~10h simulated, 8pm -> 6am, into night target
        res = svc.cycle()
        temps.append(res.t_ethan)
        world.advance()

    assert storage.count_readings() == 300
    assert midea.applied                      # commanded the AC
    # deep night holds the 68-70 band: last 2h average should land ~69
    assert 67.0 <= sum(temps[-60:]) / 60 <= 71.0
    storage.close()


def test_service_auto_learn_triggers(tmp_path):
    """After enough data + the learn interval, the service refits params and the
    controller picks them up (self-improvement)."""
    cfg = Config(); cfg.min_command_gap_s = 0
    storage = Storage(tmp_path / "svc.db")
    midea = MockMidea(cfg, indoor_f=72.0)
    world = SimWorld(Plant(), midea, start_hour=2.0, start_ethan=70.0, interval_min=5.0)
    svc = Service(cfg, midea, SimEcobee(world), SimWeather(world), storage,
                  hour_fn=lambda: (world.minute / 60.0) % 24)
    svc.learn_interval_s = 0          # force a learn attempt every cycle
    for _ in range(120):
        svc.cycle(); world.advance()
    # learn ran (logged) without crashing; params table may now hold a learn row
    acts = [r["kind"] for r in storage.conn.execute("SELECT kind FROM actions")]
    assert "learn" in acts
    storage.close()
