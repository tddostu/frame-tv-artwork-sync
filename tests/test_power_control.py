import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Power tests do not exercise solar calculations, and the lightweight test
# environment intentionally does not install pysolar.
pysolar = types.ModuleType("pysolar")
pysolar_solar = types.ModuleType("pysolar.solar")
pysolar_solar.get_altitude = lambda *args, **kwargs: 0
sys.modules.setdefault("pysolar", pysolar)
sys.modules.setdefault("pysolar.solar", pysolar_solar)

import sync_artwork
from samsung_ip_control import IPControlAuthError, IPControlError, IPControlTransportError


class PowerControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        token_dir = patch.object(sync_artwork, "TOKEN_DIR", self.tempdir.name)
        token_dir.start()
        self.addCleanup(token_dir.stop)
        self.addCleanup(self.tempdir.cleanup)

        self.tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
        self.tv_sync.ip_control = MagicMock()
        self.tv_sync.ip_control.paired = True
        self.tv_sync.ip_control.port = 1516
        self.tv_sync.ip_control.get_power_state = AsyncMock(return_value="powerOff")
        self.tv_sync.ip_control.power_on = AsyncMock(return_value="powerOn")
        self.tv_sync.ip_control.power_off = AsyncMock(return_value="powerOff")
        self.tv_sync._wait_for_port = AsyncMock(return_value=True)
        self.tv_sync._send_wol = AsyncMock(return_value=True)

    async def test_direct_power_on_does_not_send_wol(self):
        self.assertTrue(await self.tv_sync.turn_on())

        self.tv_sync.ip_control.power_on.assert_awaited_once()
        self.tv_sync._send_wol.assert_not_awaited()
        self.assertTrue(self.tv_sync.auto_started)

    async def test_already_on_tv_is_left_untouched(self):
        self.tv_sync.ip_control.get_power_state.return_value = "powerOn"

        self.assertTrue(await self.tv_sync.turn_on())

        self.tv_sync.ip_control.power_on.assert_not_awaited()
        self.tv_sync._send_wol.assert_not_awaited()
        self.assertFalse(self.tv_sync.auto_started)

    async def test_transport_failure_uses_wol_then_retries_explicit_power_on(self):
        self.tv_sync.ip_control.get_power_state.side_effect = IPControlTransportError("asleep")
        self.tv_sync.ip_control.power_on.side_effect = [
            IPControlTransportError("asleep"),
            "powerOn",
        ]

        self.assertTrue(await self.tv_sync.turn_on())

        self.assertEqual(self.tv_sync.ip_control.power_on.await_count, 2)
        self.tv_sync._send_wol.assert_awaited_once()
        self.assertEqual(
            [call.args[0] for call in self.tv_sync._wait_for_port.await_args_list],
            [1516, 8002],
        )

    async def test_auth_failure_does_not_send_wol(self):
        self.tv_sync.ip_control.get_power_state.side_effect = IPControlAuthError("rejected")

        self.assertFalse(await self.tv_sync.turn_on())

        self.tv_sync.ip_control.power_on.assert_not_awaited()
        self.tv_sync._send_wol.assert_not_awaited()

    async def test_missing_ip_permission_is_requested_in_normal_viewing(self):
        self.tv_sync.ip_control.paired = False
        self.tv_sync.pair_ip_control = AsyncMock(return_value=True)

        with patch.object(sync_artwork, "power_control_configured", return_value=True):
            await self.tv_sync._pair_missing_ip_control("off")

        self.tv_sync.pair_ip_control.assert_awaited_once()

    async def test_missing_ip_permission_is_deferred_in_art_mode(self):
        self.tv_sync.ip_control.paired = False
        self.tv_sync.pair_ip_control = AsyncMock(return_value=True)

        with patch.object(sync_artwork, "power_control_configured", return_value=True):
            await self.tv_sync._pair_missing_ip_control("on")

        self.tv_sync.pair_ip_control.assert_not_awaited()

    async def test_missing_ip_permission_is_ignored_without_power_schedule(self):
        self.tv_sync.ip_control.paired = False
        self.tv_sync.pair_ip_control = AsyncMock(return_value=True)

        with patch.object(sync_artwork, "power_control_configured", return_value=False):
            await self.tv_sync._pair_missing_ip_control("off")

        self.tv_sync.pair_ip_control.assert_not_awaited()

    async def test_auto_off_requires_positive_art_mode_status(self):
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv._get_device_info = AsyncMock(return_value={})

        self.assertFalse(
            await self.tv_sync.is_in_art_mode(require_positive=True)
        )
        self.assertTrue(await self.tv_sync.is_in_art_mode())

    async def test_auto_off_uses_positive_art_status_cached_during_connect(self):
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv._get_device_info = AsyncMock(return_value={})
        self.tv_sync.last_art_mode_status = "on"

        self.assertTrue(
            await self.tv_sync.is_in_art_mode(require_positive=True)
        )

    async def test_explicit_ip_power_off_does_not_fall_back_to_rest(self):
        self.tv_sync.ip_control.get_power_state.return_value = "powerOff"
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv._get_device_info = AsyncMock(
            return_value={"device": {"PowerState": "on"}}
        )

        self.assertFalse(await self.tv_sync._reports_powered_on())
        self.tv_sync.tv._get_device_info.assert_not_awaited()

    async def test_ip_power_off_is_verified_without_legacy_remote(self):
        self.tv_sync.ip_control.get_power_state.return_value = "powerOff"

        with (
            patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
            patch.object(sync_artwork, "SamsungTVWSAsyncRemote") as remote,
        ):
            self.assertTrue(await self.tv_sync.turn_off())

        self.tv_sync.ip_control.power_off.assert_awaited_once()
        remote.assert_not_called()

    async def test_ip_power_off_failure_uses_legacy_key_hold(self):
        self.tv_sync.ip_control.power_off.side_effect = IPControlTransportError("unreachable")
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv.on = AsyncMock(return_value=False)
        remote_instance = MagicMock()
        remote_instance.send_commands = AsyncMock()
        remote_instance.close = AsyncMock()

        with (
            patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
            patch.object(
                sync_artwork,
                "SamsungTVWSAsyncRemote",
                return_value=remote_instance,
            ),
        ):
            self.assertTrue(await self.tv_sync.turn_off())

        remote_instance.send_commands.assert_awaited_once()
        remote_instance.close.assert_awaited_once()

    async def test_ip_auth_failure_does_not_use_legacy_key_hold(self):
        self.tv_sync.ip_control.power_off.side_effect = IPControlAuthError("Parse error")

        with patch.object(sync_artwork, "SamsungTVWSAsyncRemote") as remote:
            self.assertFalse(await self.tv_sync.turn_off())

        remote.assert_not_called()

    async def test_ip_protocol_failure_does_not_use_legacy_key_hold(self):
        self.tv_sync.ip_control.power_off.side_effect = IPControlError("bad response")

        with patch.object(sync_artwork, "SamsungTVWSAsyncRemote") as remote:
            self.assertFalse(await self.tv_sync.turn_off())

        remote.assert_not_called()

    async def test_missing_ip_pairing_does_not_use_legacy_key_hold_for_auto_off(self):
        self.tv_sync.ip_control.paired = False

        with (
            patch.object(sync_artwork, "AUTO_OFF_TIME", "22:00"),
            patch.object(sync_artwork, "SamsungTVWSAsyncRemote") as remote,
        ):
            self.assertFalse(await self.tv_sync.turn_off())

        remote.assert_not_called()

    async def test_ensure_art_mode_leaves_existing_art_mode_untouched(self):
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv.get_artmode = AsyncMock(return_value="on")
        self.tv_sync.tv.set_artmode = AsyncMock()

        self.assertTrue(await self.tv_sync.ensure_art_mode())
        self.tv_sync.tv.set_artmode.assert_not_awaited()

    async def test_ensure_art_mode_enables_it_after_normal_viewing_resume(self):
        self.tv_sync.tv = MagicMock()
        self.tv_sync.tv.get_artmode = AsyncMock(side_effect=["off", "on"])
        self.tv_sync.tv.set_artmode = AsyncMock()

        with patch.object(sync_artwork.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await self.tv_sync.ensure_art_mode())

        self.tv_sync.tv.set_artmode.assert_awaited_once_with("on")


if __name__ == "__main__":
    unittest.main()
