from __future__ import annotations

import subprocess
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desktop_app import DesktopApi, DesktopClient, SW_RESTORE, activate_existing_window
from firewall_access import FirewallAuthorizationError
from process_manager import CoreStartupError, ExistingReceiverError, ReceiverProcessManager


class LanAuthorizationTests(unittest.TestCase):
    network = {"ip": "192.168.10.4", "interface": "Test", "interfaceIndex": 4}

    def setUp(self):
        self.client = Mock(lan_control_token="fixture")
        self.api = DesktopApi(self.client)
        self.api._lan_request = Mock(side_effect=[self.network, {"ok": True}])

    @patch("desktop_app.firewall_access.authorize")
    @patch("desktop_app.firewall_access.get_status", return_value={"authorized": False})
    def test_startup_without_rule_never_requests_elevation_or_starts_lan(self, _status, authorize):
        self.api._start_lan_if_authorized(self.network)
        authorize.assert_not_called()
        self.api._lan_request.assert_not_called()

    @patch("desktop_app.firewall_access.authorize")
    @patch("desktop_app.firewall_access.get_status", return_value={"authorized": True})
    def test_startup_with_existing_rule_starts_without_elevation(self, _status, authorize):
        self.api._lan_request.side_effect = None
        self.api._start_lan_if_authorized(self.network)
        authorize.assert_not_called()
        self.api._lan_request.assert_called_once_with("start", self.network)

    @patch("desktop_app.firewall_access.get_status", return_value={"authorized": False})
    def test_add_phone_authorizes_before_listening(self, _status):
        def authorize(_path, _interface):
            self.api._lan_request.assert_called_once_with("network", {})
            return {"authorized": True}

        with patch("desktop_app.firewall_access.authorize", side_effect=authorize) as elevation:
            self.assertTrue(self.api.authorize_firewall()["serviceReady"])
        elevation.assert_called_once_with(self.client.core.packaged_executable, "Test")
        self.assertEqual(self.api._lan_request.call_args.args, ("start", self.network))

    @patch("desktop_app.firewall_access.get_status", return_value={"authorized": False})
    def test_cancel_or_failure_keeps_local_ui_and_can_retry(self, _status):
        self.api._lan_request.side_effect = [self.network, self.network, self.network, {"ok": True}]
        with patch("desktop_app.firewall_access.authorize", side_effect=[
            FirewallAuthorizationError("取消", "UAC_CANCELLED", 1223),
            FirewallAuthorizationError("策略禁止授权", "RULE_CREATE_FAILED", 2),
            {"authorized": True},
        ]):
            self.assertIn("已取消", self.api.authorize_firewall()["message"])
            self.assertIn("策略禁止", self.api.authorize_firewall()["message"])
            self.assertNotIn("start", [call.args[0] for call in self.api._lan_request.call_args_list])
            self.assertTrue(self.api.authorize_firewall()["serviceReady"])


class ProcessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ReceiverProcessManager(Path(__file__).parent / "receiver.py")

    def test_receiver_events_are_dispatched_without_entering_error_output(self) -> None:
        task = "df842ef2-db65-45bb-b2a8-ba2212398a1b"
        self.manager.on_event = Mock()
        self.manager.process = Mock(stdout=iter([
            "startup information\n",
            "MORYPASTE_EVENT devices\n",
            f"MORYPASTE_EVENT file-complete {task}\n",
            "MORYPASTE_EVENT legacy-complete\n",
        ]))
        self.manager._read_output()
        self.assertEqual([call.args for call in self.manager.on_event.call_args_list], [("devices", ""), ("file-complete", task)])
        self.assertEqual(list(self.manager._output), ["startup information", "MORYPASTE_EVENT legacy-complete"])

    def test_frozen_client_launches_only_its_packaged_receiver_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            receiver = Path(temp) / "receiver" / "SendToCopyQ-Receiver.exe"
            receiver.parent.mkdir()
            receiver.write_bytes(b"test executable placeholder")
            manager = ReceiverProcessManager(Path(temp) / "_internal" / "receiver.py", packaged_executable=receiver)
            with patch("process_manager.sys.frozen", True, create=True):
                self.assertEqual(manager._build_command(), [str(receiver.resolve())])

    def test_frozen_client_fails_clearly_when_packaged_receiver_is_missing(self) -> None:
        manager = ReceiverProcessManager(Path(__file__).parent / "receiver.py", packaged_executable=Path("missing.exe"))
        with patch("process_manager.sys.frozen", True, create=True):
            with self.assertRaisesRegex(CoreStartupError, "找不到已打包的接收核心"):
                manager._build_command()

    def test_existing_receiver_is_not_relaunched_or_stopped(self) -> None:
        state = {"computerId": "stpc-test", "computerName": "existing", "ip": "192.168.1.2"}
        with patch.object(self.manager, "_get_state", return_value=state), patch(
            "process_manager.subprocess.Popen"
        ) as popen, patch.object(self.manager, "_post_quit") as quit_request:
            with self.assertRaises(ExistingReceiverError):
                self.manager.start()
            self.assertIs(self.manager.external_state, state)
            self.assertFalse(popen.called)
            self.assertTrue(self.manager.stop())
            quit_request.assert_not_called()

    def test_unknown_listener_is_not_misreported_as_a_known_receiver(self) -> None:
        error = ExistingReceiverError(detail="本机管理端口已有响应但无法安全识别：timed out。")
        self.assertIn("无法确认接收端状态", str(error))
        self.assertIn("127.0.0.1:48120", str(error))
        self.assertNotIn("未知实例，127.0.0.1:47831", str(error))

    def test_loopback_timeout_with_no_listener_is_treated_as_available(self) -> None:
        timeout_error = urllib.error.URLError(TimeoutError("timed out"))
        with patch("process_manager.urllib.request.OpenerDirector.open", side_effect=timeout_error), patch.object(
            self.manager, "_management_port_listening", return_value=False
        ):
            self.assertIsNone(self.manager._get_state(timeout=0.01))

    def test_loopback_timeout_with_listener_fails_closed(self) -> None:
        timeout_error = urllib.error.URLError(TimeoutError("timed out"))
        with patch("process_manager.urllib.request.OpenerDirector.open", side_effect=timeout_error), patch.object(
            self.manager, "_management_port_listening", return_value=True
        ):
            with self.assertRaises(ExistingReceiverError):
                self.manager._get_state(timeout=0.01)

    def test_management_listener_check_uses_a_real_tcp_connect(self) -> None:
        probe = Mock()
        probe.connect_ex.return_value = 0
        with patch("process_manager.socket.socket", return_value=probe) as create_socket:
            self.assertTrue(self.manager._management_port_listening())
        create_socket.assert_called_once()
        probe.connect_ex.assert_called_once_with(("127.0.0.1", 48120))
        probe.close.assert_called_once()

    def test_start_waits_briefly_for_a_closing_receiver_to_release_port(self) -> None:
        timeout = ExistingReceiverError(detail="本机管理端口已有响应但无法安全识别：timed out。")
        with patch.object(self.manager, "_get_state", side_effect=[timeout, None]) as probe, patch.object(
            self.manager, "_management_port_listening", return_value=True
        ), patch("process_manager.time.sleep") as sleep:
            self.assertIsNone(self.manager._probe_existing_receiver(timeout=0.01, shutdown_grace=1.0))
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once()

    def test_probe_retries_if_listener_disappears_during_timeout_handling(self) -> None:
        timeout = ExistingReceiverError(detail="本机管理端口已有响应但无法安全识别：timed out。")
        with patch.object(self.manager, "_get_state", side_effect=[timeout, None]) as probe, patch(
            "process_manager.time.sleep"
        ):
            self.assertIsNone(self.manager._probe_existing_receiver(timeout=0.01, shutdown_grace=1.0))
        self.assertEqual(probe.call_count, 2)

    def test_start_retries_own_core_api_timeout_without_calling_external_quit(self) -> None:
        timeout = ExistingReceiverError(detail="本机管理端口已有响应但无法安全识别：timed out。")
        state = {"computerId": "stpc-test", "computerName": "test", "ip": "192.168.1.2"}
        process = Mock()
        process.poll.return_value = None
        process.stdout = None
        with patch.object(self.manager, "_probe_existing_receiver", return_value=None), patch(
            "process_manager.subprocess.Popen", return_value=process
        ), patch.object(self.manager, "_core_interpreter", return_value=Path("python.exe")), patch.object(
            self.manager, "_attach_owned_process_job"
        ), patch.object(self.manager, "_get_state", side_effect=[timeout, state]) as get_state, patch.object(
            self.manager, "_post_quit"
        ) as quit_request, patch("process_manager.time.sleep"):
            with patch.object(self.manager, "service_ready", return_value=True):
                self.assertEqual(self.manager.start(timeout=1), state)
            self.assertEqual(get_state.call_count, 2)
            quit_request.assert_not_called()
        self.manager._stop_requested.set()

    def test_graceful_exit_does_not_call_taskkill(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        process.pid = 12345
        self.manager.process = process
        with patch.object(self.manager, "_post_quit") as quit_request, patch(
            "process_manager.subprocess.run"
        ) as run, patch.object(self.manager, "_wait_for_management_port_release", return_value=True):
            self.assertTrue(self.manager.stop())
        quit_request.assert_called_once()
        run.assert_not_called()
        process.wait.assert_called_once_with(timeout=10.0)

    def test_timeout_targets_only_owned_process_tree(self) -> None:
        process = Mock()
        process.poll.side_effect = [None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired("receiver", 1), 0]
        process.pid = 54321
        self.manager.process = process
        with patch.object(self.manager, "_post_quit"), patch(
            "process_manager.subprocess.run", return_value=Mock(returncode=0)
        ) as run, patch.object(self.manager, "_wait_for_management_port_release", return_value=True):
            self.assertTrue(self.manager.stop(graceful_timeout=1.0))
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["taskkill.exe", "/PID", "54321"])
        self.assertIn("/T", args)
        self.assertIn("/F", args)

    def test_shutdown_is_not_reported_complete_if_port_remains_open(self) -> None:
        process = Mock()
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        process.pid = 99999
        self.manager.process = process
        with patch.object(self.manager, "_post_quit"), patch.object(
            self.manager, "_wait_for_management_port_release", return_value=False
        ), patch("process_manager.subprocess.run") as run:
            self.assertFalse(self.manager.stop())
        self.assertIn("退出未确认", self.manager.status)
        run.assert_not_called()

    def test_core_uses_venv_interpreter_and_pinned_packages(self) -> None:
        import os
        import sys

        if sys.prefix == sys.base_prefix:
            self.skipTest("The development client is not running inside a virtual environment")
        interpreter = self.manager._core_interpreter()
        self.assertEqual(interpreter.resolve(), (Path(sys.prefix) / "Scripts" / "python.exe").resolve())
        self.assertTrue(interpreter.is_file())
        self.assertEqual(os.name, "nt")
        result = subprocess.run(
            [str(interpreter), "-c", "import sys, receiver, cryptography, segno; print(sys.prefix)"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), Path(sys.prefix).resolve())


class DesktopLifecycleTests(unittest.TestCase):
    def test_duplicate_launch_restores_the_existing_window(self) -> None:
        user32 = Mock()
        user32.FindWindowW.return_value = 1234
        with patch("desktop_app.ctypes.WinDLL", return_value=user32):
            self.assertTrue(activate_existing_window(timeout_seconds=0))
        user32.ShowWindow.assert_called_once_with(1234, SW_RESTORE)
        user32.SetForegroundWindow.assert_called_once_with(1234)

    def test_window_close_hides_without_exiting(self) -> None:
        client = DesktopClient()
        client.window = Mock()
        self.assertFalse(client._hide_on_close())
        client.window.hide.assert_called_once()
        self.assertFalse(client._shutting_down)

    def test_tray_exit_is_idempotent_and_stops_owned_resources(self) -> None:
        client = DesktopClient()
        client.core = Mock()
        client.core.status = "运行中"
        client.core.stop = Mock(return_value=True)
        client.tray = Mock()
        client.window = Mock()
        client._shutdown()
        client._shutdown()
        client.core.stop.assert_called_once()
        client.tray.stop.assert_called_once()
        client.window.destroy.assert_called_once()
        self.assertTrue(client._shutting_down)

    def test_failed_core_exit_keeps_tray_and_window_alive(self) -> None:
        client = DesktopClient()
        client.core = Mock()
        client.core.status = "运行中"
        client.core.stop = Mock(return_value=False)
        client.tray = Mock()
        client.window = Mock()
        with patch("desktop_app.show_message"):
            client._shutdown()
        client.core.stop.assert_called_once()
        client.tray.stop.assert_not_called()
        client.window.destroy.assert_not_called()
        self.assertFalse(client._shutting_down)


if __name__ == "__main__":
    unittest.main(verbosity=2)
