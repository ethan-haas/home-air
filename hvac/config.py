"""Central configuration.

Secrets (ecobee token, midea token/key) come from environment / a local .env
file so nothing sensitive is committed. Everything else has a sane default that
the simulator and tests use directly.

Load order for a value:  env var  ->  .env file  ->  hardcoded default here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent  # workspace/Home_Air
DB_PATH = HOME / "data" / "home_air.db"


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    env = HOME / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _getf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # ---- goal ----
    target_f: float = 70.0          # desired Ethan-room temperature
    deadband_f: float = 0.5         # don't fight errors smaller than this
    band_f: float = 1.0             # +/- "in band" window used by the metric

    # ---- ecobee ---- (account-specific id comes from env/secret, not the repo)
    ecobee_thermostat_id: str = ""
    ecobee_ethan_sensor: str = "rs2:101:1"   # generic ecobee remote-sensor code
    ecobee_api_base: str = "https://api.ecobee.com"   # reads: /1/thermostat
    # Token refresh: the modern ecobee web app uses Auth0. Verified live that the
    # access token is issued by auth.ecobee.com with client_id (azp) below — the
    # web app's PUBLIC client id (embedded in its JS, not a secret).
    ecobee_token_url: str = "https://auth.ecobee.com/oauth/token"
    ecobee_web_client_id: str = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"
    ecobee_token: str = ""          # short-lived bearer (~1h); refresh to keep alive
    ecobee_refresh_token: str = ""
    # client_id used for the token refresh. ecobee CLOSED new developer
    # registrations, so a personal API key may be unavailable; instead capture
    # the web app's PUBLIC client_id + refresh_token from browser devtools.
    # ECOBEE_CLIENT_ID falls back to ECOBEE_API_KEY for legacy dev-app users.
    ecobee_client_id: str = ""
    ecobee_api_key: str = ""        # legacy: ecobee developer app key
    # direct login (Auth0 password grant) — most convenient + durable: the client
    # logs in with the account email/password to mint tokens, no browser needed.
    ecobee_account: str = ""
    ecobee_password: str = ""
    ecobee_audience: str = "https://prod.ecobee.com/api/v1"
    ecobee_scope: str = "openid smartRead smartWrite offline_access"

    # ---- midea (office AC) ----
    midea_host: str = ""            # LAN IP of the AC
    midea_id: str = ""              # appliance id (from discovery)
    midea_token: str = ""           # local-control token (from midea cloud once)
    midea_key: str = ""             # local-control key
    # optional second candidate (token/key differ by device-id endianness; the
    # client tries the primary then this fallback so either works on the LAN)
    midea_token_alt: str = ""
    midea_key_alt: str = ""
    midea_cloud_account: str = ""   # MSmartHome account (cloud transport + token fetch)
    midea_cloud_password: str = ""
    # 'lan' = local control via msmart-ng (must be on home LAN);
    # 'cloud' = MSmartHome relay via midea-beautiful-air (works over WAN).
    midea_transport: str = "lan"
    # This office Duo silently refuses the turbo/boost bit over the MSmartHome
    # cloud relay (verified live: cmd turbo=true -> device confirmed turbo=false).
    # When turbo is unsupported, requesting it is worse than useless, so the
    # cloud path suppresses it and holds MAX fan instead (same airflow, no
    # phantom BOOST on the dashboard). Set HA_MIDEA_TURBO=on for a unit that
    # actually honors turbo over cloud.
    midea_turbo_supported: bool = False
    # Louver: this Duo's vertical vane is MANUAL (verified: it ignores software
    # angle/swing — no motor). Aim it by hand at the doorway. So we don't send
    # swing commands by default. (midea_swing_pos kept for motorized units.)
    midea_set_swing: bool = False   # True only on units with a motorized louver
    midea_swing_pos: int = 3        # POS_1=ceiling..POS_5=full-down (motorized only)

    # ---- midea setpoint bounds (the controller never commands outside) ----
    setpoint_min_f: float = 60.0    # Midea hardware floor
    setpoint_max_f: float = 80.0
    setpoint_step_f: float = 1.0    # Midea accepts whole/half degrees

    # ---- location for weather (set via HA_LAT/HA_LON env/secret) ----
    latitude: float = 0.0
    longitude: float = 0.0

    # ---- control loop ----
    interval_s: int = 120           # how often the live loop runs
    min_command_gap_s: int = 300    # compressor protection: min secs between setpoint changes
    log_path: str = str(HOME / "data" / "service.log")

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv()
        c = cls(
            target_f=_getf("HA_TARGET_F", 70.0),
            ecobee_token=_get("ECOBEE_TOKEN", ""),
            ecobee_refresh_token=_get("ECOBEE_REFRESH_TOKEN", ""),
            ecobee_client_id=_get("ECOBEE_CLIENT_ID", ""),
            ecobee_api_key=_get("ECOBEE_API_KEY", ""),
            ecobee_account=_get("ECOBEE_ACCOUNT", ""),
            ecobee_password=_get("ECOBEE_PASSWORD", ""),
            ecobee_thermostat_id=_get("ECOBEE_THERMOSTAT_ID", ""),
            ecobee_ethan_sensor=_get("ECOBEE_ETHAN_SENSOR", "rs2:101:1"),
            midea_host=_get("MIDEA_HOST", ""),
            midea_id=_get("MIDEA_ID", ""),
            midea_token=_get("MIDEA_TOKEN", ""),
            midea_key=_get("MIDEA_KEY", ""),
            midea_token_alt=_get("MIDEA_TOKEN_ALT", ""),
            midea_key_alt=_get("MIDEA_KEY_ALT", ""),
            midea_cloud_account=_get("MIDEA_ACCOUNT", ""),
            midea_cloud_password=_get("MIDEA_PASSWORD", ""),
            midea_transport=_get("MIDEA_TRANSPORT", "lan"),
            midea_turbo_supported=_get("HA_MIDEA_TURBO", "") in ("1", "true", "True", "on"),
            midea_set_swing=_get("MIDEA_SET_SWING", "") in ("1", "true", "True"),
            midea_swing_pos=int(_getf("MIDEA_SWING_POS", 3)),
            latitude=_getf("HA_LAT", 0.0),
            longitude=_getf("HA_LON", 0.0),
        )
        return c

    def to_dict(self) -> dict:
        d = asdict(self)
        # never echo secrets
        for k in ("ecobee_token", "ecobee_refresh_token", "ecobee_api_key",
                  "ecobee_password", "midea_token", "midea_key",
                  "midea_cloud_password"):
            if d.get(k):
                d[k] = "***"
        return d


CONFIG = Config.load()
