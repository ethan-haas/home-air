"""Home_Air: auto-hold Ethan's room at a target temp by driving the office Midea AC.

Reads Ethan's room temperature from an ecobee SmartSensor, factors in outdoor
weather, and adjusts the upstairs-office Midea Duo portable AC so heat transfer
pulls Ethan's room toward the target (~70 F).

Subpackages / modules:
  config       central tunable + secrets loading
  storage      SQLite log of readings + learned model params (self-improvement)
  controller   the control algorithm (feedforward + PI + learned office offset)
  simulator    2-room RC thermal model for offline test + metric
  ecobee_client / midea_client / weather   real-world I/O
  learn        offline fit of model params from logged history
  service      live control loop
"""

__version__ = "0.1.0"
