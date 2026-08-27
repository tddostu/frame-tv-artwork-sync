"""Minimal async client for Samsung's local IP Control JSON-RPC API."""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from pathlib import Path
from typing import Any


DEFAULT_PORTS = (1516, 1515)
COMMAND_TIMEOUT = 5.0
PAIR_TIMEOUT = 30.0


class IPControlError(Exception):
    """Base IP Control error."""


class IPControlAuthError(IPControlError):
    """The persisted token was rejected and pairing is required."""


class IPControlTransportError(IPControlError):
    """The TV could not be reached over IP Control."""


class SamsungIPControl:
    """Samsung IP Control client with per-TV token persistence."""

    def __init__(self, host: str, token_file: Path) -> None:
        self.host = host
        self.token_file = token_file
        self.token: str | None = None
        self.port = DEFAULT_PORTS[0]
        self._lock = asyncio.Lock()
        self._load()

    @property
    def paired(self) -> bool:
        return bool(self.token)

    def _load(self) -> None:
        try:
            data = json.loads(self.token_file.read_text())
            token = data.get("token")
            port = int(data.get("port", DEFAULT_PORTS[0]))
            if isinstance(token, str) and token:
                self.token = token
                self.port = port
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _save(self, token: str, port: int) -> None:
        """Atomically persist a newly issued token before exposing it in memory."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_file.with_name(self.token_file.name + ".tmp")
        try:
            temporary.write_text(json.dumps({"token": token, "port": port}) + "\n")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.token_file)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise IPControlError(
                f"could not persist IP Control token to {self.token_file}: {exc}"
            ) from exc

    @staticmethod
    def _ssl_context(*, legacy: bool = False) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if legacy:
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
        return context

    def _request_sync(
        self,
        method: str,
        extra_params: dict[str, Any] | None,
        include_token: bool,
        timeout: float,
    ) -> dict[str, Any]:
        params = dict(extra_params or {})
        if include_token:
            if not self.token:
                raise IPControlAuthError("IP Control is not paired")
            params["AccessToken"] = self.token

        body: dict[str, Any] = {"jsonrpc": "2.0", "id": "1", "method": method}
        if params:
            body["params"] = params
        payload = json.dumps(body).encode()

        last_error: Exception | None = None
        for legacy in (False, True):
            connection = http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=self._ssl_context(legacy=legacy),
            )
            try:
                connection.request(
                    "POST",
                    "/",
                    body=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
                if response.status != 200:
                    raise IPControlError(f"HTTP {response.status}: {raw!r}")
                message = json.loads(raw)
                # Samsung firmware uses both proper JSON-RPC errors
                # ({"error": {"code": ...}}) and a bare error object
                # ({"code": ..., "message": ...}). Normalize both forms.
                error = message.get("error")
                if not isinstance(error, dict) and "code" in message:
                    error = message
                if isinstance(error, dict):
                    code = error.get("code")
                    # -32010 is the documented token rejection. 2025 Frames
                    # also return -32700 "Parse error" for a stale/unrecognized
                    # AccessToken even though the request JSON is valid; a fresh
                    # pairing clears it. Only classify either as authentication
                    # failure when this request actually included a token.
                    if code in (-32010, -32700) and include_token:
                        raise IPControlAuthError(error.get("message", str(error)))
                    raise IPControlError(error.get("message", str(error)))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise IPControlError(f"invalid response: {message!r}")
                return result
            except ssl.SSLError as exc:
                last_error = exc
                if legacy:
                    break
            except IPControlError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as exc:
                raise IPControlTransportError(str(exc)) from exc
            finally:
                connection.close()
        raise IPControlTransportError(str(last_error)) from last_error

    async def _request(
        self,
        method: str,
        extra_params: dict[str, Any] | None = None,
        *,
        include_token: bool = True,
        timeout: float = COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        async with self._lock:
            try:
                return await asyncio.to_thread(
                    self._request_sync, method, extra_params, include_token, timeout
                )
            except IPControlAuthError:
                # Match the artwork-token lifecycle: only a definitive auth
                # rejection invalidates persisted state. Transport, timeout,
                # HTTP and protocol errors preserve it for a later retry.
                self.token = None
                self.token_file.unlink(missing_ok=True)
                raise

    async def pair(self) -> str:
        """Pair on the first responding port. TV must be in normal viewing."""
        failures: list[tuple[int, Exception]] = []
        for port in DEFAULT_PORTS:
            self.port = port
            try:
                result = await self._request(
                    "createAccessToken", include_token=False, timeout=PAIR_TIMEOUT
                )
            except IPControlError as exc:
                failures.append((port, exc))
                continue
            token = result.get("AccessToken")
            if not isinstance(token, str) or not token:
                failures.append(
                    (port, IPControlError(f"no AccessToken in response: {result!r}"))
                )
                continue
            self._save(token, port)
            self.token = token
            self.port = port
            return token
        details = "; ".join(
            f"port {port}: {type(error).__name__}: {error}"
            for port, error in failures
        )
        raise IPControlError(details or "no IP Control port responded")

    async def get_power_state(self) -> str:
        result = await self._request("powerControl")
        return result.get("power", "unknown")

    async def power_on(self) -> str:
        result = await self._request("powerControl", {"power": "powerOn"})
        return result.get("power", "unknown")

    async def power_off(self) -> str:
        result = await self._request("powerControl", {"power": "powerOff"})
        return result.get("power", "unknown")
