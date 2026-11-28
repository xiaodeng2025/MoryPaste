from __future__ import annotations

import json
import ctypes
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from collections import deque
from pathlib import Path
from typing import Any, Callable
from desktop_events import parse as parse_desktop_event


DASHBOARD_URL = "http://127.0.0.1:48120"
from data_paths import resolve_data_dir, child_environment
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ReceiverError(RuntimeError):
    pass


class ExistingReceiverError(ReceiverError):
    def __init__(self, state: dict[str, Any] | None = None, detail: str = ""):
        self.state = state or {}
        self.detail = detail
        if self.state:
            name = self.state.get("computerName") or "已配对接收端"
            ip = self.state.get("ip") or "本机"
            port = self.state.get("port") or 47831
            message = f"接收端已运行（{name}，{ip}:{port}）。桌面客户端不会启动第二份接收服务。"
        else:
            message = (
                "无法确认接收端状态：本机管理端口 127.0.0.1:48120 仍有监听，但管理接口没有及时响应。\n\n"
                "为避免重复启动服务，客户端暂未打开。请等几秒后再双击桌面快捷方式；"
                "若提示持续出现，请检查是否仍有旧接收端窗口或托盘实例。"
            )
        if detail:
            message = f"{message}\n\n详情：{detail}"
        super().__init__(message)


class CoreStartupError(ReceiverError):
    pass


class ReceiverProcessManager:
    """Starts, watches, and stops only the receiver process created here."""

    def __init__(
        self,
        receiver_path: Path,
        on_unexpected_exit: Callable[[int, str], None] | None = None,
        *,
        packaged_executable: Path | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.receiver_path = receiver_path.resolve()
        self.on_unexpected_exit = on_unexpected_exit
        self.on_event = on_event
        self.packaged_executable = packaged_executable.resolve() if packaged_executable is not None else None
        self.process: subprocess.Popen[str] | None = None
        self._job_handle: int | None = None
        self._kernel32 = None
        self.external_state: dict[str, Any] | None = None
        self._stop_requested = threading.Event()
        self._watcher: threading.Thread | None = None
        self._output_reader: threading.Thread | None = None
        self._output: deque[str] = deque(maxlen=60)
        self._output_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._status = "未启动"

    @property
    def status(self) -> str:
        if self.external_state is not None:
            return "已有接收端（外部管理）"
        if self.process is None:
            return self._status
        if self.process.poll() is None:
            if self._stop_requested.is_set():
                return '正在退出'
            if self._status == '正在启动':
                return '正在启动'
            return {'RUNNING': '运行中', 'WAITING': '等待局域网授权'}.get(
                self.service_state, '接收异常（进程存在，端口未就绪）')
        if self._status.startswith("退出未确认"):
            return self._status
        return f"已退出（代码 {self.process.returncode}）"

    def service_ready(self, state=None) -> bool:
        try:
            state = state or self._get_state(timeout=0.4)
            if not state or state.get('lanState') == 'waiting' or not state.get('ip') or not state.get('port'):
                return False
            with socket.create_connection((state['ip'], int(state['port'])), timeout=0.4):
                return True
        except (OSError, ValueError, ReceiverError):
            return False

    @property
    def service_state(self) -> str:
        if self._stop_requested.is_set() and self.process and self.process.poll() is None:
            return 'STOPPING'
        if self.process is None:
            return 'STOPPED'
        if self.process.poll() is not None:
            return 'STOPPED' if self._stop_requested.is_set() else 'FAILED'
        if self._status == '正在启动':
            return 'STARTING'
        if self._status == "本机界面已就绪":
            try:
                state = self._get_state(timeout=0.4)
            except ReceiverError:
                return 'DEGRADED'
            if state and state.get('lanState') == 'waiting':
                return 'WAITING'
            return 'RUNNING' if state and self.service_ready(state) else 'DEGRADED'
        return 'RUNNING' if self.service_ready() else 'DEGRADED'

    def _get_state(self, timeout: float = 0.5) -> dict[str, Any] | None:
        request = urllib.request.Request(f"{DASHBOARD_URL}/api/state", headers={"Cache-Control": "no-cache"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ExistingReceiverError(detail=f"本机管理端口已被占用（HTTP {exc.code}）。") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ConnectionRefusedError,)) or getattr(reason, "winerror", None) == 10061:
                return None
            if isinstance(reason, (TimeoutError,)) and not self._management_port_listening():
                return None
            raise ExistingReceiverError(detail=f"本机管理端口已有响应但无法安全识别：{reason}。") from exc
        except (TimeoutError, OSError) as exc:
            if getattr(exc, "winerror", None) == 10061:
                return None
            if isinstance(exc, TimeoutError) and not self._management_port_listening():
                return None
            raise ExistingReceiverError(detail=f"本机管理端口无法确认是否空闲：{exc}。") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExistingReceiverError(detail="本机管理端口返回了无法识别的数据。") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("computerId"), str):
            raise ExistingReceiverError(detail="本机管理端口被其他程序占用。")
        return payload

    @staticmethod
    def _management_port_listening() -> bool:
        """Check for an actual loopback TCP listener, excluding TIME_WAIT rows."""
        probe = None
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.35)
            return probe.connect_ex(("127.0.0.1", 48120)) == 0
        except OSError:
            return False
        finally:
            if probe is not None:
                probe.close()

    def _build_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            if self.packaged_executable is None or not self.packaged_executable.is_file():
                raise CoreStartupError(f"找不到已打包的接收核心：{self.packaged_executable or '(未配置)'}")
            return [str(self.packaged_executable)]
        if not self.receiver_path.is_file():
            raise CoreStartupError(f"找不到接收程序：{self.receiver_path}")
        return [str(self._core_interpreter()), "-u", str(self.receiver_path)]

    @classmethod
    def _wait_for_management_port_release(cls, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not cls._management_port_listening():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _probe_existing_receiver(self, timeout: float = 0.5, shutdown_grace: float = 10.0) -> dict[str, Any] | None:
        """Allow a just-closed receiver a short window to release its loopback listener."""
        deadline = time.monotonic() + max(0.0, shutdown_grace)
        last_timeout: ExistingReceiverError | None = None
        while True:
            try:
                return self._get_state(timeout=timeout)
            except ExistingReceiverError as exc:
                if "timed out" not in str(exc).lower():
                    raise
                last_timeout = exc
                if time.monotonic() >= deadline:
                    raise last_timeout
                # The listener can disappear between _get_state's timeout check and
                # this retry. Probe again instead of surfacing a stale timeout.
                time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))

    def start(self, timeout: float = 25.0, *, lan_control_token: str | None = None,
              outgoing_token: str | None = None) -> dict[str, Any]:
        if self.process is not None:
            raise ReceiverError("接收核心已经由当前桌面客户端启动。")
        command = self._build_command()

        existing = self._probe_existing_receiver(timeout=0.5, shutdown_grace=10.0)
        if existing is not None:
            self.external_state = existing
            self._status = "已有接收端（外部管理）"
            raise ExistingReceiverError(existing)

        env = child_environment(resolve_data_dir())
        env.pop("MORYPASTE_LAN_CONTROL", None)
        env.pop("MORYPASTE_OUTGOING_CONTROL", None)
        if outgoing_token is not None:
            env["MORYPASTE_OUTGOING_CONTROL"] = outgoing_token
        if lan_control_token is not None:
            env["MORYPASTE_LAN_CONTROL"] = lan_control_token
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.receiver_path.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._status = "启动失败"
            raise CoreStartupError(f"无法启动接收核心：{exc}") from exc

        try:
            self._attach_owned_process_job()
        except OSError as exc:
            self._close_owned_process_job()
            self._terminate_owned_process_tree()
            self._status = "启动失败"
            raise CoreStartupError(f"无法保护接收进程生命周期：{exc}") from exc

        self._status = "正在启动"
        self._output_reader = threading.Thread(target=self._read_output, name="sendtocopyq-core-output", daemon=True)
        self._output_reader.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.output_tail()
                self._status = f"启动失败（代码 {self.process.returncode}）"
                raise CoreStartupError(self._format_start_error(output))
            try:
                state = self._get_state(timeout=0.4)
            except ExistingReceiverError as exc:
                # A slow local handler during our own startup is not proof of a
                # second receiver. Keep waiting while our child is still alive;
                # on a real port conflict, receiver.py exits when bind() fails.
                if "timed out" in str(exc).lower() and self.process.poll() is None:
                    time.sleep(0.2)
                    continue
                self._terminate_owned_process_tree()
                self._close_owned_process_job()
                raise
            managed_ready = state is not None and state.get("lanState") == "waiting"
            if state is not None and (managed_ready if lan_control_token else self.service_ready(state)):
                self._status = "本机界面已就绪" if managed_ready else "运行中"
                self._watcher = threading.Thread(target=self._watch, name="sendtocopyq-core-watch", daemon=True)
                self._watcher.start()
                return state
            time.sleep(0.2)

        output = self.output_tail()
        self._status = "启动超时"
        # Startup never established ownership through a valid API response, so
        # do not send /api/quit to whichever process may now own that port.
        self._terminate_owned_process_tree()
        self._close_owned_process_job()
        raise CoreStartupError(self._format_start_error(output or "等待本机管理接口就绪超时。"))

    @staticmethod
    def _core_interpreter() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable)
        if sys.prefix != sys.base_prefix:
            # Run with the project's venv and its pinned dependencies. CREATE_NO_WINDOW
            # keeps the console interpreter invisible during normal operation.
            venv_python = Path(sys.prefix) / "Scripts" / "python.exe"
            if venv_python.is_file():
                return venv_python
        return Path(sys.executable)

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                cleaned = line.rstrip()
                if cleaned:
                    event = parse_desktop_event(cleaned)
                    if event:
                        if self.on_event is not None:
                            try:
                                self.on_event(*event)
                            except Exception:
                                logging.warning("接收事件处理失败", exc_info=True)
                        continue
                    with self._output_lock:
                        self._output.append(cleaned)
        except (OSError, ValueError):
            return

    def _attach_owned_process_job(self) -> None:
        """Tie the receiver process tree to this desktop client's lifetime."""
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._job_handle = handle

        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.process is None or not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(self.process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def _close_owned_process_job(self) -> None:
        handle = self._job_handle
        self._job_handle = None
        if handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(handle)

    def _terminate_owned_process_tree(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def output_tail(self) -> str:
        with self._output_lock:
            return "\n".join(self._output)

    @staticmethod
    def _format_start_error(output: str) -> str:
        tail = output[-5000:]
        return "接收核心未能启动。" + (f"\n\n启动输出：\n{tail}" if tail else "")

    def _watch(self) -> None:
        process = self.process
        if process is None:
            return
        while not self._stop_requested.wait(0.5):
            return_code = process.poll()
            if return_code is not None:
                self._close_owned_process_job()
                self._status = f"意外退出（代码 {return_code}）"
                if self.on_unexpected_exit is not None:
                    self.on_unexpected_exit(return_code, self.output_tail())
                return

    @staticmethod
    def _post_quit(timeout: float = 1.0) -> None:
        request = urllib.request.Request(
            f"{DASHBOARD_URL}/api/quit",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            response.read()

    def stop(self, graceful_timeout: float = 10.0) -> bool:
        with self._stop_lock:
            process = self.process
            if process is None or self.external_state is not None:
                self._stop_requested.set()
                return True
            self._stop_requested.set()
            if process.poll() is not None:
                self._close_owned_process_job()
                if self._wait_for_management_port_release():
                    self._status = f"已退出（代码 {process.returncode}）"
                    return True
                self._status = "退出未确认（本机管理端口仍在监听）"
                return False

            try:
                self._post_quit()
            except (OSError, urllib.error.URLError, TimeoutError):
                pass

            try:
                process.wait(timeout=graceful_timeout)
                self._close_owned_process_job()
                if not self._wait_for_management_port_release():
                    self._status = "退出未确认（本机管理端口仍在监听）"
                    return False
                self._status = "已正常退出"
                return True
            except subprocess.TimeoutExpired:
                pass

            # The PID is the exact Popen child created by this manager. /T is used
            # only here, after graceful shutdown timed out, to include its launcher
            # descendant without touching unrelated Python processes.
            try:
                cleanup = subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if cleanup.returncode != 0 and process.poll() is None:
                    self._status = "退出未确认"
                    self._stop_requested.clear()
                    self._watcher = threading.Thread(target=self._watch, name="sendtocopyq-core-watch", daemon=True)
                    self._watcher.start()
                    return False
                process.wait(timeout=5)
                self._close_owned_process_job()
                if not self._wait_for_management_port_release():
                    self._status = "退出未确认（本机管理端口仍在监听）"
                    return False
                self._status = "已退出（正常关闭超时后清理自有进程树）"
                return True
            except (OSError, subprocess.TimeoutExpired):
                self._status = "退出未确认"
                if process.poll() is None:
                    self._stop_requested.clear()
                    self._watcher = threading.Thread(target=self._watch, name="sendtocopyq-core-watch", daemon=True)
                    self._watcher.start()
                return False
