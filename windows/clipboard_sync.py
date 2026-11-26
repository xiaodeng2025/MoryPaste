"""Read-only access to live Windows clipboard text for device sync."""
import ctypes
from ctypes import wintypes


class ClipboardSyncReader:
    MAX_BYTES = 64 * 1024

    def __init__(self):
        user = ctypes.WinDLL("user32")
        kernel = ctypes.WinDLL("kernel32")
        self.sequence = user.GetClipboardSequenceNumber
        self.sequence.restype = wintypes.DWORD
        self.open = user.OpenClipboard
        self.open.argtypes = [wintypes.HWND]
        self.close = user.CloseClipboard
        self.available = user.IsClipboardFormatAvailable
        self.available.argtypes = [wintypes.UINT]
        self.get_data = user.GetClipboardData
        self.get_data.argtypes = [wintypes.UINT]
        self.get_data.restype = wintypes.HANDLE
        self.size = kernel.GlobalSize
        self.size.argtypes = [wintypes.HANDLE]
        self.size.restype = ctypes.c_size_t
        self.lock = kernel.GlobalLock
        self.lock.argtypes = [wintypes.HANDLE]
        self.lock.restype = ctypes.c_void_p
        self.unlock = kernel.GlobalUnlock
        self.unlock.argtypes = [wintypes.HANDLE]
        register = user.RegisterClipboardFormatW
        register.argtypes = [wintypes.LPCWSTR]
        self.excluded = register("ExcludeClipboardContentFromMonitorProcessing")
        self.cloud = register("CanUploadToCloudClipboard")
        self.history = register("CanIncludeInClipboardHistory")
        self.last_sequence = self.sequence()

    def _read(self, format_id, limit):
        handle = self.get_data(format_id)
        if not handle or not 0 < (size := self.size(handle)) <= limit:
            return b""
        pointer = self.lock(handle)
        if not pointer:
            return b""
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.unlock(handle)

    def next_text(self):
        if self.sequence() == self.last_sequence or not self.open(None):
            return None
        try:
            self.last_sequence = self.sequence()
            if not self.available(13) or self.available(15) or self.available(self.excluded):
                return None
            if any(self._read(flag, 64)[:4] == bytes(4) for flag in (self.cloud, self.history)):
                return None
            data = self._read(13, self.MAX_BYTES)
            return data.decode("utf-16-le", errors="replace").partition("\0")[0] or None
        finally:
            self.close()
