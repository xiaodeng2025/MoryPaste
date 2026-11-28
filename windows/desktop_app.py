from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
import traceback
import subprocess
import uuid
import io
import zipfile
from collections import deque
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
import winreg

import pystray
import segno
import webview
from PIL import Image

from process_manager import CoreStartupError, ExistingReceiverError, ReceiverProcessManager
from desktop_settings import APP_VERSION, DesktopSettings
from management_view import recent_receipts
import firewall_access
from update_check import ANDROID_FALLBACK_PAGE, android_release_page, check_update, download_installer, verify_installer


APP_NAME = "MoryPaste"
DASHBOARD_URL = "http://127.0.0.1:48120/"
WINDOW_WIDTH = 960
WINDOW_HEIGHT = 640
WINDOW_MIN_SIZE = (820, 580)
WINDOWS_ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
MB_OK = 0x00000000
MB_ICONERROR = 0x00000010
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND = 0x00010000
MB_YESNO = 0x00000004
IDYES = 6
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
INSTALL_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR
WINDOW_ICON_PATH = BASE_DIR / "desktop_resources" / "morypaste.ico"


def _existing_explorer_window(directory: Path) -> int | None:
    # pywebview already ships the .NET runtime bridge. Use the Shell window
    # collection so identical folder names elsewhere do not match.
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    initialized = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if initialized not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE: COM was initialized already.
        return None
    try:
        import clr
        clr.AddReference("Microsoft.VisualBasic")
        from Microsoft.VisualBasic import CallType, Interaction
        from System import Array, Int32, Object

        shell = Interaction.CreateObject("Shell.Application")
        windows = Interaction.CallByName(shell, "Windows", CallType.Method)
        target = os.path.normcase(os.path.normpath(directory))
        for index in range(int(Interaction.CallByName(windows, "Count", CallType.Get))):
            try:
                window = Interaction.CallByName(windows, "Item", CallType.Method, Array[Object]([Int32(index)]))
                program = Interaction.CallByName(window, "FullName", CallType.Get)
                if Path(str(program)).name.lower() != "explorer.exe":
                    continue
                url = urllib.parse.urlsplit(str(Interaction.CallByName(window, "LocationURL", CallType.Get)))
                if url.scheme != "file":
                    continue
                path = urllib.request.url2pathname(url.path)
                if url.netloc and url.netloc.lower() != "localhost":
                    path = "\\\\" + urllib.parse.unquote(url.netloc) + path
                if os.path.normcase(os.path.normpath(path)) == target:
                    return int(Interaction.CallByName(window, "HWND", CallType.Get))
            except Exception:
                # A Shell window can close while its properties are being read.
                continue
    except Exception:
        logging.debug("无法枚举已打开的资源管理器窗口", exc_info=True)
    finally:
        if initialized in (0, 1):
            ole.CoUninitialize()
    return None


def _open_explorer_folder(directory: Path) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetShellWindow.argtypes = []
    user.GetShellWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user.AllowSetForegroundWindow.restype = wintypes.BOOL
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindowAsync.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    existing = _existing_explorer_window(directory)
    pid = wintypes.DWORD()
    shell = user.GetShellWindow()
    if shell and user.GetWindowThreadProcessId(shell, ctypes.byref(pid)) and pid.value:
        # The shell may handle the request in its existing Explorer process.
        user.AllowSetForegroundWindow(pid)
    os.startfile(directory, "open", show_cmd=1)  # SW_SHOWNORMAL
    foreground = user.GetForegroundWindow()
    if (existing and foreground and user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
            and pid.value == os.getpid()):
        if user.IsIconic(existing):
            user.ShowWindowAsync(existing, SW_RESTORE)
        user.SetForegroundWindow(existing)
from data_paths import resolve_data_dir
try:
    APP_DATA = resolve_data_dir()
except Exception as exc:
    ctypes.windll.user32.MessageBoxW(None, str(exc), 'MoryPaste 数据隔离检查失败', 0x10)
    raise SystemExit(2)

LOG_PATH = APP_DATA / "desktop-client.log"
WEBVIEW2_CLIENT_ID = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def configure_logging() -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1024 * 1024:
        backup = LOG_PATH.with_suffix(".log.1")
        if backup.exists():
            backup.unlink()
        LOG_PATH.replace(backup)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )

    logging.info("实际数据目录：%s", APP_DATA)

def show_message(title: str, message: str, icon: int = MB_ICONERROR) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, title, MB_OK | icon | MB_SETFOREGROUND)


def ask_yes_no(title: str, message: str) -> bool:
    return ctypes.windll.user32.MessageBoxW(
        None, message, title, MB_YESNO | MB_ICONINFORMATION | MB_SETFOREGROUND
    ) == IDYES


def install_webview2_if_needed() -> bool:
    if webview2_runtime_version() is not None:
        return True
    bootstrapper = INSTALL_DIR / "runtime" / "MicrosoftEdgeWebview2Setup.exe"
    if not bootstrapper.is_file():
        show_message("需要 Microsoft WebView2", f"未找到随安装包提供的 Microsoft WebView2 安装组件。\n\n请从微软官方页面安装后重试：\n{WEBVIEW2_DOWNLOAD_URL}", MB_ICONWARNING)
        try:
            os.startfile(WEBVIEW2_DOWNLOAD_URL)
        except OSError:
            pass
        return False

    while True:
        if not ask_yes_no(
            "安装 Microsoft WebView2",
            "MoryPaste 的桌面窗口需要 Microsoft WebView2 Runtime。当前电脑尚未检测到它。\n\n"
            "现在将运行随安装包提供的微软官方安装程序。若电脑没有网络，安装可能失败；"
            "此过程使用当前用户权限，不会安装 Windows 服务。\n\n是否现在安装？",
        ):
            try:
                os.startfile(WEBVIEW2_DOWNLOAD_URL)
            except OSError:
                pass
            return False
        try:
            result = subprocess.run(
                [str(bootstrapper), "/silent", "/install"], capture_output=True, timeout=300,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = webview2_runtime_version()
            if version:
                logging.info("WebView2 Runtime 安装并验证成功：%s", version)
                return True
            detail = f"安装程序退出代码：{result.returncode}。"
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
        logging.error("WebView2 Runtime 安装未完成：%s", detail)
        if not ask_yes_no(
            "WebView2 安装未完成",
            f"没有检测到安装成功的 WebView2 Runtime。\n{detail}\n\n"
            "选择“是”重试；选择“否”打开微软官方页面，稍后可重新启动 MoryPaste。",
        ):
            try:
                os.startfile(WEBVIEW2_DOWNLOAD_URL)
            except OSError:
                pass
            return False


def activate_existing_window(timeout_seconds: float = 3.0) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def webview2_runtime_version() -> str | None:
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
    )
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                version, _kind = winreg.QueryValueEx(key, "pv")
            if isinstance(version, str) and version and version != "0.0.0.0":
                return version
        except OSError:
            continue
    return None


class SingleInstance:
    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        self._kernel32 = kernel32
        self._handle = kernel32.CreateMutexW(None, 0, r"Local\SendToCopyQ-Desktop-0.6")
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "无法创建客户端单实例锁")
        self.already_running = ctypes.get_last_error() == WINDOWS_ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class DesktopClient:
    def __init__(self) -> None:
        self.window = None
        self.tray: pystray.Icon | None = None
        self._tray_stop = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False
        self._exit_result: bool | None = None
        self.pending_installer: tuple[Path, str] | None = None
        self.pending_restart = False
        self._window_visible = "--start-hidden" not in sys.argv
        self._native_activation_hooked = False
        self._notified_tasks = deque(maxlen=256)
        packaged_receiver = INSTALL_DIR / "receiver" / "SendToCopyQ-Receiver.exe" if getattr(sys, "frozen", False) else None
        self.lan_control_token = uuid.uuid4().hex if packaged_receiver is not None else None
        self.outgoing_token = uuid.uuid4().hex
        self.core = ReceiverProcessManager(
            BASE_DIR / "receiver.py", self._on_unexpected_core_exit, packaged_executable=packaged_receiver,
            on_event=self._on_core_event,
        )
        self.settings = DesktopSettings(APP_DATA, BASE_DIR)
        self.clipboard_archive = None
        if getattr(sys, "frozen", False) or "--clipboard-history-local" in sys.argv:
            from clipboard_archive import ClipboardArchive

            self.clipboard_archive = ClipboardArchive(APP_DATA)
        self.api = DesktopApi(self)

    def _on_core_event(self, kind: str, task_id: str) -> None:
        if self._shutting_down:
            return
        if kind in ("file-complete", "legacy-complete"):
            key = (kind, task_id)
            if key not in self._notified_tasks:
                self._notified_tasks.append(key)
                try:
                    if self.tray is not None and self.settings.preferences()["notifications"]:
                        message = ("已接收文件，可在接收目录中查看。" if kind == "file-complete"
                                   else "图片已写入系统剪贴板，可以直接 Ctrl+V。")
                        self.tray.notify(message, APP_NAME)
                except Exception:
                    logging.warning("Windows 通知暂不可用", exc_info=True)
            kind = "files" if kind == "file-complete" else "legacy"
        if self._window_visible:
            self._send_window_event("morypasteReceiverChanged", kind)

    def _send_window_event(self, event_type: str, value: str | bool | int | None) -> None:
        window = self.window
        if window is None or not window.events.loaded.is_set() or window.native is None:
            return
        form = window.native

        def deliver() -> None:
            # Drop stale visibility transitions and receiver events queued before hiding.
            if isinstance(value, bool):
                if value != self._window_visible:
                    return
            elif not self._window_visible:
                return
            try:
                form.webview.CoreWebView2.PostWebMessageAsJson(json.dumps({"type": event_type, "value": value}))
            except Exception:
                logging.debug("桌面窗口未接收事件", exc_info=True)

        try:
            from System import Action

            form.BeginInvoke(Action(deliver))
        except Exception:
            logging.debug("桌面窗口未接收事件", exc_info=True)

    def _set_window_visible(self, visible: bool) -> None:
        if self._window_visible != visible:
            self._window_visible = visible
            self._send_window_event("morypasteWindowVisible", visible)

    def _window_loaded(self, *_args) -> None:
        self._set_native_window_icon()
        self._watch_native_window_activation()
        self.window.events.loaded.wait(timeout=1)
        self._send_window_event("morypasteWindowVisible", self._window_visible)

    def _watch_native_window_activation(self) -> None:
        native = getattr(self.window, "native", None)
        form = getattr(native, "form", native)
        if not self._native_activation_hooked and form is not None and hasattr(form, "Activated"):
            # The existing-instance shortcut restores the HWND directly.
            def activated(_sender, _event) -> None:
                self._set_window_visible(True)

            form.Activated += activated
            self._native_activation_hooked = True

    def _tray_appearance(self) -> tuple[str, str]:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                light = winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] != 0
        except OSError:
            light = True
        return ("light" if light else "dark", "running" if self.core.service_state == "RUNNING" else "idle")

    @staticmethod
    def _tray_image(appearance: tuple[str, str]) -> Image.Image:
        theme, status = appearance
        with Image.open(BASE_DIR / "desktop_resources" / f"tray-{theme}-{status}.png") as image:
            return image.convert("RGBA")

    def _monitor_tray(self, previous: tuple[str, str]) -> None:
        while not self._tray_stop.wait(2):
            appearance = self._tray_appearance()
            if self._tray_stop.is_set() or appearance == previous:
                continue
            try:
                if self.tray is not None:
                    self.tray.icon = self._tray_image(appearance)
                    self.tray.title = f"{APP_NAME} · " + ("接收服务运行中" if appearance[1] == "running" else "接收服务待机")
            except Exception:
                logging.exception("更新托盘图标失败")
            previous = appearance

    def _on_unexpected_core_exit(self, return_code: int, output: str) -> None:
        logging.error("接收核心意外退出，return_code=%s", return_code)
        if self._window_visible:
            self._send_window_event("morypasteReceiverChanged", "status")
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                logging.exception("更新托盘菜单状态失败")
        detail = output[-2500:] if output else "接收程序已停止，请检查本机诊断日志。"
        show_message("MoryPaste 接收服务已停止", f"接收核心意外退出（代码 {return_code}）。\n\n{detail}")

    def _show_window(self, *_args) -> None:
        window = self.window
        if window is not None:
            try:
                window.restore()
                window.show()
                self._set_window_visible(True)
            except Exception:
                logging.exception("恢复主窗口失败")

    def _hide_on_close(self) -> bool:
        if self._shutting_down:
            return True
        self._hide_window()
        return False

    def _hide_window(self) -> None:
        self._set_window_visible(False)
        if self.window is not None:
            self.window.hide()

    def _open_logs(self, *_args) -> None:
        diagnostics = APP_DATA / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(diagnostics))
        except OSError as exc:
            logging.exception("打开诊断日志目录失败")
            show_message("无法打开日志目录", f"{exc}\n\n{diagnostics}")

    def _open_diagnostics(self, *_args) -> None:
        self._show_window()
        if self.window is not None:
            try:
                self.window.evaluate_js("window.navigate && window.navigate('about')")
            except Exception:
                logging.exception("打开诊断页面失败")

    def _exit_from_tray(self, *_args) -> None:
        threading.Thread(target=self._shutdown, name="sendtocopyq-shutdown", daemon=True).start()

    def _restart_from_tray(self, *_args) -> None:
        threading.Thread(
            target=self._shutdown, kwargs={"restart": True}, name="morypaste-restart", daemon=True
        ).start()

    def _shutdown(self, installer: tuple[Path, str] | None = None, *, restart: bool = False) -> bool:
        with self._shutdown_lock:
            if self._shutting_down:
                return False
            self._shutting_down = True

        logging.info("正在正常退出桌面客户端；核心状态：%s", self.core.status)
        self._exit_result = self.core.stop()
        if not self._exit_result:
            with self._shutdown_lock:
                self._shutting_down = False
            show_message(
                "接收进程退出未确认",
                "桌面客户端没有确认自己启动的接收子进程已退出，因此仍保持运行。请检查日志后重试退出。",
            )
            return False
        self._tray_stop.set()
        self.pending_installer = installer
        self.pending_restart = restart
        if self.clipboard_archive and self.clipboard_archive.thread is not None:
            self.clipboard_archive.call("stop")
            self.clipboard_archive.thread.join(timeout=2)
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                logging.exception("停止托盘失败")
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                logging.exception("关闭主窗口失败")
        return True

    def _set_native_window_icon(self, *_args) -> None:
        try:
            native = getattr(self.window, "native", None)
            form = getattr(native, "form", native)
            if form is not None and hasattr(form, "Icon"):
                from System.Drawing import Icon

                self._native_icon = Icon(str(WINDOW_ICON_PATH))
                form.Icon = self._native_icon
        except Exception:
            logging.exception("设置桌面窗口图标失败")

    def run(self) -> int:
        try:
            runtime_version = webview2_runtime_version()
            if runtime_version is None:
                if not install_webview2_if_needed():
                    return 2
                runtime_version = webview2_runtime_version()
                if runtime_version is None:
                    return 2
            logging.info("已检测到 WebView2 Runtime %s", runtime_version)
            try:
                network = self.core.start(
                    lan_control_token=self.lan_control_token, outgoing_token=self.outgoing_token,
                )
            except ExistingReceiverError as exc:
                logging.warning("检测到既有接收端：%s", exc)
                title = "接收端已在运行" if exc.state else "无法确认接收端状态"
                show_message(title, str(exc), MB_ICONWARNING)
                return 2
            except CoreStartupError as exc:
                logging.error("启动接收核心失败：%s", exc)
                show_message("MoryPaste 启动失败", f"{exc}\n\n桌面日志：{LOG_PATH}")
                return 2

            if self.lan_control_token:
                self.api._start_lan_if_authorized(network)

            self.window = webview.create_window(
                APP_NAME,
                url=DASHBOARD_URL,
                width=WINDOW_WIDTH,
                height=WINDOW_HEIGHT,
                min_size=WINDOW_MIN_SIZE,
                resizable=True,
                confirm_close=False,
                background_color="#F8FAFC",
                js_api=self.api,
                hidden=not self._window_visible,
            )
            self.window.events.closing += lambda: self._hide_on_close()
            self.window.events.loaded += self._window_loaded
            self.window.events.minimized += lambda: self._set_window_visible(False)
            self.window.events.restored += lambda: self._set_window_visible(True)

            appearance = self._tray_appearance()
            self.tray = pystray.Icon(
                "SendToCopyQ",
                self._tray_image(appearance),
                f"{APP_NAME} · " + ("接收服务运行中" if appearance[1] == "running" else "接收服务待机"),
                menu=pystray.Menu(
                    pystray.MenuItem("打开 MoryPaste", self._show_window, default=True),
                    pystray.MenuItem(lambda _item: f"接收服务：{self.core.status}", None, enabled=False),
                    pystray.MenuItem("打开诊断", self._open_diagnostics),
                    pystray.MenuItem("重启 MoryPaste", self._restart_from_tray),
                    pystray.MenuItem("退出程序", self._exit_from_tray),
                ),
            )
            self.tray.run_detached()
            threading.Thread(target=self._monitor_tray, args=(appearance,), name="morypaste-tray-status", daemon=True).start()
            if self.clipboard_archive:
                self.clipboard_archive.start()
            logging.info("桌面客户端就绪；接收核心：%s", self.core.status)
            webview.start(gui="edgechromium", debug=False)
            return 0
        except Exception as exc:
            logging.error("桌面客户端启动或运行异常：%s\n%s", exc, traceback.format_exc())
            if isinstance(exc, (ImportError, RuntimeError)) and any(
                term in str(exc).lower() for term in ("webview2", "edge chromium", "browser engine", "edgechromium")
            ):
                help_text = "\n\n请安装或修复 Microsoft Edge WebView2 Evergreen Runtime 后重试。"
            else:
                help_text = ""
            show_message("MoryPaste 桌面窗口无法启动", f"{exc}{help_text}\n\n详细日志：{LOG_PATH}")
            return 2
        finally:
            if not self._shutting_down:
                self._shutdown()


class DesktopApi:
    """Narrow bridge available only in the trusted, loopback desktop window."""
    def __init__(self, client: DesktopClient):
        self._client = client
        self._update_page = None
        self._android_download_page: str | None = None
        self._update_installer: tuple[str, str] | None = None
        self._update_lock = threading.Lock()
        self._lock = threading.Lock()
        self._firewall_cache_at = 0.0
        self._lan_lock = threading.Lock()
        self._firewall_status: dict[str, Any] = {"authorized": None, "reason": "正在读取授权状态。"}

    def _receiver_network_state(self) -> dict[str, Any]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(DASHBOARD_URL + "api/state", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def send_files(self, device_id: str) -> dict:
        paths = self._client.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        if not paths:
            return {"canceled": True}
        try:
            return self._local_receiver_request("outgoing", {"deviceId": device_id, "paths": paths})
        except (ValueError, OSError) as exc:
            return {"error": str(exc) if isinstance(exc, ValueError) else "无法连接本机接收服务。"}

    def _lan_request(self, action: str, value: dict) -> dict:
        return self._local_receiver_request("lan-" + action, value)

    def _local_receiver_request(self, action: str, value: dict) -> dict:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            DASHBOARD_URL + "api/local/" + action, data=json.dumps(value).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-MoryPaste-Lan-Control": self._client.outgoing_token if action == "outgoing" else self._client.lan_control_token,
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                message = json.load(exc).get("error")
            raise ValueError(message or "本机接收端无法完成请求，请重试。") from exc

    def _start_lan_if_authorized(self, network: dict) -> None:
        interface = network.get("interface")
        if not interface:
            return
        try:
            status = firewall_access.get_status(self._client.core.packaged_executable, interface)
            if status.get("authorized") is True:
                self._lan_request("start", {key: network[key] for key in ("ip", "interface", "interfaceIndex")})
        except Exception as exc:
            logging.warning("自动启用局域网接收未完成：%s", exc)

    def choose_receive_directory(self) -> dict:
        paths = self._client.window.create_file_dialog(webview.FileDialog.FOLDER)
        if not paths:
            return {"changed": False}
        directory = Path(paths[0]).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("请选择一个文件夹。")
        with self._lock:
            target = APP_DATA / "receive-directory.txt"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(str(directory), "utf-8")
            os.replace(temporary, target)
        return {"changed": True, "root": str(directory)}

    def open_received_folder(self, batch_id=None, ordinal=None) -> dict:
        if batch_id is None and ordinal is None:
            action = {"action": "openRoot"}
        elif type(batch_id) is int and batch_id > 0 and type(ordinal) is int and ordinal > 0:
            action = {"action": "openFolder", "id": batch_id, "ordinal": ordinal}
        else:
            return {"ok": False, "error": "接收记录无效。"}
        request = urllib.request.Request(
            DASHBOARD_URL + "api/received/action",
            data=json.dumps({**action, "resolveOnly": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=4) as response:
                folder = Path(json.load(response)["folderPath"])
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    error = json.load(exc).get("error")
                except (ValueError, AttributeError):
                    error = None
                return {"ok": False, "error": error or "接收端无法定位该文件夹。"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"ok": False, "error": "无法从接收端取得文件夹位置。"}
        if not folder.is_absolute() or not folder.is_dir():
            return {"ok": False, "error": "文件夹已移动、删除或无法访问。"}
        try:
            _open_explorer_folder(folder)
        except OSError:
            logging.warning("无法打开接收文件夹", exc_info=True)
            return {"ok": False, "error": "Windows 未能打开此文件夹。"}
        return {"ok": True}

    def _get_firewall_status(self) -> dict[str, Any]:
        if not getattr(sys, "frozen", False):
            return {"available": False, "authorized": None,
                    "reason": "开发版沿用当前开发环境的网络授权。"}
        if time.monotonic() - self._firewall_cache_at < 15:
            return self._firewall_status
        try:
            network = self._receiver_network_state()
            receiver_path = self._client.core.packaged_executable
            status = firewall_access.get_status(receiver_path, str(network.get("interface") or ""))
            self._firewall_status = {"available": True, **status}
        except Exception as exc:
            logging.warning("读取防火墙授权状态失败：%s", exc)
            self._firewall_status = {"available": True, "authorized": False,
                                     "reason": "无法确认当前网络的防火墙授权，可重试授权。"}
        self._firewall_cache_at = time.monotonic()
        return self._firewall_status

    def get_state(self) -> dict:
        service_state = self._client.core.service_state
        running = service_state == "RUNNING" and not self._client._shutting_down
        return {"version": APP_VERSION, "serviceState": service_state, "running": running,
                "firewall": self._get_firewall_status() if running or service_state == "WAITING" else None}

    def get_settings(self) -> dict:
        copyq_running = None
        try:
            result = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq copyq.exe", "/FO", "CSV", "/NH"],
                                    capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0:
                copyq_running = b'"copyq.exe"' in result.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            logging.debug("读取 CopyQ 进程状态失败", exc_info=True)
        return {"preferences": self._client.settings.preferences(), "startup": self._client.settings.startup_state(),
                "notificationsSupported": bool(pystray.Icon.HAS_NOTIFICATION), "copyqRunning": copyq_running,
                "firewall": self._get_firewall_status()}

    def get_clipboard_archive(self) -> dict:
        archive = self._client.clipboard_archive
        return {"available": True, **archive.call("state")} if archive else {"available": False}

    def change_clipboard_archive(self, action, value=None) -> dict:
        archive = self._client.clipboard_archive
        if archive is None:
            return {"error": "本地剪贴板历史未启用。"}
        if action == "enable" and type(value) is bool:
            return archive.call("enable", value)
        if action == "shortcut" and isinstance(value, list) and len(value) == 2:
            return archive.call("shortcut", *value)
        if action == "storage" and isinstance(value, list) and len(value) in (2, 3):
            return archive.call("storage", *value)
        if action == "storage_folder":
            os.startfile(archive.path.parent)
            return {}
        if action in ("clear", "view"):
            return archive.call(action)
        if action == "settings":
            os.startfile("ms-settings:clipboard")
            return {}
        return {"error": "无法识别此剪贴板历史操作。"}

    def get_legacy_receipts(self) -> list[dict]:
        return [item for item in recent_receipts(APP_DATA / "diagnostics") if item.get("receiveMode") != "batch"]

    def authorize_firewall(self) -> dict[str, Any]:
        if not self._client.lan_control_token:
            ready = self._client.core.service_ready()
            return {"serviceReady": ready, "message": "接收服务已就绪。" if ready else "接收服务尚未就绪。"}
        with self._lan_lock:
            try:
                network = self._lan_request("network", {})
                interface = network.get("interface")
                if not interface or not network.get("interfaceIndex"):
                    raise ValueError("找不到可用的局域网网卡，请检查电脑的网络连接。")
                receiver_path = self._client.core.packaged_executable
                status = firewall_access.authorize(
                    receiver_path, interface,
                    on_progress=lambda phase: self._client._send_window_event("morypastePairProgress", phase),
                )
                if status.get("authorized") is not True:
                    raise ValueError("当前网络的防火墙授权尚未通过核验。")
                self._client._send_window_event("morypastePairProgress", "start")
                self._lan_request("start", network)
                ready = self._client.core.service_ready()
                return {**status, "serviceReady": ready,
                        "message": "局域网接收已启用。" if ready else "已授权，接收服务尚未就绪，请重试。"}
            except firewall_access.FirewallAuthorizationError as exc:
                message = ("已取消 Windows 授权。需要时可再次启用局域网接收。"
                           if exc.status == "UAC_CANCELLED" else str(exc))
                return {"serviceReady": False, "message": message}
            except (OSError, ValueError) as exc:
                logging.warning("启用局域网接收未完成：%s", exc)
                return {"serviceReady": False, "message": str(exc) or "接收服务暂不可用，请重试。"}
            finally:
                self._firewall_cache_at = 0

    def set_notifications(self, enabled: bool) -> dict:
        return self._client.settings.set_notifications(enabled)

    def set_startup(self, enabled: bool) -> dict:
        return self._client.settings.set_startup(enabled)

    def hide_window(self) -> None:
        self._client._hide_window()

    def open_startup_settings(self) -> None:
        os.startfile("ms-settings:startupapps")

    def check_for_updates(self) -> dict:
        self._update_page = None
        self._update_installer = None
        config = BASE_DIR / "desktop_resources" / "update-channel.json"
        try:
            feed = json.loads(config.read_text("utf-8")).get("manifest_url")
            result = check_update(APP_VERSION, feed)
        except (OSError, TimeoutError):
            return {"status": "error", "message": "暂时无法连接更新服务，请稍后重试。"}
        except (AttributeError, ValueError) as exc:
            logging.warning("更新信息读取失败：%s", exc)
            return {"status": "error", "message": "更新信息有误，请稍后重试。"}
        self._update_page = result.pop("page_url", None)
        digest = result.pop("installer_sha256", None)
        can_install = bool(getattr(sys, "frozen", False) and digest)
        if can_install:
            self._update_installer = (result["version"], digest)
        result["canInstall"] = can_install
        return result

    def install_update(self) -> dict:
        if not self._update_lock.acquire(blocking=False):
            return {"started": False, "message": "正在下载更新，请稍候。"}
        try:
            target = self._update_installer
            if not getattr(sys, "frozen", False) or not target:
                return {"started": False, "message": "请先检查可直接安装的更新。"}
            version, digest = target
            path = download_installer(version, digest, lambda percent: self._client._send_window_event("morypasteUpdateProgress", percent))
            logging.info("已下载并校验 Windows 更新 %s", version)
            if not self._client._shutdown((path, digest)):
                return {"started": False, "message": "未能正常退出接收服务，请稍后重试。"}
            return {"started": True}
        except ValueError as exc:
            logging.warning("Windows 安装包校验失败", exc_info=True)
            return {"started": False, "message": str(exc)}
        except OSError:
            logging.warning("Windows 安装包下载失败", exc_info=True)
            return {"started": False, "message": "下载安装包失败，请重试或前往 GitHub 下载。"}
        finally:
            self._update_lock.release()

    def open_update_page(self) -> None:
        if not self._update_page:
            raise ValueError("请先检查更新。")
        os.startfile(self._update_page)

    def get_android_download_guide(self) -> dict:
        try:
            page = android_release_page() or ANDROID_FALLBACK_PAGE
        except (OSError, ValueError) as exc:
            logging.info("Android 更新渠道暂不可用，使用已发布的下载页：%s", type(exc).__name__)
            page = ANDROID_FALLBACK_PAGE
        qr = segno.make_qr(page, error="M").png_data_uri(scale=5, border=4)
        self._android_download_page = page
        return {"qr": qr}

    def open_android_download_page(self) -> dict:
        if self._android_download_page is None:
            self.get_android_download_guide()
        try:
            os.startfile(self._android_download_page)
        except OSError:
            return {"opened": False, "message": "暂时无法打开 Android 下载页，请稍后重试。"}
        return {"opened": True}

    def open_feedback_page(self) -> None:
        os.startfile("https://github.com/xiaodeng2025/MoryPaste/issues")

    def open_logs(self) -> None:
        self._client._open_logs()

    def export_logs(self) -> dict:
        paths = self._client.window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=f"MoryPaste-logs-{datetime.now():%Y%m%d-%H%M%S}.zip",
            file_types=("日志文件 (*.zip)",),
        )
        if not paths:
            return {"cancelled": True}
        destination = Path(paths if isinstance(paths, str) else paths[0])
        buffer = io.BytesIO()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("version.txt", f"MoryPaste {APP_VERSION} (Windows)\n")
            for name in ("desktop-client.log", "desktop-client.log.1"):
                source = APP_DATA / name
                try:
                    with source.open("rb") as stream:
                        bundle.writestr(name, stream.read(1024 * 1024))
                except FileNotFoundError:
                    pass
            diagnostics = APP_DATA / "diagnostics"
            for source in sorted(diagnostics.glob("windows-*.jsonl"), reverse=True)[:16]:
                try:
                    if source.stat().st_mtime >= cutoff.timestamp():
                        with source.open("rb") as stream:
                            bundle.writestr(f"diagnostics/{source.name}", stream.read(1024 * 1024))
                except FileNotFoundError:
                    pass
        destination.write_bytes(buffer.getvalue())
        return {"saved": True}

    def export_diagnostics(self, task_id: str) -> dict:
        task = str(uuid.UUID(task_id))
        paths = self._client.window.create_file_dialog(webview.FileDialog.SAVE,
                    save_filename=f"SendToCopyQ-{task}.zip", file_types=("诊断包 (*.zip)",))
        if not paths:
            return {"cancelled": True}
        destination = Path(paths if isinstance(paths, str) else paths[0])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{DASHBOARD_URL}api/diagnostics/export?task_id={task}", timeout=15) as response:
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("诊断包超出大小限制")
        destination.write_bytes(data)
        return {"saved": True}


def main() -> int:
    if "--check-clipboard-runtime" in sys.argv:
        from tempfile import TemporaryDirectory
        from clipboard_archive import ClipboardArchive
        from receiver import _NativeClipboardApi

        with TemporaryDirectory(prefix="morypaste-runtime-") as temp:
            archive = ClipboardArchive(Path(temp))
            archive.start()
            state = archive.call("state")
            archive.call("stop")
            archive.thread.join(timeout=5)
            if archive.thread.is_alive() or state.get("error") or state.get("enabled") is not False:
                return 2
            _NativeClipboardApi(None, 1, 1)
        return 0
    try:
        configure_logging()
        instance = SingleInstance()
    except Exception as exc:
        show_message("MoryPaste 无法启动", str(exc))
        return 2

    try:
        if instance.already_running:
            if (BASE_DIR / 'test-data-profile.json').exists():
                show_message('测试客户端未启动', '已有客户端运行。测试版不会恢复或接管现有实例；请自行退出后再启动测试版。')
                return 2
            if "--start-hidden" in sys.argv:
                return 0
            if not activate_existing_window():
                show_message(
                    "MoryPaste 正在后台运行",
                    "已找到正在运行的客户端，但暂时没有找到主窗口。请稍等片刻，或检查系统托盘中的 MoryPaste 图标。",
                    MB_ICONINFORMATION,
                )
            return 0
        client = DesktopClient()
        result = client.run()
    finally:
        instance.close()
    if result == 0 and client.pending_restart:
        command = [sys.executable]
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        else:
            command.append(str(Path(__file__).resolve()))
        command.extend(arg for arg in sys.argv[1:] if arg != "--start-hidden")
        try:
            subprocess.Popen(command, env=env, cwd=INSTALL_DIR)
        except OSError as exc:
            logging.exception("无法重新启动桌面客户端")
            show_message("无法重启 MoryPaste", f"{exc}\n\n请从原来的入口重新打开 MoryPaste。")
            return 2
    if result == 0 and client.pending_installer is not None:
        path, digest = client.pending_installer
        try:
            verify_installer(path, digest)
            os.startfile(path)
        except (OSError, ValueError) as exc:
            logging.exception("无法启动更新安装器")
            show_message("无法启动更新安装器", f"{exc}\n\n你可以重新打开 MoryPaste 并重试。")
            return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())

