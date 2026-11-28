"""Actual local management handler, isolated in-memory state and random test port."""
import http.server
import json
import socket
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import receiver


class ManagementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_state = getattr(receiver.DashboardHandler, "state", None)
        state = SimpleNamespace(lock=threading.RLock(), devices={}, last_pairing=None, bind_ip="192.168.3.53", interface="test", interface_index=1,
                                copyq_version="not checked", fingerprint="test", computer_id="test", computer_name="test",
                                discovery_status="registered", pair_codes={}, last_event={}, clipboard_stream=None,
                                outgoing={})
        state.issue_pair_code = lambda: receiver.State.issue_pair_code(state)
        receiver.DashboardHandler.state = state
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), receiver.DashboardHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        receiver.DashboardHandler.state = cls.old_state

    def test_page_assets_and_version(self):
        for path, content_type in (("/", "text/html"), ("/desktop.css", "text/css"), ("/desktop.js", "text/javascript")):
            with self.opener.open(self.url + path, timeout=3) as response:
                self.assertTrue(response.headers["Content-Type"].startswith(content_type))
                self.assertGreater(len(response.read()), 100)
        with self.opener.open(self.url + "/api/state", timeout=3) as response:
            state = json.load(response)
        self.assertEqual(state["appVersion"], receiver.APP_VERSION)
        self.assertEqual(state["protocolVersion"], "1")
        self.assertIsInstance(state["copyqInstalled"], bool)

    def test_unknown_static_path_is_not_exposed(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(self.url + "/receiver.py", timeout=3)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_pairing_qr_uses_existing_api_without_real_pairing_data(self):
        request = urllib.request.Request(self.url + "/api/pairing", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=3) as response:
            result = json.load(response)
        self.assertEqual(result["expiresIn"], 300)
        self.assertIn("<svg", result["svg"])
        self.assertIn("path", result["svg"])
        svg = ET.fromstring(result["svg"])
        width = float(svg.attrib["width"].removesuffix("px"))
        height = float(svg.attrib["height"].removesuffix("px"))
        view_box = [float(value) for value in svg.attrib["viewBox"].split()]
        self.assertEqual(view_box, [0.0, 0.0, width, height])

    def test_local_only_mode_rejects_qr_and_unauthenticated_start(self):
        state = self.server.RequestHandlerClass.state
        lan = Mock(token="test-control", state_name="waiting")
        with patch.object(state, "lan", lan, create=True):
            for route, headers, body, status in (
                ("/api/pairing", {}, b"{}", 409),
                ("/api/local/lan-start", {}, b"{}", 403),
                ("/api/local/lan-start", {"X-MoryPaste-Lan-Control": "test-control"}, b"null", 400),
            ):
                request = urllib.request.Request(self.url + route, data=body, headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.opener.open(request, timeout=3)
                with caught.exception as response:
                    self.assertEqual(response.code, status)
        lan.start.assert_not_called()

    def test_authenticated_local_start_passes_selected_endpoint(self):
        state = self.server.RequestHandlerClass.state
        lan = Mock(token="test-control", state_name="waiting")
        expected = {"ip": state.bind_ip, "interface": state.interface, "interfaceIndex": state.interface_index}
        with patch.object(state, "lan", lan, create=True):
            request = urllib.request.Request(self.url + "/api/local/lan-start", data=json.dumps(expected).encode("utf-8"),
                                             headers={"X-MoryPaste-Lan-Control": "test-control"})
            with self.opener.open(request, timeout=3) as response:
                self.assertTrue(json.load(response)["ok"])
        lan.start.assert_called_once_with(expected)

    def test_changed_network_cannot_start_an_unchecked_listener(self):
        state = SimpleNamespace(bind_ip="192.168.3.53", interface="test", interface_index=1,
                                stop_event=threading.Event())
        lan = receiver.LanReceiver(state, Path("cert"), Path("key"), "test-control")
        expected = {"ip": state.bind_ip, "interface": state.interface, "interfaceIndex": state.interface_index}
        with patch("receiver.discover_lan_endpoint", return_value=("192.168.3.54", "test", 1)), patch(
            "receiver.http.server.ThreadingHTTPServer"
        ) as listener:
            with self.assertRaisesRegex(ValueError, "已变化"):
                lan.start(expected)
        listener.assert_not_called()
        self.assertEqual(lan.state_name, "waiting")

    def test_verified_local_start_opens_listener_and_close_releases_it(self):
        endpoint = ("127.0.0.1", "test", 1)
        state = SimpleNamespace(bind_ip=endpoint[0], interface=endpoint[1], interface_index=endpoint[2],
                                stop_event=threading.Event(), computer_id="test", computer_name="test")
        expected = dict(zip(("ip", "interface", "interfaceIndex"), endpoint))
        with tempfile.TemporaryDirectory() as temp, patch("receiver.app_data_dir", return_value=Path(temp)), patch(
            "receiver.discover_lan_endpoint", return_value=endpoint
        ), patch("receiver.TLS_PORT", 0), patch("receiver.MdnsRegistration") as mdns:
            cert, key, _fingerprint = receiver.certificate_material(endpoint[0])
            lan = receiver.LanReceiver(state, cert, key, "test-control")
            self.assertIsNone(lan.transfer)
            mdns.assert_not_called()
            try:
                lan.start(expected)
                address = lan.transfer.server_address
                with socket.create_connection(address, timeout=2):
                    self.assertEqual(lan.state_name, "running")
                lan.start(expected)
                mdns.assert_called_once()
            finally:
                lan.close()
            with self.assertRaises(OSError):
                socket.create_connection(address, timeout=1)

    def test_clipboard_authorization_survives_disconnect(self):
        state = self.server.RequestHandlerClass.state
        devices = {"one": {"clipboardSync": True}, "two": {}}
        with patch.object(state, "devices", devices), patch.object(state, "clipboard_stream", (object(), "one")):
            with self.opener.open(self.url + "/api/state", timeout=3) as response:
                connected, unauthorized = json.load(response)["devices"]
            self.assertTrue(connected["clipboardSync"])
            self.assertTrue(connected["clipboardConnected"])
            self.assertFalse(unauthorized["clipboardSync"])
            self.assertFalse(unauthorized["clipboardConnected"])
            state.clipboard_stream = None
            with self.opener.open(self.url + "/api/state", timeout=3) as response:
                disconnected = json.load(response)["devices"][0]
            self.assertTrue(disconnected["clipboardSync"])
            self.assertFalse(disconnected["clipboardConnected"])

    def test_state_returns_one_row_per_authorization_and_last_pairing_status(self):
        self.server.RequestHandlerClass.state.devices = {
            "one": {"name": "nubia NX737J", "created": "2026-11-16T10:00:00+08:00"},
            "two": {"name": "nubia NX737J", "created": "2026-11-16T11:00:00+08:00"},
        }
        self.server.RequestHandlerClass.state.last_pairing = {
            "status": "updated", "device": "nubia NX737J", "time": 100.0,
        }
        with self.opener.open(self.url + "/api/state", timeout=3) as response:
            state = json.load(response)
        self.assertEqual(len(state["devices"]), 2)
        self.assertEqual(state["lastPairing"]["status"], "updated")
        self.assertEqual(state["lastPairing"]["device"], "nubia NX737J")


if __name__ == "__main__":
    unittest.main()
