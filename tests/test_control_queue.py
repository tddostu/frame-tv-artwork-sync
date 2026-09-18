import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Power/control tests do not exercise solar calculations, and the lightweight
# test environment intentionally does not install pysolar.
pysolar = types.ModuleType("pysolar")
pysolar_solar = types.ModuleType("pysolar.solar")
pysolar_solar.get_altitude = lambda *args, **kwargs: 0
sys.modules.setdefault("pysolar", pysolar)
sys.modules.setdefault("pysolar.solar", pysolar_solar)

import sync_artwork
from samsung_ip_control import IPControlError
from tv_control import (
    ControlConflict,
    ControlError,
    ControlNotFound,
    ControlQueue,
    ControlRequest,
    ControlUnavailable,
)


def make_tv(ip="192.0.2.10", *, connected=True, paired=True, art_status=None):
    tv = MagicMock()
    tv.tv_ip = ip
    tv.tv = MagicMock() if connected else None
    tv.last_art_mode_status = art_status
    tv.auto_started = False
    tv.ip_control.paired = paired
    tv.ip_control.get_power_state = AsyncMock(return_value="powerOff")
    tv.turn_on = AsyncMock(return_value=True)
    tv.connect = AsyncMock(return_value=True)
    tv.ensure_art_mode = AsyncMock(return_value=True)
    tv.is_in_art_mode = AsyncMock(return_value=True)
    tv.turn_off = AsyncMock(return_value=True)
    tv.close = AsyncMock()
    return tv


class ControlQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_action_is_rejected(self):
        queue = ControlQueue()
        with self.assertRaises(ControlError):
            await queue.request("192.0.2.10", "reboot")

    async def test_request_resolves_after_drain(self):
        queue = ControlQueue()
        task = asyncio.create_task(queue.request("192.0.2.10", "status"))
        await asyncio.sleep(0)

        self.assertTrue(await queue.wait(0.1))
        pending = queue.drain()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].action, "status")

        pending[0].future.set_result({"ip": "192.0.2.10", "state": "on"})
        self.assertEqual(await task, {"ip": "192.0.2.10", "state": "on"})

    async def test_wait_times_out_without_a_request(self):
        queue = ControlQueue()
        self.assertFalse(await queue.wait(0.01))

    async def test_wait_processes_requests_before_the_full_interval(self):
        class OneShotQueue:
            def __init__(self):
                self.calls = 0

            async def wait(self, timeout):
                self.calls += 1
                return self.calls == 1

            def drain(self):
                return []

        with (
            patch.object(sync_artwork, "_CONTROL_QUEUE", OneShotQueue()),
            patch.object(sync_artwork, "SYNC_INTERVAL_MINUTES", 1),
            patch.object(sync_artwork, "KEEPALIVE_INTERVAL", 60),
            patch.object(
                sync_artwork, "process_control_requests", AsyncMock()
            ) as process,
        ):
            await sync_artwork.wait_until_next_sync([])

        process.assert_awaited_once()

    async def test_process_control_requests_resolves_future(self):
        queue = ControlQueue()
        tv = make_tv(art_status="on")
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOn")

        with (
            patch.object(sync_artwork, "_CONTROL_QUEUE", queue),
            patch.object(sync_artwork, "TV_IPS", ["192.0.2.10"]),
        ):
            task = asyncio.create_task(queue.request("192.0.2.10", "status"))
            await asyncio.sleep(0)
            await sync_artwork.process_control_requests([tv])
            result = await task

        self.assertEqual(result, {"ip": "192.0.2.10", "state": "art"})

    async def test_result_is_dropped_when_caller_cancels_mid_flight(self):
        queue = ControlQueue()

        async def cancelling_execute(request, tvs_to_keepalive):
            request.future.cancel()
            return {"ip": request.ip, "state": "on"}

        with (
            patch.object(sync_artwork, "_CONTROL_QUEUE", queue),
            patch.object(
                sync_artwork, "execute_control_request", new=cancelling_execute
            ),
        ):
            task = asyncio.create_task(queue.request("192.0.2.10", "on"))
            await asyncio.sleep(0)
            await sync_artwork.process_control_requests([])

            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_process_control_requests_surfaces_errors(self):
        queue = ControlQueue()

        with (
            patch.object(sync_artwork, "_CONTROL_QUEUE", queue),
            patch.object(sync_artwork, "TV_IPS", ["192.0.2.10"]),
        ):
            task = asyncio.create_task(queue.request("192.0.2.99", "off"))
            await asyncio.sleep(0)
            await sync_artwork.process_control_requests([])
            with self.assertRaises(ControlNotFound):
                await task


class ControlExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_ip_is_not_found(self):
        request = ControlRequest("192.0.2.99", "status", None)
        with patch.object(sync_artwork, "TV_IPS", ["192.0.2.10"]):
            with self.assertRaises(ControlNotFound):
                await sync_artwork.execute_control_request(request, [])

    async def test_status_reports_off_when_ip_control_says_off(self):
        tv = make_tv(art_status="on")
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOff")
        self.assertEqual(
            await sync_artwork._control_status("192.0.2.10", tv),
            {"ip": "192.0.2.10", "state": "off"},
        )

    async def test_status_reports_art_when_powered_on_in_art_mode(self):
        tv = make_tv(art_status="on")
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOn")
        self.assertEqual(
            await sync_artwork._control_status("192.0.2.10", tv),
            {"ip": "192.0.2.10", "state": "art"},
        )

    async def test_status_reports_on_when_powered_on_with_content(self):
        tv = make_tv(art_status="off")
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOn")
        self.assertEqual(
            await sync_artwork._control_status("192.0.2.10", tv),
            {"ip": "192.0.2.10", "state": "on"},
        )

    async def test_status_is_unknown_without_a_power_answer(self):
        tv = make_tv(paired=False, art_status=None)
        self.assertEqual(
            await sync_artwork._control_status("192.0.2.10", tv),
            {"ip": "192.0.2.10", "state": "unknown"},
        )

    async def test_on_leaves_an_already_on_tv_untouched(self):
        tv = make_tv(art_status="on")
        tv.turn_on = AsyncMock(return_value=True)
        tv.auto_started = False
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOn")

        result = await sync_artwork._control_on("192.0.2.10", tv)

        self.assertEqual(result, {"ip": "192.0.2.10", "state": "art"})
        tv.ensure_art_mode.assert_not_awaited()
        tv.connect.assert_not_awaited()

    async def test_on_wakes_an_off_tv_into_art_mode(self):
        tv = make_tv(connected=False)
        tv.auto_started = True
        tv.turn_on = AsyncMock(return_value=True)
        tv.connect = AsyncMock(return_value=True)
        tv.ensure_art_mode = AsyncMock(return_value=True)

        result = await sync_artwork._control_on("192.0.2.10", tv)

        self.assertEqual(result, {"ip": "192.0.2.10", "state": "art"})
        tv.ensure_art_mode.assert_awaited_once()

    async def test_on_reports_unavailable_when_power_on_fails(self):
        tv = make_tv()
        tv.turn_on = AsyncMock(return_value=False)

        with self.assertRaises(ControlUnavailable):
            await sync_artwork._control_on("192.0.2.10", tv)

    async def test_off_is_refused_while_showing_content(self):
        tv = make_tv(connected=True, art_status="off")
        tv.is_in_art_mode = AsyncMock(return_value=False)

        with self.assertRaises(ControlConflict):
            await sync_artwork._control_off("192.0.2.10", tv)

        tv.turn_off.assert_not_awaited()

    async def test_off_powers_off_a_tv_in_art_mode(self):
        tv = make_tv(connected=True, art_status="on")
        tv.is_in_art_mode = AsyncMock(return_value=True)
        tv.turn_off = AsyncMock(return_value=True)

        result = await sync_artwork._control_off("192.0.2.10", tv)

        self.assertEqual(result, {"ip": "192.0.2.10", "state": "off"})
        tv.turn_off.assert_awaited_once()

    async def test_off_treats_unreachable_standby_tv_as_success(self):
        tv = make_tv(connected=False)
        tv.connect = AsyncMock(return_value=False)
        tv.ip_control.get_power_state = AsyncMock(return_value="powerOff")

        result = await sync_artwork._control_off("192.0.2.10", tv)

        self.assertEqual(result, {"ip": "192.0.2.10", "state": "off"})
        tv.turn_off.assert_not_awaited()

    async def test_off_reports_unavailable_when_unreachable_and_not_confirmed(self):
        tv = make_tv(connected=False)
        tv.connect = AsyncMock(return_value=False)
        tv.ip_control.get_power_state = AsyncMock(
            side_effect=IPControlError("no answer")
        )

        with self.assertRaises(ControlUnavailable):
            await sync_artwork._control_off("192.0.2.10", tv)


if __name__ == "__main__":
    unittest.main()
