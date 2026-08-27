#!/usr/bin/env python3
"""
Samsung Frame TV Artwork Sync Script
Syncs artwork from a local directory to multiple Samsung Frame TVs
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Set, Dict, Optional, Any
import time
import hashlib
import datetime
import zoneinfo
import socket

from samsungtvws import async_connection
from samsungtvws.async_art import SamsungTVAsyncArt
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.connection import SamsungTVWSConnection
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from samsungtvws.remote import REMOTE_ENDPOINT, SendRemoteKey
from samsung_ip_control import (
    IPControlAuthError,
    IPControlError,
    IPControlTransportError,
    SamsungIPControl,
)

# Track websockets created during a channel handshake so we can close orphans.
#
# Upstream's async open() assigns self.connection only *after* ms.channel.connect
# arrives. We bound that wait with asyncio.wait_for (a TV can accept the socket
# and never send the event), but cancelling mid-handshake leaves the websocket
# open with no reference on the client object — so close() can't reap it and the
# fd leaks. Wrapping the connect() upstream calls lets us record each socket the
# moment it exists; _bounded_art_call() then closes any that wasn't adopted.
#
# The recording box is passed via a ContextVar: asyncio.wait_for runs the inner
# coroutine in a child Task, which copies the current context, so the child sees
# the same list object the caller created. If upstream ever stops routing through
# this name the box simply stays empty and we're back to the old behaviour.
_handshake_sockets: contextvars.ContextVar = contextvars.ContextVar('handshake_sockets', default=None)

# TVs whose guarded Art-token refresh was already offered but not completed.
# Keep this in memory so an unattended TV is not prompted every sync cycle. A
# later successful Art connection clears the entry, as does a service restart.
_ART_TOKEN_REFRESH_ATTEMPTED: Set[str] = set()

if callable(getattr(async_connection, 'connect', None)):
    _upstream_ws_connect = async_connection.connect

    async def _tracking_ws_connect(*args, **kwargs):
        conn = await _upstream_ws_connect(*args, **kwargs)
        box = _handshake_sockets.get()
        if box is not None:
            box.append(conn)
        return conn

    async_connection.connect = _tracking_ws_connect
else:
    # Emitted before basicConfig runs, so use warning — the last-resort handler
    # still surfaces it on stderr.
    logging.getLogger(__name__).warning(
        "samsungtvws.async_connection.connect not found; handshake socket tracking "
        "disabled (upstream API changed). Timed-out handshakes may leak a socket."
    )

# A Frame may broadcast Art requests and other clients' disconnects to a newly
# opened socket before sending that socket's own ms.channel.connect event. The
# upstream connection routine treats any unrecognized first event as a failed
# authentication handshake. That caused valid tokens to look stale and could
# launch an unnecessary pairing flow when another controller (such as Home
# Assistant) was active. These events carry no definitive authentication result,
# so keep waiting for connect within _bounded_art_call's overall deadline.
_ART_STARTUP_BROADCAST_EVENTS = (
    'art_app_request',
    'd2d_service_message',
    'ms.channel.clientDisconnect',
)
if hasattr(async_connection, 'IGNORE_EVENTS_AT_STARTUP'):
    async_connection.IGNORE_EVENTS_AT_STARTUP = tuple(dict.fromkeys(
        (*async_connection.IGNORE_EVENTS_AT_STARTUP, *_ART_STARTUP_BROADCAST_EVENTS)
    ))

_upstream_async_connection_open = async_connection.SamsungTVWSAsyncConnection.open


async def _open_art_channel_ignoring_broadcasts(self):
    """Accept ms.channel.connect as a complete Art handshake.

    Newer Frames do not reliably send the additional ms.channel.ready event.
    The base open still verifies connect/unauthorized/timeout and captures any
    issued token. SamsungTVAsyncArt's listener can safely consume a late ready
    event alongside the normal Art responses.
    """
    return await _upstream_async_connection_open(self)


SamsungTVAsyncArt.open = _open_art_channel_ignoring_broadcasts

# Token acquisition is explicit in _acquire_token(). Upstream's Art-client
# constructor otherwise creates a high-level remote client here, which performs
# a hidden REST probe and may open another pairing window on newer TVs. Besides
# duplicating prompts, that extra socket can contribute to the Art channel
# saturation we observed on a 2025 Frame. Constructing an Art client must never
# initiate pairing by itself.
def _skip_implicit_art_token_pairing(self):
    return None

SamsungTVAsyncArt.get_token = _skip_implicit_art_token_pairing

from pysolar.solar import get_altitude

# Configure logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment variables with defaults
ARTWORK_DIR = os.getenv('ARTWORK_DIR', '/artwork')
TV_IPS = os.getenv('TV_IPS', '').split(',')
TV_IPS = [ip.strip() for ip in TV_IPS if ip.strip()]
SYNC_INTERVAL_MINUTES = int(os.getenv('SYNC_INTERVAL_MINUTES', '5'))
MATTE_STYLE = os.getenv('MATTE_STYLE', 'none')
TOKEN_DIR = os.getenv('TOKEN_DIR', '/tokens')

# Optional slideshow override settings (if any are set, all are used with defaults)
SLIDESHOW_ENABLED = os.getenv('SLIDESHOW_ENABLED', '').lower() in ('true', '1', 'yes')
SLIDESHOW_INTERVAL = int(os.getenv('SLIDESHOW_INTERVAL', '15'))
SLIDESHOW_TYPE = os.getenv('SLIDESHOW_TYPE', 'shuffle').lower()
SLIDESHOW_OVERRIDE = os.getenv('SLIDESHOW_ENABLED') or os.getenv('SLIDESHOW_INTERVAL') or os.getenv('SLIDESHOW_TYPE')

# Optional brightness setting (0-50, where 50 is brightest)
BRIGHTNESS = os.getenv('BRIGHTNESS', '')
BRIGHTNESS = int(BRIGHTNESS) if BRIGHTNESS else None

# Optional solar-based brightness settings
SOLAR_BRIGHTNESS_ENABLED = os.getenv('SOLAR_BRIGHTNESS_ENABLED', '').lower() in ('true', '1', 'yes')
LOCATION_LATITUDE = float(os.getenv('LOCATION_LATITUDE', '0')) if os.getenv('LOCATION_LATITUDE') else None
LOCATION_LONGITUDE = float(os.getenv('LOCATION_LONGITUDE', '0')) if os.getenv('LOCATION_LONGITUDE') else None
LOCATION_TIMEZONE = os.getenv('LOCATION_TIMEZONE', 'UTC')
BRIGHTNESS_MIN = int(os.getenv('BRIGHTNESS_MIN', '2'))
BRIGHTNESS_MAX = int(os.getenv('BRIGHTNESS_MAX', '10'))

# Optional cleanup setting
REMOVE_UNKNOWN_IMAGES = os.getenv('REMOVE_UNKNOWN_IMAGES', '').lower() in ('true', '1', 'yes')

# Client name sent to the TV during WebSocket handshake
CLIENT_NAME = os.getenv('CLIENT_NAME', 'FrameTVArtworkSync')

# Optional auto-off settings (turn off TVs at a specific time when in art mode)
AUTO_OFF_TIME = os.getenv('AUTO_OFF_TIME', '')  # 24-hour format, e.g., "22:00"
AUTO_OFF_GRACE_HOURS = float(os.getenv('AUTO_OFF_GRACE_HOURS', '2'))  # Hours after AUTO_OFF_TIME to keep trying
# Optional auto-on setting. It is attempted once per scheduled day.
AUTO_ON_TIME = os.getenv('AUTO_ON_TIME', '')  # 24-hour format, e.g., "07:00"
WOL_BROADCAST_ADDRESS = os.getenv('WOL_BROADCAST_ADDRESS', '255.255.255.255')
WOL_PORT = int(os.getenv('WOL_PORT', '9'))
WOL_REPEAT = int(os.getenv('WOL_REPEAT', '3'))

# Dry run mode (set by command line argument)
DRY_RUN = False

# Validate brightness range
if BRIGHTNESS_MIN >= BRIGHTNESS_MAX:
    logger.error(f"Invalid brightness range: BRIGHTNESS_MIN ({BRIGHTNESS_MIN}) must be less than BRIGHTNESS_MAX ({BRIGHTNESS_MAX}).")
    sys.exit(1)

# Supported image formats
SUPPORTED_FORMATS = {'.jpg', '.jpeg', '.png'}

# Timeout and delay constants (in seconds)
CONNECTION_TIMEOUT = float(os.getenv('CONNECTION_TIMEOUT', '10.0'))
AUTH_TIMEOUT = float(os.getenv('AUTH_TIMEOUT', '30.0'))
KEEPALIVE_INTERVAL = int(os.getenv('KEEPALIVE_INTERVAL', '60'))  # seconds
CONNECT_MAX_ATTEMPTS = int(os.getenv('CONNECT_MAX_ATTEMPTS', '3'))
CHANNEL_DROP_RETRY_DELAY = float(os.getenv('CHANNEL_DROP_RETRY_DELAY', '3.0'))
PAIRING_MAX_RETRIES = int(os.getenv('PAIRING_MAX_RETRIES', '5'))
PAIRING_RETRY_DELAY = float(os.getenv('PAIRING_RETRY_DELAY', '5.0'))
FAST_PAIRING_TIMEOUT_THRESHOLD = 3.0
# Generic art-app request timeout. Slower TVs take most of 10s just to answer
# get_slideshow_status, so the old hard-coded 10 was right on the edge.
API_TIMEOUT = int(os.getenv('API_TIMEOUT', '20'))
# How long to wait for the TV's uploaded-image list. Upstream's default is 4s,
# which is far too short on a TV with a large art collection: serialising the
# list is done TV-side and the reply arrives in one big message. A 2022 Frame
# with ~500 images sends ~766KB and takes about 10 seconds. Timing out here is
# not harmless — the caller can't tell "slow" from "empty", so the whole folder
# looks missing and gets re-uploaded every cycle.
CONTENT_LIST_TIMEOUT = int(os.getenv('CONTENT_LIST_TIMEOUT', '45'))

# Validate intervals — values < 1 cause busy/infinite loops in wait_until_next_sync
if SYNC_INTERVAL_MINUTES < 1:
    logger.error(f"SYNC_INTERVAL_MINUTES must be >= 1 (got {SYNC_INTERVAL_MINUTES})")
    sys.exit(1)
if KEEPALIVE_INTERVAL < 1:
    logger.error(f"KEEPALIVE_INTERVAL must be >= 1 (got {KEEPALIVE_INTERVAL})")
    sys.exit(1)
UPLOAD_DELAY = 1.0
DELETE_DELAY = 0.5
UPLOAD_ATTEMPTS = 2
POWER_OFF_VERIFY_DELAY = 5.0  # Seconds to wait after a power-off before checking it took effect
POWER_ON_VERIFY_TIMEOUT = float(os.getenv('POWER_ON_VERIFY_TIMEOUT', '30.0'))
_AUTO_ON_LAST_ATTEMPT: Optional[datetime.date] = None


def power_control_configured() -> bool:
    """Return whether this installation needs automatic IP Control pairing."""
    return bool(AUTO_ON_TIME or AUTO_OFF_TIME)


def is_within_schedule_window(schedule_time: str, grace_hours: float, label: str) -> bool:
    """
    Check if the current time is within a daily schedule window.

    The window starts at schedule_time and extends for grace_hours, including
    windows that cross midnight.
    """
    if not schedule_time:
        return False

    try:
        # Parse the configured off time
        off_hour, off_minute = map(int, schedule_time.split(':'))

        # Get current time in the configured timezone
        tz = zoneinfo.ZoneInfo(LOCATION_TIMEZONE)
        now = datetime.datetime.now(tz)

        # Create today's off time
        today_off_time = now.replace(hour=off_hour, minute=off_minute, second=0, microsecond=0)

        # Calculate the end of the grace period
        grace_end = today_off_time + datetime.timedelta(hours=grace_hours)

        # Handle the case where the window spans midnight
        # If we're before today's off time, check if we're in yesterday's window
        if now < today_off_time:
            yesterday_off_time = today_off_time - datetime.timedelta(days=1)
            yesterday_grace_end = yesterday_off_time + datetime.timedelta(hours=grace_hours)
            if yesterday_off_time <= now < yesterday_grace_end:
                logger.debug(f"Within {label} window (from yesterday): {yesterday_off_time.strftime('%H:%M')} to {yesterday_grace_end.strftime('%H:%M')}")
                return True

        # Check if we're in today's window
        if today_off_time <= now < grace_end:
            logger.debug(f"Within {label} window: {today_off_time.strftime('%H:%M')} to {grace_end.strftime('%H:%M')}")
            return True

        return False

    except Exception as e:
        logger.warning(f"Failed to check {label} window: {e}")
        return False


def is_within_auto_off_window() -> bool:
    """Return whether the current time is in the configured auto-off window."""
    return is_within_schedule_window(AUTO_OFF_TIME, AUTO_OFF_GRACE_HOURS, 'auto-off')


def should_attempt_auto_on() -> bool:
    """Return True once on the first polling cycle after today's on time."""
    global _AUTO_ON_LAST_ATTEMPT
    if not AUTO_ON_TIME:
        return False
    try:
        hour, minute = map(int, AUTO_ON_TIME.split(':'))
        tz = zoneinfo.ZoneInfo(LOCATION_TIMEZONE)
        now = datetime.datetime.now(tz)
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # The loop normally runs exactly once per SYNC_INTERVAL_MINUTES. Add a
        # small margin for connection work so the scheduled edge is not missed.
        attempt_deadline = scheduled + datetime.timedelta(
            minutes=SYNC_INTERVAL_MINUTES + 1
        )
        if not scheduled <= now < attempt_deadline:
            return False
        if _AUTO_ON_LAST_ATTEMPT == scheduled.date():
            return False
        _AUTO_ON_LAST_ATTEMPT = scheduled.date()
        return True
    except Exception as e:
        logger.warning(f"Failed to check auto-on time: {e}")
        return False


def brightness_from_elevation(elevation: float) -> int:
    """
    Calculate brightness from sun elevation angle using atmospheric air mass model.

    This uses a physics-based approach that models how sunlight intensity changes
    as it passes through the atmosphere at different angles. The calculation is
    based on the air mass coefficient and Kasten-Young atmospheric attenuation formula.

    Args:
        elevation: Sun elevation angle in degrees (negative = below horizon)

    Returns:
        Brightness value between BRIGHTNESS_MIN and BRIGHTNESS_MAX
    """
    # If sun is at or below horizon, use minimum brightness
    if elevation <= 0:
        return BRIGHTNESS_MIN

    # Convert elevation to radians for calculation
    import math
    elevation_rad = math.radians(elevation)

    # Calculate air mass (AM) using Kasten-Young formula
    # This provides accurate results at all sun angles, especially near horizon
    # At zenith (90°): AM ≈ 1.0 (shortest path)
    # At 30° elevation: AM ≈ 2.0 (twice the atmosphere)
    air_mass = 1.0 / (math.sin(elevation_rad) + 0.50572 * (elevation + 6.07995)**(-1.6364))

    # Apply Kasten-Young atmospheric attenuation formula
    # Relative irradiance = 0.7^(AM^0.678)
    # This models how atmosphere absorbs/scatters sunlight
    # 0.7 represents typical clear-sky atmospheric transmittance
    relative_irradiance = 0.7 ** (air_mass ** 0.678)

    # Map relative irradiance (0.0 to 1.0) to brightness range
    brightness = BRIGHTNESS_MIN + int((BRIGHTNESS_MAX - BRIGHTNESS_MIN) * relative_irradiance)

    return brightness


def calculate_solar_brightness() -> Optional[int]:
    """
    Calculate brightness based on current sun position.
    Returns brightness value (min to max based on sun elevation angle).
    """
    if not SOLAR_BRIGHTNESS_ENABLED:
        return None

    if LOCATION_LATITUDE is None or LOCATION_LONGITUDE is None:
        logger.warning("Solar brightness enabled but LOCATION_LATITUDE or LOCATION_LONGITUDE not set")
        return None

    try:
        # Get current time in the specified timezone
        tz = zoneinfo.ZoneInfo(LOCATION_TIMEZONE)
        local_time = datetime.datetime.now(tz)
        utc_time = local_time.astimezone(datetime.timezone.utc)

        # Calculate sun elevation angle in degrees
        elevation = get_altitude(LOCATION_LATITUDE, LOCATION_LONGITUDE, utc_time)

        logger.debug(f"Sun elevation at {local_time.strftime('%Y-%m-%d %H:%M %Z')}: {elevation:.2f}°")

        # Calculate brightness from elevation
        brightness = brightness_from_elevation(elevation)

        if elevation <= 0:
            logger.info(f"Sun below horizon (elevation: {elevation:.2f}°), using minimum brightness: {brightness}")
        else:
            logger.info(f"Sun elevation: {elevation:.2f}° -> brightness: {brightness} "
                       f"(min: {BRIGHTNESS_MIN}, max: {BRIGHTNESS_MAX})")

        return brightness

    except Exception as e:
        logger.warning(f"Failed to calculate solar brightness: {e}")
        return None


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing special characters.
    Keeps alphanumeric, spaces, hyphens, underscores, and the file extension dot.
    Collapses multiple spaces into one and strips leading/trailing spaces from the stem.
    """
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()
    # Keep only alphanumeric, spaces, hyphens, underscores
    stem = re.sub(r'[^a-zA-Z0-9 _-]', '', stem)
    # Collapse multiple spaces
    stem = re.sub(r' +', ' ', stem).strip()
    return f"{stem}{ext}" if stem else f"image{ext}"


class TVArtworkSync:
    """Manages artwork synchronization for a Samsung Frame TV"""

    def __init__(self, tv_ip: str) -> None:
        self.tv_ip = tv_ip
        self.tv = None
        self.last_art_mode_status: Optional[str] = None
        self.auto_started = False
        self.token_file = Path(TOKEN_DIR) / f'tv_{tv_ip.replace(".", "_")}.txt'
        self.ip_control_file = Path(TOKEN_DIR) / f'tv_{tv_ip.replace(".", "_")}_ip_control.json'
        self.mac_file = Path(TOKEN_DIR) / f'tv_{tv_ip.replace(".", "_")}_mac.txt'
        self.ip_control = SamsungIPControl(self.tv_ip, self.ip_control_file)
        self.mapping_file = Path(TOKEN_DIR) / f'tv_{tv_ip.replace(".", "_")}_mapping.json'
        self.file_mapping: Dict[str, str] = {}  # filename -> content_id mapping
        self._load_mapping()

    def _save_mac(self, mac: str) -> None:
        """Persist the TV MAC for Wake-on-LAN fallback."""
        normalized = re.sub(r'[^0-9A-Fa-f]', '', mac)
        if len(normalized) != 12:
            return
        self.mac_file.parent.mkdir(parents=True, exist_ok=True)
        self.mac_file.write_text(normalized.lower() + '\n')

    def _load_mac(self) -> Optional[str]:
        try:
            value = re.sub(r'[^0-9A-Fa-f]', '', self.mac_file.read_text())
            return value if len(value) == 12 else None
        except OSError:
            return None

    async def pair_ip_control(self) -> bool:
        """Pair Samsung IP Control and persist its token/working port."""
        if self.ip_control.paired:
            logger.info(f"IP Control already paired for TV {self.tv_ip}")
            return True
        try:
            logger.info(
                f"Waiting for IP Control approval on TV {self.tv_ip}; "
                "the TV must be in normal viewing, not Art Mode"
            )
            await self.ip_control.pair()
            logger.info(f"IP Control paired for TV {self.tv_ip} on port {self.ip_control.port}")
            return True
        except IPControlError as e:
            logger.warning(f"IP Control pairing failed for TV {self.tv_ip}: {e}")
            return False

    async def _pair_missing_ip_control(self, art_mode_status: Any) -> None:
        """Acquire the second Samsung permission during normal sync pairing.

        The IP Control endpoint does not answer pairing requests from Art Mode,
        so defer silently until a sync sees the TV in normal viewing. Pairing
        failure never blocks artwork syncing; a later normal-viewing cycle can
        request approval again.
        """
        if not power_control_configured() or self.ip_control.paired:
            return
        if str(art_mode_status).lower() == 'on':
            logger.info(
                f"IP Control is not paired for TV {self.tv_ip}; authorization "
                "will be requested during a sync while the TV is in normal viewing"
            )
            return
        logger.info(
            f"Requesting the additional IP Control approval for TV {self.tv_ip}"
        )
        await self.pair_ip_control()

    async def _wait_for_port(self, port: int, timeout: float) -> bool:
        """Wait until a TCP port responds, without opening a Samsung channel."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.tv_ip, port), timeout=2
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                return True
            except (asyncio.TimeoutError, OSError):
                await asyncio.sleep(1)
        return False

    async def _send_wol(self) -> bool:
        """Send WOL as a fallback when deep standby closes IP Control."""
        mac = self._load_mac()
        if not mac:
            logger.warning(f"No learned MAC for TV {self.tv_ip}; cannot use WOL fallback")
            return False
        packet = b'\xff' * 6 + bytes.fromhex(mac) * 16

        def send() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for attempt in range(max(1, WOL_REPEAT)):
                    if attempt:
                        time.sleep(0.25)
                    sock.sendto(packet, (WOL_BROADCAST_ADDRESS, WOL_PORT))

        try:
            await asyncio.to_thread(send)
            return True
        except OSError as e:
            logger.warning(f"WOL failed for TV {self.tv_ip}: {e}")
            return False

    def _load_mapping(self) -> None:
        """Load filename to content_id mapping from disk"""
        if self.mapping_file.exists():
            try:
                with open(self.mapping_file, 'r') as f:
                    self.file_mapping = json.load(f)
                logger.debug(f"Loaded mapping for TV {self.tv_ip}: {self.file_mapping}")
            except Exception as e:
                logger.warning(f"Failed to load mapping file: {e}")
                self.file_mapping = {}

    def _save_mapping(self) -> None:
        """Save filename to content_id mapping to disk"""
        try:
            self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.mapping_file, 'w') as f:
                json.dump(self.file_mapping, f, indent=2)
            logger.debug(f"Saved mapping for TV {self.tv_ip}")
        except Exception as e:
            logger.warning(f"Failed to save mapping file: {e}")

    async def connect(self) -> bool:
        """Connect to the TV, handling first-time pairing and stale tokens."""
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            return await self._try_connect()
        except Exception as e:
            logger.warning(f"Unexpected error connecting to TV at {self.tv_ip}: {e}")
            return False

    async def _try_connect(self) -> bool:
        """
        Try to connect to the TV, with bounded retries.

        Pairing-flow phase transitions (acquiring a token after user approval)
        don't count against the attempt budget — _acquire_token has its own
        internal retry loop sized for human reaction time.
        """
        ip_pair_attempted = False
        had_saved_art_token = self.token_file.exists()
        art_token_refresh_attempted = False
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            # Probe before anything else because the pairing flow cannot
            # distinguish "TV unreachable" from "waiting for approval" and
            # would otherwise spend the full human-approval timeout on an
            # offline TV.
            if not await self._is_tv_reachable():
                logger.warning(f"Failed to connect to TV at {self.tv_ip} (TV may be off or unreachable)")
                return False

            # Acquire a token first if we don't have one.
            if not self.token_file.exists():
                if not await self._acquire_token():
                    # _acquire_token() owns the complete human-approval retry
                    # budget. Re-entering it through this connection loop would
                    # multiply PAIRING_MAX_RETRIES by CONNECT_MAX_ATTEMPTS and
                    # produce a long stream of duplicate TV prompts.
                    logger.warning(f"Failed to acquire token for TV {self.tv_ip}")
                    return False

            # Use the token. Keep construction off the event loop for upstream
            # compatibility, although the implicit token-negotiation hook is
            # disabled above and all pairing is handled explicitly.
            t0 = time.monotonic()
            try:
                self.tv = await asyncio.to_thread(
                    SamsungTVAsyncArt,
                    host=self.tv_ip,
                    port=8002,
                    token_file=str(self.token_file),
                    timeout=CONNECTION_TIMEOUT,
                    name=CLIENT_NAME
                )
                # Probe with get_artmode_status, not get_content_list. It's the
                # cheapest art-app request and the most widely supported: 2022
                # Frames answer it in milliseconds but never answer a
                # get_content_list with a null category, which left the probe
                # timing out and the whole TV reported as unreachable.
                #
                # This also opens the art channel, and upstream's async open()
                # awaits the ms.channel.connect event with no timeout of its own
                # (the client's `timeout` only bounds the websocket handshake).
                # A TV that accepts the socket but never sends that event would
                # hang this coroutine forever — and since sync_all_tvs gathers
                # all TVs' connects, that wedges the entire sync loop.
                art_mode_status = await self._bounded_art_call(
                    self.tv.get_artmode, CONNECTION_TIMEOUT
                )
                self.last_art_mode_status = str(art_mode_status).lower()
                _ART_TOKEN_REFRESH_ATTEMPTED.discard(self.tv_ip)
                logger.info(f"Successfully connected to TV at {self.tv_ip} (attempt {attempt})")
                await self._pair_missing_ip_control(art_mode_status)
                return True

            except (asyncio.TimeoutError, AssertionError):
                # _is_tv_reachable already proved port 8002 accepted a TCP
                # connection. Upstream raises AssertionError when its internal
                # get_artmode response wait expires, while our outer handshake
                # deadline raises TimeoutError. Both mean the Art channel did
                # not produce a usable status, not that the TV is off.
                logger.warning(
                    f"Art channel handshake/status request timed out for TV {self.tv_ip} "
                    f"(attempt {attempt}); retrying with the saved token..."
                )
                await self.close()
                if (
                    power_control_configured()
                    and not self.ip_control.paired
                    and not ip_pair_attempted
                ):
                    # Some TVs do not complete the Art channel while showing a
                    # normal HDMI/TV source. That is precisely when IP Control
                    # can be approved, so do not make its automatic pairing
                    # depend on a successful get_artmode response.
                    ip_pair_attempted = True
                    logger.info(
                        f"Art mode could not be determined for TV {self.tv_ip}; "
                        "requesting the missing IP Control approval now"
                    )
                    await self.pair_ip_control()
                await asyncio.sleep(CHANNEL_DROP_RETRY_DELAY)
                continue

            except UnauthorizedError:
                logger.warning(f"Token rejected by TV {self.tv_ip}, deleting and re-pairing...")
                self.token_file.unlink(missing_ok=True)
                continue

            except ConnectionFailure as e:
                # Upstream raises ConnectionFailure for ms.channel.timeOut and
                # ms.channel.clientDisconnect events during the initial handshake.
                # ms.channel.timeOut is ambiguous: it can mean a rejected token,
                # but also that the TV did not accept the channel in time. Only
                # UnauthorizedError is definitive enough to destroy a persisted
                # token. Preserve it here so a transient TV-side timeout cannot
                # turn an unattended sync into a fresh pairing loop.
                elapsed = time.monotonic() - t0
                event = ""
                if e.args and isinstance(e.args[0], dict):
                    event = e.args[0].get("event", "")

                logger.warning(
                    f"Channel drop for TV {self.tv_ip} ({event}, attempt {attempt}) "
                    f"after {elapsed:.2f}s; preserving its saved token..."
                )
                if (
                    event == "ms.channel.timeOut"
                    and power_control_configured()
                    and not self.ip_control.paired
                    and not ip_pair_attempted
                ):
                    ip_pair_attempted = True
                    logger.info(
                        f"Art mode could not be determined for TV {self.tv_ip}; "
                        "requesting the missing IP Control approval now"
                    )
                    await self.pair_ip_control()

                if (
                    event == "ms.channel.timeOut"
                    and had_saved_art_token
                    and not art_token_refresh_attempted
                    and self.tv_ip not in _ART_TOKEN_REFRESH_ATTEMPTED
                    and await self._reports_powered_on()
                ):
                    art_token_refresh_attempted = True
                    _ART_TOKEN_REFRESH_ATTEMPTED.add(self.tv_ip)
                    if await self._refresh_art_token_once():
                        # The failed saved-token request may already have shown
                        # an ineffective approval prompt. Reconnect immediately
                        # with the replacement; do not repeat the stale token.
                        continue
                    # One guarded tokenless request is enough for this cycle.
                    # Avoid a confusing stream of approval prompts when nobody
                    # is present to approve it.
                    return False
                await asyncio.sleep(CHANNEL_DROP_RETRY_DELAY)
                continue

            except Exception as e:
                # Include the type: upstream asserts on a missing response, and a
                # bare AssertionError stringifies to "" — which logged as an
                # empty reason and told nobody anything.
                logger.warning(f"Failed to connect to TV at {self.tv_ip}: {type(e).__name__}: {e}")
                return False

        logger.warning(f"Giving up connecting to TV at {self.tv_ip} after {CONNECT_MAX_ATTEMPTS} attempts")
        return False

    async def _reports_powered_on(self) -> bool:
        """Return True only when an authoritative source says the TV is awake."""
        if self.ip_control.paired:
            try:
                power_state = await self.ip_control.get_power_state()
                if power_state == 'powerOn':
                    return True
                if power_state == 'powerOff':
                    return False
            except (IPControlError, TypeError):
                pass

        if self.tv is None:
            return False
        try:
            device_info = await self.tv._get_device_info()
            return device_info.get("device", {}).get("PowerState") == "on"
        except Exception:
            return False

    def _replace_art_token(self, token: bytes) -> None:
        """Atomically install a replacement Art token."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.token_file.parent,
                prefix=self.token_file.name + ".replace-",
                delete=False,
            ) as temporary:
                temporary.write(token)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(self.token_file)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def _refresh_art_token_once(self) -> bool:
        """Try one tokenless pairing request without removing the saved token."""
        if not self.token_file.exists():
            logger.warning(f"No saved Art token to refresh for TV {self.tv_ip}")
            return False

        await self.close()
        self.tv = None

        # Pair into an isolated temporary path. The working token remains on
        # disk throughout, so a cancellation, process crash, or container
        # restart cannot turn a guarded refresh into lost authorization.
        with tempfile.TemporaryDirectory(
            dir=self.token_file.parent,
            prefix=self.token_file.name + ".refresh-",
        ) as refresh_dir:
            refresh_token_file = Path(refresh_dir) / "token.txt"

            logger.warning(
                f"Saved Art token was not accepted by TV {self.tv_ip}; "
                "approve the next TV prompt to refresh it"
            )
            for refresh_attempt in (1, 2):
                started = time.monotonic()
                try:
                    await asyncio.to_thread(
                        self._pair_via_remote_channel, refresh_token_file
                    )
                except Exception as exc:
                    elapsed = time.monotonic() - started
                    event = ""
                    if exc.args and isinstance(exc.args[0], dict):
                        event = exc.args[0].get("event", "")
                    if (
                        refresh_attempt == 1
                        and event == "ms.channel.timeOut"
                        and elapsed < FAST_PAIRING_TIMEOUT_THRESHOLD
                    ):
                        # Some 2025 Frames display the approval prompt but close the
                        # requesting socket in under a second — too quickly for a
                        # person to respond. Give approval time to register, then
                        # make exactly one more tokenless request. This is separate
                        # from normal first-pairing's multi-attempt loop so a stale
                        # token cannot create a stream of prompts.
                        logger.info(
                            f"TV {self.tv_ip} closed the Art pairing request after "
                            f"{elapsed:.2f}s; waiting {PAIRING_RETRY_DELAY:g}s for "
                            "approval before one final refresh attempt"
                        )
                        await asyncio.sleep(PAIRING_RETRY_DELAY)
                        continue
                    logger.warning(f"Guarded Art token refresh failed for TV {self.tv_ip}: {exc}")
                break

            try:
                replacement_token = refresh_token_file.read_bytes()
            except OSError:
                replacement_token = b""

            if replacement_token.strip():
                try:
                    self._replace_art_token(replacement_token)
                except OSError as exc:
                    logger.error(
                        f"Replacement Art token was received but could not be saved "
                        f"for TV {self.tv_ip}: {exc}"
                    )
                    return False
                logger.info(f"Replacement Art token received for TV {self.tv_ip}; continuing")
                await asyncio.sleep(2)
                return True

        logger.warning(
            f"Guarded Art token refresh was not approved for TV {self.tv_ip}; "
            "kept the existing token"
        )
        return False

    async def _bounded_art_call(self, make_coro, timeout: float) -> Any:
        """
        Run an art-app call — which opens the art channel on first use — under a
        timeout, closing any websocket upstream created during the handshake but
        never adopted.

        make_coro is a zero-arg callable returning the coroutine to await, so the
        coroutine is created inside the ContextVar scope set up here.

        See the _handshake_sockets comment at the top of this file for why the
        orphan exists. On the success path the socket *is* adopted, so the
        identity check leaves it alone and close() owns it as usual.
        """
        box: List[Any] = []
        ctx_token = _handshake_sockets.set(box)
        try:
            return await asyncio.wait_for(make_coro(), timeout=timeout)
        finally:
            _handshake_sockets.reset(ctx_token)
            adopted = getattr(self.tv, 'connection', None)
            for sock in box:
                if sock is adopted:
                    continue
                try:
                    await sock.close()
                    logger.debug(f"Closed orphaned handshake socket for TV {self.tv_ip}")
                except Exception:
                    pass

    async def _is_tv_reachable(self) -> bool:
        """Probe the TV's websocket port with a plain TCP connect."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.tv_ip, 8002),
                timeout=CONNECTION_TIMEOUT,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return True
        except (asyncio.TimeoutError, OSError):
            return False

    def _pair_via_remote_channel(self, token_file: Optional[Path] = None) -> None:
        """
        Open the remote-control channel to trigger pairing and capture the token.

        This is the channel that issues tokens: on approval the TV returns one in
        the ms.channel.connect payload, and upstream's _check_for_token writes it
        to the token file. The art channel does *not* — its connect payload only
        carries the client list, so no token is ever saved.

        Use the upstream base connection rather than SamsungTVWS itself. The
        high-level constructor performs a hidden REST model probe and, on 2024+
        TVs, silently calls open() once during construction. Calling open() here
        afterward then creates two pairing windows, while the constructor hides
        the first failure; two 30-second socket waits can look like one attempt
        hung for over a minute. The base connection has the same remote endpoint
        and token handling but opens exactly once when explicitly requested.

        Blocking (websocket-client), so call it via asyncio.to_thread.
        """
        pairing_token_file = token_file or self.token_file
        remote = SamsungTVWSConnection(
            host=self.tv_ip,
            endpoint=REMOTE_ENDPOINT,
            port=8002,
            token_file=str(pairing_token_file),
            timeout=AUTH_TIMEOUT,
            name=CLIENT_NAME,
        )
        try:
            # Blocks until the user approves on the TV (or the timeout expires).
            remote.open()
        finally:
            try:
                remote.close()
            except Exception:
                pass

    async def _acquire_token(self) -> bool:
        """
        Trigger first-time pairing and wait for the TV to issue a token.

        Retries internally with a delay sized for human reaction time, since
        the user has to physically approve the prompt on the TV. Returns True
        if a token was written to disk.
        """
        for pairing_attempt in range(1, PAIRING_MAX_RETRIES + 1):
            if pairing_attempt == 1:
                logger.info(f"No token for TV {self.tv_ip}, waiting for pairing approval on TV...")
            else:
                logger.info(f"Retrying pairing for TV {self.tv_ip} ({pairing_attempt}/{PAIRING_MAX_RETRIES})...")
                await asyncio.sleep(PAIRING_RETRY_DELAY)

            # Pair on the remote-control channel — the only one that issues
            # tokens. Run it in a thread: it's the blocking client, and it waits
            # up to AUTH_TIMEOUT for the user to approve on the TV.
            try:
                await asyncio.to_thread(self._pair_via_remote_channel)
            except Exception as e:
                # Upstream swallows token-write failures and model-year parse errors
                # into its own debug logs, so this is often the only surface for a
                # non-writable /tokens mount. Don't hide it behind LOG_LEVEL=DEBUG.
                logger.warning(
                    f"Pairing attempt {pairing_attempt} for TV {self.tv_ip} "
                    f"(remote channel) failed: {e}"
                )

            if self.token_file.exists():
                logger.info(f"Token received for TV {self.tv_ip}")
                await asyncio.sleep(2)  # Let TV finalize before we reconnect
                return True

            # No token this round — drop any half-open channel so the next
            # attempt starts from a clean connection rather than reusing one
            # the TV may already consider dead.
            await self.close()
            self.tv = None

        logger.warning(
            f"No token for TV {self.tv_ip} after {PAIRING_MAX_RETRIES} pairing attempts. "
            f"If you approved the prompt on the TV, check that {TOKEN_DIR} is writable "
            f"by the container and re-run with LOG_LEVEL=DEBUG."
        )
        return False

    async def is_in_art_mode(self, *, require_positive: bool = False) -> bool:
        """Check whether the TV is in Art Mode.

        Artwork sync preserves the historical optimistic fallback when status is
        unavailable. Destructive actions pass require_positive=True and fail
        closed unless the TV explicitly reports Art Mode on.
        """
        try:
            # Read device info ourselves rather than calling tv.on(). Upstream's
            # _get_device_info() swallows every exception and returns {}, and on()
            # then reads that as PowerState 'off' — so a REST blip is indistinguishable
            # from a powered-off TV, and we would silently skip a TV that is sitting
            # there in art mode. That path also bypasses the except below, which
            # exists precisely to keep syncing when the state is unknown.
            device_info = await self.tv._get_device_info()
            if not device_info:
                logger.warning(
                    f"Could not read device info for TV {self.tv_ip} (REST call failed "
                    f"or returned nothing)"
                )
                if require_positive:
                    return self.last_art_mode_status == 'on'
                return not require_positive

            power_state = device_info.get("device", {}).get("PowerState", "unknown")
            device = device_info.get("device", {})
            if mac := device.get("wifiMac") or device.get("networkMac"):
                self._save_mac(mac)
            # 2025 Frames can report REST PowerState=standby while visibly in
            # Art Mode. When paired, powerControl is authoritative: Art Mode
            # reports powerOn and true standby reports powerOff.
            semantic_power = None
            if self.ip_control.paired:
                try:
                    semantic_power = await self.ip_control.get_power_state()
                except IPControlError as e:
                    logger.debug(f"IP Control power-state read failed for TV {self.tv_ip}: {e}")
            if semantic_power == 'powerOff' or (semantic_power is None and power_state != "on"):
                logger.info(f"Skipping TV {self.tv_ip}: PowerState={power_state}")
                return False

            # Check if TV is in art mode
            art_mode_status = await self.tv.get_artmode()
            self.last_art_mode_status = str(art_mode_status).lower()
            is_art_mode = art_mode_status == 'on'

            if not is_art_mode:
                # Logged at info, not debug, so a skip says *why*. Deliberately
                # factual: a TV legitimately in use reports 'off' every cycle for
                # as long as someone is watching, so this must not editorialise
                # about stale tokens — see the troubleshooting note in the README
                # for the case where a TV reports 'off' while visibly in art mode.
                logger.info(f"TV {self.tv_ip} reports art mode status: {art_mode_status}")
            else:
                logger.debug(f"TV {self.tv_ip} art mode status: {art_mode_status}")
            return is_art_mode

        except Exception as e:
            logger.debug(f"Could not determine art mode status for TV {self.tv_ip}: {e}")
            if require_positive:
                return self.last_art_mode_status == 'on'
            return not require_positive

    async def get_local_images(self) -> Set[str]:
        """Get list of image files from local directory"""
        local_files = set()
        artwork_path = Path(ARTWORK_DIR)

        if not artwork_path.exists():
            logger.warning(f"Artwork directory does not exist: {ARTWORK_DIR}")
            return local_files

        for file_path in artwork_path.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_FORMATS:
                local_files.add(file_path.name)

        logger.info(f"Found {len(local_files)} images in {ARTWORK_DIR}")
        return local_files

    async def get_tv_images(self) -> Optional[tuple[Set[str], Set[str]]]:
        """
        Get list of uploaded images on the TV.

        Returns:
            Tuple of (tracked_files, unknown_content_ids):
            - tracked_files: Set of filenames we've uploaded and are tracking
            - unknown_content_ids: Set of content_ids on TV that we don't recognize

            Or None if the TV's content list could not be read at all. Callers
            must not treat that as "the TV is empty": doing so makes every local
            image look missing and re-uploads the whole folder, every cycle. That
            is how a two-image folder ends up as twenty copies on the TV.
        """
        try:
            # Get available images from "MY-C0002" category (My Photos/uploaded images only)
            available = await self.tv.available(
                category='MY-C0002', timeout=CONTENT_LIST_TIMEOUT
            )
            tv_content_ids = set()

            # Debug: log the raw response
            logger.debug(f"Available response: {available}")

            # The API returns a list directly, collect content_ids
            if available and isinstance(available, list):
                for item in available:
                    if 'content_id' in item:
                        tv_content_ids.add(item['content_id'])

            # Map content_ids back to filenames using our mapping
            tracked_files = set()
            unknown_content_ids = set()
            reverse_mapping = {v: k for k, v in self.file_mapping.items()}

            for content_id in tv_content_ids:
                if content_id in reverse_mapping:
                    tracked_files.add(reverse_mapping[content_id])
                else:
                    unknown_content_ids.add(content_id)

            logger.info(f"TV {self.tv_ip} has {len(tracked_files)} tracked images, {len(unknown_content_ids)} unknown images")
            return tracked_files, unknown_content_ids

        except Exception as e:
            logger.warning(
                f"Failed to get uploaded images from TV {self.tv_ip}: {type(e).__name__}: {e}"
            )
            return None

    async def upload_image(self, file_path: Path) -> bool:
        """Upload a single image to the TV with retry logic.
        Uses a sanitized filename to avoid issues with special characters."""
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would upload {file_path.name} to TV {self.tv_ip}")
            return True

        # Create a temp copy with a sanitized filename
        safe_name = sanitize_filename(file_path.name)
        use_temp = safe_name != file_path.name
        temp_dir = None

        try:
            if use_temp:
                temp_dir = tempfile.mkdtemp()
                upload_path = Path(temp_dir) / safe_name
                import shutil
                shutil.copy2(file_path, upload_path)
                logger.debug(f"Sanitized filename: {file_path.name} -> {safe_name}")
            else:
                upload_path = file_path

            for attempt in range(UPLOAD_ATTEMPTS):
                try:
                    if attempt > 0:
                        logger.info(f"Retrying upload of {file_path.name} to TV {self.tv_ip} (attempt {attempt + 1}/{UPLOAD_ATTEMPTS})")
                    else:
                        logger.info(f"Uploading {file_path.name} to TV {self.tv_ip}")

                    content_id = await self.tv.upload(
                        file=str(upload_path),
                        file_type='png' if file_path.suffix.lower() == '.png' else 'jpg',
                        matte=MATTE_STYLE if MATTE_STYLE != 'none' else None
                    )

                    if content_id:
                        # Save the mapping using the original filename as the key
                        self.file_mapping[file_path.name] = content_id
                        self._save_mapping()
                        logger.info(f"Successfully uploaded {file_path.name} to TV {self.tv_ip} (content_id: {content_id})")
                        return True
                    else:
                        logger.warning(f"Upload returned no content_id for {file_path.name} to TV {self.tv_ip}")
                        if attempt < UPLOAD_ATTEMPTS - 1:
                            await asyncio.sleep(UPLOAD_DELAY)
                        continue

                except Exception as e:
                    logger.warning(f"Error uploading {file_path.name} to TV {self.tv_ip}: {e}")
                    if attempt < UPLOAD_ATTEMPTS - 1:
                        await asyncio.sleep(UPLOAD_DELAY)
                    continue

            logger.warning(f"Failed to upload {file_path.name} to TV {self.tv_ip} after {UPLOAD_ATTEMPTS} attempts")
            return False

        finally:
            if temp_dir:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def get_slideshow_settings(self) -> Optional[Dict[str, Any]]:
        """Get current slideshow settings from the TV"""
        try:
            logger.debug(f"Checking slideshow settings on TV {self.tv_ip}")

            get_result = await self.tv._send_art_request(
                {
                    "request": "get_slideshow_status"
                },
                timeout=API_TIMEOUT
            )

            if not get_result:
                logger.debug(f"Could not get slideshow status from TV {self.tv_ip}")
                return None

            # Parse the current settings
            current_value = get_result.get('value', 'off')
            current_type = get_result.get('type', 'shuffleslideshow')
            current_category = get_result.get('category_id', 'MY-C0002')

            logger.info(f"TV {self.tv_ip} slideshow settings: value={current_value}, type={current_type}, category={current_category}")

            # Return settings only if slideshow is enabled
            if current_value != 'off' and current_value:
                return {
                    'value': current_value,
                    'type': current_type if current_type else 'shuffleslideshow',
                    'category_id': current_category if current_category else 'MY-C0002'
                }
            else:
                logger.info(f"Slideshow is disabled on TV {self.tv_ip}")
                return None

        except Exception as e:
            logger.debug(f"Could not get slideshow settings from TV {self.tv_ip}: {e}")
            return None

    async def restart_slideshow(self, settings: Dict[str, Any]) -> bool:
        """Restart slideshow with given settings"""
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would restart slideshow on TV {self.tv_ip} with {settings['value']} minutes")
            return True

        try:
            logger.info(f"Restarting slideshow on TV {self.tv_ip} with {settings['value']} minutes")

            set_result = await self.tv._send_art_request(
                {
                    "request": "set_slideshow_status",
                    "value": settings['value'],
                    "category_id": settings['category_id'],
                    "type": settings['type']
                },
                timeout=API_TIMEOUT
            )

            if set_result:
                logger.info(f"Successfully restarted slideshow on TV {self.tv_ip}")
                return True
            else:
                logger.debug(f"Slideshow restart returned no response on TV {self.tv_ip}")
                return False

        except Exception as e:
            logger.debug(f"Could not restart slideshow on TV {self.tv_ip}: {e}")
            return False

    async def set_brightness(self, brightness: int) -> bool:
        """Set brightness on the TV (0-50 range)"""
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would set brightness to {brightness} on TV {self.tv_ip}")
            return True

        try:
            logger.info(f"Setting brightness to {brightness} on TV {self.tv_ip}")

            result = await self.tv.set_brightness(brightness)

            if result:
                logger.info(f"Successfully set brightness on TV {self.tv_ip}")
                return True
            else:
                logger.debug(f"Brightness setting returned no response on TV {self.tv_ip}")
                return False

        except Exception as e:
            logger.warning(f"Could not set brightness on TV {self.tv_ip}: {e}")
            return False

    async def turn_off(self) -> bool:
        """Turn off the TV via a separate remote control connection.

        Verifies afterwards that the TV actually went off, so a TV that gets
        woken straight back up (e.g. an HDMI-CEC source asserting active-source)
        is reported instead of being silently assumed off.
        """
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would turn off TV {self.tv_ip}")
            return True

        logger.info(f"Turning off TV {self.tv_ip}")

        # Preferred path: explicit hardware power-off. Unlike KEY_POWER, this
        # does not confuse normal viewing, Art Mode and true standby.
        if self.ip_control.paired:
            try:
                result = await self.ip_control.power_off()
                if result not in ('powerOff', 'unknown'):
                    logger.warning(f"Unexpected IP Control power-off response for TV {self.tv_ip}: {result}")
                # A Frame can keep its REST/IP stack alive in standby. The
                # semantic power getter is therefore the verification source.
                await asyncio.sleep(POWER_OFF_VERIFY_DELAY)
                try:
                    state = await self.ip_control.get_power_state()
                except IPControlTransportError:
                    state = 'powerOff'  # Older sets disappear from the network.
                if state == 'powerOff':
                    logger.info(f"Successfully turned off TV {self.tv_ip} via IP Control")
                    return True
                logger.warning(f"TV {self.tv_ip} reports {state} after IP Control power-off")
                return False
            except IPControlAuthError as e:
                logger.warning(
                    f"IP Control token rejected for TV {self.tv_ip}: {e}; "
                    "authorization will be requested automatically during a later "
                    "sync while the TV is in normal viewing"
                )
                return False
            except IPControlTransportError as e:
                logger.warning(f"IP Control power-off failed for TV {self.tv_ip}: {e}; falling back to KEY_POWER hold")
            except IPControlError as e:
                logger.warning(
                    f"IP Control power-off failed for TV {self.tv_ip}: {e}; "
                    "not using KEY_POWER because the failure was not a network outage"
                )
                return False
        elif power_control_configured():
            logger.warning(
                f"Cannot reliably auto-off TV {self.tv_ip}: IP Control is not paired. "
                "Authorization will be requested automatically while the TV is in "
                "normal viewing; not using KEY_POWER because it may only exit Art Mode."
            )
            return False

        try:
            logger.info(f"Using legacy KEY_POWER hold fallback for TV {self.tv_ip}")

            # The art API uses a different websocket endpoint and can't send
            # remote keys, so we need a separate remote control connection
            remote = SamsungTVWSAsyncRemote(
                host=self.tv_ip,
                port=8002,
                token_file=str(self.token_file),
                timeout=CONNECTION_TIMEOUT,
                name=CLIENT_NAME
            )
            try:
                # Frame TVs require holding the power button for 3 seconds to
                # actually power off. A single press just toggles art mode.
                await remote.send_commands(SendRemoteKey.hold("KEY_POWER", 3))
            finally:
                await remote.close()

            # Verify the power-off actually took. If something wakes the TV back
            # up, a later cycle retries rather than leaving it silently on.
            await asyncio.sleep(POWER_OFF_VERIFY_DELAY)
            try:
                still_on = await self.tv.on()
            except Exception:
                still_on = False  # Can't reach it for status — most likely powered off
            if still_on:
                logger.warning(f"TV {self.tv_ip} still reports on after power-off command; will retry next cycle")
                return False

            logger.info(f"Successfully turned off TV {self.tv_ip}")
            return True

        except Exception as e:
            logger.warning(f"Could not turn off TV {self.tv_ip}: {e}")
            return False

    async def turn_on(self) -> bool:
        """Power on a fully-off TV, preferring explicit IP Control."""
        self.auto_started = False
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would turn on TV {self.tv_ip}")
            self.auto_started = True
            return True

        if not self.ip_control.paired:
            logger.warning(
                f"Cannot reliably auto-on TV {self.tv_ip}: IP Control is not paired. "
                "Turn it on normally once; authorization will be requested "
                "automatically while it is in normal viewing."
            )
            return False

        try:
            if await self.ip_control.get_power_state() == 'powerOn':
                logger.debug(f"TV {self.tv_ip} is already powered on")
                return True
        except IPControlAuthError as e:
            logger.warning(f"IP Control token rejected for TV {self.tv_ip}: {e}")
            return False
        except IPControlTransportError:
            pass  # Deep standby may temporarily close port 1516.
        except IPControlError as e:
            logger.debug(f"Could not read power state for TV {self.tv_ip}: {e}")

        try:
            result = await self.ip_control.power_on()
            if result not in ('powerOn', 'unknown'):
                logger.warning(f"Unexpected IP Control power-on response for TV {self.tv_ip}: {result}")
        except IPControlAuthError as e:
            logger.warning(f"IP Control token rejected for TV {self.tv_ip}: {e}")
            return False
        except IPControlTransportError as first_error:
            # Most tested 2024/2025 Frames keep IP Control reachable in standby,
            # but retain WOL for models/network settings that do not.
            logger.info(f"Direct IP power-on could not reach TV {self.tv_ip} ({first_error}); trying WOL fallback")
            if not await self._send_wol():
                return False
            if not await self._wait_for_port(self.ip_control.port, POWER_ON_VERIFY_TIMEOUT):
                logger.warning(f"TV {self.tv_ip} did not expose IP Control after WOL")
                return False
            try:
                await self.ip_control.power_on()
            except IPControlError as e:
                logger.warning(f"IP Control power-on after WOL failed for TV {self.tv_ip}: {e}")
                return False
        except IPControlError as e:
            logger.warning(f"IP Control power-on failed for TV {self.tv_ip}: {e}")
            return False

        if await self._wait_for_port(8002, POWER_ON_VERIFY_TIMEOUT):
            self.auto_started = True
            logger.info(f"Successfully powered on TV {self.tv_ip} via IP Control")
            return True
        logger.warning(f"TV {self.tv_ip} did not become reachable after power-on")
        return False

    async def ensure_art_mode(self) -> bool:
        """Leave an already-on Art Mode untouched; otherwise enter it via WS."""
        try:
            if await self.tv.get_artmode() == 'on':
                return True
            logger.info(f"TV {self.tv_ip} woke into normal viewing; enabling Art Mode")
            await self.tv.set_artmode('on')
            for _ in range(10):
                await asyncio.sleep(1)
                if await self.tv.get_artmode() == 'on':
                    logger.info(f"TV {self.tv_ip} entered Art Mode")
                    return True
            logger.warning(f"TV {self.tv_ip} did not enter Art Mode after power-on")
            return False
        except Exception as e:
            logger.warning(f"Could not enable Art Mode on TV {self.tv_ip}: {e}")
            return False

    async def sync(self, local_images: Set[str] = None) -> bool:
        """Synchronize artwork with the TV"""
        try:
            # Get local images if not provided
            if local_images is None:
                local_images = await self.get_local_images()

            # Get TV images (tracked and unknown)
            tv_state = await self.get_tv_images()

            # If we couldn't read what's already on the TV, do nothing this cycle.
            # Assuming "empty" here would re-upload the entire folder every sync,
            # stacking duplicate copies on the TV until someone notices.
            if tv_state is None:
                logger.warning(
                    f"Skipping sync for TV {self.tv_ip}: could not read its current "
                    f"image list, so there is no safe way to tell what needs uploading. "
                    f"Will retry next cycle."
                )
                return False

            tv_images, unknown_images = tv_state

            # Determine what to upload and delete
            to_upload = local_images - tv_images
            to_delete = tv_images - local_images

            # Handle unknown images based on configuration
            if unknown_images:
                if REMOVE_UNKNOWN_IMAGES:
                    logger.info(f"TV {self.tv_ip}: Found {len(unknown_images)} unknown images, will remove them (REMOVE_UNKNOWN_IMAGES=true)")
                else:
                    logger.warning(f"TV {self.tv_ip}: Found {len(unknown_images)} unknown images on TV that are not in the artwork folder. "
                                 f"Set REMOVE_UNKNOWN_IMAGES=true to remove them. Content IDs: {', '.join(sorted(unknown_images))}")

            logger.info(f"TV {self.tv_ip} sync: {len(to_upload)} to upload, {len(to_delete)} tracked to delete{f', {len(unknown_images)} unknown to delete' if REMOVE_UNKNOWN_IMAGES and unknown_images else ''}")

            # Determine desired slideshow settings from environment (checked every sync run)
            desired_slideshow_settings = None
            if SLIDESHOW_OVERRIDE and SLIDESHOW_ENABLED:
                slideshow_type = 'shuffleslideshow' if SLIDESHOW_TYPE == 'shuffle' else 'slideshow'
                desired_slideshow_settings = {
                    'value': str(SLIDESHOW_INTERVAL),
                    'type': slideshow_type,
                    'category_id': 'MY-C0002'
                }

            # For image changes without override, we need to preserve TV's current settings
            preserve_slideshow_settings = None
            if (to_upload or to_delete or (REMOVE_UNKNOWN_IMAGES and unknown_images)) and local_images:
                if not SLIDESHOW_OVERRIDE:
                    # Preserve TV's current slideshow settings to restore after image changes
                    preserve_slideshow_settings = await self.get_slideshow_settings()

            # Determine brightness to apply (every sync run, regardless of image changes)
            brightness_to_apply = None

            # Solar brightness takes precedence over manual brightness
            solar_brightness = calculate_solar_brightness()
            if solar_brightness is not None:
                brightness_to_apply = solar_brightness
            elif BRIGHTNESS is not None:
                brightness_to_apply = BRIGHTNESS
                logger.info(f"Using manual brightness override: {BRIGHTNESS}")

            # Upload new images
            for filename in to_upload:
                file_path = Path(ARTWORK_DIR) / filename
                await self.upload_image(file_path)
                # Small delay between uploads to avoid overwhelming the TV
                await asyncio.sleep(UPLOAD_DELAY)

            # Delete removed images (batch delete for efficiency)
            if to_delete:
                content_ids_to_delete = [self.file_mapping.get(filename) for filename in to_delete]
                content_ids_to_delete = [cid for cid in content_ids_to_delete if cid]  # Filter out None values

                if content_ids_to_delete:
                    if DRY_RUN:
                        logger.info(f"[DRY RUN] Would delete {len(content_ids_to_delete)} tracked images from TV {self.tv_ip}: {', '.join(to_delete)}")
                    else:
                        logger.info(f"Deleting {len(content_ids_to_delete)} tracked images from TV {self.tv_ip}")
                        try:
                            await self.tv.delete_list(content_ids_to_delete)
                            # Remove from mapping
                            for filename in to_delete:
                                if filename in self.file_mapping:
                                    del self.file_mapping[filename]
                            self._save_mapping()
                            logger.info(f"Successfully deleted {len(content_ids_to_delete)} tracked images from TV {self.tv_ip}")
                        except Exception as e:
                            logger.warning(f"Error batch deleting tracked images from TV {self.tv_ip}: {e}")

            # Delete unknown images if configured (batch delete for efficiency)
            if REMOVE_UNKNOWN_IMAGES and unknown_images:
                if DRY_RUN:
                    logger.info(f"[DRY RUN] Would delete {len(unknown_images)} unknown images from TV {self.tv_ip}")
                else:
                    logger.info(f"Deleting {len(unknown_images)} unknown images from TV {self.tv_ip}")
                    try:
                        await self.tv.delete_list(list(unknown_images))
                        logger.info(f"Successfully deleted {len(unknown_images)} unknown images from TV {self.tv_ip}")
                    except Exception as e:
                        logger.warning(f"Error batch deleting unknown images from TV {self.tv_ip}: {e}")

            # If we made changes and have images, select an image and restore preserved slideshow
            if local_images and (to_upload or to_delete or (REMOVE_UNKNOWN_IMAGES and unknown_images)):
                # Verify file_mapping against what's actually on the TV now
                verified_mapping = {}
                try:
                    tv_state_now = await self.get_tv_images()
                    if tv_state_now is None:
                        # Couldn't re-read the TV; trust the mapping rather than
                        # narrowing it to nothing and skipping image selection.
                        logger.debug(f"Could not verify TV images for selection on {self.tv_ip}")
                        verified_mapping = self.file_mapping
                    else:
                        current_on_tv, _ = tv_state_now
                        verified_mapping = {k: v for k, v in self.file_mapping.items() if k in current_on_tv}
                except Exception as e:
                    logger.debug(f"Could not verify TV images for selection on {self.tv_ip}: {e}")
                    verified_mapping = self.file_mapping

                if verified_mapping:
                    try:
                        # Pick random image if shuffle mode, otherwise pick first
                        import random
                        # Use desired settings if available, otherwise preserved settings for shuffle check
                        settings_for_mode = desired_slideshow_settings or preserve_slideshow_settings
                        if settings_for_mode and settings_for_mode.get('type') == 'shuffleslideshow':
                            content_id = random.choice(list(verified_mapping.values()))
                            if DRY_RUN:
                                logger.info(f"[DRY RUN] Would select random image on TV {self.tv_ip} for shuffle mode")
                            else:
                                logger.info(f"Selecting random image on TV {self.tv_ip} for shuffle mode")
                        else:
                            content_id = list(verified_mapping.values())[0]
                            if DRY_RUN:
                                logger.info(f"[DRY RUN] Would select first image on TV {self.tv_ip} to prevent default art")
                            else:
                                logger.info(f"Selecting first image on TV {self.tv_ip} to prevent default art")

                        if not DRY_RUN:
                            await self.tv.select_image(content_id, show=True)

                        # Restore preserved slideshow settings (when no override is set)
                        if preserve_slideshow_settings:
                            await self.restart_slideshow(preserve_slideshow_settings)

                    except Exception as e:
                        logger.warning(f"Failed to select image on TV {self.tv_ip}: {e}")
                else:
                    logger.warning(f"No verified images available on TV {self.tv_ip} to select, skipping image selection")

            # Apply slideshow settings every sync run if override is set (compare with current to avoid unnecessary updates)
            if desired_slideshow_settings:
                try:
                    current_settings = await self.get_slideshow_settings()
                    # Check if settings differ (compare value and type)
                    needs_update = (
                        not current_settings or
                        current_settings.get('value') != desired_slideshow_settings['value'] or
                        current_settings.get('type') != desired_slideshow_settings['type']
                    )
                    if needs_update:
                        logger.info(f"Slideshow settings changed, updating TV {self.tv_ip}: {desired_slideshow_settings['value']} min, {desired_slideshow_settings['type']}")
                        await self.restart_slideshow(desired_slideshow_settings)
                    else:
                        logger.info(f"Slideshow settings unchanged on TV {self.tv_ip}")
                except Exception as e:
                    logger.warning(f"Failed to update slideshow settings on TV {self.tv_ip}: {e}")

            # Apply brightness every sync run (not just when images change)
            if brightness_to_apply is not None:
                try:
                    await self.set_brightness(brightness_to_apply)
                except Exception as e:
                    logger.warning(f"Failed to set brightness on TV {self.tv_ip}: {e}")

            logger.info(f"Sync completed for TV {self.tv_ip}")
            return True

        except Exception as e:
            logger.warning(f"Error during sync for TV {self.tv_ip}: {e}")
            return False

    async def close(self) -> None:
        """Close connection to TV"""
        if self.tv:
            try:
                await self.tv.close()
            except Exception:
                pass


async def wait_until_next_sync(tvs_to_keepalive: List['TVArtworkSync']) -> None:
    """Sleep until the next sync interval, pinging any provided TVs to keep their channels open."""
    if tvs_to_keepalive:
        logger.info(
            f"Keeping connections alive for {SYNC_INTERVAL_MINUTES} minute(s) "
            "until next sync..."
        )
    else:
        logger.info(
            f"Waiting {SYNC_INTERVAL_MINUTES} minute(s) until next sync..."
        )
    sync_interval_seconds = SYNC_INTERVAL_MINUTES * 60
    elapsed = 0
    while elapsed < sync_interval_seconds:
        chunk = min(KEEPALIVE_INTERVAL, sync_interval_seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk
        if elapsed < sync_interval_seconds:
            for tv_sync in tvs_to_keepalive:
                try:
                    # get_artmode_status, not get_content_list — see the probe in
                    # _try_connect. Cheaper, and answered by TVs that ignore a
                    # null-category content list request.
                    await tv_sync.tv.get_artmode()
                    logger.debug(f"Keepalive ping OK for TV {tv_sync.tv_ip}")
                except Exception as e:
                    logger.debug(f"Keepalive ping failed for TV {tv_sync.tv_ip}: {e}")


async def sync_all_tvs() -> None:
    """Synchronize artwork to all configured TVs"""
    if not TV_IPS:
        logger.error("No TV IPs configured. Set TV_IPS environment variable.")
        await wait_until_next_sync([])
        return

    logger.info(f"Starting sync for {len(TV_IPS)} TV(s): {', '.join(TV_IPS)}")

    tv_syncs = [TVArtworkSync(ip) for ip in TV_IPS]

    auto_off_window = is_within_auto_off_window()
    auto_on_attempt = should_attempt_auto_on()
    if auto_on_attempt and auto_off_window:
        logger.warning("Auto-on time falls within the auto-off window; auto-off takes precedence")
        auto_on_attempt = False

    # Power-on must run before the normal art connection: fully-off TVs cannot
    # pass connect(), so waiting until after it would make AUTO_ON_TIME inert.
    if auto_on_attempt:
        logger.info(f"Auto-on time reached ({AUTO_ON_TIME}); making today's single power-on attempt")
        await asyncio.gather(*[tv.turn_on() for tv in tv_syncs])

    # Do not put a barrier between connecting and powering off. A TV awaiting
    # first-time pairing can spend several minutes in _acquire_token(); healthy
    # TVs must still be turned off as soon as their own connection is ready.
    if auto_off_window:
        grace_display = int(AUTO_OFF_GRACE_HOURS) if AUTO_OFF_GRACE_HOURS == int(AUTO_OFF_GRACE_HOURS) else AUTO_OFF_GRACE_HOURS
        logger.info(
            f"Within auto-off window ({AUTO_OFF_TIME} + {grace_display}h grace); "
            "processing each TV independently"
        )

        async def connect_and_turn_off(tv_sync: TVArtworkSync) -> bool:
            try:
                if not await tv_sync.connect():
                    return False
                if not await tv_sync.is_in_art_mode(require_positive=True):
                    logger.info(
                        f"Skipping TV {tv_sync.tv_ip} - not in art mode (may be in use)"
                    )
                    return False
                return await tv_sync.turn_off()
            finally:
                await tv_sync.close()

        await asyncio.gather(*[connect_and_turn_off(tv) for tv in tv_syncs])
        await wait_until_next_sync([])
        return

    connect_results = await asyncio.gather(*[tv.connect() for tv in tv_syncs])
    connected_tvs = [tv for tv, ok in zip(tv_syncs, connect_results) if ok]

    if not connected_tvs:
        logger.warning("No TVs are currently available")
        await asyncio.gather(*[tv.close() for tv in tv_syncs])
        await wait_until_next_sync([])
        return

    tvs_in_art_mode = []
    for tv_sync in connected_tvs:
        if await tv_sync.is_in_art_mode():
            tvs_in_art_mode.append(tv_sync)
        elif auto_on_attempt and tv_sync.auto_started and await tv_sync.ensure_art_mode():
            # A TV may resume to its last input instead of Art Mode. Scheduled
            # startup always converges to Art Mode, but never touches an
            # already-on TV outside the auto-on window.
            tvs_in_art_mode.append(tv_sync)
        else:
            logger.info(f"Skipping TV {tv_sync.tv_ip} - not in art mode (may be in use)")

    if not tvs_in_art_mode:
        logger.info("No TVs in art mode")
        await asyncio.gather(*[tv.close() for tv in tv_syncs])
        await wait_until_next_sync([])
        return

    local_images = await tvs_in_art_mode[0].get_local_images()
    await asyncio.gather(*[tv.sync(local_images) for tv in tvs_in_art_mode])

    await wait_until_next_sync(tvs_in_art_mode)

    await asyncio.gather(*[tv.close() for tv in tv_syncs])
    logger.info("Sync cycle completed")


def check_token_dir_writable() -> None:
    """
    Verify TOKEN_DIR exists and is writable before we try to pair.

    Pairing failures caused by a non-writable token directory are otherwise
    invisible: the TV issues a token, the upstream library's write raises, and
    upstream swallows the error into a debug log — leaving the service looping
    on "waiting for pairing approval" forever even though the user approved it.
    Common causes are a read-only bind mount, a NAS/SMB/NFS share that squashes
    the container's user, or SELinux without a :z mount flag.
    """
    token_dir = Path(TOKEN_DIR)
    probe = token_dir / '.write-test'
    try:
        token_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text('ok')
        probe.unlink()
        return
    except OSError as e:
        existing_tokens = list(token_dir.glob('tv_*.txt')) if token_dir.is_dir() else []
        logger.error(f"Token directory {TOKEN_DIR} is not writable: {e}")
        logger.error("Tokens cannot be saved, so TV pairing will never complete.")
        logger.error(f"Check the volume mapped to {TOKEN_DIR} (read-only mount, NAS share "
                     "permissions, or SELinux — try adding :z to the mount).")
        if not existing_tokens:
            logger.error("No existing tokens found either. Exiting.")
            sys.exit(1)
        logger.warning("Continuing with existing tokens, but re-pairing will fail.")


async def main() -> None:
    """Main loop - sync periodically"""
    logger.info("=" * 60)
    logger.info("Samsung Frame TV Artwork Sync Service")
    logger.info("=" * 60)
    logger.info(f"Artwork directory: {ARTWORK_DIR}")
    logger.info(f"TV IPs: {', '.join(TV_IPS) if TV_IPS else 'None configured'}")
    logger.info(f"Sync interval: {SYNC_INTERVAL_MINUTES} minutes")
    logger.info(f"Matte style: {MATTE_STYLE}")
    logger.info("=" * 60)

    if not TV_IPS:
        logger.error("No TV IPs configured. Exiting.")
        sys.exit(1)

    check_token_dir_writable()

    while True:
        try:
            await sync_all_tvs()
        except Exception as e:
            logger.error(f"Error in sync cycle: {e}", exc_info=True)
            await asyncio.sleep(SYNC_INTERVAL_MINUTES * 60)


async def pair_ip_control(tv_ip: str) -> int:
    """Pair one explicitly selected TV; never prompt several rooms at once."""
    if tv_ip not in TV_IPS:
        logger.error(f"{tv_ip} is not present in TV_IPS")
        return 2
    tv = TVArtworkSync(tv_ip)
    return 0 if await tv.pair_ip_control() else 1


if __name__ == '__main__':
    # Check for command-line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == '--test-solar':
            # Run solar brightness test mode
            if LOCATION_LATITUDE is None or LOCATION_LONGITUDE is None:
                from solar_test_output import print_test_error
                print_test_error("Location not configured")
                sys.exit(1)

            from solar_test_output import run_solar_brightness_test

            # Use the actual brightness calculation function
            run_solar_brightness_test(
                LOCATION_LATITUDE,
                LOCATION_LONGITUDE,
                LOCATION_TIMEZONE,
                BRIGHTNESS_MIN,
                BRIGHTNESS_MAX,
                brightness_from_elevation
            )
            sys.exit(0)
        elif sys.argv[1] == '--dry-run':
            # Enable dry run mode
            DRY_RUN = True
            logger.info("=" * 60)
            logger.info("DRY RUN MODE - No artwork, settings, or power changes will be made")
            logger.info("Connection authorization may still prompt and save missing tokens")
            logger.info("=" * 60)
        elif sys.argv[1] == '--pair-ip-control':
            if len(sys.argv) != 3:
                logger.error("Usage: sync_artwork.py --pair-ip-control <TV_IP>")
                sys.exit(2)
            sys.exit(asyncio.run(pair_ip_control(sys.argv[2])))

    # Normal operation mode
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)
