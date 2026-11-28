import io,json,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace,MethodType
from unittest.mock import patch
import receiver

class RevokeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'paired-devices.json'
        self.a='a'*24;self.b='b'*24
        self.state=SimpleNamespace(lock=threading.RLock(),devices={self.a:{'name':'same'},self.b:{'name':'same'}},devices_path=self.path,outgoing={})
        self.state.save_devices=MethodType(receiver.State.save_devices,self.state)
        self.state.revoke_device=MethodType(receiver.State.revoke_device,self.state)
        self.state.save_devices()

    def call(self,device):
        handler=object.__new__(receiver.DashboardHandler)
        handler.state=self.state;handler.client_address=('127.0.0.1',1);handler.path='/api/revoke'
        raw=json.dumps({'deviceId':device}).encode();handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw)
        result=[];handler.reply=lambda status,value:result.append((status,value))
        with patch.object(receiver,'log_event'),patch.object(receiver,'log_exception'):
            handler.do_POST()
        return result[0]

    def test_revoke_persists_and_preserves_other_device(self):
        code,result=self.call(self.a);self.assertEqual(code,200);self.assertTrue(result['revoked'])
        self.assertEqual(list(self.state.devices),[self.b]);self.assertEqual(list(json.loads(self.path.read_text())),[self.b])
        self.assertEqual(self.call(self.a)[1]['status'],'already_absent')
        self.assertEqual(self.call('c'*24)[1]['status'],'already_absent')

    def test_write_failure_restores_memory_and_file(self):
        original=self.path.read_bytes()
        with patch.object(self.state,'save_devices',side_effect=PermissionError('denied')):
            code,result=self.call(self.a)
        self.assertEqual(code,500);self.assertEqual(result['code'],'REVOKE_SAVE_FAILED')
        self.assertIn(self.a,self.state.devices);self.assertEqual(self.path.read_bytes(),original)

    def test_invalid_id_returns_json_error(self):
        self.assertEqual(self.call('bad')[0],400)

    def test_repair_after_revoke_and_repeat_stays_unique(self):
        self.call(self.a)
        self.state.pair_codes={}
        identity='12345678-1234-1234-1234-123456789abc'
        self.state.pair_codes[receiver.hashlib.sha256(b'test-code').hexdigest()]={'expires':receiver.time.time()+60}
        with patch.object(receiver,'protect_device_secret',side_effect=lambda x:x), patch.object(receiver,'unprotect_device_secret',side_effect=lambda x:x):
            first=receiver.State.claim_pair_code(self.state,'test-code','same',identity)
            self.assertEqual(len(self.state.devices),2)
            self.state.pair_codes[receiver.hashlib.sha256(b'test-code-2').hexdigest()]={'expires':receiver.time.time()+60}
            second=receiver.State.claim_pair_code(self.state,'test-code-2','same',identity)
        self.assertEqual(first['deviceId'],second['deviceId'])
        self.assertEqual(second['pairingStatus'],'updated')
        self.assertIn(self.b,self.state.devices)
        self.assertEqual(len(self.state.devices),2)
