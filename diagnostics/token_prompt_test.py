#!/usr/bin/env python3
"""
Diagnostic for the "TV skipped as not-in-art-mode while a pairing prompt is on
screen" report.

Answers three questions we currently only have theories about:

  1. While an approval prompt is displayed, what does the TV report for
     PowerState and for get_artmode_status?
  2. When you approve a prompt raised by an ART-channel connection, does the TV
     hand back a token anywhere we could see?
  3. If it does, does the library save it?

Safety:
  - Your real token file is never opened for writing. It is copied into a temp
    directory and only the copies are ever passed to the library.
  - Nothing is uploaded, deleted, or changed on the TV.
  - It DOES deliberately raise an approval prompt on the TV, and will likely
    leave a junk entry in the TV's Device Connection Manager to clean up.

Run:  .venv-test/bin/python diagnostics/token_prompt_test.py [ip] [--seconds N]
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from samsungtvws import connection as conn_mod
from samsungtvws.async_art import SamsungTVAsyncArt

REAL_TOKEN_DIR = Path(__file__).resolve().parent.parent / "tokens"
JUNK_TOKEN = "00000000"
CLIENT_NAME = "FrameTVArtworkSync"

EVENTS = []          # every websocket event the library sees, with a timestamp
_orig_event = conn_mod.SamsungTVWSBaseConnection._websocket_event


def _recording_event(self, event, response):
    EVENTS.append((time.time(), event, response))
    return _orig_event(self, event, response)


conn_mod.SamsungTVWSBaseConnection._websocket_event = _recording_event


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


async def read_state(tv, label):
    """Read PowerState (REST) and art mode (websocket), reporting failures."""
    try:
        info = await tv._get_device_info()
        power = info.get("device", {}).get("PowerState", "<missing>") if info else "<no device info>"
    except Exception as e:
        power = f"<raised {type(e).__name__}>"
    try:
        art = await asyncio.wait_for(tv.get_artmode(), timeout=8)
    except Exception as e:
        art = f"<raised {type(e).__name__}>"
    print(f"  {label:<22} PowerState={power!r:<20} get_artmode={art!r}")
    return power, art


async def connect(host, token_file, timeout=10):
    return await asyncio.to_thread(
        SamsungTVAsyncArt, host=host, port=8002,
        token_file=str(token_file), timeout=timeout, name=CLIENT_NAME,
    )


def dump_events(since=0.0):
    """Print every websocket event seen, and where a token appeared in it."""
    found = False
    for ts, event, response in EVENTS:
        if ts < since:
            continue
        found = True
        data = response.get("data", {}) or {}
        if not isinstance(data, dict):
            print(f"  {event}")
            continue
        print(f"  {event}   data.token={data.get('token')!r}")
        for c in (data.get("clients") or []):
            mark = "  <-- us" if c.get("id") == data.get("id") else ""
            print(f"     client {c.get('id')!r} "
                  f"attributes.token={(c.get('attributes') or {}).get('token')!r}{mark}")
    if not found:
        print("  (no events captured)")


async def probe_art(host, token_file, label):
    """ONE art-channel attempt. Each attempt raises at most one prompt."""
    mark = time.time()
    print(f"\n--- {label} ---")
    try:
        tv = await connect(host, token_file)
        await read_state(tv, "state:")
        try:
            await tv.close()
        except Exception:
            pass
    except Exception as e:
        print(f"  connect raised: {type(e).__name__}: {e}")
    dump_events(since=mark)
    return mark


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", help="IP address of the TV")
    ap.add_argument("--seconds", type=int, default=90,
                    help="how long to poll while the prompt is up (default 90)")
    args = ap.parse_args()
    host = args.ip

    real_token = REAL_TOKEN_DIR / f"tv_{host.replace('.', '_')}.txt"
    tmp = Path(tempfile.mkdtemp(prefix="frametv-test-"))
    good_copy = tmp / "good.txt"
    junk_copy = tmp / "junk.txt"
    junk_copy.write_text(JUNK_TOKEN)

    banner(f"Frame TV token/prompt diagnostic — {host}")
    print(f"Temp dir (real token never written): {tmp}")
    if real_token.exists():
        shutil.copy2(real_token, good_copy)
        print(f"Copied real token from {real_token} ({good_copy.read_text().strip()[:4]}...)")
    else:
        good_copy = None
        print(f"No real token at {real_token} — skipping the baseline phase.")

    # ---- Phase 1: baseline with the working token -------------------------
    if good_copy:
        banner("PHASE 1  baseline, using a COPY of your working token")
        try:
            tv = await connect(host, good_copy)
            await read_state(tv, "with valid token:")
            await tv.close()
        except Exception as e:
            print(f"  baseline connect failed: {type(e).__name__}: {e}")

    # ---- Phase 2: junk token, ONE attempt ------------------------------
    banner("PHASE 2  one connection with a JUNK token — expect ONE prompt")
    print(">>> Watch the TV. Do NOT approve yet. <<<")
    await probe_art(host, junk_copy, "attempt 1: junk token, before approval")
    print(f"\n  junk token file: {junk_copy.read_text().strip()!r}")

    # REST only while we wait — no websocket, so no further prompts.
    print(f"\nPolling REST PowerState only for {args.seconds}s (raises no prompt).")
    print(">>> APPROVE the prompt on the TV now. <<<\n")
    from samsungtvws.async_rest import SamsungTVAsyncRest
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        rest = SamsungTVAsyncRest(host=host, port=8002, session=sess)
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            try:
                d = await rest.rest_device_info()
                power = (d.get("device", {}) or {}).get("PowerState", "<missing>")
            except Exception as e:
                power = f"<raised {type(e).__name__}>"
            print(f"  PowerState={power!r}")
            await asyncio.sleep(10)

    # ---- Phase 3: one more attempt, after approval ----------------------
    banner("PHASE 3  one more attempt with whatever is in the token file now")
    await probe_art(host, junk_copy, "attempt 2: after approval")
    final = junk_copy.read_text().strip()
    print(f"\n  junk token file now: {final!r}  "
          f"({'CHANGED - a token was saved' if final != JUNK_TOKEN else 'unchanged'})")

    banner("SUMMARY — please paste everything above")
    print(f"real token file untouched: {real_token} "
          f"({'still present' if real_token.exists() else 'MISSING - unexpected'})")
    print(f"temp dir left for inspection: {tmp}")
    print("\nRemember to remove the junk device entry from the TV:")
    print("  Settings > General > External Device Manager > Device Connection Manager")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        sys.exit(130)
