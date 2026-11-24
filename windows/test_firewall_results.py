import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import firewall_access as f
class ResultTests(unittest.TestCase):
 def test_file_failure_categories(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'result.json'
   for content,expected in [(None,'RESULT_MISSING'),(b'','RESULT_EMPTY'),(b'bad','RESULT_JSON_INVALID'),(b'{}','RESULT_SCHEMA_INVALID')]:
    if content is not None:p.write_bytes(content)
    with self.assertRaises(f.FirewallAuthorizationError) as e:f.read_helper_result(p)
    self.assertEqual(e.exception.status,expected)
   with patch.object(Path,'read_bytes',side_effect=PermissionError()):
    with self.assertRaises(f.FirewallAuthorizationError) as e:f.read_helper_result(p)
    self.assertEqual(e.exception.status,'RESULT_READ_FAILED')
 def test_missing_result_recovered_only_by_verified_rule(self):
  for authorized in [True,False]:
   with patch.object(f,'get_status',side_effect=[{'authorized':False},{'authorized':authorized}]),patch.object(f,'_shell_execute_elevated',return_value=0):
    if authorized:
     self.assertEqual(f.authorize(Path('receiver.exe'),'Ethernet')['status'],'AUTHORIZED_RESULT_RECOVERED')
    else:
     with self.assertRaises(f.FirewallAuthorizationError) as e:f.authorize(Path('receiver.exe'),'Ethernet')
     self.assertEqual(e.exception.status,'RESULT_MISSING')
 def test_bad_results_and_crash_recovered_by_verified_rule(self):
  for category in ['RESULT_EMPTY','RESULT_JSON_INVALID','RESULT_READ_FAILED','RESULT_SCHEMA_INVALID']:
   with patch.object(f,'get_status',side_effect=[{'authorized':False},{'authorized':True}]),patch.object(f,'_shell_execute_elevated',return_value=0),patch.object(f,'read_helper_result',side_effect=f.FirewallAuthorizationError('bad',category)):
    self.assertEqual(f.authorize(Path('receiver.exe'),'Ethernet')['resultWarning'],category)
 def test_uac_cancel_preserved(self):
  with patch.object(f,'get_status',return_value={'authorized':False}),patch.object(f,'_shell_execute_elevated',side_effect=f.FirewallAuthorizationError('cancel','UAC_CANCELLED',1223)):
   with self.assertRaises(f.FirewallAuthorizationError) as e:f.authorize(Path('receiver.exe'),'Ethernet')
   self.assertEqual(e.exception.status,'UAC_CANCELLED')
 def test_helper_failure_does_not_report_success_without_rule(self):
  payload={'success':False,'status':'failed','errorCode':'RULE_CREATE_FAILED','message':'denied','ruleCreated':False,'ruleVerified':False}
  with patch.object(f,'get_status',return_value={'authorized':False}),patch.object(f,'_shell_execute_elevated',return_value=2),patch.object(f,'read_helper_result',return_value=payload):
   with self.assertRaises(f.FirewallAuthorizationError) as e:f.authorize(Path('receiver.exe'),'Ethernet')
   self.assertEqual(e.exception.exit_code,2)

 def test_rule_parameter_matching_ps51_and_ps7(self):
  import subprocess
  source=Path('windows/firewall_rule.ps1').read_text('utf-8-sig')
  start=source.index('function Test-Rule(');end=source.index('\ntry {\n    switch',start)
  fixture = source[start:end] + r"""
function Get-NetFirewallApplicationFilter { [pscustomobject]@{Program=$script:program} }
function Get-NetFirewallPortFilter { [pscustomobject]@{Protocol=$script:protocol;LocalPort=$script:port} }
function Get-NetFirewallAddressFilter { [pscustomobject]@{RemoteAddress=$script:remote} }
function Get-NetFirewallInterfaceFilter { [pscustomobject]@{InterfaceAlias=$script:alias} }
$script:program='C:\Test App\receiver.exe';$script:protocol='TCP';$script:port='47831';$script:remote='LocalSubnet';$script:alias='Ethernet'
$rule=[pscustomobject]@{Enabled='True';Direction='Inbound';Action='Allow';Profile='Public'}
if (-not (Test-Rule $rule 'C:\Test App\receiver.exe' 'Ethernet' 'Public')) {throw 'valid rule rejected'}
foreach($field in @('program','protocol','port','remote','alias')) {
 $old=Get-Variable $field -Scope Script -ValueOnly
 Set-Variable $field 'wrong' -Scope Script
 if(Test-Rule $rule 'C:\Test App\receiver.exe' 'Ethernet' 'Public'){throw ('invalid rule accepted '+$field)}
 Set-Variable $field $old -Scope Script
}
$rule.Enabled='False'
if(Test-Rule $rule 'C:\Test App\receiver.exe' 'Ethernet' 'Public'){throw 'disabled accepted'}
$rule.Enabled='True';$rule.Profile='Private'
if(Test-Rule $rule 'C:\Test App\receiver.exe' 'Ethernet' 'Public'){throw 'profile accepted'}
"""
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'verify.ps1';path.write_text(fixture,'utf-8-sig')
   for engine in [f._powershell(),'pwsh.exe']:
    r=subprocess.run([engine,'-NoProfile','-ExecutionPolicy','RemoteSigned','-File',str(path)],capture_output=True)
    self.assertEqual(r.returncode,0,f.decode_output(r.stderr))
