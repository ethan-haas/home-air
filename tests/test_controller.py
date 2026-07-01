import math

from hvac.controller import Controller, ControllerParams


def test_too_hot_lowers_setpoint():
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=75.0, target=70.0, outdoor_f=90.0, dt_s=120)
    # room well above target -> setpoint must be below target
    assert d.setpoint_f < 70.0
    assert d.ac_should_run is True
    assert d.error == 5.0


def test_feedforward_grows_with_outdoor():
    c = Controller(ControllerParams())
    cool = c.feedforward_offset(70.0)
    hot = c.feedforward_offset(100.0)
    assert hot > cool  # hotter outside -> office must run colder


def test_setpoint_clamped_and_quantized():
    p = ControllerParams()
    c = Controller(p, setpoint_min=62.0, setpoint_max=80.0, step=1.0)
    d = c.decide(t_ethan=90.0, target=70.0, outdoor_f=105.0, dt_s=120)
    assert 62.0 <= d.setpoint_f <= 80.0
    assert math.isclose(d.setpoint_f % 1.0, 0.0, abs_tol=1e-9)


def test_deadband_freezes_integral():
    c = Controller(ControllerParams(deadband_f=0.5))
    c.decide(t_ethan=70.2, target=70.0, outdoor_f=80.0, dt_s=120)
    assert c.integral == 0.0  # within deadband -> no integration


def test_anti_windup_bounds_integral():
    p = ControllerParams(ki=0.01, integral_clamp_f=3.0)
    c = Controller(p, setpoint_min=62.0, setpoint_max=80.0)
    for _ in range(500):
        c.decide(t_ethan=85.0, target=70.0, outdoor_f=100.0, dt_s=300)
    assert abs(p.ki * c.integral) <= p.integral_clamp_f + 1e-6


def test_idle_when_cold_outside_and_below_target():
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=67.0, target=70.0, outdoor_f=60.0, dt_s=120)
    assert d.ac_should_run is False
    assert d.mode == "off"


def test_forecast_precools_on_ramp():
    c = Controller(ControllerParams(forecast_weight=0.6))
    now = c.decide(t_ethan=70.0, target=70.0, outdoor_f=80.0, dt_s=120,
                   outdoor_forecast_f=80.0)
    c.reset()
    ahead = c.decide(t_ethan=70.0, target=70.0, outdoor_f=80.0, dt_s=120,
                     outdoor_forecast_f=98.0)
    # a hotter forecast must push the setpoint lower (pre-cool)
    assert ahead.setpoint_f <= now.setpoint_f


def test_fan_quiet_at_night_when_near_band():
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=70.2, target=70.0, outdoor_f=85.0, dt_s=120,
                 sleep_mode=True)
    assert d.turbo is False                      # never boost while sleeping
    assert d.fan_speed in ("silent", "low")      # whisper once near band


def test_fan_boosts_at_night_when_far_off():
    # turbo is OK any time (operator preference): a hot room at night -> boost
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=76.0, target=68.0, outdoor_f=80.0, dt_s=120,
                 sleep_mode=True)
    assert d.turbo is True
    assert d.fan_speed == "max"


def test_fan_boost_on_big_daytime_deficit():
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=78.0, target=72.0, outdoor_f=95.0, dt_s=120,
                 sleep_mode=False)
    assert d.turbo is True
    assert d.fan_speed == "max"


def test_fan_gentle_when_near_target_day():
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=72.1, target=72.0, outdoor_f=80.0, dt_s=120,
                 sleep_mode=False)
    assert d.turbo is False
    assert d.fan_speed in ("auto", "medium")


def test_boost_on_above_band_even_when_office_cold():
    """Boost is an AIRFLOW lever on this Duo: the isolated room being above band
    means the air needs moving, even when the office is already cold. (Old policy
    skipped boost here; the office-cold gate was removed.)"""
    c = Controller(ControllerParams(), setpoint_min=60, setpoint_max=80)
    d = c.decide(t_ethan=78.0, target=70.0, outdoor_f=80.0, dt_s=120,
                 office_temp=61.0)   # office already cold
    assert d.fan_speed == "max"
    assert d.turbo is True           # room above band -> boost airflow regardless


def test_boost_on_when_office_still_warm():
    c = Controller(ControllerParams(), setpoint_min=60, setpoint_max=80)
    d = c.decide(t_ethan=82.0, target=70.0, outdoor_f=100.0, dt_s=120,
                 office_temp=78.0)
    assert d.fan_speed == "max"
    assert d.turbo is True


def test_no_boost_when_in_band():
    """Boost turns off once the room is back inside the comfort band."""
    c = Controller(ControllerParams(), setpoint_min=60, setpoint_max=80)
    d = c.decide(t_ethan=70.3, target=70.0, outdoor_f=85.0, dt_s=120,
                 office_temp=65.0)   # error 0.3 -> in band
    assert d.turbo is False


def test_free_cool_fan_when_cold_outside_and_office_cold():
    """Outdoor below target + office already cold -> circulate (FAN), no compressor."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=72.0, target=70.0, outdoor_f=64.0, dt_s=120,
                 office_temp=60.0)
    assert d.free_cool is True and d.mode == "fan"    # FAN_ONLY, compressor off
    assert d.fan_speed == "max" and d.turbo is False


def test_compressor_runs_to_chill_office_then_free_cool():
    """Cold outside but office not cold yet -> run compressor to chill it first."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=74.0, target=70.0, outdoor_f=64.0, dt_s=120,
                 office_temp=72.0)   # office warm -> no free cold air yet
    assert d.mode == "cool"


def test_compressor_when_hot_outside():
    """Hot outside -> real refrigeration needed, not free cooling."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=75.0, target=70.0, outdoor_f=90.0, dt_s=120,
                 office_temp=62.0)
    assert d.mode == "cool"


def test_no_free_cool_when_hot_outside_even_if_office_cold():
    """Data-corrected: with the compressor OFF the office's cold is a depleting
    bank — real logs show the room drifts +2.0 to +2.4 F/hr in fan-mode at outdoor
    65-75 (vs +0.02 at outdoor<65). So when it's hot out, run the compressor to
    hold the room; don't coast on fan and let it climb."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=71.0, target=70.0, outdoor_f=92.0, dt_s=120,
                 office_temp=62.0)
    assert d.mode == "cool"   # hot out -> refrigerate, free-cool can't sustain


def test_outdoor_gate_blocks_free_cool_in_danger_band():
    """Outdoor 72 (the 65-75 band where logs show fan-mode room drift ~+2.4 F/hr):
    even with the office cold and the room on target, do NOT free-cool — the office
    would warm and the room would climb. Run the compressor instead."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=70.0, target=70.0, outdoor_f=72.0, dt_s=120,
                 office_temp=60.0)
    assert d.mode == "cool" and d.free_cool is False


def test_outdoor_gate_exit_hysteresis():
    """Once free-cooling at cold outdoor, a warm-up past the exit threshold kicks
    it back to the compressor (no thrash below it)."""
    c = Controller(ControllerParams())
    d1 = c.decide(t_ethan=70.0, target=70.0, outdoor_f=62.0, dt_s=120,
                  office_temp=60.0)
    assert d1.mode == "fan"                      # free-cooling: cold out, cold office
    d2 = c.decide(t_ethan=70.0, target=70.0, outdoor_f=66.0, dt_s=120,
                  office_temp=60.0)
    assert d2.mode == "fan"                      # 66 < exit(67): hysteresis holds
    d3 = c.decide(t_ethan=70.0, target=70.0, outdoor_f=69.0, dt_s=120,
                  office_temp=60.0)
    assert d3.mode == "cool"                     # past exit -> compressor


def test_full_cool_when_room_far_above_even_if_office_cold():
    """Big recovery beats efficiency (operator: coolness first when far off)."""
    c = Controller(ControllerParams())
    d = c.decide(t_ethan=76.0, target=70.0, outdoor_f=85.0, dt_s=120,
                 office_temp=61.0)
    assert d.mode == "cool"
