#!/usr/bin/env python3
"""
Read-only observation of what a Frame TV reports for the two checks that
is_in_art_mode() depends on: REST PowerState, and the art app's
get_artmode_status reply (raw, so we can see the actual field names).

One connection, opened once and reused. Nothing is written to the TV.
No token file is written: the token is passed in memory.

  python diagnostics/artmode_watch.py <ip> <token> [--seconds N] [--interval N]
"""
import argparse, asyncio, json, sys, time
from samsungtvws import connection as conn_mod
from samsungtvws.async_art import SamsungTVAsyncArt

EVENTS = []
_orig = conn_mod.SamsungTVWSBaseConnection._websocket_event
def _rec(self, event, response):
    EVENTS.append((time.time(), event, response))
    return _orig(self, event, response)
conn_mod.SamsungTVWSBaseConnection._websocket_event = _rec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip"); ap.add_argument("token")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--interval", type=int, default=6)
    a = ap.parse_args()

    # token= passes it in memory; token_file stays None so nothing is written.
    tv = await asyncio.to_thread(
        SamsungTVAsyncArt, host=a.ip, port=8002, token=a.token,
        timeout=10, name="FrameTVArtworkSync")

    print(f"observing {a.ip} for {a.seconds}s\n")
    info = await tv._get_device_info()
    dev = info.get("device", {}) if info else {}
    print(f"device: model={dev.get('model')!r} PowerState={dev.get('PowerState')!r} "
          f"FrameTVSupport={dev.get('FrameTVSupport')!r}")
    print(f"device_info empty? {not bool(info)}\n")

    print(f"{'t':>5}  {'PowerState':<12} {'get_artmode':<24} raw get_artmode_status reply")
    print("-" * 100)
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        mark = len(EVENTS)
        i = await tv._get_device_info()
        power = (i.get("device", {}) or {}).get("PowerState", "<missing>") if i else "<EMPTY DICT>"
        try:
            art = repr(await asyncio.wait_for(tv.get_artmode(), timeout=10))
        except Exception as e:
            art = f"<raised {type(e).__name__}>"
        raw = ""
        for _, ev, resp in EVENTS[mark:]:
            if ev == "d2d_service_message":
                try:
                    d = json.loads(resp.get("data", "{}"))
                except Exception:
                    continue
                if "artmode_status" in d.get("event", ""):
                    raw = json.dumps({k: v for k, v in d.items() if k != "request_id"})
        print(f"{int(time.time()-t0):>5}  {power!r:<12} {art:<24} {raw}")
        await asyncio.sleep(a.interval)

    try:
        await tv.close()
    except Exception:
        pass
    kinds = {}
    for _, ev, _ in EVENTS:
        kinds[ev] = kinds.get(ev, 0) + 1
    print(f"\nevent types seen: {kinds}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
