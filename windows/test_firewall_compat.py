import os
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile, subprocess, json
import firewall_access
from process_manager import ReceiverProcessManager

class CompatibilityTests(unittest.TestCase):
    def test_live_process_without_listener_is_degraded(self):
        m=ReceiverProcessManager(Path(__file__))
        m.process=Mock(); m.process.poll.return_value=None; m._status='运行中'
        with patch.object(m,'service_ready',return_value=False):
            self.assertEqual(m.service_state,'DEGRADED')
        with patch.object(m,'service_ready',return_value=True):
            self.assertEqual(m.service_state,'RUNNING')
        m._status='正在启动'
        self.assertEqual(m.service_state,'STARTING')

    def test_ps51_and_ps7_isolated_authorize_without_listener(self):
        script=Path(__file__).with_name('firewall_rule.ps1').read_text('utf-8-sig')
        # Only fixture copies bypass elevation. No real network/firewall cmdlet is called.
        a=script.index('            $identity =',script.index("'Authorize' {")); b=script.index('            if (-not (Test-Path',a)
        script=script[:a]+script[b:]
        fixture='''
function Get-NetAdapter { [pscustomobject]@{Name='以太网';InterfaceDescription='Realtek';Status='Up';ifIndex=6} }
function Get-NetConnectionProfile { [pscustomobject]@{NetworkCategory='Public'} }
function Get-NetIPAddress { [pscustomobject]@{IPAddress='192.168.3.99'} }
function Get-NetTCPConnection { throw 'Must never query listening ports during authorization' }
function Get-NetFirewallRule { if($script:created){ [pscustomobject]@{Name='fixture';Group=$ruleGroup} } }
function Test-Rule { return $true }
function New-NetFirewallRule { $script:created=$true }
function Remove-NetFirewallRule { throw 'Must not remove rules' }
'''
        script=script.replace('try {\n    switch ($Action)',fixture+'\ntry {\n    switch ($Action)')
        engines=[firewall_access._powershell(), 'pwsh.exe']
        with tempfile.TemporaryDirectory(prefix='中文 路径 ') as d:
            path=Path(d)/'模拟.ps1';path.write_text(script,'utf-8-sig')
            exe=Path(d)/'接收 核心.exe';exe.touch()
            for engine in engines:
                result=subprocess.run([engine,'-NoProfile','-ExecutionPolicy','RemoteSigned','-File',str(path),'-Action','Authorize','-ReceiverPath',str(exe),'-InterfaceAlias','以太网','-ResultPath',str(Path(d)/'result.json')],capture_output=True)
                self.assertEqual(result.returncode,0,firewall_access.decode_output(result.stdout+result.stderr))
                payload=json.loads(firewall_access.decode_output(result.stdout))
                self.assertTrue(payload['ruleVerified'])
                self.assertEqual(payload['status'],'RULE_CREATED')
                result_file=Path(d)/'result.json'
                self.assertEqual(json.loads(result_file.read_text('utf-8-sig')),payload)
                result_file.unlink()

    def test_ps51_and_ps7_uninstall_scoped_to_persistent_rules_for_current_path(self):
        source = Path(__file__).with_name("firewall_rule.ps1").read_text("utf-8-sig")
        start = source.index("            $identity =", source.index("'Remove' {"))
        end = source.index("            $stage = 'RULE_REMOVE_FAILED'", start)
        source = source[:start] + source[end:]
        fixture = r"""
$script:rules = @(
    [pscustomobject]@{Name="${rulePrefix}0123456789AB"; Group=$ruleGroup; Program=([string]$ReceiverPath).ToUpperInvariant()}
    [pscustomobject]@{Name="${rulePrefix}0123456789AC"; Group=$ruleGroup; Program=$ReceiverPath}
    [pscustomobject]@{Name="${rulePrefix}0123456789AD"; Group=$ruleGroup; Program='C:\Other\SendToCopyQ-Receiver.exe'}
    [pscustomobject]@{Name="${rulePrefix}0123456789AE"; Group='Other software'; Program=$ReceiverPath}
    [pscustomobject]@{Name='TCP Query User'; Group=$ruleGroup; Program=$ReceiverPath}
)
if ($env:MORY_TEST_RULE_CASE -eq 'other-only') {
    $script:rules = @($script:rules | Select-Object -Skip 2)
}
function Get-NetFirewallRule {
    param($PolicyStore)
    if ($PolicyStore -ne 'PersistentStore') { throw 'Wrong policy store for uninstall lookup' }
    if ($env:MORY_TEST_RULE_CASE -eq 'read-error') { throw '读取模拟规则失败' }
    $script:rules
}
function Get-NetFirewallApplicationFilter {
    param($AssociatedNetFirewallRule)
    $AssociatedNetFirewallRule
}
function Remove-NetFirewallRule {
    param($Name, $PolicyStore, $Confirm)
    if ($PolicyStore -ne 'PersistentStore' -or $Confirm -ne $false) { throw 'Unexpected removal flags' }
    if ($Name -notin @("${rulePrefix}0123456789AB", "${rulePrefix}0123456789AC")) { throw "Wrong rule: $Name" }
    Add-Content -LiteralPath (Join-Path $PSScriptRoot 'removed.txt') -Value $Name
    if ($env:MORY_TEST_RULE_CASE -ne 'still-present') {
        $script:rules = @($script:rules | Where-Object Name -ne $Name)
    }
}
"""
        source = source.replace("try {\n    switch ($Action)", fixture + "\ntry {\n    switch ($Action)")
        with tempfile.TemporaryDirectory(prefix="卸载 测试 ") as temp:
            path = Path(temp) / "测试.ps1"
            path.write_text(source, "utf-8-sig")
            removals = path.with_name("removed.txt")
            receiver = Path(temp) / "接收 核心.exe"
            for engine in (firewall_access._powershell(), "pwsh.exe"):
                for action, case, expected, count in (
                    ("HasRules", "normal", 0, 0),
                    ("HasRules", "other-only", 1, 0),
                    ("HasRules", "read-error", 2, 0),
                    ("Remove", "normal", 0, 2),
                    ("Remove", "other-only", 0, 0),
                    ("Remove", "still-present", 2, 2),
                    ("Remove", "missing-path", 2, 0),
                ):
                    with self.subTest(engine=engine, action=action, case=case):
                        removals.unlink(missing_ok=True)
                        args = [engine, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(path),
                                "-Action", action]
                        if case != "missing-path":
                            args += ["-ReceiverPath", str(receiver)]
                        result = subprocess.run(args, env={**os.environ, "MORY_TEST_RULE_CASE": case},
                                                capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, expected,
                                         firewall_access.decode_output(result.stdout + result.stderr))
                        actual = removals.read_text().splitlines() if removals.exists() else []
                        self.assertEqual(actual, ["SendToCopyQ-LAN-Receiver-0123456789AB",
                                                  "SendToCopyQ-LAN-Receiver-0123456789AC"][:count])

if __name__=='__main__': unittest.main()
