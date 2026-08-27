import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from samsung_ip_control import IPControlAuthError, IPControlError, SamsungIPControl


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeConnection:
    responses = []
    requests = []

    def __init__(self, host, port, timeout, context):
        self.host = host
        self.port = port

    def request(self, method, path, body, headers):
        self.requests.append(json.loads(body))

    def getresponse(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    def close(self):
        pass


class SamsungIPControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeConnection.responses = []
        FakeConnection.requests = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tempdir.name) / "ip-control.json"

    def tearDown(self):
        self.tempdir.cleanup()

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_pair_persists_token_and_power_commands_reuse_it(self):
        FakeConnection.responses = [
            {"result": {"AccessToken": "secret"}},
            {"result": {"power": "powerOff"}},
            {"result": {"power": "powerOn"}},
        ]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        self.assertEqual(await client.pair(), "secret")
        self.assertEqual(await client.power_off(), "powerOff")
        self.assertEqual(await client.power_on(), "powerOn")

        saved = json.loads(self.token_file.read_text())
        self.assertEqual(saved, {"token": "secret", "port": 1516})
        self.assertNotIn("params", FakeConnection.requests[0])
        self.assertEqual(
            FakeConnection.requests[1]["params"],
            {"power": "powerOff", "AccessToken": "secret"},
        )

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_rejected_token_has_distinct_error(self):
        self.token_file.write_text(json.dumps({"token": "stale", "port": 1516}))
        FakeConnection.responses = [
            {"error": {"code": -32010, "message": "Unauthorized"}}
        ]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        with self.assertRaises(IPControlAuthError):
            await client.get_power_state()

        self.assertFalse(client.paired)
        self.assertFalse(self.token_file.exists())

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_bare_parse_error_rejects_stale_token(self):
        self.token_file.write_text(json.dumps({"token": "still-valid", "port": 1516}))
        FakeConnection.responses = [
            {"code": -32700, "message": "Parse error"}
        ]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        with self.assertRaises(IPControlAuthError):
            await client.get_power_state()

        self.assertFalse(client.paired)
        self.assertFalse(self.token_file.exists())

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_parse_error_without_token_is_not_auth_rejection(self):
        FakeConnection.responses = [
            {"code": -32700, "message": "Parse error"},
            ConnectionRefusedError("refused 1515"),
        ]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        with self.assertRaises(IPControlError):
            await client.pair()

        self.assertFalse(client.paired)
        self.assertFalse(self.token_file.exists())

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_pair_reports_failures_for_both_ports(self):
        FakeConnection.responses = [
            ConnectionRefusedError("refused 1516"),
            ConnectionRefusedError("refused 1515"),
        ]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        with self.assertRaises(IPControlError) as raised:
            await client.pair()

        message = str(raised.exception)
        self.assertIn("port 1516", message)
        self.assertIn("refused 1516", message)
        self.assertIn("port 1515", message)
        self.assertIn("refused 1515", message)

    @patch("samsung_ip_control.http.client.HTTPSConnection", FakeConnection)
    async def test_pair_does_not_expose_token_when_persistence_fails(self):
        FakeConnection.responses = [{"result": {"AccessToken": "secret"}}]
        client = SamsungIPControl("192.0.2.10", self.token_file)

        with (
            patch.object(Path, "replace", side_effect=OSError("disk full")),
            self.assertRaises(IPControlError),
        ):
            await client.pair()

        self.assertFalse(client.paired)
        self.assertFalse(self.token_file.exists())


if __name__ == "__main__":
    unittest.main()
