"""Minimal HTTP control API for the sync service.

Exposes the guarded power actions over HTTP so external controllers such as
Homebridge can trigger them. The server runs in a background thread and bridges
each request onto the sync event loop, where the ControlQueue is drained using
the loop's live TV connections.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent import futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

from tv_control import (
    ControlConflict,
    ControlError,
    ControlNotFound,
    ControlQueue,
    ControlUnavailable,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0


class ControlHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the queue and event loop to handlers."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        control: ControlQueue,
        loop: asyncio.AbstractEventLoop,
        timeout: float,
    ) -> None:
        super().__init__(address, _ControlHandler)
        self.control = control
        self.loop = loop
        self.timeout = timeout


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "FrameTVControl/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_text(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Discard any body so keep-alive connections stay in sync.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._route("POST")

    def _route(self, method: str) -> None:
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        if parts == ["health"]:
            self._respond(200, {"status": "ok"})
            return
        if len(parts) == 3 and parts[0] == "tv":
            ip, action = parts[1], parts[2]
            if action == "status" and method == "GET":
                self._dispatch(ip, "status")
                return
            # Plain-text "1"/"0" for simple HTTP contact-sensor plugins.
            if action == "art" and method == "GET":
                self._dispatch(ip, "status", plain_art=True)
                return
            if action in ("on", "off") and method == "POST":
                self._dispatch(ip, action)
                return
        self._respond(404, {"error": "not found"})

    def _dispatch(self, ip: str, action: str, *, plain_art: bool = False) -> None:
        server = self.server
        assert isinstance(server, ControlHTTPServer)
        concurrent = asyncio.run_coroutine_threadsafe(
            server.control.request(ip, action), server.loop
        )
        try:
            result = concurrent.result(timeout=server.timeout)
        except (futures.TimeoutError, TimeoutError):
            concurrent.cancel()
            self._respond(504, {"error": "timed out"})
        except ControlNotFound as exc:
            self._respond(404, {"error": str(exc)})
        except ControlConflict as exc:
            self._respond(409, {"error": str(exc)})
        except ControlUnavailable as exc:
            self._respond(502, {"error": str(exc)})
        except ControlError as exc:
            self._respond(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            logger.warning("control request failed for %s: %s", ip, exc)
            self._respond(500, {"error": "internal error"})
        else:
            if plain_art:
                self._respond_text(200, "1" if result.get("state") == "art" else "0")
            else:
                self._respond(200, result)


def start_control_server(
    control: ControlQueue,
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> ControlHTTPServer:
    """Bind the control API and serve it from a daemon thread."""
    server = ControlHTTPServer((host, port), control, loop, timeout)
    thread = threading.Thread(
        target=server.serve_forever, name="control-api", daemon=True
    )
    thread.start()
    logger.info("Control API listening on http://%s:%s", host, server.server_address[1])
    return server
