"""Midea control over the MSmartHome CLOUD relay (WAN, no LAN needed).

This is how the phone app controls the AC from anywhere: commands go to Midea's
cloud, which relays them to the unit. Built on midea-beautiful-air, which
implements the (otherwise undocumented) MSmartHome transparent-send crypto.

Use this transport when the controller runs off-LAN (e.g. GitHub Actions cron,
a free cloud VM) so it can reach the AC without anything on the home network.

Mirrors hvac.midea_client.MideaClient's interface (connect / refresh / apply)
so hvac.service.Service works with either transport unchanged.

Gotcha: Midea caps concurrent logins per account (error 65027). LOGIN ONCE and
reuse the session — don't reconnect every cycle. This client keeps one cloud
session for its lifetime.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .midea_client import MideaState, f_to_c, c_to_f, round_half

# Midea numeric enums (midea-beautiful-air uses these)
_MODE = {"cool": 2, "fan": 5, "auto": 1, "dry": 3, "heat": 4}
_MODE_INV = {v: k for k, v in _MODE.items()}
_FAN = {"silent": 20, "low": 40, "medium": 60, "high": 80, "max": 100, "auto": 102}
_FAN_INV = {v: k for k, v in _FAN.items()}


def _patch_library():
    """Fix two staleness/identity issues in midea-beautiful-air 0.10.5 (Apr 2024)
    that break MSmartHome cloud control on today's servers:

    1. Its `appliance_transparent_send` posts the field `applianceId`, but the
       current API renamed it to `applianceCode` (verified: old name -> HTTP 9999
       "Unrecognized field applianceId"). We re-post with `applianceCode`.
    2. It ships a single hardcoded deviceId shared by every user of the library;
       once that slot saturates you get error 65027 "online devices exceeded".
       Callers set a UNIQUE per-account deviceId before login (see _ensure).
    Idempotent.
    """
    import midea_beautiful.cloud as mbc
    from secrets import token_hex
    if getattr(mbc, "_homeair_patched", False):
        return
    _encode = mbc._encode_as_csv
    _decode = mbc._decode_from_csv
    ProtocolError = mbc.ProtocolError

    def appliance_transparent_send(self, appliance_id, data):
        order = self._security.aes_encrypt_string(_encode(data))
        # Passing reqId makes api_request SKIP its appVNum/appVersion/clientVersion
        # block, which the transparent endpoint rejects ("Unrecognized field").
        # applianceCode (not applianceId) is the current field name.
        response = self.api_request(
            "/v1/appliance/transparent/send",
            {"order": order, "funId": "0000", "applianceCode": appliance_id,
             "reqId": token_hex(16)},
        )
        reply = _decode(self._security.aes_decrypt_string(response["reply"]))
        if len(reply) < 50:
            raise ProtocolError(f"Invalid payload size {len(reply)} (expected 50)")
        return [reply[40:]]

    mbc.MideaCloud.appliance_transparent_send = appliance_transparent_send
    mbc._homeair_patched = True


def _stable_device_id(account: str) -> str:
    # deterministic, account-specific, NOT the library's shared constant
    return "ha" + hashlib.md5((account + "homeair").encode()).hexdigest()[:14]


class MideaCloudClient:
    """Control the office AC through the MSmartHome cloud."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cloud = None
        self._dev = None

    # STABLE push token: midea-beautiful-air generates a RANDOM pushToken per
    # login, and Midea counts each unique pushToken as a new "online device" ->
    # repeated logins exhaust the per-account device cap (error 65027). Pinning a
    # constant pushToken makes every login reuse ONE device slot, so a 24/7 cron
    # that re-logs-in each run never trips the cap.
    PUSH_TOKEN = "homeair-stable-pushtoken-do-not-change-7f3a9c21b5e84d60ab12"

    def _ensure(self):
        if self._dev is not None:
            return
        from midea_beautiful import appliance_state, SUPPORTED_APPS
        import midea_beautiful.cloud as mbc
        from midea_beautiful.cloud import MideaCloud
        acct = self.cfg.midea_cloud_account
        pw = self.cfg.midea_cloud_password
        if not (acct and pw and self.cfg.midea_id):
            raise RuntimeError(
                "Midea cloud not configured: need MIDEA_ACCOUNT, MIDEA_PASSWORD, "
                "MIDEA_ID")
        _patch_library()                          # applianceCode + (deviceId via below)
        # unique, stable, account-specific deviceId -> a clean device slot that is
        # reused every login (not the library's saturated shared constant)
        mbc.CLOUD_API_DEVICE_ID = _stable_device_id(acct)
        app = SUPPORTED_APPS["MSmartHome"]
        cloud = MideaCloud(
            appkey=app["appkey"], account=acct, password=pw, appid=app["appid"],
            api_url=app["apiurl"], sign_key=app["signkey"],
            iot_key=app.get("iotkey"), hmac_key=app.get("hmackey"),
            proxied=app.get("proxied"),
        )
        # pin the device slot BEFORE authenticating
        try:
            cloud._pushtoken = self.PUSH_TOKEN
        except Exception:
            pass
        # the cloud relay can be slow; give it room (default ~2-3s times out)
        try:
            cloud.request_timeout = 15
        except Exception:
            pass
        cloud.authenticate()                 # login ONCE; reuse for lifetime
        self._cloud = cloud
        self._dev = appliance_state(cloud=cloud,
                                    appliance_id=str(self.cfg.midea_id),
                                    use_cloud=True, appliance_type="0xac",
                                    cloud_timeout=15)

    # ---- interface parity with MideaClient ----
    def connect(self):
        self._ensure()

    def refresh(self) -> MideaState:
        self._ensure()
        self._dev.refresh(cloud=self._cloud)
        s = self._dev.state
        it = getattr(s, "indoor_temperature", None)
        tt = getattr(s, "target_temperature", None)
        mode = getattr(s, "mode", None)
        fan = getattr(s, "fan_speed", None)
        return MideaState(
            indoor_temp_f=c_to_f(it) if it is not None else None,
            target_f=c_to_f(tt) if tt is not None else None,
            power=bool(getattr(s, "running", False)),
            mode=_MODE_INV.get(mode, str(mode)),
            online=bool(getattr(s, "online", True)),
            fan_speed=_FAN_INV.get(fan, str(fan) if fan is not None else None),
            turbo=(bool(getattr(s, "turbo", False))
                   or bool(getattr(s, "turbo_fan", False))),   # boost = turbo_fan on this Duo
        )

    def apply(self, setpoint_f: float, mode: str = "cool", power: bool = True,
              fan_speed: str = "auto", turbo: bool = False) -> Optional[MideaState]:
        self._ensure()
        c = round_half(f_to_c(setpoint_f))
        c = max(16.0, min(30.0, c))
        # This Duo's app "Turbo/Boost" button drives the `turbo_fan` wire
        # attribute (data[8] bit5), NOT the library's `turbo` (data[10] bit1),
        # which this unit ignores (verified live: app boost ON -> turbo_fan=True,
        # turbo=False). We READ boost off turbo_fan (below), but do NOT command
        # turbo_fan here — that would let the controller clear a boost the user
        # set by hand in the app. Set `apply_turbo_fan=True` (autonomous boost
        # control) only once the operator opts in; the requested fan is kept
        # (turbo_fan coexists with fan=max).
        kwargs = {
            "running": bool(power),
            "mode": _MODE.get(mode, 2),
            "fan_speed": _FAN.get(fan_speed, 102),
            "turbo": bool(turbo),
            "fahrenheit": True,        # unit displays F (16C shows as 60F floor)
            "cloud": self._cloud,
        }
        if getattr(self.cfg, "midea_apply_turbo_fan", False):
            kwargs["turbo_fan"] = bool(turbo)   # autonomous boost (opt-in)
        # mirror the LAN guard: setting a target while commanding FAN_ONLY makes
        # the Duo snap back to COOL, so free-cool would never stick off-LAN.
        if mode != "fan":
            kwargs["target_temperature"] = c
        self._dev.set_state(**kwargs)
        # the library auto-refreshes after set_state (needs_refresh()==True), so
        # self._dev.state already holds the DEVICE-CONFIRMED values here. Read
        # them back and return a confirmed MideaState (same construction as
        # refresh()) so the caller can tell intent from what actually landed.
        try:
            s = self._dev.state
            it = getattr(s, "indoor_temperature", None)
            tt = getattr(s, "target_temperature", None)
            dmode = getattr(s, "mode", None)
            dfan = getattr(s, "fan_speed", None)
            return MideaState(
                indoor_temp_f=c_to_f(it) if it is not None else None,
                target_f=c_to_f(tt) if tt is not None else None,
                power=bool(getattr(s, "running", False)),
                mode=_MODE_INV.get(dmode, str(dmode)),
                online=bool(getattr(s, "online", True)),
                fan_speed=_FAN_INV.get(dfan, str(dfan) if dfan is not None else None),
                turbo=(bool(getattr(s, "turbo", False))
                   or bool(getattr(s, "turbo_fan", False))),   # this unit's boost = turbo_fan
            )
        except Exception:
            return None
