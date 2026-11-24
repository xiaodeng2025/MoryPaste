"""Pairing API tests using isolated in-memory state and temporary persistence."""
import base64
import copy
import http.server
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

import receiver


class PairingIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_state = getattr(receiver.TransferHandler, "state", None)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), receiver.TransferHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        receiver.TransferHandler.state = cls.old_state

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stcq-pair-test-")
        self.root = Path(self.temp.name)
        self.data_path = self.root / "paired-devices.json"
        self.diagnostics_path = self.root / "diagnostics"
        self.devices = {}
        self.state = SimpleNamespace(
            lock=threading.RLock(), devices=self.devices, pair_codes={},
            devices_path=self.data_path, last_event={},
            last_pairing=None, computer_id="stpc-" + "a" * 32,
            computer_name="pair-test", bind_ip="127.0.0.1", fingerprint="a" * 64,
        )

        def save_devices():
            temp_path = self.data_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self.state.devices), encoding="utf-8")
            temp_path.replace(self.data_path)

        self.state.save_devices = save_devices
        self.state.issue_pair_code = MethodType(receiver.State.issue_pair_code, self.state)
        self.state.claim_pair_code = MethodType(receiver.State.claim_pair_code, self.state)
        self.state.report = MethodType(receiver.State.report, self.state)
        receiver.TransferHandler.state = self.state
        self.log_patch = patch.object(receiver, "DIAGNOSTIC_DIR", self.diagnostics_path)
        self.log_patch.start()
        self.crypto_patch = patch.multiple(
            receiver,
            protect_device_secret=lambda value: value,
            unprotect_device_secret=lambda value: value,
        )
        self.crypto_patch.start()

    def tearDown(self):
        self.crypto_patch.stop()
        self.log_patch.stop()
        self.temp.cleanup()

    def claim(self, client_id=None, previous_id=None, name="nubia NX737J"):
        code, _ = self.state.issue_pair_code()
        body = {"pairingCode": code, "deviceName": name}
        if client_id is not None:
            body["clientInstanceId"] = client_id
        if previous_id is not None:
            body["previousDeviceId"] = previous_id
        request = urllib.request.Request(
            self.url + "/pair/claim", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_same_phone_repeated_pairing_updates_one_record_and_preserves_secret(self):
        client_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        status, first = self.claim(client_id)
        self.assertEqual(status, 200)
        self.assertEqual(first["pairingStatus"], "created")
        self.assertEqual(self.state.last_pairing["status"], "created")
        self.assertEqual(self.state.last_pairing["device"], "nubia NX737J")
        for _ in range(10):
            status, repeated = self.claim(client_id, first["deviceId"])
            self.assertEqual(status, 200)
            self.assertEqual(repeated["pairingStatus"], "updated")
            self.assertEqual(repeated["deviceId"], first["deviceId"])
            self.assertEqual(repeated["secret"], first["secret"])
        self.assertEqual(len(self.state.devices), 1)

    def test_same_model_phones_have_independent_records(self):
        first_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        _, first = self.claim(first_id)
        _, second = self.claim(second_id)
        self.assertNotEqual(first["deviceId"], second["deviceId"])
        self.assertEqual(len(self.state.devices), 2)
        _, repeated = self.claim(second_id, second["deviceId"])
        self.assertEqual(repeated["deviceId"], second["deviceId"])
        self.assertEqual(len(self.state.devices), 2)

    def test_legacy_authorization_migrates_by_exact_previous_device_id(self):
        device_id = "0123456789abcdef01234567"
        secret = b"l" * 32
        self.state.devices[device_id] = {
            "name": "nubia NX737J",
            "secretProtected": base64.b64encode(secret).decode("ascii"),
            "created": "2026-11-12T09:49:25+08:00",
        }
        client_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        status, response = self.claim(client_id, device_id)
        self.assertEqual(status, 200)
        self.assertEqual(response["pairingStatus"], "updated")
        self.assertEqual(response["deviceId"], device_id)
        self.assertEqual(base64.b64decode(response["secret"]), secret)
        self.assertEqual(len(self.state.devices), 1)
        self.assertEqual(self.state.devices[device_id]["clientInstanceId"], client_id)

    def test_conflicting_identity_and_previous_id_returns_conflict_without_mutation(self):
        one = "111111111111111111111111"
        two = "222222222222222222222222"
        client_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        self.state.devices.update({
            one: {"name": "phone A", "clientInstanceId": client_id, "secretProtected": base64.b64encode(b"a" * 32).decode(), "created": "a"},
            two: {"name": "phone B", "secretProtected": base64.b64encode(b"b" * 32).decode(), "created": "b"},
        })
        before = copy.deepcopy(self.state.devices)
        status, response = self.claim(client_id, two)
        self.assertEqual(status, 409)
        self.assertIn("不同设备", response["error"])
        self.assertEqual(self.state.devices, before)

    def test_failed_persistence_does_not_replace_live_authorization(self):
        original = {"old": {"name": "old", "secretProtected": "unchanged", "created": "old"}}
        self.state.devices = original

        def fail_save():
            raise OSError("test write failure")

        self.state.save_devices = fail_save
        code, _ = self.state.issue_pair_code()
        with self.assertRaises(OSError):
            receiver.State.claim_pair_code(self.state, code, "phone", "ffffffff-ffff-4fff-8fff-ffffffffffff", None)
        self.assertIs(self.state.devices, original)

    def test_old_android_request_without_new_identity_fields_still_works(self):
        status, result = self.claim()
        self.assertEqual(status, 200)
        self.assertEqual(result["pairingStatus"], "created")
        self.assertEqual(len(self.state.devices), 1)
        self.assertNotIn("clientInstanceId", next(iter(self.state.devices.values())))

    def test_pairing_records_survive_reload_from_isolated_file(self):
        client_id = "99999999-9999-4999-8999-999999999999"
        self.claim(client_id)
        persisted = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(next(iter(persisted.values()))["clientInstanceId"], client_id)


if __name__ == "__main__":
    unittest.main()
