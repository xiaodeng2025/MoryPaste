"""Installation adaptation tests; do not touch Windows Firewall or clipboard."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import firewall_access
import receiver


class CopyQDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.old_copyq = receiver.COPYQ_EXE

    def tearDown(self):
        receiver.COPYQ_EXE = self.old_copyq

    def test_saved_valid_path_has_priority(self):
        saved = self.root / "saved" / "copyq.exe"
        saved.parent.mkdir()
        saved.touch()
        discovered = self.root / "registry" / "copyq.exe"
        discovered.parent.mkdir()
        discovered.touch()
        (self.data / "settings.json").write_text(json.dumps({"copyqPath": str(saved)}), "utf-8")
        with patch.object(receiver, "APP_DATA", self.data), patch.object(receiver, "copyq_candidates", return_value=[discovered]), patch.object(
            receiver, "check_copyq", side_effect=lambda path: "CopyQ test" if path == saved else self.fail("saved path must win")
        ):
            self.assertEqual(receiver.load_settings()["copyqPath"], str(saved.resolve()))

    def test_discovered_copyq_is_verified_before_use(self):
        discovered = self.root / "install" / "copyq.exe"
        discovered.parent.mkdir()
        discovered.touch()
        with patch.object(receiver, "APP_DATA", self.data), patch.object(receiver, "copyq_candidates", return_value=[discovered]), patch.object(
            receiver, "check_copyq", return_value="CopyQ Clipboard Manager 17.0.0"
        ) as check:
            self.assertEqual(receiver.load_settings()["copyqPath"], str(discovered.resolve()))
            check.assert_called_once_with(discovered)

    def test_missing_copyq_keeps_native_clipboard_fallback_path(self):
        with patch.object(receiver, "APP_DATA", self.data), patch.object(receiver, "copyq_candidates", return_value=[]):
            result = receiver.load_settings()
        self.assertEqual(result["copyqPath"], "copyq.exe")
        self.assertFalse(receiver.COPYQ_EXE.is_file())


class FirewallAuthorizationTests(unittest.TestCase):
    def test_packaged_helper_is_resolved_from_pyinstaller_resource_directory(self):
        resource_dir = Path("C:/isolated/send-to-copyq/_internal")
        with patch.object(firewall_access.sys, "_MEIPASS", str(resource_dir), create=True):
            self.assertEqual(firewall_access.helper_path(), resource_dir / "firewall_rule.ps1")

    def test_status_uses_process_scoped_remote_signed_execution_policy(self):
        expected = {"authorized": False, "interface": "Ethernet", "profile": None, "reason": "not authorized"}
        result = type("Result", (), {"returncode": 0, "stdout": b'{"authorized":false,"interface":"Ethernet","profile":null,"reason":"not authorized"}', "stderr": b""})()
        with tempfile.TemporaryDirectory() as temp:
            base_dir = Path(temp)
            (base_dir / "firewall_rule.ps1").touch()
            with patch.object(firewall_access, "_powershell", return_value="powershell.exe"), patch.object(
                firewall_access.subprocess, "run", return_value=result
            ) as run:
                self.assertEqual(
                    firewall_access.get_status(Path("C:/receiver.exe"), "Ethernet", base_dir=base_dir), expected
                )
        self.assertIn("-ExecutionPolicy", run.call_args.args[0])
        self.assertIn("RemoteSigned", run.call_args.args[0])

    def test_existing_rule_does_not_request_uac(self):
        with patch.object(firewall_access, 'get_status', return_value={'authorized': True}), patch.object(firewall_access, '_shell_execute_elevated') as elevate:
            result = firewall_access.authorize(Path('C:/测试 目录/receiver.exe'), '以太网', base_dir=Path(__file__).parent)
        self.assertEqual(result['status'], 'ALREADY_AUTHORIZED')
        elevate.assert_not_called()

    def test_structured_helper_result_and_chinese_paths(self):
        import subprocess
        def helper(args):
            parts = args.split('-ResultPath ', 1)[1].strip('"')
            Path(parts).write_text(json.dumps({'success': True, 'status': 'RULE_CREATED', 'ruleCreated': True, 'ruleVerified': True, 'message': '授权成功'}, ensure_ascii=False), 'utf-8')
            return 0
        with patch.object(firewall_access, 'get_status', side_effect=[{'authorized': False}, {'authorized': True}]), patch.object(firewall_access, '_shell_execute_elevated', side_effect=helper):
            result = firewall_access.authorize(Path('C:/测试 目录/receiver.exe'), '以太网', base_dir=Path(__file__).parent)
        self.assertEqual(result['message'], '授权成功')

    def test_firewall_rule_is_narrow_and_does_not_touch_development_rule(self):
        script = Path(__file__).with_name("firewall_rule.ps1").read_text("utf-8")
        self.assertIn("-Protocol TCP -LocalPort 47831 -RemoteAddress LocalSubnet", script)
        self.assertIn("-InterfaceAlias $InterfaceAlias", script)
        self.assertIn("SendToCopyQ {8D16AF61-0A5F-48C1-B6D0-4CF3A9876E30}", script)
        self.assertIn("Remove-NetFirewallRule -Name $rule.Name -PolicyStore PersistentStore", script)
        self.assertNotIn("Get-ProductRules | Remove-NetFirewallRule", script)
        self.assertNotIn("Set-NetFirewallProfile", script)
        self.assertNotIn("0.0.0.0", script)

    def test_desktop_pyinstaller_spec_bundles_firewall_helper(self):
        spec = Path(__file__).parent / "packaging" / "SendToCopyQ.spec"
        self.assertIn('"firewall_rule.ps1"', spec.read_text("utf-8"))

    def test_uninstaller_probes_and_removes_with_the_same_installed_receiver_path(self):
        script = (Path(__file__).parent / "packaging" / "MoryPaste-0.6.3.iss").read_text("utf-8")
        cleanup = script.split("procedure RemoveProductFirewallRules;", 1)[1].split(
            "procedure CurUninstallStepChanged", 1
        )[0]
        self.assertIn(r"ExpandConstant('{app}\receiver\SendToCopyQ-Receiver.exe')", cleanup)
        self.assertIn('-ReceiverPath "', cleanup)
        self.assertIn("Parameters + 'HasRules'", cleanup)
        self.assertLess(cleanup.index("if ResultCode = 0"), cleanup.index("ShellExec('runas'"))
        self.assertIn("Parameters + 'Remove'", cleanup)
        self.assertIn("if ResultCode <> 1223", cleanup)


if __name__ == "__main__":
    unittest.main()
