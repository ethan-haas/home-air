#!/usr/bin/env python
"""One-time Midea setup: find the office AC on the LAN + get its local token/key.

Midea local control needs a per-device token+key that you fetch ONCE from the
Midea/NetHome Plus cloud using your app account. After this prints them, paste
into .env (MIDEA_HOST / MIDEA_ID / MIDEA_TOKEN / MIDEA_KEY) and the service runs
fully local (no cloud) thereafter.

  python scripts/discover_midea.py --account you@email --password 'pw'
  python scripts/discover_midea.py --account ... --password ... --ip 192.168.1.50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def run(account: str, password: str, ip: str | None) -> None:
    from msmart.discover import Discover

    if ip:
        dev = await Discover.discover_single(ip, account=account, password=password)
        devices = [dev] if dev else []
    else:
        devices = await Discover.discover(account=account, password=password)

    if not devices:
        print("No Midea devices found. Check the AC is on Wi-Fi and on this LAN.")
        return

    for d in devices:
        print("-" * 60)
        print(f"name:   {getattr(d, 'name', '?')}")
        print(f"type:   0x{getattr(d, 'type', 0):02X}")
        print(f"MIDEA_HOST={getattr(d, 'ip', '')}")
        print(f"MIDEA_ID={getattr(d, 'id', '')}")
        print(f"MIDEA_TOKEN={getattr(d, 'token', '') or ''}")
        print(f"MIDEA_KEY={getattr(d, 'key', '') or ''}")
    print("-" * 60)
    print("Paste the matching block into Home_Air/.env")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="Midea/NetHome Plus email")
    ap.add_argument("--password", required=True)
    ap.add_argument("--ip", default=None, help="AC IP if broadcast discovery fails")
    args = ap.parse_args()
    asyncio.run(run(args.account, args.password, args.ip))


if __name__ == "__main__":
    main()
