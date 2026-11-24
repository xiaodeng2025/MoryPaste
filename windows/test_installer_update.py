import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desktop_app import DesktopApi, DesktopClient, main
from update_check import RELEASES, WINDOWS_MANIFEST, check_update, download_installer


class InstallerUpdateTests(unittest.TestCase):
    def manifest(self, feed=WINDOWS_MANIFEST, **fields):
        metadata = {"version": "0.6.7", "release_page": f"{RELEASES}/tag/v0.6.7", **fields}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(metadata).encode())
        with patch("update_check.urllib.request.build_opener", return_value=opener):
            return check_update("0.6.6", feed)

    def test_direct_install_requires_the_official_feed_page_and_hash(self):
        digest = "a" * 64
        self.assertNotIn("installer_sha256", self.manifest())
        self.assertNotIn("installer_sha256", self.manifest(installer_sha256="wrong"))
        self.assertNotIn("installer_sha256", self.manifest("https://example.com/update.json", installer_sha256=digest))
        self.assertNotIn("installer_sha256", self.manifest(release_page="https://example.com", installer_sha256=digest))
        self.assertEqual(self.manifest(installer_sha256=digest)["installer_sha256"], digest)

    def test_download_verifies_bytes_and_preserves_internet_zone(self):
        payload = b"simulated installer"
        with tempfile.TemporaryDirectory() as root:
            stage = Path(root) / "stage"
            stage.mkdir()
            response = io.BytesIO(payload)
            response.headers = {"Content-Length": str(len(payload))}
            opener = Mock()
            opener.open.return_value = response
            with patch("update_check.tempfile.mkdtemp", return_value=stage), \
                    patch("update_check.urllib.request.build_opener", return_value=opener):
                path = download_installer("0.6.7", hashlib.sha256(payload).hexdigest(), Mock())
            self.assertEqual(path.read_bytes(), payload)
            self.assertIn("ZoneId=3", path.with_name(f"{path.name}:Zone.Identifier").read_text("ascii"))
            opener.open.assert_called_once_with(f"{RELEASES}/download/v0.6.7/MoryPaste-0.6.7-Setup.exe", timeout=12)

    def test_corrupt_download_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            stage = Path(root) / "stage"
            stage.mkdir()
            response = io.BytesIO(b"unexpected bytes")
            response.headers = {}
            opener = Mock()
            opener.open.return_value = response
            with patch("update_check.tempfile.mkdtemp", return_value=stage), \
                    patch("update_check.urllib.request.build_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    download_installer("0.6.7", "0" * 64, Mock())
            self.assertFalse(stage.exists())

    def test_verification_failure_does_not_stop_receiver(self):
        client = Mock()
        api = DesktopApi(client)
        api._update_installer = ("0.6.7", "a" * 64)
        with patch("desktop_app.sys.frozen", True, create=True), \
                patch("desktop_app.download_installer", side_effect=ValueError("校验失败")), self.assertLogs():
            self.assertFalse(api.install_update()["started"])
        client._shutdown.assert_not_called()

    def test_stop_failure_does_not_queue_an_installer(self):
        client = DesktopClient()
        client.core = Mock()
        client.core.stop.return_value = False
        with patch("desktop_app.show_message"):
            self.assertFalse(client._shutdown((Path("fixture.exe"), "a" * 64)))
        self.assertIsNone(client.pending_installer)
        self.assertFalse(client._shutting_down)

    def test_main_rechecks_installer_after_releasing_instance_mutex(self):
        path = Path("fixture.exe")
        digest = "a" * 64
        sequence = Mock()
        instance = Mock(already_running=False, close=sequence.close)
        client = Mock(pending_installer=(path, digest))
        client.run.return_value = 0
        with patch("desktop_app.configure_logging"), patch("desktop_app.SingleInstance", return_value=instance), \
                patch("desktop_app.DesktopClient", return_value=client), \
                patch("desktop_app.verify_installer", sequence.verify), patch("desktop_app.os.startfile", sequence.launch):
            self.assertEqual(main(), 0)
        self.assertEqual([call.args for call in sequence.mock_calls], [(), (path, digest), (path,)])
