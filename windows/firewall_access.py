"""Narrow, explicit Windows Firewall authorization for the installed receiver."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import tempfile
import logging
from pathlib import Path
from typing import Any


class FirewallAuthorizationError(RuntimeError):
    def __init__(self, message, status='HELPER_FAILED', exit_code=None):
        super().__init__(message)
        self.status = status
        self.exit_code = exit_code


def decode_output(data: bytes) -> str:
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('mbcs' if os.name == 'nt' else 'utf-8', errors='replace')



def helper_path(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir / "firewall_rule.ps1"
    resource_dir = getattr(sys, "_MEIPASS", None)
    if resource_dir:
        return Path(resource_dir) / "firewall_rule.ps1"
    return Path(__file__).resolve().parent / "firewall_rule.ps1"


def _powershell() -> str:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidate = windir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def get_status(receiver_path: Path, interface_alias: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    script = helper_path(base_dir)
    if not script.is_file() or not interface_alias:
        return {"authorized": False, "interface": interface_alias, "profile": None,
                "reason": "尚未确认接收网卡或授权助手。"}
    result = subprocess.run(
        [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(script), "-Action", "Status",
         "-ReceiverPath", str(receiver_path), "-InterfaceAlias", interface_alias],
        capture_output=True, timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        detail = decode_output(result.stderr).strip()
        return {"authorized": False, "interface": interface_alias, "profile": None,
                "reason": detail or "无法读取 Windows 防火墙授权状态。"}
    try:
        payload = json.loads(decode_output(result.stdout).strip().splitlines()[-1])
        return payload if isinstance(payload, dict) else {"authorized": False, "reason": "防火墙状态返回无效。"}
    except (IndexError, json.JSONDecodeError):
        return {"authorized": False, "interface": interface_alias, "profile": None,
                "reason": "无法读取 Windows 防火墙授权状态。"}


def _shell_execute_elevated(parameters: str, *, timeout: int = 180) -> int:
    if os.name != "nt":
        raise FirewallAuthorizationError("Windows 防火墙授权仅支持 Windows。")

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD), ("fMask", ctypes.wintypes.ULONG),
            ("hwnd", ctypes.wintypes.HWND), ("lpVerb", ctypes.wintypes.LPCWSTR),
            ("lpFile", ctypes.wintypes.LPCWSTR), ("lpParameters", ctypes.wintypes.LPCWSTR),
            ("lpDirectory", ctypes.wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.wintypes.LPCWSTR), ("hkeyClass", ctypes.wintypes.HKEY),
            ("dwHotKey", ctypes.wintypes.DWORD), ("hIcon", ctypes.wintypes.HANDLE),
            ("hProcess", ctypes.wintypes.HANDLE),
        ]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = _powershell()
    info.lpParameters = parameters
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = ctypes.wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise FirewallAuthorizationError("你取消了 Windows 安全授权。", "UAC_CANCELLED", 1223)
        raise FirewallAuthorizationError(f"无法启动防火墙授权：Windows 错误 {error}。")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
        wait_result = kernel32.WaitForSingleObject(info.hProcess, timeout * 1000)
        if wait_result != 0:
            raise FirewallAuthorizationError("等待 Windows 防火墙授权超时，请稍后重试。")
        exit_code = ctypes.wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise FirewallAuthorizationError("无法确认防火墙授权结果。")
        return int(exit_code.value)
    finally:
        if info.hProcess:
            ctypes.windll.kernel32.CloseHandle(info.hProcess)


def read_helper_result(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise FirewallAuthorizationError('授权结果文件未生成。', 'RESULT_MISSING') from exc
    except OSError as exc:
        raise FirewallAuthorizationError('授权结果文件读取失败。', 'RESULT_READ_FAILED') from exc
    if not raw.strip():
        raise FirewallAuthorizationError('授权结果文件为空。', 'RESULT_EMPTY')
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as exc:
        raise FirewallAuthorizationError('授权结果文件编码或 JSON 格式错误。', 'RESULT_JSON_INVALID') from exc
    if (not isinstance(value, dict) or any(type(value.get(key)) is not bool for key in ('success','ruleCreated','ruleVerified'))
            or not isinstance(value.get('status'), str) or not isinstance(value.get('message'), str)):
        raise FirewallAuthorizationError('授权结果字段或类型无效。', 'RESULT_SCHEMA_INVALID')
    return value


def authorize(receiver_path: Path, interface_alias: str, *, base_dir: Path | None = None,
              on_progress=None) -> dict[str, Any]:
    script = helper_path(base_dir)
    if not script.is_file():
        raise FirewallAuthorizationError(f"找不到防火墙授权组件：{script}")
    args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
            "-File", str(script), "-Action", "Authorize",
            "-ReceiverPath", str(receiver_path.resolve()), "-InterfaceAlias", interface_alias]
    existing = get_status(receiver_path, interface_alias, base_dir=base_dir)
    if existing.get('authorized'):
        return {**existing, 'success': True, 'status': 'ALREADY_AUTHORIZED', 'ruleCreated': False, 'ruleVerified': True}
    code = None
    result = None
    failure = None
    try:
        with tempfile.TemporaryDirectory(prefix='stcq-firewall-') as temp:
            result_path = Path(temp) / 'result.json'
            args += ['-ResultPath', str(result_path)]
            if on_progress:
                on_progress("windows")
            code = _shell_execute_elevated(subprocess.list2cmdline(args))
            result = read_helper_result(result_path)
            if code != 0 or not result['success'] or not result['ruleVerified']:
                raise FirewallAuthorizationError(result.get('message') or '授权助手异常退出。',
                    result.get('errorCode') or 'HELPER_EXIT_FAILED', code)
    except FirewallAuthorizationError as exc:
        failure = exc
        logging.warning('防火墙结果回传异常 status=%s exit_code=%s exception=%s cause=%s',
                        exc.status, code if code is not None else exc.exit_code,
                        type(exc).__name__, type(exc.__cause__).__name__ if exc.__cause__ else None)
    # A valid product rule is authoritative even if the helper result was lost.
    if on_progress:
        on_progress("verify")
    status = get_status(receiver_path, interface_alias, base_dir=base_dir)
    if status.get('authorized') is True:
        if failure:
            return {**status, 'success': True, 'status': 'AUTHORIZED_RESULT_RECOVERED',
                    'ruleCreated': False, 'ruleVerified': True, 'helperExitCode': code,
                    'resultWarning': failure.status, 'message': '局域网接收授权已确认。'}
        return {**status, **result}
    if failure:
        raise FirewallAuthorizationError(str(failure) + ' 当前程序的窄范围规则尚未核验通过。',
                                         failure.status, code if code is not None else failure.exit_code) from failure
    raise FirewallAuthorizationError(str(status.get('reason') or '规则回读验证失败。'), 'RULE_VERIFY_FAILED', code)



def remove_rules(receiver_path: Path, *, base_dir: Path | None = None) -> int:
    script = helper_path(base_dir)
    if not script.is_file():
        raise FirewallAuthorizationError(f"找不到防火墙授权组件：{script}")
    args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
            "-File", str(script), "-Action", "Remove", "-ReceiverPath", str(receiver_path.resolve())]
    return _shell_execute_elevated(subprocess.list2cmdline(args))
