import base64
import email.message
import hashlib
import hmac
import http.server
import io
import struct
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import receiver
from desktop_app import DesktopApi
from received_files import ReceiveStore, default_receive_root, file_drop_payload, safe_name


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")


class FileReceiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ReceiveStore(self.root / "index.sqlite3", self.root / "MoryPaste 接收")
        self.device_id = "a" * 24
        self.secret = b"isolated-test-key"
        self.task = str(uuid.uuid4())
        self.state = SimpleNamespace(
            lock=threading.RLock(), receive_lock=threading.RLock(), used_nonces={},
            devices={self.device_id: {"name": "测试手机", "secretProtected": base64.b64encode(self.secret)}},
            received_files=lambda: self.store)
        self.state.authenticate = MethodType(receiver.State.authenticate, self.state)
        for name in ("log_event", "log_exception"):
            patcher = patch.object(receiver, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_run_clipboard_job", "write_image_to_clipboard"):
            patcher = patch.object(receiver, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def receive(self, index, total, name, mime, body, *, hash_body=None, tamper_header=False):
        digest = hashlib.sha256(body if hash_body is None else hash_body).hexdigest()
        stamp, nonce = str(int(time.time())), uuid.uuid4().hex
        encoded = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
        canonical = f"{stamp}\n{nonce}\n{mime}\n{len(body)}\n{digest}\n/receive\n{self.task}\n{index}\n{total}\n{encoded}"
        signature = hmac.new(self.secret, canonical.encode(), hashlib.sha256).hexdigest()
        handler = object.__new__(receiver.TransferHandler)
        handler.state = self.state
        handler.path = "/receive"
        handler.connection = Mock()
        handler.headers = email.message.Message()
        for key, value in {
            "Content-Type": mime, "Content-Length": len(body), "X-File-Name": ("YWJj" if tamper_header else encoded),
            "X-Task-Id": self.task, "X-Image-Index": index, "X-Image-Count": total, "X-Device-Id": self.device_id,
            "X-Timestamp": stamp, "X-Nonce": nonce, "X-Content-SHA256": digest, "X-Signature": signature,
        }.items():
            handler.headers[key] = str(value)
        handler.rfile = io.BytesIO(body)
        replies = []
        handler.reply = lambda status, value: replies.append((status, value))
        with patch.object(receiver, "unprotect_device_secret", side_effect=lambda key: key):
            handler.do_POST()
        return replies[0]

    def test_single_png_preserves_image_clipboard_format(self):
        result = self.receive(1, 1, "截图.png", "image/png", PNG)
        self.assertEqual(result[1]["clipboard"], "image")
        self.assertEqual((self.store.root / "图片" / "截图.png").read_bytes(), PNG)
        self.write_image_to_clipboard.assert_called_once_with(PNG, "image/png", self.task, 1, 1)
        self._run_clipboard_job.assert_not_called()

    def test_mixed_batch_copies_only_images_in_original_order(self):
        self.assertEqual(self.receive(1, 4, "截图.png", "image/png", PNG)[1]["clipboard"], "pending")
        self.assertEqual(self.receive(2, 4, "说明.pdf", "application/pdf", b"%PDF")[1]["clipboard"], "pending")
        self.assertEqual(self.receive(3, 4, "截图.png", "image/png", PNG)[1]["clipboard"], "pending")
        self._run_clipboard_job.assert_not_called()
        result = self.receive(4, 4, "视频.mp4", "video/mp4", b"video bytes")
        self.assertEqual((result[0], result[1]["savedCount"], result[1]["clipboard"]), (200, 4, "files"))
        paths = self._run_clipboard_job.call_args.args[0]["paths"]
        self.assertEqual([p.name for p in paths], ["截图.png", "截图 (2).png"])
        self.assertTrue(all(p.parent.name == "图片" for p in paths))
        self.assertEqual((self.store.root / "视频" / "视频.mp4").read_bytes(), b"video bytes")
        self.assertEqual((self.store.root / "文档" / "说明.pdf").read_bytes(), b"%PDF")
        self.assertEqual(len(ReceiveStore(self.store.database, self.store.root).recent()[0]["files"]), 4)
        self.write_image_to_clipboard.assert_not_called()

    def test_non_images_do_not_touch_clipboard(self):
        self.assertEqual(self.receive(1, 1, "文档.pdf", "application/pdf", b"%PDF")[1]["clipboard"], "unchanged")
        self._run_clipboard_job.assert_not_called()
        self.write_image_to_clipboard.assert_not_called()

    def test_recent_returns_four_latest_batches_with_display_filenames(self):
        for index in range(5):
            self.task = str(uuid.uuid4())
            status, _ = self.receive(1, 1, f"报告 第{index}份.pdf", "application/pdf", b"%PDF")
            self.assertEqual(status, 200)

        recent = self.store.recent()
        self.assertEqual([batch["files"][0]["name"] for batch in recent],
                         ["报告 第4份.pdf", "报告 第3份.pdf", "报告 第2份.pdf", "报告 第1份.pdf"])
        self.assertNotIn("path", recent[0]["files"][0])

    def test_retry_of_completed_batch_does_not_copy_or_save_twice(self):
        for _ in range(2):
            self.assertEqual(self.receive(1, 1, "截图.png", "image/png", PNG)[1]["clipboard"], "image")
        self.write_image_to_clipboard.assert_called_once()
        self.assertEqual(len(list((self.store.root / "图片").iterdir())), 1)

    def test_failed_hash_leaves_partial_batch_and_keeps_clipboard(self):
        self.receive(1, 2, "截图.png", "image/png", PNG)
        self.assertEqual(self.receive(2, 2, "文本.txt", "text/plain", b"abc", hash_body=b"foo")[0], 400)
        self.assertEqual(len(self.store.recent()[0]["files"]), 1)
        self.assertEqual(list((self.store.root / "文档").iterdir()), [])
        self._run_clipboard_job.assert_not_called()
        self.write_image_to_clipboard.assert_not_called()

    def test_clipboard_failure_keeps_saved_original_and_reports_separately(self):
        self.write_image_to_clipboard.side_effect = RuntimeError("mock busy")
        status, result = self.receive(1, 1, "截图.png", "image/png", PNG)
        self.assertEqual((status, result["savedCount"], result["clipboard"]), (200, 1, "failed"))
        self.assertEqual((self.store.root / "图片" / "截图.png").read_bytes(), PNG)

    def test_authenticated_metadata_and_reserved_names(self):
        self.assertEqual(self.receive(1, 1, "伪造.png", "image/png", PNG, tamper_header=True)[0], 401)
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(safe_name("../CON.txt"), "_CON.txt")
        self.assertEqual(safe_name(r"C:\目录\测试.txt"), "测试.txt")
        self.assertTrue(default_receive_root().is_absolute())

    def test_file_drop_contains_one_unicode_list(self):
        files = [self.root / "图片 甲.png", self.root / "图片 乙.png"]
        for file in files:
            file.touch()
        payload = file_drop_payload(files)
        self.assertEqual(struct.unpack("<IiiII", payload[:20]), (20, 0, 0, 0, 1))
        self.assertEqual(payload[20:].decode("utf-16le"), "\0".join(map(str, files)) + "\0\0")
        files[0].unlink()
        with self.assertRaises(ValueError):
            file_drop_payload(files)

    def test_desktop_opens_validated_receipt_and_root_without_receiver_launch(self):
        self.receive(1, 1, "说明.pdf", "application/pdf", b"%PDF")
        batch_id = self.store.recent()[0]["id"]
        handler = type("IsolatedDashboard", (receiver.DashboardHandler,), {"state": self.state})
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = DesktopApi(SimpleNamespace())
                url = f"http://127.0.0.1:{server.server_port}/"
                with patch("desktop_app.DASHBOARD_URL", url), patch("desktop_app._open_explorer_folder") as open_folder, \
                     patch("receiver.os.startfile") as receiver_launch:
                    self.assertTrue(api.open_received_folder()["ok"])
                    self.assertTrue(api.open_received_folder(batch_id, 1)["ok"])
                    self.assertEqual([call.args[0] for call in open_folder.call_args_list],
                                     [self.store.root, self.store.root / "文档"])
                    self.assertFalse(api.open_received_folder("C:\\", 1)["ok"])
                    self.assertFalse(api.open_received_folder(batch_id, 2)["ok"])
                    (self.store.root / "文档" / "说明.pdf").unlink()
                    self.assertEqual(api.open_received_folder(batch_id, 1)["error"], "这个文件已移动或删除。")
                    receiver_launch.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=3)
