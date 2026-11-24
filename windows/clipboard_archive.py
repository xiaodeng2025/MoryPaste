"""Local archive of Windows history, driven by Windows change notifications."""
import asyncio
from bisect import bisect_right
import ctypes
import hashlib
import hmac
import secrets
from ctypes import wintypes
from concurrent.futures import Future
from datetime import datetime, timedelta
import logging
from io import BytesIO
import queue
import shutil
import sqlite3
import time
import threading

MIB = 1024 * 1024
DISK_RESERVE = 128 * MIB
PAGE_SIZE = 30
DAY = 86400
HOTKEY, SYNC, COMMAND, REGISTER = 0x0312, 0x8001, 0x8002, 0x8003


WATCH_POPUP = 0x8004
MOUSE_DOWN = (0x0201, 0x0204, 0x0207, 0x020B)
InputHook = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class GuiThreadInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD),
                *((name, wintypes.HWND) for name in
                  ("active", "focus", "capture", "menu", "move_size", "caret")),
                ("caret_rect", wintypes.RECT)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def history_fingerprint(text, html, image, key):
    digest = hmac.new(key, digestmod=hashlib.sha256)
    for value in (text, html, image):
        if value is None:
            digest.update(b"\0")
            continue
        payload = value.encode("utf-8") if isinstance(value, str) else value
        digest.update(b"\1")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.digest()


def clip_preview_lines(text, measure, width):
    lines = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "  ").split("\n"):
        line = ""
        for char in paragraph:
            if line and measure(line + char) > width:
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    if len(lines) <= 4:
        return lines
    lines = lines[:4]
    while lines[-1] and measure(lines[-1] + "…") > width:
        lines[-1] = lines[-1][:-1]
    lines[-1] += "…"
    return lines


def history_time_label(when, today):
    clock = when.strftime("%H:%M")
    if when.date() == today:
        return clock
    if when.date() == today - timedelta(days=1):
        return f"昨天\n{clock}"
    date = when.strftime("%m/%d" if when.year == today.year else "%Y/%m/%d")
    return f"{date}\n{clock}"


def popup_placement(anchor, work, width, height, editor=None):
    gap = 8
    x = max(work.left, min(anchor.left, work.right - width))
    area = editor if editor is not None else anchor
    below = max(work.top, area.bottom + gap)
    above = min(work.bottom, area.top - gap)
    if below + height <= work.bottom:
        return x, below, height
    if above - height >= work.top:
        return x, above - height, height
    if editor is not None:
        y = max(work.top, min(anchor.top, work.bottom - height))
        if editor.right + gap + width <= work.right:
            return editor.right + gap, y, height
        if editor.left - gap - width >= work.left:
            return editor.left - gap - width, y, height
        free_above = max(0, above - work.top)
        free_below = max(0, work.bottom - below)
        if max(free_above, free_below) >= 56:
            if free_above >= free_below:
                return x, work.top, free_above
            return x, below, free_below
        # If the editor fills the screen, keep at least the caret line visible.
        if anchor is not editor:
            return popup_placement(anchor, work, width, height)
    return x, max(work.top, min(above - height, work.bottom - height)), height


class ClipboardArchive:
    def __init__(self, directory):
        self.path = directory / "clipboard-history.sqlite3"
        self.commands = queue.SimpleQueue()
        self.ui_events = queue.SimpleQueue()
        self.ready = threading.Event()
        self.message_thread_id = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="clipboard-history", daemon=True)
        self.thread.start()

    def call(self, action, *args):
        if not self.ready.wait(3) or self.message_thread_id is None:
            return {"error": "剪贴板历史服务不可用，请查看客户端日志。"}
        result = Future()
        self.commands.put((action, args, result))
        if not self.user.PostThreadMessageW(self.message_thread_id, COMMAND, 0, 0):
            result.cancel()
            return {"error": "剪贴板历史窗口不可用。"}
        try:
            return result.result(timeout=15)
        except TimeoutError:
            result.cancel()
            return {"error": "剪贴板历史操作暂未完成。"}

    def _run(self):
        try:
            self._run_window()
        except Exception:
            logging.exception("剪贴板历史服务启动失败")
        finally:
            self.message_thread_id = None
            self.ready.set()

    def _run_window(self):
        import tkinter as tk
        import winrt.runtime as runtime
        import winrt.windows.foundation  # registers async operation types
        from winrt.windows.applicationmodel.datatransfer import Clipboard

        runtime.init_apartment(runtime.MTA)
        try:
            self._run_apartment(tk, Clipboard)
        finally:
            runtime.uninit_apartment()

    def _run_apartment(self, tk, clipboard):
        self.tk = tk
        self.clipboard = clipboard
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "PostThreadMessageW": ([wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "GetMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int),
            "RegisterHotKey": ([wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "UnregisterHotKey": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
            "SetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
            "SetWindowsHookExW": ([ctypes.c_int, InputHook, wintypes.HMODULE, wintypes.DWORD], wintypes.HANDLE),
            "UnhookWindowsHookEx": ([wintypes.HANDLE], wintypes.BOOL),
            "CallNextHookEx": ([wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "WindowFromPhysicalPoint": ([wintypes.POINT], wintypes.HWND),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.c_void_p], wintypes.DWORD),
            "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "MonitorFromRect": ([ctypes.POINTER(wintypes.RECT), wintypes.DWORD], wintypes.HANDLE),
            "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "GetClipboardSequenceNumber": ([], wintypes.DWORD),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "keybd_event": ([wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.WPARAM], None),
        }
        for name, (arguments, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = arguments, result
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.update_idletasks()
        ui_thread = threading.Thread(target=self._deliver_ui_events, daemon=True)
        ui_thread.start()
        message_ready = threading.Event()
        self.hotkey_requests = queue.SimpleQueue()
        message_thread = threading.Thread(target=self._pump_messages, args=(message_ready,), daemon=True)
        message_thread.start()
        message_ready.wait(3)
        if self.message_thread_id is None:
            self.root.destroy()
            raise RuntimeError("Clipboard history message queue unavailable")
        thread_id = self.message_thread_id
        tokens = []
        self.popup = None
        self.popup_window = None
        self.dismiss_popup = None
        self.popup_key_action = None
        self.popup_cancelled = None
        self.registered = None
        self.sync_pending = False
        self.sync_error = ""
        try:
            with sqlite3.connect(self.path) as db:
                self.db = db
                db.executescript("""
                    PRAGMA auto_vacuum=FULL;
                    PRAGMA secure_delete=ON;
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS history (
                        id TEXT PRIMARY KEY, copied REAL NOT NULL, text TEXT, html TEXT,
                        image BLOB, size INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS history_time ON history(copied DESC, id DESC);
                """)
                self._prepare_history_index()
                self.enabled = self._setting("enabled", "0") == "1"
                mods, key = self._setting("shortcut", "7V")
                self.shortcut = (int(mods), key)

                def changed(*_args):
                    self.user.PostThreadMessageW(thread_id, SYNC, 0, 0)

                tokens.append((clipboard.remove_history_enabled_changed, clipboard.add_history_enabled_changed(changed)))
                self.on_change = changed
                self.history_token = None
                self._set_subscription()
                self.ready.set()
                self.root.after_idle(self._daily_cleanup)
                self.root.after_idle(self._sync)
                self.root.mainloop()
        finally:
            self.message_thread_id = None
            if getattr(self, "history_token", None) is not None:
                clipboard.remove_history_changed(self.history_token)
            for remove, token in tokens:
                remove(token)
            self.user.PostThreadMessageW(thread_id, 0x0012, 0, 0)
            self.ui_events.put(None)
            message_thread.join(timeout=2)
            ui_thread.join(timeout=2)
            self.root.destroy()
            self.root = None
            if hasattr(self, "db"):
                self.db.close()

    def _pump_messages(self, ready):
        message = wintypes.MSG()
        self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.message_thread_id = threading.get_native_id()
        ready.set()
        registered = None
        watched = None
        hook = None
        keyboard_hook = None
        target = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        module = kernel.GetModuleHandleW(None)

        @InputHook
        def on_mouse(code, message, data):
            if code == 0 and watched and message in MOUSE_DOWN:
                point = ctypes.cast(data, ctypes.POINTER(wintypes.POINT)).contents
                clicked = self.user.WindowFromPhysicalPoint(point)
                if not clicked or self.user.GetAncestor(clicked, 2) != watched:
                    cancelled = self.popup_cancelled
                    if watched == self.popup_window and cancelled is not None:
                        cancelled.set()
                    self.ui_events.put((self._dismiss_window, (watched,)))
            # Leave the click for the window underneath.
            return self.user.CallNextHookEx(None, code, message, data)

        @InputHook
        def on_keyboard(code, message, data):
            if code == 0 and watched and target and self.user.GetForegroundWindow() == target:
                key = ctypes.cast(data, ctypes.POINTER(wintypes.DWORD)).contents.value
                if message == 0x0100 and key in (0x1B, 0x0D, 0x26, 0x28):
                    if not any(self.user.GetAsyncKeyState(modifier) < 0
                               for modifier in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
                        self.ui_events.put((self._dispatch_popup_key, (watched, key)))
                        return 1
            return self.user.CallNextHookEx(None, code, message, data)

        try:
            while self.user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == REGISTER:
                    binding, result = self.hotkey_requests.get_nowait()
                    if not result.set_running_or_notify_cancel():
                        continue
                    if binding is None:
                        if registered is not None:
                            self.user.UnregisterHotKey(None, registered)
                        registered = None
                        result.set_result(None)
                    else:
                        mods, key = binding
                        candidate = 2 if registered == 1 else 1
                        if not self.user.RegisterHotKey(None, candidate, mods | 0x4000, ord(key)):
                            result.set_result(None)
                            continue
                        if registered is not None:
                            self.user.UnregisterHotKey(None, registered)
                        registered = candidate
                        result.set_result(registered)
                elif message.message == HOTKEY and message.wParam == registered:
                    self.ui_events.put((self._show_popup, (self.user.GetForegroundWindow(),)))
                elif message.message == WATCH_POPUP:
                    watched = message.wParam or None
                    target = message.lParam or None
                    if watched and not hook:
                        hook = self.user.SetWindowsHookExW(14, on_mouse, module, 0)
                        if not hook:
                            logging.warning("Clipboard popup outside-click hook unavailable: %s", ctypes.get_last_error())
                    elif not watched and hook and self.user.UnhookWindowsHookEx(hook):
                        hook = None
                    if watched and target and not keyboard_hook:
                        keyboard_hook = self.user.SetWindowsHookExW(13, on_keyboard, module, 0)
                        if not keyboard_hook:
                            logging.warning("Clipboard popup keyboard unavailable: %s", ctypes.get_last_error())
                    elif not watched and keyboard_hook and self.user.UnhookWindowsHookEx(keyboard_hook):
                        keyboard_hook = None
                elif message.message == SYNC:
                    self.ui_events.put((self._queue_sync, ()))
                elif message.message == COMMAND:
                    self.ui_events.put((self._dispatch, ()))
        finally:
            if hook:
                self.user.UnhookWindowsHookEx(hook)
            if keyboard_hook:
                self.user.UnhookWindowsHookEx(keyboard_hook)
            if registered is not None:
                self.user.UnregisterHotKey(None, registered)

    def _deliver_ui_events(self):
        while (event := self.ui_events.get()) is not None:
            callback, args = event
            self._schedule(callback, *args)

    def _schedule(self, callback, *args):
        root = self.root
        if root is None or self.message_thread_id is None:
            return
        try:
            root.after_idle(callback, *args)
        except (RuntimeError, self.tk.TclError):
            if self.message_thread_id is not None:
                logging.warning("Clipboard history window could not accept an event")

    def _queue_sync(self):
        if not self.sync_pending:
            self.sync_pending = True
            self.root.after_idle(self._sync)

    def _setting(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _save(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def _dispatch(self):
        while not self.commands.empty():
            action, args, result = self.commands.get_nowait()
            if not result.set_running_or_notify_cancel():
                continue
            try:
                result.set_result(getattr(self, "_" + action)(*args))
            except Exception:
                logging.exception("剪贴板历史操作失败：%s", action)
                result.set_result({"error": "剪贴板历史操作失败，请查看客户端日志。"})
            if action == "stop":
                break

    def _stop(self):
        self.root.quit()

    def _set_subscription(self):
        if self.enabled and self.history_token is None:
            self.history_token = self.clipboard.add_history_changed(self.on_change)
        elif not self.enabled and self.history_token is not None:
            self.clipboard.remove_history_changed(self.history_token)
            self.history_token = None
        if self.enabled and self.registered is None:
            self._register(self.shortcut)
        elif not self.enabled and self.registered is not None:
            self._request_hotkey(None)
            self.registered = None

    def _request_hotkey(self, binding):
        result = Future()
        self.hotkey_requests.put((binding, result))
        if not self.user.PostThreadMessageW(self.message_thread_id, REGISTER, 0, 0):
            result.cancel()
            return None
        try:
            return result.result(timeout=2)
        except TimeoutError:
            result.cancel()
            return None

    def _register(self, shortcut):
        identifier = self._request_hotkey(shortcut)
        if identifier is None:
            return False
        self.registered = identifier
        return True

    def _state(self):
        return {"enabled": self.enabled, "windowsEnabled": self.clipboard.is_history_enabled(),
                "mods": self.shortcut[0], "key": self.shortcut[1],
                "hotkeyReady": self.registered is not None, "syncError": self.sync_error,
                "storage": self._storage_usage()}

    def _storage_options(self):
        return int(self._setting("retention_days", "0")), int(self._setting("image_limit_mib", "0"))

    @staticmethod
    def _cutoff(days):
        return time.time() - days * DAY if days else 0

    def _storage_usage(self):
        days, limit = self._storage_options()
        text, image = self.db.execute("""
            SELECT COALESCE(SUM(size - image_bytes), 0),
                   COALESCE(SUM(image_bytes), 0) FROM history
        """).fetchone()
        return {"days": days, "imageLimit": limit, "textBytes": text, "imageBytes": image}

    def _prune(self, days, limit, confirmed_count=None):
        cutoff = self._cutoff(days)
        sql = """
            WITH eligible_images AS (
                SELECT id, SUM(image_bytes) OVER (
                    ORDER BY copied DESC, id DESC ROWS UNBOUNDED PRECEDING
                ) AS total
                FROM history WHERE copied >= :cutoff AND image IS NOT NULL AND :limit > 0
            )
            SELECT id FROM history WHERE copied < :cutoff
            UNION ALL
            SELECT id FROM eligible_images WHERE total > :limit
        """
        params = {"cutoff": cutoff, "limit": limit * MIB}
        if confirmed_count is None:
            return self.db.execute("SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
        count = self.db.execute("SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
        if count <= confirmed_count:
            self.db.execute("DELETE FROM history WHERE id IN (" + sql + ")", params)
        return count

    def _storage(self, days, limit, confirmed_count=None):
        if type(days) is not int or days not in (0, 7, 30, 90) or type(limit) is not int or not 0 <= limit <= 102400:
            return {"error": "存储设置无效。图片上限可填 0 至 102400 MB。"}
        if confirmed_count is not None and (type(confirmed_count) is not int or confirmed_count < 0):
            return {"error": "清理确认无效。"}
        with self.db:
            count = self._prune(days, limit, confirmed_count)
            if confirmed_count is None or count > confirmed_count:
                return {"pending": count}
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                (("retention_days", str(days)), ("image_limit_mib", str(limit))))
        return self._state()

    def _daily_cleanup(self):
        try:
            days, limit = self._storage_options()
            if days or limit:
                with self.db:
                    self._prune(days, limit, float("inf"))
        except sqlite3.Error:
            logging.warning("本地剪贴板历史定期清理未完成")
        finally:
            if self.root is not None:
                self.root.after(DAY * 1000, self._daily_cleanup)

    def _prepare_history_index(self):
        from clipboard_protection import protect, unprotect

        columns = {row[1] for row in self.db.execute("PRAGMA table_info(history)")}
        existing = self.db.execute("SELECT value FROM settings WHERE key='content_key'").fetchone()
        if existing is not None:
            self.content_key = unprotect(existing[0])
            return

        self.content_key = secrets.token_bytes(32)
        protected_key = protect(self.content_key)
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DROP INDEX IF EXISTS history_fingerprint_time")
            if "fingerprint" not in columns:
                self.db.execute("ALTER TABLE history ADD COLUMN fingerprint BLOB")
            if "image_bytes" not in columns:
                self.db.execute("ALTER TABLE history ADD COLUMN image_bytes INTEGER NOT NULL DEFAULT 0")
            # Migrate in bounded batches. The transaction rolls back on error.
            after = None
            while True:
                rows = self.db.execute("""
                    SELECT id,text,html,image FROM history
                    WHERE (? IS NULL OR id > ?) ORDER BY id LIMIT 16
                """, (after, after)).fetchall()
                if not rows:
                    break
                for item_id, text, html, image in rows:
                    encrypted = self._protect_record(text, html, image)
                    fingerprint = history_fingerprint(text, html, image, self.content_key)
                    self.db.execute("""
                        UPDATE history SET text=?,html=?,image=?,image_bytes=?,fingerprint=? WHERE id=?
                    """, (*encrypted, len(image or b""), fingerprint, item_id))
                after = rows[-1][0]
            self.db.execute("""
                CREATE INDEX history_fingerprint_time ON history(fingerprint, copied DESC, id DESC)
            """)
            self.db.execute("INSERT INTO settings VALUES ('content_key', ?)", (protected_key,))

    @staticmethod
    def _protect_record(text, html, image):
        from clipboard_protection import protect

        return (protect(text.encode("utf-8")) if text is not None else None,
                protect(html.encode("utf-8")) if html is not None else None,
                protect(image) if image is not None else None)

    @staticmethod
    def _unprotect_record(text, html, image):
        from clipboard_protection import unprotect

        return (unprotect(text).decode("utf-8") if text is not None else None,
                unprotect(html).decode("utf-8") if html is not None else None,
                unprotect(image) if image is not None else None)

    def _history_page(self, cursor=None):
        days, _ = self._storage_options()
        records = self.db.execute("""
            SELECT entry.id,entry.copied,entry.text,
                   entry.image IS NOT NULL,entry.html IS NOT NULL
            FROM history AS entry WHERE entry.copied >= ?
                AND (entry.copied, entry.id) < (?, ?)
                AND NOT EXISTS (
                    SELECT 1 FROM history AS newer
                    WHERE newer.fingerprint = entry.fingerprint
                      AND (newer.copied, newer.id) > (entry.copied, entry.id)
                )
            ORDER BY entry.copied DESC,entry.id DESC LIMIT ?
        """, (self._cutoff(days), *(cursor or (float("inf"), "")), PAGE_SIZE)).fetchall()
        from clipboard_protection import unprotect

        return [(item_id, copied, unprotect(text).decode("utf-8")[:1000] if text else "", image, html)
                for item_id, copied, text, image, html in records]

    def _record_history(self, item_id, copied, text, html, image, size):
        fingerprint = history_fingerprint(text, html, image, self.content_key)
        previous = self.db.execute("""
            SELECT id,copied FROM history WHERE fingerprint=?
            ORDER BY copied DESC,id DESC LIMIT 1
        """, (fingerprint,)).fetchone()
        if previous is not None:
            if copied > previous[1]:
                self.db.execute("UPDATE history SET copied=? WHERE id=?", (copied, previous[0]))
            return False
        self._check_free_space(size)
        self.db.execute("""
            INSERT INTO history (id,copied,text,html,image,size,fingerprint,image_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (item_id, copied, *self._protect_record(text, html, image), size, fingerprint, len(image or b"")))
        return True

    def _enable(self, enable):
        self._save("enabled", "1" if enable else "0")
        self.enabled = enable
        self._set_subscription()
        self._sync()
        return self._state()

    def _shortcut(self, mods, key):
        if type(mods) is not int or mods not in (1, 2, 3, 5, 6, 7) or not isinstance(key, str) or len(key) != 1 or not "A" <= key <= "Z":
            return {"error": "请使用 Ctrl 或 Alt 搭配一个字母。"}
        shortcut = (mods, key)
        if self.enabled and (shortcut != self.shortcut or self.registered is None) and not self._register(shortcut):
            return {"error": "该快捷键已被占用，请换一个。"}
        self._save("shortcut", f"{mods}{key}")
        self.shortcut = shortcut
        return self._state()

    def _sync(self):
        self.sync_pending = False
        if not self.enabled:
            self.sync_error = ""
            return
        try:
            asyncio.run(asyncio.wait_for(self._read_snapshot(), timeout=5))
            self.sync_error = ""
        except OSError as exc:
            if exc.errno == 28:
                self.sync_error = "磁盘空间不足，已暂停保存新记录。"
                return
            self.sync_error = "暂时无法读取或保存 Windows 历史。"
        except Exception as exc:
            if not self.sync_error:
                logging.warning("Windows 剪贴板历史读取或保存失败：%s", type(exc).__name__)
            self.sync_error = "暂时无法读取或保存 Windows 历史。"

    async def _read_snapshot(self):
        from winrt.windows.applicationmodel.datatransfer import ClipboardHistoryItemsResultStatus as Status
        from winrt.windows.storage.streams import DataReader

        if not self.clipboard.is_history_enabled():
            return
        result = await self.clipboard.get_history_items_async()
        if result.status == Status.CLIPBOARD_HISTORY_DISABLED:
            return
        if result.status != Status.SUCCESS:
            raise RuntimeError("Windows denied clipboard history access")
        items = list(result.items)
        seen = {row[0] for row in self.db.execute("SELECT id FROM seen")}
        inserted = False
        unreadable = False
        days, image_limit = self._storage_options()
        cutoff = self._cutoff(days)
        try:
            for item in reversed(items):
                if item.timestamp.timestamp() < cutoff or item.id in seen or self.db.execute("SELECT 1 FROM history WHERE id=?", (item.id,)).fetchone():
                    continue
                try:
                    content = item.content
                    text = await content.get_text_async() if content.contains("Text") else None
                    html = await content.get_html_format_async() if content.contains("HTML Format") else None
                    image = None
                    if content.contains("Bitmap"):
                        reference = await content.get_bitmap_async()
                        with await reference.open_read_async() as stream:
                            if stream.size > 0:
                                self._check_free_space(stream.size)
                                with DataReader(stream.get_input_stream_at(0)) as reader:
                                    length = await reader.load_async(stream.size)
                                    if length != stream.size:
                                        raise OSError("Incomplete clipboard image")
                                    buffer = bytearray(length)
                                    reader.read_bytes(buffer)
                                    image = bytes(buffer)
                except (OSError, ValueError, TypeError) as exc:
                    if getattr(exc, "errno", None) == 28:
                        raise
                    logging.warning("Could not read a Windows clipboard history item: %s", type(exc).__name__)
                    unreadable = True
                    continue
                size = len((text or "").encode()) + len((html or "").encode()) + len(image or b"")
                with self.db:
                    if size:
                        inserted |= self._record_history(
                            item.id, item.timestamp.timestamp(), text, html, image, size
                        )
                    self.db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (item.id,))
        finally:
            with self.db:
                current = {item.id for item in items}
                self.db.executemany("DELETE FROM seen WHERE id=?", ((key,) for key in seen - current))
                if inserted and (days or image_limit):
                    self._prune(days, image_limit, float("inf"))
        if unreadable:
            raise OSError("Some clipboard history items could not be saved")

    def _check_free_space(self, size):
        if shutil.disk_usage(self.path.parent).free < DISK_RESERVE + size:
            raise OSError(28, "Clipboard history volume is low on space")

    def _clear(self):
        if self.clipboard.is_history_enabled():
            asyncio.run(asyncio.wait_for(self._mark_current_seen(), timeout=5))
        with self.db:
            self.db.execute("DELETE FROM history")
        return self._state()

    async def _mark_current_seen(self):
        from winrt.windows.applicationmodel.datatransfer import ClipboardHistoryItemsResultStatus as Status

        result = await self.clipboard.get_history_items_async()
        if result.status != Status.SUCCESS:
            raise RuntimeError("Cannot enumerate current Windows clipboard items")
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?)", ((item.id,) for item in result.items))

    def _view(self):
        self._show_popup(None)
        return {}

    def _accessible_anchor(self, target, bounds):
        # Browser-based editors may expose only an accessibility caret or a
        # focused edit rectangle, with no Win32 caret HWND.
        edit = None
        try:
            import clr
            from pathlib import Path
            from System.Runtime.InteropServices import RuntimeEnvironment

            wpf = Path(str(RuntimeEnvironment.GetRuntimeDirectory())) / "WPF"
            for assembly in ("WindowsBase", "UIAutomationTypes", "UIAutomationClient"):
                clr.AddReference(str(wpf / (assembly + ".dll")))
            from System.Windows.Automation import AutomationElement, ControlType, TextPattern, TreeWalker, ValuePattern

            element = AutomationElement.FocusedElement
            if element is None or self.user.GetForegroundWindow() != target:
                return None
            # Some editors report a text fragment or an internal child as focused.
            for _ in range(6):
                if element is None:
                    break
                control = element.Current
                patterns = element.GetSupportedPatterns()
                has_text = TextPattern.Pattern in patterns
                is_editor = control.ControlType == ControlType.Edit
                if not is_editor and ValuePattern.Pattern in patterns:
                    is_editor = not element.GetCurrentPattern(ValuePattern.Pattern).Current.IsReadOnly
                if is_editor or (has_text and control.ControlType == ControlType.Document):
                    edit = self._bounded_anchor(control.BoundingRectangle, bounds)
                    if edit is not None:
                        if has_text:
                            pattern = element.GetCurrentPattern(TextPattern.Pattern)
                            for selection in pattern.GetSelection():
                                rectangles = selection.GetBoundingRectangles()
                                if len(rectangles):
                                    caret = self._bounded_anchor(rectangles[-1], edit)
                                    if caret is not None:
                                        return caret, edit
                        return edit, edit
                element = TreeWalker.RawViewWalker.GetParent(element)
            return None
        except Exception as exc:
            logging.debug("Clipboard popup accessibility anchor unavailable: %s", type(exc).__name__)
            return (edit, edit) if edit is not None else None

    @staticmethod
    def _bounded_anchor(rect, bounds):
        if rect.IsEmpty:
            return None
        left, top = max(bounds.left, round(rect.Left)), max(bounds.top, round(rect.Top))
        right, bottom = min(bounds.right, round(rect.Right)), min(bounds.bottom, round(rect.Bottom))
        if left <= right and top < bottom and left < bounds.right:
            return wintypes.RECT(left, top, max(left + 1, right), bottom)
        return None

    def _native_edit_rect(self, focused, bounds):
        if not focused:
            return None
        name = ctypes.create_unicode_buffer(128)
        if not self.user.GetClassNameW(focused, name, len(name)):
            return None
        class_name = name.value.lower()
        if class_name != "edit" and not class_name.startswith("richedit"):
            return None
        rect = wintypes.RECT()
        if not self.user.GetWindowRect(focused, ctypes.byref(rect)):
            return None
        left, top = max(rect.left, bounds.left), max(rect.top, bounds.top)
        right, bottom = min(rect.right, bounds.right), min(rect.bottom, bounds.bottom)
        return wintypes.RECT(left, top, right, bottom) if left < right and top < bottom else None

    def _pointer_anchor(self):
        point = wintypes.POINT()
        self.user.GetCursorPos(ctypes.byref(point))
        return wintypes.RECT(point.x, point.y, point.x + 1, point.y + 1)

    def _popup_anchor(self, target):
        target = target or self.user.GetForegroundWindow()
        bounds = wintypes.RECT()
        if not target or not self.user.GetWindowRect(target, ctypes.byref(bounds)):
            return self._pointer_anchor(), None
        caret = None
        gui = GuiThreadInfo(size=ctypes.sizeof(GuiThreadInfo))
        thread = self.user.GetWindowThreadProcessId(target, None)
        has_gui_info = thread and self.user.GetGUIThreadInfo(thread, ctypes.byref(gui))
        if has_gui_info and gui.caret:
            rect = gui.caret_rect
            start = wintypes.POINT(rect.left, rect.top)
            end = wintypes.POINT(rect.right, rect.bottom)
            if (rect.bottom > rect.top
                    and self.user.ClientToScreen(gui.caret, ctypes.byref(start))
                    and self.user.ClientToScreen(gui.caret, ctypes.byref(end))
                    and bounds.left <= start.x < bounds.right
                    and bounds.top <= start.y < bounds.bottom):
                caret = wintypes.RECT(start.x, start.y, max(start.x + 1, end.x), end.y)
        accessible = self._accessible_anchor(target, bounds)
        if accessible is not None:
            accessible_anchor, editor = accessible
            if caret is None:
                return accessible_anchor, editor
            if editor.left <= caret.left < editor.right and editor.top <= caret.top < editor.bottom:
                return caret, editor
        editor = self._native_edit_rect(gui.focus, bounds) if has_gui_info else None
        if editor is not None:
            return (caret if caret is not None else editor), editor
        if caret is not None:
            line = wintypes.RECT(bounds.left, max(bounds.top, caret.top - 12),
                                 bounds.right, min(bounds.bottom, caret.bottom + 12))
            return caret, line
        return self._pointer_anchor(), None

    def _dismiss_window(self, window):
        if window == self.popup_window and self.dismiss_popup is not None:
            self.dismiss_popup()

    def _dispatch_popup_key(self, window, key):
        if window == self.popup_window and self.popup_key_action is not None:
            self.popup_key_action(key)

    def _show_popup(self, target):
        tk = self.tk
        if self.popup is not None:
            return
        anchor, editor = self._popup_anchor(target)
        monitor = self.user.MonitorFromRect(ctypes.byref(anchor), 2)
        info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        if not self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            logging.warning("Cannot locate clipboard history popup monitor")
            return
        popup = self.popup = tk.Toplevel(self.root)
        cancelled = threading.Event()
        paste_pending = False
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#9eb4d7", padx=1, pady=1)

        thumbnail_job = None

        def cancel_thumbnails(_event=None):
            nonlocal thumbnail_job
            if thumbnail_job is not None:
                popup.after_cancel(thumbnail_job)
                thumbnail_job = None

        def close():
            if self.popup is popup:
                cancel_thumbnails()
                cancelled.set()
                self.popup = None
                self.popup_window = None
                self.dismiss_popup = None
                self.popup_key_action = None
                self.popup_cancelled = None
                self.user.PostThreadMessageW(self.message_thread_id, WATCH_POPUP, 0, 0)
                popup.destroy()

        popup.bind("<Escape>", lambda _event: close())
        header = tk.Frame(popup, bg="#244eb4")
        header.pack(fill="x")
        tk.Label(header, text="剪贴板历史", anchor="w", padx=12, pady=8,
                 bg="#244eb4", fg="white", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        dismiss = tk.Button(header, text="×", command=close, width=3, borderwidth=0,
                            bg="#244eb4", fg="white", activebackground="#3a62bf",
                            activeforeground="white", font=("Segoe UI", 13),
                            takefocus=not target, highlightthickness=0)
        dismiss.pack(side="right", padx=4, pady=2)
        from tkinter import font
        from PIL import Image, ImageTk
        from clipboard_protection import unprotect

        width = min(376, info.work.right - info.work.left)
        rows = tk.Canvas(popup, width=width - 2, bg="#f4f6fa", borderwidth=0, highlightthickness=0)
        status = tk.Label(popup, bg="white", fg="#53637a", anchor="w", padx=10, pady=6)

        def show_status(message):
            status.configure(text=message)
            if not status.winfo_ismapped():
                status.pack(fill="x", side="bottom", before=rows)

        rows.pack(fill="both", expand=True)
        body_font = font.Font(root=popup, family="Microsoft YaHei UI", size=10)
        time_font = font.Font(root=popup, family="Segoe UI", size=9)
        line_height = body_font.metrics("linespace")
        text_left, time_right, column_gap = 15, width - 17, 14
        results, starts, labels = [], [0], []
        selected = 0
        exhausted = False
        today = datetime.now().date()

        def append_page():
            nonlocal exhausted
            cursor = (results[-1][1], results[-1][0]) if results else None
            page = self._history_page(cursor)
            exhausted = len(page) < PAGE_SIZE
            for _id, copied, text, image, html in page:
                preview = "" if image else (text or ("富文本" if html else "文字"))
                when = datetime.fromtimestamp(copied)
                time_label = history_time_label(when, today)
                time_width = max(time_font.measure(line) for line in time_label.split("\n"))
                content_width = max(1, time_right - time_width - column_gap - text_left)
                lines = clip_preview_lines(preview, body_font.measure, content_width)
                labels.append((lines, time_label, content_width))
                height = 99 if image else max(48 if "\n" in time_label else 43, len(lines) * line_height + 24)
                starts.append(starts[-1] + height)
            results.extend(page)
            rows.configure(scrollregion=(0, 0, width - 2, max(43, starts[-1])))

        append_page()
        thumbnails = {}
        failed_thumbnails = set()

        def render_rows(viewport_height=None):
            nonlocal thumbnail_job
            thumbnail_job = None
            top = max(0, rows.canvasy(0))
            bottom = top + (viewport_height or rows.winfo_height())
            if not exhausted and bottom >= starts[max(0, len(results) - 5)]:
                append_page()
            start = max(0, bisect_right(starts, top) - 1)
            end = min(len(results), bisect_right(starts, bottom))
            visible = {i for i in range(start, end) if results[i][3]}
            rows.delete("all")
            for index in thumbnails.keys() - visible:
                del thumbnails[index]
            if not results:
                rows.create_text(13, 21, text="暂无剪贴板历史", font=body_font,
                                 fill="#53637a", anchor="w")
                return
            for index in sorted(visible - thumbnails.keys() - failed_thumbnails):
                record = self.db.execute("SELECT image FROM history WHERE id=?", (results[index][0],)).fetchone()
                try:
                    if not record or not record[0]:
                        failed_thumbnails.add(index)
                        continue
                    with Image.open(BytesIO(unprotect(record[0]))) as image:
                        image_height = 112 if image.height > image.width else 76
                        image.thumbnail((labels[index][2], image_height), Image.Resampling.LANCZOS)
                        thumbnails[index] = ImageTk.PhotoImage(image.convert("RGBA"), master=popup)
                    target_height = image_height + 23
                    extra = target_height - (starts[index + 1] - starts[index])
                    if extra:
                        starts[index + 1:] = [position + extra for position in starts[index + 1:]]
                        rows.configure(scrollregion=(0, 0, width - 2, starts[-1]))
                except (OSError, ValueError, Image.DecompressionBombError):
                    failed_thumbnails.add(index)
            end = min(len(results), bisect_right(starts, bottom))
            for index in range(start, end):
                y, next_y = starts[index:index + 2]
                x1, y1, x2, y2, radius = 6, y + 3, width - 8, next_y - 2, 7
                active = index == selected
                rows.create_polygon(
                    x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
                    x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
                    x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
                    smooth=True, fill="#eaf2ff" if active else "#ffffff",
                    outline="#86a9e5" if active else "#dce4ee",
                )
                lines, time_label, _ = labels[index]
                rows.create_text(time_right, y + 12, text=time_label, font=time_font,
                                 fill="#53637a", anchor="ne", justify="right")
                if index in thumbnails:
                    thumbnail = thumbnails[index]
                    image_top = y + 11
                    rows.create_image(text_left, image_top, image=thumbnail, anchor="nw")
                    rows.create_rectangle(text_left - 1, image_top - 1,
                                          text_left + thumbnail.width(), image_top + thumbnail.height(),
                                          outline="#b7c6da")
                elif not results[index][3]:
                    for offset, line in enumerate(lines):
                        rows.create_text(text_left, y + 11 + offset * line_height, text=line,
                                         font=body_font, fill="#18243a", anchor="nw")

        def schedule_thumbnails(*_args):
            nonlocal thumbnail_job
            if thumbnail_job is None:
                thumbnail_job = popup.after_idle(render_rows)

        def scroll(event):
            if event.delta:
                rows.yview_scroll(-int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1), "units")
            return "break"

        rows.configure(yscrollincrement=24, yscrollcommand=schedule_thumbnails)
        rows.bind("<MouseWheel>", scroll)
        rows.bind("<Configure>", schedule_thumbnails)
        rows.bind("<Destroy>", cancel_thumbnails)

        def row_at(x, y):
            position = rows.canvasy(y)
            index = bisect_right(starts, position) - 1
            if (6 <= x < width - 8 and 0 <= y < rows.winfo_height()
                    and 0 <= index < len(results)
                    and starts[index] + 3 <= position < starts[index + 1] - 2):
                return index
            return None

        def finish_paste(sequence, retries=8):
            if self.popup is not popup:
                return
            if (cancelled.is_set() or self.user.GetForegroundWindow() != target
                    or self.user.GetClipboardSequenceNumber() != sequence):
                close()
                return
            if any(self.user.GetAsyncKeyState(key) < 0 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
                if retries:
                    popup.after(35, finish_paste, sequence, retries - 1)
                else:
                    close()
                return
            if not cancelled.is_set():
                for key, flags in ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)):
                    self.user.keybd_event(key, 0, flags, 0)
            close()

        def paste(_event=None):
            nonlocal paste_pending
            if paste_pending or cancelled.is_set():
                return
            if not results or selected >= len(results):
                return
            days, _ = self._storage_options()
            record = self.db.execute("SELECT text,html,image FROM history WHERE id=? AND copied >= ?",
                                     (results[selected][0], self._cutoff(days))).fetchone()
            if record is None:
                show_status("此记录已清除，请重新打开历史。")
                return
            if target and self.user.GetForegroundWindow() != target:
                close()
                return
            try:
                sequence = self._copy_record(*self._unprotect_record(*record))
            except Exception as exc:
                logging.warning("从剪贴板历史恢复失败：%s (Win32=%s)", type(exc).__name__, getattr(exc, "winerror", None))
                show_status("无法恢复所选内容，请重试。")
                return
            if target:
                paste_pending = True
                finish_paste(sequence)
            else:
                close()

        def keyboard_action(key):
            nonlocal selected
            if target and self.user.GetForegroundWindow() != target:
                close()
                return
            if key == 0x1B:
                close()
            elif key == 0x0D:
                paste()
            elif results:
                if key == 0x28 and selected + 1 == len(results) and not exhausted:
                    append_page()
                selected = min(len(results) - 1, max(0, selected + (1 if key == 0x28 else -1)))
                top = rows.canvasy(0)
                bottom = top + rows.winfo_height()
                if starts[selected] < top:
                    rows.yview_moveto(starts[selected] / starts[-1])
                elif starts[selected + 1] > bottom:
                    rows.yview_moveto(max(0, starts[selected + 1] - rows.winfo_height() + 23) / starts[-1])
                schedule_thumbnails()

        def check_target():
            if self.popup is popup:
                if self.user.GetForegroundWindow() != target:
                    close()
                else:
                    popup.after(150, check_target)

        rows.bind("<Return>", paste)
        rows.bind("<Up>", lambda _event: keyboard_action(0x26))
        rows.bind("<Down>", lambda _event: keyboard_action(0x28))
        pressed_row = None

        def select_with_mouse(event):
            nonlocal pressed_row, selected
            pressed_row = row_at(event.x, event.y)
            if pressed_row is not None:
                selected = pressed_row
                schedule_thumbnails()
            return "break"

        def release_row(event):
            nonlocal pressed_row
            index, pressed_row = pressed_row, None
            if index is not None and row_at(event.x, event.y) == index:
                paste()
            return "break"

        rows.bind("<Button-1>", select_with_mouse)
        rows.bind("<ButtonRelease-1>", release_row)
        if not target:
            popup.bind("<FocusOut>", lambda _event: popup.after_idle(
                lambda: close() if self.popup is popup and self.user.GetForegroundWindow() != self.popup_window else None
            ))
        cancel_thumbnails()
        render_rows(max(43, min(338, starts[min(5, len(results))])))
        rows.configure(height=max(43, min(338, starts[min(5, len(results))])))
        popup.update_idletasks()
        height = min(popup.winfo_reqheight(), info.work.bottom - info.work.top)
        x, y, height = popup_placement(anchor, info.work, width, height, editor)
        popup.geometry(f"{width}x{height}")
        popup.update_idletasks()
        window = self.user.GetAncestor(popup.winfo_id(), 2)
        if target:
            # WS_EX_NOACTIVATE keeps the original editor focused even on click.
            style = self.user.GetWindowLongPtrW(window, -20)
            self.user.SetWindowLongPtrW(window, -20, style | 0x08000000)
            if not self.user.GetWindowLongPtrW(window, -20) & 0x08000000:
                close()
                return
        if not self.user.SetWindowPos(window, None, x, y, width, height, 0x0014):
            close()
            return
        if target:
            # Unlike deiconify(), Tk's normal state maps without requesting focus.
            popup.attributes("-disabled", True)
            popup.state("normal")
            popup.update_idletasks()
            window = self.user.GetAncestor(popup.winfo_id(), 2)
            style = self.user.GetWindowLongPtrW(window, -20)
            self.user.SetWindowLongPtrW(window, -20, style | 0x08000000)
            self.user.SetWindowPos(window, None, x, y, width, height, 0x0014)
            popup.attributes("-disabled", False)
            if self.user.GetForegroundWindow() != target:
                logging.warning("Clipboard popup mapping changed the foreground window")
                close()
                return
            popup.after(150, check_target)
        else:
            popup.deiconify()
            self.user.SetWindowPos(window, None, x, y, width, height, 0x0014)
            rows.focus_force()
        self.popup_cancelled = cancelled
        self.popup_window = window
        self.dismiss_popup = close
        self.popup_key_action = keyboard_action if target else None
        self.user.PostThreadMessageW(self.message_thread_id, WATCH_POPUP, window, target or 0)

    def _copy_record(self, text, html, image):
        from receiver import _NativeClipboardApi, _native_dib_payloads, _publish_clipboard_payloads
        from winrt.windows.applicationmodel.datatransfer import HtmlFormatHelper

        api = _NativeClipboardApi(None, 1, 1)
        payloads = {}
        if text:
            payloads[13] = text.encode("utf-16-le") + b"\0\0"
        if html:
            if not html.lstrip().startswith("Version:"):
                html = HtmlFormatHelper.create_html_format(html)
            payloads[api.call("RegisterClipboardFormatW", "HTML Format")] = html.encode("utf-8") + b"\0"
        if image:
            from PIL import Image

            with Image.open(BytesIO(image)) as bitmap:
                if bitmap.width * bitmap.height * 4 > 256 * 1024 * 1024:
                    raise ValueError("Clipboard image is too large")
                with bitmap.convert("RGBA") as rgba:
                    payloads.update(_native_dib_payloads(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA")))
        if not payloads:
            raise ValueError("No supported content in history record")
        exclusion = api.call("RegisterClipboardFormatW", "ExcludeClipboardContentFromMonitorProcessing")
        payloads[exclusion] = b"\0"
        _publish_clipboard_payloads(api, self.popup_window, payloads)
        return api.call("GetClipboardSequenceNumber")

