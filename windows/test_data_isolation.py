import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from data_paths import resolve_data_dir, child_environment

class DataIsolationTests(unittest.TestCase):
    def test_missing_and_main_directory_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'test-data-profile.json').write_text(json.dumps({'directory':'SendToCopyQ-PairingTest'}))
            env={'LOCALAPPDATA':d}
            with self.assertRaises(RuntimeError): resolve_data_dir(env,root)
            env['SENDTOCOPYQ_DATA_DIR']=str(root/'SendToCopyQ')
            with self.assertRaises(RuntimeError): resolve_data_dir(env,root)

    def test_initialization_restart_and_identity_separation(self):
        import receiver
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'test-data-profile.json').write_text(json.dumps({'directory':'SendToCopyQ-PairingTest'}))
            main=root/'SendToCopyQ';main.mkdir(); sentinel=main/'computer-id.txt';sentinel.write_text('main-original')
            target=root/'SendToCopyQ-PairingTest'
            env={'LOCALAPPDATA':d,'SENDTOCOPYQ_DATA_DIR':str(target)}
            self.assertEqual(resolve_data_dir(env,root),target)
            with patch.object(receiver,'APP_DATA',target):
                receiver.app_data_dir()
                cert,key,fingerprint=receiver.certificate_material('192.168.3.53')
                identity=receiver.load_or_create_computer_id(fingerprint)
                self.assertEqual(receiver.load_or_create_computer_id(fingerprint),identity)
                self.assertNotEqual(identity,'main-original')
                self.assertEqual(cert.parent,target)
                self.assertEqual(key.parent,target)
            target.joinpath('isolation-sentinel').write_text('test')
            self.assertEqual(resolve_data_dir(env,root),target)
            self.assertEqual(sentinel.read_text(),'main-original')
            self.assertFalse((target/'paired-devices.json').exists())
            with patch.dict('os.environ',env,clear=True):
                self.assertEqual(child_environment(target)['SENDTOCOPYQ_DATA_DIR'],str(target.resolve()))

    def test_bat_sets_data_before_start(self):
        script=(Path(__file__).parent/'packaging/Start-SendToCopyQ-Test.bat').read_text()
        self.assertLess(script.index('set "SENDTOCOPYQ_DATA_DIR='),script.index('start ""'))
        self.assertIn('%~dp0SendToCopyQ',script)
