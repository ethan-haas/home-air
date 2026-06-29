from hvac.schedule import schedule_point, ScheduleConfig


def test_night_holds_firm_70():
    for h in (21.0, 0.0, 3.0, 8.5):
        sp = schedule_point(h, outdoor=85.0)
        assert sp.target == 70.0
        assert (sp.low, sp.high) == (69.0, 71.0)
        assert sp.night is True


def test_night_firm_regardless_of_outdoor():
    cool = schedule_point(2.0, outdoor=66.0).target
    hot = schedule_point(2.0, outdoor=98.0).target
    assert cool == 70.0 and hot == 70.0      # firm 70, not cost-drifting


def test_day_band_is_72_74():
    sp = schedule_point(12.0, outdoor=80.0)
    assert (sp.low, sp.high) == (72.0, 74.0)
    assert 72.0 <= sp.target <= 74.0
    assert sp.night is False


def test_day_rides_warm_when_hot_outside():
    cool = schedule_point(12.0, outdoor=74.0).target
    hot = schedule_point(12.0, outdoor=95.0).target
    assert cool < hot


def test_evening_precool_drives_toward_70_by_9pm():
    t5 = schedule_point(17.0, outdoor=85.0).target
    t8 = schedule_point(20.0, outdoor=82.0).target
    assert 71.0 <= t5 <= 72.5
    assert t8 < t5                           # gliding down toward 70
    assert abs(schedule_point(20.99, outdoor=80.0).target - 70.0) < 0.6
    assert schedule_point(19.0, outdoor=85.0).night is False  # aggressive, turbo ok


def test_target_70_at_9pm_onward():
    sp = schedule_point(21.0, outdoor=72.0)
    assert sp.target == 70.0 and sp.night is True
