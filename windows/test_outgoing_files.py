"""Exercise the authenticated outgoing protocol with isolated files and real TLS."""
import base64
import hashlib
import hmac
import http.server
import json
import secrets
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import receiver


class OutgoingFilesTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.enterContext(patch.object(receiver, "app_data_dir", return_value=self.root))
        self.enterContext(patch.object(receiver, "desktop_event"))
        cert, key, fingerprint = receiver.certificate_material("127.0.0.1")
        self.state = receiver.State("127.0.0.1", "test", fingerprint, 1)
        self.state.lan = SimpleNamespace(state_name="running")
        self.state.outgoing_token = secrets.token_hex(16)
        self.paired = {device: secrets.token_bytes(32) for device in ("target", "other")}
        self.state.devices = {
            device: {"secretProtected": base64.b64encode(receiver.protect_device_secret(secret)).decode()}
            for device, secret in self.paired.items()
        }
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert, key)
        client_tls = ssl.create_default_context(cafile=str(cert))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=client_tls)
        )
        self.dashboard = self.start_server(receiver.DashboardHandler)
        self.lan = self.start_server(receiver.TransferHandler, server_tls)

    def start_server(self, handler, tls=None):
        isolated_handler = type("IsolatedHandler", (handler,), {"state": self.state})
        server = self.enterContext(http.server.ThreadingHTTPServer(("127.0.0.1", 0), isolated_handler))
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}"

    def post(self, url, body, headers):
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def send(self, *paths, authorized=True):
        return self.post(self.dashboard + "/api/local/outgoing",
                         json.dumps({"deviceId": "target", "paths": list(map(str, paths))}).encode(),
                         {"X-MoryPaste-Lan-Control": self.state.outgoing_token if authorized else ""})[0]

    def phone(self, action, device="target", valid=True, retries=4, **values):
        mime = "application/vnd.morypaste.outgoing"
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(18)
        body = json.dumps({"action": action, **values}).encode()
        digest = hashlib.sha256(body).hexdigest()
        canonical = f"{timestamp}\n{nonce}\n{mime}\n{len(body)}\n{digest}".encode()
        signature = hmac.new(self.paired[device], canonical, hashlib.sha256).hexdigest() if valid else "0" * 64
        headers = {
            "Content-Type": mime, "X-Device-Id": device, "X-Timestamp": timestamp,
            "X-Nonce": nonce, "X-Content-SHA256": digest, "X-Signature": signature,
        }
        status, response = self.post(self.lan + "/outgoing", body, headers)
        if status == 425 and retries:
            # The handset retries with a fresh nonce while the previous file closes.
            time.sleep(0.1)
            return self.phone(action, device=device, valid=valid, retries=retries - 1, **values)
        return status, response

    def offer(self, device="target"):
        status, response = self.phone("poll", device=device)
        self.assertEqual(status, 200)
        return json.loads(response)["offer"]

    def status(self):
        with self.opener.open(self.dashboard + "/api/state", timeout=3) as response:
            return next(row for row in json.load(response)["devices"] if row["id"] == "target")

    def file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_only_target_can_accept_and_download_with_text_sync_off(self):
        files = [(self.file("传档.txt", b"example" * 20000), b"example" * 20000),
                 (self.file("empty.txt", b""), b"")]
        self.assertEqual(self.send(*(path for path, _ in files)), 200)
        self.assertEqual(self.status()["outgoing"]["status"], "waiting")
        self.assertGreater(self.status()["outgoing"]["remainingMs"], 89000)
        self.assertFalse(self.status()["clipboardSync"])
        offer = self.offer()
        transfer = offer["id"]
        self.assertEqual(offer["files"], [{"name": p.name, "size": len(data)} for p, data in files])
        self.assertIsNone(self.offer("other"))
        self.assertEqual(self.phone("poll", valid=False)[0], 401)
        self.assertEqual(self.phone("accept", device="other", id=transfer)[0], 409)
        self.assertEqual(self.phone("download", id=transfer, index=0)[0], 409)
        self.assertEqual(self.phone("accept", id=transfer)[0], 200)
        self.assertEqual(self.phone("complete", id=transfer)[0], 409)
        for index, (_, expected) in enumerate(files):
            self.assertEqual(self.phone("download", id=transfer, index=index), (200, expected))
        self.assertTrue(self.status()["outgoing"]["saving"])
        self.assertEqual(self.phone("complete", id=transfer)[0], 200)
        self.assertEqual(self.phone("complete", id=transfer)[0], 200)
        self.assertEqual(self.status()["outgoing"], {"id": transfer, "status": "complete", "total": 140000, "sent": 140000})
        with self.state.lock:
            self.state.outgoing["target"]["updated"] -= 10
        self.assertEqual(self.phone("complete", id=transfer)[0], 200)
        self.assertIsNone(self.status()["outgoing"])

    def test_reject_and_retry(self):
        path = self.file("test.txt", b"test")
        self.assertEqual(self.send(path, authorized=False), 403)
        self.assertEqual(self.send(path), 200)
        transfer = self.offer()["id"]
        self.assertEqual(self.send(path), 409)
        self.assertEqual(self.phone("reject", id=transfer)[0], 200)
        self.assertEqual(self.status()["outgoing"]["status"], "rejected")
        self.assertEqual(self.phone("download", id=transfer, index=0)[0], 409)
        self.assertEqual(self.send(path), 200)
        self.assertNotEqual(self.offer()["id"], transfer)

    def test_file_removed_after_invitation_reports_failure(self):
        path = self.file("test.txt", b"test")
        self.assertEqual(self.send(path), 200)
        transfer = self.offer()["id"]
        path.unlink()
        self.assertEqual(self.phone("accept", id=transfer)[0], 200)
        self.assertEqual(self.phone("download", id=transfer, index=0)[0], 409)
        self.assertEqual(self.status()["outgoing"]["status"], "failed")

    def test_invitation_expires(self):
        self.assertEqual(self.send(self.file("test.txt", b"test")), 200)
        transfer = self.offer()["id"]
        with self.state.lock:
            self.state.outgoing["target"]["updated"] -= 88
        self.assertGreater(self.status()["outgoing"]["remainingMs"], 0)
        self.assertLessEqual(self.status()["outgoing"]["remainingMs"], 2000)
        with self.state.lock:
            self.state.outgoing["target"]["updated"] -= 2
        self.assertIsNone(self.offer())
        self.assertEqual(self.phone("accept", id=transfer)[0], 409)
        self.assertEqual(self.status()["outgoing"]["status"], "timeout")
