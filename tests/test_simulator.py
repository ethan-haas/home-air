from hvac.simulator import Plant, SimState, step, ac_cooling, diurnal


def test_no_ac_room_drifts_toward_outdoor():
    p = Plant()
    s = SimState(70.0, 70.0)
    for _ in range(600):  # 10h, no cooling
        s = step(s, outdoor=95.0, setpoint=70.0, running=False, plant=p, dt_min=1)
    assert s.t_ethan > 80.0  # warms toward the hot outdoors


def test_ac_cools_office_below_setpoint_region():
    p = Plant()
    s = SimState(80.0, 80.0)
    for _ in range(600):
        s = step(s, outdoor=85.0, setpoint=64.0, running=True, plant=p, dt_min=1)
    assert s.t_office < 70.0           # office driven down to its setpoint region
    # Ethan's room drops too, but only modestly: the calibrated plant has WEAK
    # office->Ethan coupling (real LAN data), so cross-room cooling is limited.
    assert s.t_ethan < 80.0            # some drop from the 80F start
    assert s.t_office < s.t_ethan      # office is the cold source; room lags well above


def test_ac_cooling_modulates_and_zero_when_off():
    p = Plant()
    assert ac_cooling(75.0, 70.0, running=False, plant=p) == 0.0
    full = ac_cooling(80.0, 70.0, running=True, plant=p)   # 10F above -> full
    part = ac_cooling(71.0, 70.0, running=True, plant=p)   # 1F above -> partial
    assert full > part > 0.0
    assert full <= p.cap_cool + 1e-9


def test_diurnal_peaks_afternoon():
    temps = [diurnal(h, 70, 90, peak_hour=16) for h in range(24)]
    peak_h = max(range(24), key=lambda h: temps[h])
    assert 14 <= peak_h <= 18
