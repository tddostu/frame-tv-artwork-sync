import asyncio
import json
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

pysolar = types.ModuleType("pysolar")
pysolar_solar = types.ModuleType("pysolar.solar")
pysolar_solar.get_altitude = lambda *args, **kwargs: 0
sys.modules.setdefault("pysolar", pysolar)
sys.modules.setdefault("pysolar.solar", pysolar_solar)

import control_api
from tv_control import (
    ControlConflict,
    ControlNotFound,
    ControlUnavailable,
)


class FakeControl:
    """Stand-in for ControlQueue returning canned results or raising."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def request(self, ip, action):
        self.calls.append((ip, action))
        result = self.results.get(action)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.control = FakeControl({"status": {"ip": "192.0.2.10", "state": "art"}})
        self.server = control_api.ControlHTTPServer(
            ("127.0.0.1", 0), self.control, self.loop, timeout=5.0
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05),
            name="control-api-test",
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server, self.server)

    @staticmethod
    def _stop_server(server):
        server.shutdown()
        server.server_close()

    def _http(self, method, path):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def _http_raw(self, path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as response:
            return response.status, response.read().decode()

    async def _call(self, method, path):
        return await asyncio.to_thread(self._http, method, path)

    async def test_health(self):
        status, body = await self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    async def test_status_routes_to_queue(self):
        status, body = await self._call("GET", "/tv/192.0.2.10/status")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ip": "192.0.2.10", "state": "art"})
        self.assertEqual(self.control.calls, [("192.0.2.10", "status")])

    async def test_art_endpoint_returns_plain_one_in_art_mode(self):
        self.control.results = {"status": {"ip": "192.0.2.10", "state": "art"}}
        status, body = await asyncio.to_thread(self._http_raw, "/tv/192.0.2.10/art")
        self.assertEqual(status, 200)
        self.assertEqual(body, "1")

    async def test_art_endpoint_returns_plain_zero_for_content(self):
        self.control.results = {"status": {"ip": "192.0.2.10", "state": "on"}}
        status, body = await asyncio.to_thread(self._http_raw, "/tv/192.0.2.10/art")
        self.assertEqual(status, 200)
        self.assertEqual(body, "0")

    async def test_on_routes_to_queue(self):
        self.control.results = {"on": {"ip": "192.0.2.10", "state": "art"}}
        status, body = await self._call("POST", "/tv/192.0.2.10/on")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ip": "192.0.2.10", "state": "art"})
        self.assertEqual(self.control.calls, [("192.0.2.10", "on")])

    async def test_off_conflict_is_409(self):
        self.control.results = {"off": ControlConflict("in use")}
        status, body = await self._call("POST", "/tv/192.0.2.10/off")
        self.assertEqual(status, 409)
        self.assertIn("in use", body["error"])

    async def test_unknown_tv_is_404(self):
        self.control.results = {"status": ControlNotFound("not configured")}
        status, _ = await self._call("GET", "/tv/192.0.2.99/status")
        self.assertEqual(status, 404)

    async def test_unavailable_tv_is_502(self):
        self.control.results = {"on": ControlUnavailable("unreachable")}
        status, _ = await self._call("POST", "/tv/192.0.2.10/on")
        self.assertEqual(status, 502)

    async def test_unknown_route_is_404(self):
        status, _ = await self._call("GET", "/nope")
        self.assertEqual(status, 404)

    async def test_wrong_method_is_404(self):
        status, _ = await self._call("GET", "/tv/192.0.2.10/on")
        self.assertEqual(status, 404)

    async def test_timeout_is_504(self):
        async def slow():
            await asyncio.sleep(5)

        self.control.results = {"on": slow}
        server = control_api.ControlHTTPServer(
            ("127.0.0.1", 0), self.control, self.loop, timeout=0.05
        )
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        )
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/tv/192.0.2.10/on", method="POST"
            )
            try:
                await asyncio.to_thread(
                    urllib.request.urlopen, request, timeout=5
                )
                self.fail("expected a 504")
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 504)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
