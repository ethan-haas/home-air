"""ecobee client — reads Ethan's-room temperature from a SmartSensor.

Two auth modes:
  * Static bearer token (ECOBEE_TOKEN) — quick start, expires ~1h. This is the
    token the bundled ecobee_update.sh grabs from the browser.
  * OAuth refresh (ECOBEE_API_KEY + ECOBEE_REFRESH_TOKEN) — durable; the client
    refreshes the access token automatically when it expires (error code 14).

The remote-sensor JSON nests temperature as a capability in tenths of a degree F
(e.g. "720" -> 72.0). We match Ethan's sensor by id prefix (rs2:101) or by name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from .config import Config


class EcobeeError(RuntimeError):
    pass


@dataclass
class SensorReading:
    name: str
    sensor_id: str
    temp_f: Optional[float]
    occupied: Optional[bool]


class EcobeeClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token = cfg.ecobee_token

    # ---- auth ----
    def _client_id(self) -> str:
        return (self.cfg.ecobee_client_id
                or self.cfg.ecobee_web_client_id
                or self.cfg.ecobee_api_key)

    def _password_login(self) -> None:
        """Mint tokens via the Auth0 password grant (no browser needed)."""
        if not (self.cfg.ecobee_account and self.cfg.ecobee_password):
            raise EcobeeError("no ecobee account/password configured")
        r = requests.post(self.cfg.ecobee_token_url, json={
            "grant_type": "password",
            "username": self.cfg.ecobee_account,
            "password": self.cfg.ecobee_password,
            "client_id": self._client_id(),
            "audience": self.cfg.ecobee_audience,
            "scope": self.cfg.ecobee_scope,
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:
            raise EcobeeError(f"ecobee password login failed: {data}")
        self._token = data["access_token"]
        if data.get("refresh_token"):
            self.cfg.ecobee_refresh_token = data["refresh_token"]

    def ensure_token(self) -> None:
        """Make sure we hold an access token, minting one if needed."""
        if self._token:
            return
        if self.cfg.ecobee_refresh_token:
            try:
                self._refresh_token()
                return
            except Exception:
                pass
        self._password_login()

    def _refresh_token(self) -> None:
        # client_id priority: user-supplied -> ecobee web app's public id -> legacy key
        client_id = self._client_id()
        if not (self.cfg.ecobee_refresh_token and client_id):
            raise EcobeeError(
                "access token expired and no refresh token configured. Capture "
                "ECOBEE_REFRESH_TOKEN from the ecobee web app's token response "
                "(browser devtools).")
        url = self.cfg.ecobee_token_url
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.cfg.ecobee_refresh_token,
            "client_id": client_id,
        }
        # Auth0 (auth.ecobee.com) expects a JSON/form body; legacy ecobee
        # (api.ecobee.com/token) expects query params.
        if "auth.ecobee.com" in url or "/oauth/" in url:
            r = requests.post(url, json=payload, timeout=15)
        else:
            r = requests.post(url, params=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:
            raise EcobeeError(f"ecobee refresh failed: {data}")
        self._token = data["access_token"]
        # persist rotated refresh token back onto the config object
        if data.get("refresh_token"):
            self.cfg.ecobee_refresh_token = data["refresh_token"]

    # ---- read ----
    def _get_thermostat(self, retry: bool = True, attempts: int = 3) -> dict:
        if not self._token:
            self.ensure_token()
        body = {
            "selection": {
                "selectionType": "thermostats",
                "selectionMatch": self.cfg.ecobee_thermostat_id,
                "includeSensors": True,
                "includeRuntime": True,
                "includeEquipmentStatus": True,
            }
        }
        import json as _json
        import time as _time
        last_err = None
        for i in range(max(1, attempts)):
            try:
                r = requests.get(
                    f"{self.cfg.ecobee_api_base}/1/thermostat",
                    params={"format": "json", "json": _json.dumps(body)},
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json;charset=UTF-8"},
                    timeout=15,
                )
                data = r.json()
            except Exception as e:           # network/timeout/JSON -> transient
                last_err = e
                _time.sleep(1.5 * (i + 1))
                continue
            code = (data.get("status") or {}).get("code")
            if code == 14 and retry:        # token expired -> refresh, else re-login
                try:
                    self._refresh_token()
                except Exception:
                    self._password_login()
                return self._get_thermostat(retry=False, attempts=attempts)
            # code 3 (Processing error) and 5xx-ish are transient -> retry
            if code in (3,) and i < attempts - 1:
                last_err = EcobeeError(f"transient code={code}")
                _time.sleep(1.5 * (i + 1))
                continue
            if code not in (0, None):
                raise EcobeeError(f"ecobee api error code={code}: "
                                  f"{(data.get('status') or {}).get('message')}")
            tl = data.get("thermostatList") or []
            if not tl:
                raise EcobeeError("no thermostat returned")
            return tl[0]
        raise EcobeeError(f"ecobee read failed after {attempts} attempts: {last_err}")

    def read_all_sensors(self) -> list[SensorReading]:
        therm = self._get_thermostat()
        out: list[SensorReading] = []
        for s in therm.get("remoteSensors", []):
            temp = None
            occ = None
            for cap in s.get("capability", []):
                if cap.get("type") == "temperature":
                    try:
                        v = int(cap["value"]) / 10.0
                        # a disconnected sensor reports a sentinel (e.g. -5002 ->
                        # -500.2) that parses cleanly; reject anything out of a
                        # physical indoor range so it can't poison control/learn.
                        temp = v if -40.0 <= v <= 140.0 else None
                    except (ValueError, KeyError, TypeError):
                        temp = None
                elif cap.get("type") == "occupancy":
                    occ = cap.get("value") == "true"
            out.append(SensorReading(
                name=s.get("name", ""), sensor_id=s.get("id", ""),
                temp_f=temp, occupied=occ))
        return out

    def read_named(self) -> dict:
        """All room temps in one call: {'ethan','living','heather','thermostat'}."""
        out: dict[str, float] = {}
        want = ":".join(self.cfg.ecobee_ethan_sensor.split(":")[:2])
        for s in self.read_all_sensors():
            if s.temp_f is None:
                continue
            n = s.name.lower()
            if "ethan" in n or s.sensor_id == want:
                out["ethan"] = s.temp_f
            elif "living" in n:
                out["living"] = s.temp_f
            elif "heather" in n or "heathers" in n:
                out["heather"] = s.temp_f
            elif "ecobee" in n:
                out["thermostat"] = s.temp_f
        return out

    def read_full(self) -> dict:
        """One API call -> all room temps + indoor humidity + whether the house
        CENTRAL AC is running (a confound: it cools Ethan's room too, so logging
        it lets future learning isolate the office Midea's own effect)."""
        therm = self._get_thermostat()
        out: dict = {}
        want = ":".join(self.cfg.ecobee_ethan_sensor.split(":")[:2])
        for s in therm.get("remoteSensors", []):
            temp = None
            for cap in s.get("capability", []):
                if cap.get("type") == "temperature":
                    try:
                        v = int(cap["value"]) / 10.0
                        # a disconnected sensor reports a sentinel (e.g. -5002 ->
                        # -500.2) that parses cleanly; reject anything out of a
                        # physical indoor range so it can't poison control/learn.
                        temp = v if -40.0 <= v <= 140.0 else None
                    except (ValueError, KeyError, TypeError):
                        temp = None
            if temp is None:
                continue
            n = s.get("name", "").lower(); sid = s.get("id", "")
            if "ethan" in n or sid == want:
                out["ethan"] = temp
            elif "living" in n:
                out["living"] = temp
            elif "heather" in n:
                out["heather"] = temp
            elif "ecobee" in n:
                out["thermostat"] = temp
        rt = therm.get("runtime", {}) or {}
        h = rt.get("actualHumidity")
        out["indoor_hum"] = float(h) if h is not None else None
        eq = therm.get("equipmentStatus") or ""
        out["central_cool"] = 1 if "compCool" in eq else 0
        out["equipment"] = eq
        return out

    def read_ethan_temp(self) -> float:
        """Return Ethan's-room temperature in F, or raise if unavailable."""
        want = self.cfg.ecobee_ethan_sensor          # 'rs2:101:1'
        want_prefix = ":".join(want.split(":")[:2])   # 'rs2:101'
        sensors = self.read_all_sensors()
        for s in sensors:
            if s.sensor_id == want_prefix or s.sensor_id == want:
                if s.temp_f is None:
                    raise EcobeeError(f"sensor {want} reported no temperature")
                return s.temp_f
        # fall back to name match
        for s in sensors:
            if "ethan" in s.name.lower() and s.temp_f is not None:
                return s.temp_f
        names = ", ".join(f"{s.name}({s.sensor_id})" for s in sensors)
        raise EcobeeError(f"Ethan sensor {want} not found. Saw: {names}")
