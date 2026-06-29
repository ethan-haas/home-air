"""SQLite storage: the substrate that lets the algorithm improve over time.

Two roles:
  1. `readings`     — every control cycle is logged (room temp, outdoor temp,
                      commanded setpoint, AC state, controller internals). This
                      is the training data.
  2. `model_params` — the learned parameters the controller reads each cycle.
                      `learn.py` refits these from `readings` and writes a new
                      versioned row; the controller always uses the newest.

Keeping the log and the params in the same DB means the whole self-improvement
loop is one file you can copy, inspect, or replay.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    t_ethan       REAL,          -- Ethan-room temp (sensor), F
    t_office      REAL,          -- office temp if known, F
    outdoor_temp  REAL,          -- F
    outdoor_hum   REAL,          -- %
    target        REAL,          -- target at this cycle
    setpoint_cmd  REAL,          -- setpoint commanded to Midea
    ac_running    INTEGER,       -- 1/0
    mode          TEXT,          -- cool / fan / off
    error         REAL,          -- t_ethan - target
    integral      REAL,          -- controller integral term
    note          TEXT,
    forecast_temp REAL,          -- outdoor forecast used this cycle, F
    feedforward   REAL,          -- controller feedforward offset, F
    fan           TEXT,          -- fan speed commanded
    turbo         INTEGER,       -- boost on/off
    t_living      REAL,          -- Living Room sensor, F (whole-house context)
    t_heather     REAL,          -- Heather's bedroom sensor, F
    indoor_hum    REAL,          -- ecobee indoor humidity, %
    central_cool  INTEGER        -- house central AC cooling now (1/0) — confound
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS model_params (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    params      TEXT NOT NULL,   -- JSON blob of ControllerParams
    source      TEXT,            -- 'default' | 'learn' | 'manual'
    n_samples   INTEGER,         -- rows used to fit (0 for default)
    score       REAL             -- in-sim score of these params, if known
);

CREATE TABLE IF NOT EXISTS actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT,              -- 'setpoint' | 'mode' | 'error' | 'startup'
    detail    TEXT
);
"""


@dataclass
class Reading:
    ts: float
    t_ethan: Optional[float] = None
    t_office: Optional[float] = None
    outdoor_temp: Optional[float] = None
    outdoor_hum: Optional[float] = None
    target: Optional[float] = None
    setpoint_cmd: Optional[float] = None
    ac_running: Optional[int] = None
    mode: Optional[str] = None
    error: Optional[float] = None
    integral: Optional[float] = None
    note: Optional[str] = None
    forecast_temp: Optional[float] = None
    feedforward: Optional[float] = None
    fan: Optional[str] = None
    turbo: Optional[int] = None
    t_living: Optional[float] = None
    t_heather: Optional[float] = None
    indoor_hum: Optional[float] = None
    central_cool: Optional[int] = None


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # WAL lets the dashboard's large reads run concurrently with the service's
        # per-cycle writes; busy_timeout waits out a brief lock instead of raising
        # "database is locked" (which silently dropped a logged reading each clash).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns missing from older DBs (keeps existing data)."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(readings)")}
        add = {
            "forecast_temp": "REAL", "feedforward": "REAL", "fan": "TEXT",
            "turbo": "INTEGER", "t_living": "REAL", "t_heather": "REAL",
            "indoor_hum": "REAL", "central_cool": "INTEGER",
        }
        for col, typ in add.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {typ}")

    # ---- readings ----
    def log_reading(self, r: Reading) -> int:
        cols = list(asdict(r).keys())
        ph = ",".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT INTO readings ({','.join(cols)}) VALUES ({ph})",
            [getattr(r, c) for c in cols],
        )
        self.conn.commit()
        return cur.lastrowid

    def recent_readings(self, limit: int = 5000) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM readings ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return list(reversed(cur.fetchall()))

    def count_readings(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    # ---- model params ----
    def save_params(self, params: dict, source: str = "learn",
                    n_samples: int = 0, score: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO model_params (ts, params, source, n_samples, score) "
            "VALUES (?,?,?,?,?)",
            (time.time(), json.dumps(params), source, n_samples, score),
        )
        self.conn.commit()
        return cur.lastrowid

    def latest_params(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT params FROM model_params ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["params"]) if row else None

    def params_history(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM model_params ORDER BY ts ASC").fetchall())

    # ---- actions ----
    def log_action(self, kind: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO actions (ts, kind, detail) VALUES (?,?,?)",
            (time.time(), kind, detail),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
