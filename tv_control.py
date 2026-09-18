"""In-process queue for external TV control requests.

The sync loop owns the only Art connection to each TV, so external requests
(such as a Homebridge power switch) are queued and drained by that loop instead
of opening a second, competing connection. Each request carries a future that
the loop resolves with the resulting state.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List

# Supported actions. "status" is read-only; "on"/"off" are guarded.
ACTIONS = ("on", "off", "status")


class ControlError(Exception):
    """Base error for a control request."""


class ControlNotFound(ControlError):
    """The requested TV is not configured."""


class ControlConflict(ControlError):
    """The TV is in use, so the requested action was refused."""


class ControlUnavailable(ControlError):
    """The TV could not be reached or did not complete the request."""


@dataclass
class ControlRequest:
    ip: str
    action: str
    future: "asyncio.Future[Dict[str, Any]]"


class ControlQueue:
    """Queue external control requests and wake the sync loop to run them."""

    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._pending: Deque[ControlRequest] = deque()

    async def request(self, ip: str, action: str) -> Dict[str, Any]:
        """Submit a request and wait for the sync loop's result.

        Must be called from the event loop that owns the sync cycle. A caller in
        another thread (the HTTP handler) should schedule this with
        ``asyncio.run_coroutine_threadsafe``.
        """
        if action not in ACTIONS:
            raise ControlError(f"unknown action: {action!r}")
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        entry = ControlRequest(ip, action, future)
        self._pending.append(entry)
        self._wake.set()
        try:
            return await future
        except asyncio.CancelledError:
            # The HTTP caller gave up. Drop the request so the sync loop does
            # not act on it after the caller stopped listening.
            try:
                self._pending.remove(entry)
            except ValueError:
                pass
            raise

    async def wait(self, timeout: float) -> bool:
        """Wait for a request or timeout. Returns True if a request arrived."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def drain(self) -> List[ControlRequest]:
        """Take all pending requests and clear the wake signal."""
        pending = list(self._pending)
        self._pending.clear()
        self._wake.clear()
        return pending
