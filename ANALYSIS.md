# LAN data analysis — gen-2 calibration (2026-06-20)

Analyzed **742 readings over 26.2 h** of live LAN operation (`data/home_air.db`).

## What the data shows

| Period | MAE vs target | Mean error | AC on% | Verdict |
|---|---|---|---|---|
| **Night (target 70)** | 0.91°F | −0.20 | 73% | **Good** — holds ~70 |
| **Day (band 72–74)** | 2.25°F | **+1.80** (too hot) | 90% | **Runs hot**, up to 76°F |

## Root cause (the key finding)

Correlation of Ethan's-room temperature:
- with **outdoor temp: 0.92** ← dominant driver
- with the **office temp: 0.16** ← office AC barely moves it
- with the office **setpoint: −0.82** ← this is the *controller's* own feedback signature (it lowers the setpoint when the room is hot), not plant gain. You cannot read plant gain from closed-loop data — the naive `setpoint→room` fit even comes out with the wrong sign.

**Midday (12–18 h):** the controller already drives the setpoint to the **floor (62.8°F avg)**, the office reaches **65.6°F**, yet Ethan's room sits at **74.2°F (peak 76.2)**. The office AC reaches its *own* setpoint only 69% of the time on hot afternoons.

**Conclusion:** on warm afternoons the office AC is **maxed out and still can't pull Ethan's room into the band**. This is a **physical ceiling — weak office→room coupling**, not a tuning gap. The room is heated by the outdoors faster than the office can bleed cool air across.

## Software changes made (data-driven)

1. **Recalibrated the simulator plant** to the measured dynamics — outdoor-dominated, weak cross-room coupling (`k_eo` 0.005→0.020, `k_c` 0.030→0.012). The simulator was previously too optimistic; it now matches reality (at outdoor 80°F with the office floored, the room settles ~74°F, exactly as observed).
2. **Re-tuned the controller** on the calibrated plant: more aggressive and more outdoor-led — `kp` 2.42→4.54, `base_offset` 5.1→6.3, stronger `outdoor_gain`. It cools harder and earlier on heat ramps (best-effort on hot days; the metric confirms it floors the setpoint rather than giving up).
3. **Lowered the setpoint floor** 62→61°F (the unit's real minimum) for a little more authority.
4. Night/mild performance stays strong; hot-day in-band is now reported honestly as capacity-bound.

## The real fix is physical (biggest win, ~$0–150)

Software is already optimal — the bottleneck is **airflow between the office and Ethan's room**. In rough order of cost/effort:

1. **Open the office door fully + put a box/floor fan in the doorway** blowing office air toward Ethan's room. This directly attacks the weak 0.16 coupling and is the cheapest, likely-largest gain.
2. **A through-wall/door transfer fan** for a permanent version of #1.
3. **A small dedicated AC (or the Midea moved) into Ethan's room** — removes the cross-room problem entirely on hot afternoons.

Night cooling is fine as-is; these only matter for hot daytime hours.

## Gen-3 professional analysis (2026-06-22, +central_cool data)

Combined ~2.2k readings (52h dynamics archive + 477 rows with the new
`central_cool` signal).

**De-confounded drivers of Ethan's room** (regression, R²=0.71):
`ethan = 42.6 + 0.226·outdoor + 0.37·central_ac + 0.205·office`
- **Outdoor dominates** (0.226/°F).
- **Central AC barely matters** (+0.37, wrong sign = noise) — his room is
  thermally isolated from the 66–69°F house. Both AC systems struggle to reach it.
- **Midea office→room transfer ≈ 0.205** (weak): lowering the office 1°F moves
  Ethan's room only ~0.2°F.

**Efficiency finding (the actionable one):** the office is already cold (~65°F)
while the room sits ~73°F. The bottleneck is **cross-room airflow, not cooling
power**. So flooring the setpoint to 60 and running **turbo over-cool the office
for ~negligible room benefit** (~0.5°F) while burning ~36% more power. Turbo only
helped office depth when the office was still warm (compressor-limited).

**Algorithm change:** turbo now engages only when the office is still warmer than
its setpoint (`office_cold_margin_f`); once the office is cold, use **MAX fan
(airflow) with NO turbo** — same room effect, less energy. Office temp is fed
into the controller (prior-cycle reading).

**Room cooling rate:** ~0 net F/min while holding against load; ~12 °F/hr when
actively dropping — slow. Confirms the pre-cool needs a long lead (kept at 4h).

**Bottom line:** software is at the hardware limit. The single biggest remaining
win is **physical airflow** (manual vane aimed at the doorway + a doorway box
fan moving the already-cool house air in) — not more compressor power.
