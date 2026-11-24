import json
import os
import tempfile
import unittest
import subprocess
import winreg
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

from desktop_settings import DesktopSettings, STARTUP_NAME
from desktop_app import DesktopApi, DesktopClient
from management_view import recent_receipts


class DesktopProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = DesktopSettings(self.root / "data", self.root / "app", self.root / "startup")

    def test_default_read_does_not_create_preferences_or_startup(self):
        self.assertFalse(self.settings.preferences()["notifications"])
        with patch.object(self.settings, "_approval", return_value="unregistered"):
            self.assertEqual(self.settings.startup_state()["state"], "off")
        self.assertFalse(self.settings.path.exists())
        self.assertFalse(self.settings.startup_dir.exists())

    def test_notification_preference_persists_in_user_data(self):
        self.settings.set_notifications(True)
        self.assertTrue(self.settings.preferences()["notifications"])

    def test_windows_approval_decodes_enabled_disabled_and_unknown_without_writes(self):
        for code, expected in ((2, "enabled"), (3, "system_disabled"), (99, "unknown")):
            with self.subTest(code=code), patch("desktop_settings.winreg.OpenKey") as opened, patch(
                "desktop_settings.winreg.QueryValueEx", return_value=(code.to_bytes(4, "little") + bytes(8), winreg.REG_BINARY)
            ), patch("desktop_settings.winreg.SetValueEx") as write:
                self.assertEqual(self.settings._approval(), expected)
                self.assertEqual(opened.call_args.args[3], winreg.KEY_READ)
                write.assert_not_called()

    def test_external_windows_disable_is_reread_and_does_not_change_current_process(self):
        self.settings.startup_dir.mkdir()
        (self.settings.startup_dir / STARTUP_NAME).touch()
        with patch.object(self.settings, "_approval", side_effect=["enabled", "system_disabled"]), patch(
            "desktop_settings.subprocess.run"
        ) as run:
            self.assertTrue(self.settings.startup_state()["enabled"])
            self.assertEqual(self.settings.startup_state()["state"], "system_disabled")
            run.assert_not_called()

    def test_windows_disabled_entry_is_not_reenabled(self):
        self.settings.startup_dir.mkdir()
        link = self.settings.startup_dir / STARTUP_NAME
        link.write_bytes(b"existing shortcut")
        with patch.object(self.settings, "_approval", return_value="system_disabled"), patch(
            "desktop_settings.subprocess.run"
        ) as run:
            state = self.settings.set_startup(True)
        self.assertEqual(state["state"], "system_disabled")
        self.assertFalse(state["enabled"])
        run.assert_not_called()
        self.assertEqual(link.read_bytes(), b"existing shortcut")

    def test_no_shortcut_but_system_disabled_does_not_bypass_windows(self):
        with patch.object(self.settings, "_approval", return_value="system_disabled"), patch(
            "desktop_settings.subprocess.run"
        ) as run:
            with self.assertRaises(ValueError):
                self.settings.set_startup(True)
        run.assert_not_called()

    def test_unknown_approval_does_not_claim_enabled(self):
        self.settings.startup_dir.mkdir()
        (self.settings.startup_dir / STARTUP_NAME).touch()
        with patch.object(self.settings, "_approval", return_value="unknown"):
            state = self.settings.startup_state()
        self.assertTrue(state["registered"])
        self.assertFalse(state["enabled"])

    def test_opt_in_shortcut_points_to_current_build_and_hidden_flag(self):
        def create_shortcut(*args, **kwargs):
            env = kwargs["env"]
            self.assertIn("desktop_app.py", env["STCQ_ARGUMENTS"])
            self.assertIn("--start-hidden", env["STCQ_ARGUMENTS"])
            self.assertTrue(env["STCQ_TARGET"].endswith("pythonw.exe"))
            Path(env["STCQ_SHORTCUT"]).touch()
            return Mock(returncode=0)
        with patch.object(self.settings, "_approval", return_value="unregistered"), patch(
            "desktop_settings.subprocess.run", side_effect=create_shortcut
        ):
            state = self.settings.set_startup(True)
        self.assertEqual(state["state"], "registered")
        self.assertFalse(state["enabled"])

    def test_real_windows_shortcut_roundtrip_in_isolated_directory(self):
        settings = DesktopSettings(self.root / "data", Path(__file__).parent, self.root / "startup")
        with patch.object(settings, "_approval", return_value="unregistered"):
            settings.set_startup(True)
            link = settings.startup_dir / STARTUP_NAME
            original_time = link.stat().st_mtime_ns
            settings.set_startup(True)
            self.assertEqual(link.stat().st_mtime_ns, original_time)
            self.assertEqual(len(list(settings.startup_dir.glob("*.lnk"))), 1)
            env = os.environ.copy()
            env["STCQ_TEST_LINK"] = str(link)
            script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:STCQ_TEST_LINK); @{target=$s.TargetPath;arguments=$s.Arguments;directory=$s.WorkingDirectory}|ConvertTo-Json -Compress"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(result.stdout)
            self.assertTrue(Path(saved["target"]).is_file())
            self.assertTrue(saved["target"].endswith("pythonw.exe"))
            self.assertIn(str(Path(__file__).parent / "desktop_app.py"), saved["arguments"])
            self.assertIn("--start-hidden", saved["arguments"])
            settings.set_startup(False)
            self.assertFalse(link.exists())

    def test_disable_only_removes_own_shortcut(self):
        self.settings.startup_dir.mkdir()
        (self.settings.startup_dir / STARTUP_NAME).touch()
        other = self.settings.startup_dir / "Other.lnk"
        other.touch()
        with patch.object(self.settings, "_approval", return_value="enabled"):
            self.assertEqual(self.settings.set_startup(False)["state"], "off")
        self.assertTrue(other.exists())

    def test_recent_receipts_only_projects_success_metadata(self):
        rows = [
            {"task_id": "t", "image_index": 1, "timestamp": "2026-11-13T00:00:01Z", "event": "DEVICE_AUTHENTICATED", "device_name": "Phone", "mime": "image/png", "secret": "never export"},
            {"task_id": "t", "image_index": 1, "timestamp": "2026-11-13T00:00:02Z", "event": "RECEIVE_FINISHED", "status": "ok", "bytes_received": 42, "backend": "CopyQ"},
            {"task_id": "bad", "timestamp": "2026-11-13T00:00:03Z", "event": "RECEIVE_FINISHED", "status": "failed"},
        ]
        (self.root / "windows-test.jsonl").write_text("\n".join(json.dumps(r) for r in rows), "utf-8")
        result = recent_receipts(self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["device"], "Phone")
        self.assertEqual(result[0]["bytes"], 42)
        self.assertEqual(result[0]["mime"], "image/png")
        self.assertNotIn("secret", result[0])


class NotificationAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client._shutting_down = False
        self.client._window_visible = False
        self.client._notified_tasks = deque(maxlen=256)
        self.client.core.service_state = "RUNNING"
        self.client.settings.preferences.return_value = {"notifications": False}
        self.client.settings.startup_state.return_value = {"state": "off"}
        self.api = DesktopApi(self.client)

    def event(self, kind, task=""):
        DesktopClient._on_core_event(self.client, kind, task)

    def test_service_check_does_not_scan_history_or_launch_tasklist(self):
        with patch("desktop_app.recent_receipts") as receipts, patch("desktop_app.subprocess.run") as command:
            self.assertTrue(self.api.get_state()["running"])
            self.client.core.service_state = "DEGRADED"
            self.assertFalse(self.api.get_state()["running"])
            receipts.assert_not_called()
            command.assert_not_called()
            self.client.settings.preferences.assert_not_called()
            self.client.tray.notify.assert_not_called()

    def test_notifications_default_off_and_stop_after_disable(self):
        self.event("file-complete", "first")
        self.client.tray.notify.assert_not_called()
        self.client.settings.preferences.return_value = {"notifications": True}
        self.event("file-complete", "first")
        self.client.tray.notify.assert_not_called()
        self.event("file-complete", "second")
        self.client.tray.notify.assert_called_once()
        self.client.settings.preferences.return_value = {"notifications": False}
        self.event("legacy-complete", "third")
        self.client.tray.notify.assert_called_once()
        self.client._send_window_event.assert_not_called()

    def test_notification_is_deduplicated_and_contains_no_task_metadata(self):
        self.client.settings.preferences.return_value = {"notifications": True}
        self.event("legacy-complete", "private-task")
        self.event("legacy-complete", "private-task")
        self.client.tray.notify.assert_called_once()
        message, title = self.client.tray.notify.call_args.args
        self.assertNotIn("private-task", message + title)

    def test_partial_and_device_events_only_refresh_visible_window(self):
        self.client.settings.preferences.return_value = {"notifications": True}
        self.event("legacy")
        self.client._send_window_event.assert_not_called()
        self.client._window_visible = True
        self.event("files")
        self.event("devices")
        self.client.tray.notify.assert_not_called()
        self.client.settings.preferences.assert_not_called()
        self.assertEqual([call.args[1] for call in self.client._send_window_event.call_args_list], ["files", "devices"])

    def test_notification_failure_does_not_prevent_window_refresh_or_retry_notification(self):
        self.client._window_visible = True
        self.client.settings.preferences.return_value = {"notifications": True}
        self.client.tray.notify.side_effect = OSError("notification unavailable")
        with self.assertLogs(level="WARNING"):
            self.event("file-complete", "test")
        self.event("file-complete", "test")
        self.client.tray.notify.assert_called_once()
        self.client._send_window_event.assert_called_with("morypasteReceiverChanged", "files")

    def test_copyq_process_status_is_independent_of_path_and_version(self):
        with patch("desktop_app.subprocess.run", return_value=Mock(returncode=0, stdout=b'"copyq.exe","42"')):
            self.assertTrue(self.api.get_settings()["copyqRunning"])
        with patch("desktop_app.subprocess.run", return_value=Mock(returncode=0, stdout=b'No tasks')):
            self.assertFalse(self.api.get_settings()["copyqRunning"])
        with patch("desktop_app.subprocess.run", side_effect=subprocess.TimeoutExpired("tasklist", 3)):
            self.assertIsNone(self.api.get_settings()["copyqRunning"])


if __name__ == "__main__":
    unittest.main()
