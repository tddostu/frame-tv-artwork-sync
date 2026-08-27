import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

pysolar = types.ModuleType("pysolar")
pysolar_solar = types.ModuleType("pysolar.solar")
pysolar_solar.get_altitude = lambda *args, **kwargs: 0
sys.modules.setdefault("pysolar", pysolar)
sys.modules.setdefault("pysolar.solar", pysolar_solar)

import sync_artwork
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError


class ConnectionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sync_artwork._ART_TOKEN_REFRESH_ATTEMPTED.clear()

    async def test_art_handshake_ignores_broadcast_and_accepts_connect(self):
        connection = MagicMock()
        connection.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "event": "art_app_request",
                        "data": {"request": "get_current_artwork"},
                        "from": "another-client",
                    }
                ),
                json.dumps(
                    {
                        "event": "ms.channel.connect",
                        "data": {"token": "accepted-token"},
                    }
                ),
            ]
        )
        art_client = MagicMock()
        art_client.is_alive.return_value = False
        art_client._format_websocket_url.return_value = "wss://tv/art"
        art_client._is_ssl_connection.return_value = False
        art_client.endpoint = "com.samsung.art-app"
        art_client.timeout = 10
        art_client.host = "192.0.2.10"

        with patch.object(
            sync_artwork,
            "_upstream_ws_connect",
            AsyncMock(return_value=connection),
        ):
            result = await sync_artwork._open_art_channel_ignoring_broadcasts(
                art_client
            )

        self.assertIs(result, connection)
        self.assertEqual(connection.recv.await_count, 2)
        self.assertIs(art_client.connection, connection)
        art_client._check_for_token.assert_called_once()
        self.assertIn(
            "ms.channel.clientDisconnect",
            sync_artwork.async_connection.IGNORE_EVENTS_AT_STARTUP,
        )

    async def test_art_timeout_automatically_pairs_missing_ip_control(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("saved")
            tv_sync.ip_control = MagicMock(paired=False)

            async def pair_ip_control():
                tv_sync.ip_control.paired = True
                return True

            tv_sync.pair_ip_control = AsyncMock(side_effect=pair_ip_control)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(side_effect=asyncio.TimeoutError)
            connected = MagicMock()
            connected.get_artmode = AsyncMock(return_value="off")
            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 2),
                patch.object(sync_artwork, "power_control_configured", return_value=True),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=[timed_out, connected],
                ),
            ):
                self.assertTrue(await tv_sync._try_connect())

            tv_sync.pair_ip_control.assert_awaited_once()

    def test_remote_pairing_opens_exactly_one_channel(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            connection = MagicMock()

            def issue_token():
                tv_sync.token_file.write_text("issued")

            connection.open.side_effect = issue_token
            with patch.object(
                sync_artwork,
                "SamsungTVWSConnection",
                return_value=connection,
            ) as connection_class:
                tv_sync._pair_via_remote_channel()

            connection_class.assert_called_once_with(
                host="192.0.2.10",
                endpoint=sync_artwork.REMOTE_ENDPOINT,
                port=8002,
                token_file=str(tv_sync.token_file),
                timeout=sync_artwork.AUTH_TIMEOUT,
                name=sync_artwork.CLIENT_NAME,
            )
            connection.open.assert_called_once_with()
            connection.close.assert_called_once_with()

    def test_art_client_constructor_hook_never_initiates_pairing(self):
        art_client = MagicMock()

        self.assertIsNone(sync_artwork._skip_implicit_art_token_pairing(art_client))

    async def test_art_timeout_does_not_pair_ip_without_power_schedule(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("saved")
            tv_sync.ip_control = MagicMock(paired=False)
            tv_sync.pair_ip_control = AsyncMock(return_value=True)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(side_effect=asyncio.TimeoutError)
            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 1),
                patch.object(sync_artwork, "power_control_configured", return_value=False),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(sync_artwork, "SamsungTVAsyncArt", return_value=timed_out),
            ):
                self.assertFalse(await tv_sync._try_connect())

            tv_sync.pair_ip_control.assert_not_awaited()

    async def test_art_handshake_timeout_retries_with_saved_token(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("saved")
            tv_sync.ip_control = MagicMock(paired=True)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(side_effect=asyncio.TimeoutError)
            connected = MagicMock()
            connected.get_artmode = AsyncMock(return_value="off")
            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 2),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=[timed_out, connected],
                ),
            ):
                self.assertTrue(await tv_sync._try_connect())

            self.assertEqual(tv_sync.token_file.read_text(), "saved")

    async def test_first_time_pairing_budget_is_not_repeated_by_connect_loop(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)
            tv_sync._acquire_token = AsyncMock(return_value=False)

            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 3),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
            ):
                self.assertFalse(await tv_sync._try_connect())

            tv_sync._acquire_token.assert_awaited_once()

    async def test_missing_art_status_response_retries_with_saved_token(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("saved")
            tv_sync.ip_control = MagicMock(paired=True)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            no_response = MagicMock()
            no_response.get_artmode = AsyncMock(side_effect=AssertionError)
            connected = MagicMock()
            connected.get_artmode = AsyncMock(return_value="on")
            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 2),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=[no_response, connected],
                ),
            ):
                self.assertTrue(await tv_sync._try_connect())

            self.assertEqual(tv_sync.token_file.read_text(), "saved")

    async def test_wait_without_connections_logs_next_cycle_delay(self):
        with (
            patch.object(sync_artwork, "SYNC_INTERVAL_MINUTES", 1),
            patch.object(sync_artwork, "KEEPALIVE_INTERVAL", 60),
            patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
            self.assertLogs("sync_artwork", level="INFO") as logs,
        ):
            await sync_artwork.wait_until_next_sync([])

        self.assertTrue(
            any("Waiting 1 minute(s) until next sync" in line for line in logs.output)
        )

    async def test_explicit_unauthorized_deletes_artwork_token(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("stale")
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 1),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=UnauthorizedError({"event": "ms.channel.unauthorized"}),
                ),
            ):
                self.assertFalse(await tv_sync._try_connect())

            self.assertFalse(tv_sync.token_file.exists())

    async def test_channel_timeout_preserves_existing_token(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("still-valid-until-proven-otherwise")
            tv_sync.ip_control = MagicMock(paired=True)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 1),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=ConnectionFailure({"event": "ms.channel.timeOut"}),
                ),
            ):
                self.assertFalse(await tv_sync._try_connect())

            self.assertEqual(
                tv_sync.token_file.read_text(),
                "still-valid-until-proven-otherwise",
            )

    async def test_awake_tv_retries_a_refresh_that_tv_closes_too_quickly(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("stale")
            tv_sync.ip_control_file = Path(token_dir) / "ip-control.json"
            tv_sync.ip_control_file.write_text('{"token": "ip-token"}')
            tv_sync.ip_control = MagicMock(paired=False)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(
                side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
            )
            timed_out._get_device_info = AsyncMock(
                return_value={"device": {"PowerState": "on"}}
            )
            connected = MagicMock()
            connected.get_artmode = AsyncMock(return_value="off")

            def timeout_then_issue_replacement(refresh_token_file):
                self.assertEqual(tv_sync.token_file.read_text(), "stale")
                self.assertNotEqual(refresh_token_file, tv_sync.token_file)
                if tv_sync._pair_via_remote_channel.call_count == 1:
                    raise ConnectionFailure({"event": "ms.channel.timeOut"})
                refresh_token_file.write_text("replacement")

            tv_sync._pair_via_remote_channel = MagicMock(
                side_effect=timeout_then_issue_replacement
            )
            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 2),
                patch.object(sync_artwork, "power_control_configured", return_value=False),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(
                    sync_artwork,
                    "SamsungTVAsyncArt",
                    side_effect=[timed_out, connected],
                ) as art_client,
            ):
                self.assertTrue(await tv_sync._try_connect())

            self.assertEqual(art_client.call_count, 2)
            self.assertEqual(tv_sync._pair_via_remote_channel.call_count, 2)
            self.assertEqual(tv_sync.token_file.read_text(), "replacement")
            self.assertEqual(tv_sync.ip_control_file.read_text(), '{"token": "ip-token"}')

    async def test_cancelled_guarded_refresh_never_removes_saved_token(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("preserve-me")
            tv_sync._pair_via_remote_channel = MagicMock(
                side_effect=asyncio.CancelledError
            )

            with self.assertRaises(asyncio.CancelledError):
                await tv_sync._refresh_art_token_once()

            self.assertEqual(tv_sync.token_file.read_text(), "preserve-me")

    async def test_failed_guarded_refresh_keeps_token_and_stops_prompting(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("preserve-me")
            tv_sync.ip_control = MagicMock(paired=False)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(
                side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
            )
            timed_out._get_device_info = AsyncMock(
                return_value={"device": {"PowerState": "on"}}
            )
            tv_sync._pair_via_remote_channel = MagicMock(
                side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
            )

            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 3),
                patch.object(sync_artwork, "power_control_configured", return_value=False),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(sync_artwork, "SamsungTVAsyncArt", return_value=timed_out) as art_client,
            ):
                self.assertFalse(await tv_sync._try_connect())

            self.assertEqual(art_client.call_count, 1)
            self.assertEqual(tv_sync._pair_via_remote_channel.call_count, 2)
            self.assertEqual(tv_sync.token_file.read_text(), "preserve-me")

    async def test_sleeping_tv_does_not_attempt_guarded_token_refresh(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("saved")
            tv_sync.ip_control = MagicMock(paired=False)
            tv_sync._is_tv_reachable = AsyncMock(return_value=True)

            timed_out = MagicMock()
            timed_out.get_artmode = AsyncMock(
                side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
            )
            timed_out._get_device_info = AsyncMock(
                return_value={"device": {"PowerState": "standby"}}
            )
            tv_sync._pair_via_remote_channel = MagicMock()

            with (
                patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 2),
                patch.object(sync_artwork, "power_control_configured", return_value=False),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                patch.object(sync_artwork, "SamsungTVAsyncArt", return_value=timed_out),
            ):
                self.assertFalse(await tv_sync._try_connect())

            tv_sync._pair_via_remote_channel.assert_not_called()
            self.assertEqual(tv_sync.token_file.read_text(), "saved")

    async def test_failed_refresh_is_not_reoffered_until_service_restart(self):
        with tempfile.TemporaryDirectory() as token_dir:
            token_file = Path(token_dir) / "token.txt"
            token_file.write_text("saved")

            async def run_cycle():
                tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
                tv_sync.token_file = token_file
                tv_sync.ip_control = MagicMock(paired=False)
                tv_sync._is_tv_reachable = AsyncMock(return_value=True)
                timed_out = MagicMock()
                timed_out.get_artmode = AsyncMock(
                    side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
                )
                timed_out._get_device_info = AsyncMock(
                    return_value={"device": {"PowerState": "on"}}
                )
                tv_sync._pair_via_remote_channel = MagicMock(
                    side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
                )
                with (
                    patch.object(sync_artwork, "CONNECT_MAX_ATTEMPTS", 1),
                    patch.object(sync_artwork, "power_control_configured", return_value=False),
                    patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
                    patch.object(sync_artwork, "SamsungTVAsyncArt", return_value=timed_out),
                ):
                    self.assertFalse(await tv_sync._try_connect())
                return tv_sync

            first_cycle = await run_cycle()
            second_cycle = await run_cycle()

            self.assertEqual(first_cycle._pair_via_remote_channel.call_count, 2)
            second_cycle._pair_via_remote_channel.assert_not_called()
            self.assertEqual(token_file.read_text(), "saved")

    async def test_slow_failed_refresh_is_not_retried(self):
        with tempfile.TemporaryDirectory() as token_dir:
            tv_sync = sync_artwork.TVArtworkSync("192.0.2.10")
            tv_sync.token_file = Path(token_dir) / "token.txt"
            tv_sync.token_file.write_text("preserve-me")
            tv_sync._pair_via_remote_channel = MagicMock(
                side_effect=ConnectionFailure({"event": "ms.channel.timeOut"})
            )

            with (
                patch.object(sync_artwork, "FAST_PAIRING_TIMEOUT_THRESHOLD", 0),
                patch.object(sync_artwork.asyncio, "sleep", AsyncMock()),
            ):
                self.assertFalse(await tv_sync._refresh_art_token_once())

            tv_sync._pair_via_remote_channel.assert_called_once()
            self.assertEqual(tv_sync.token_file.read_text(), "preserve-me")

    async def test_auto_off_does_not_wait_for_another_tvs_pairing(self):
        blocked_connect = asyncio.Event()
        release_connect = asyncio.Event()
        healthy_turned_off = asyncio.Event()

        class FakeTV:
            def __init__(self, ip):
                self.tv_ip = ip

            async def connect(self):
                if self.tv_ip == "blocked":
                    blocked_connect.set()
                    await release_connect.wait()
                    return False
                return True

            async def is_in_art_mode(self, *, require_positive=False):
                self.require_positive = require_positive
                return True

            async def turn_off(self):
                healthy_turned_off.set()
                return True

            async def close(self):
                return None

        async def no_wait(_):
            return None

        with (
            patch.object(sync_artwork, "TV_IPS", ["blocked", "healthy"]),
            patch.object(sync_artwork, "TVArtworkSync", FakeTV),
            patch.object(sync_artwork, "is_within_auto_off_window", return_value=True),
            patch.object(sync_artwork, "should_attempt_auto_on", return_value=False),
            patch.object(sync_artwork, "wait_until_next_sync", no_wait),
        ):
            task = asyncio.create_task(sync_artwork.sync_all_tvs())
            await asyncio.wait_for(blocked_connect.wait(), timeout=1)
            await asyncio.wait_for(healthy_turned_off.wait(), timeout=1)
            self.assertFalse(task.done())
            release_connect.set()
            await asyncio.wait_for(task, timeout=1)


if __name__ == "__main__":
    unittest.main()
