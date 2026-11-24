from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import winreg
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from ctypes import wintypes
from contextlib import closing

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import segno
from mdns_dnsapi import Registration as MdnsRegistration
from desktop_events import emit as desktop_event
from received_files import MAX_FILE_BYTES, ReceiveStore, category_for, default_receive_root, file_drop_payload, safe_name


APP_NAME = "SendToCopyQ"
TLS_PORT = 47831
DASHBOARD_PORT = 48120
MAX_IMAGE_BYTES = 50 * 1024 * 1024
PAIRING_TTL_SECONDS = 300
from data_paths import resolve_data_dir
try:
    APP_DATA = resolve_data_dir()
except Exception as exc:
    ctypes.windll.user32.MessageBoxW(None, str(exc), 'SendToCopyQ 数据隔离检查失败', 0x10)
    raise SystemExit(2)

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DEFAULT_COPYQ_EXE = os.environ.get("COPYQ_EXE", "copyq.exe")
COPYQ_EXE = Path(DEFAULT_COPYQ_EXE)
APP_VERSION = "0.6.11"
PROTOCOL_VERSION = "1"
DIAGNOSTIC_DIR = APP_DATA / "diagnostics"
DIAGNOSTIC_MAX_FILE = 1024 * 1024
DIAGNOSTIC_MAX_TOTAL = 10 * 1024 * 1024
DIAGNOSTIC_RETENTION_DAYS = 7
_diagnostic_lock = threading.RLock()


def _safe_exception(exc: BaseException) -> dict[str, Any]:
    message = str(exc)
    message = __import__("re").sub(r"(?i)(content|file)://\S+", "<uri>", message)
    message = __import__("re").sub(r"(?i)(token|secret|signature|pairingCode)[=: ]+[^\s,;]+", r"\1=<redacted>", message)
    frames = []
    for frame in __import__("traceback").extract_tb(exc.__traceback__)[-10:]:
        frames.append({"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno})
    return {"exception_type": type(exc).__name__, "exception_message": message[:600], "stack": frames}


def log_event(task_id: str | None, event: str, level: str = "INFO", status: str = "ok", **fields: Any) -> None:
    """Append privacy-filtered JSONL locally. Diagnostics can never fail a transfer."""
    try:
        DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
        forbidden = ("secret", "token", "signature", "credential", "uri", "payload", "private_key")
        clean = {key: value for key, value in fields.items()
                 if key == "uri_scheme" or not any(term in key.lower() for term in forbidden)}
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "task_id": task_id,
            "event": event,
            "level": level,
            "status": status,
            "app_version": APP_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            **clean,
        }
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        with _diagnostic_lock:
            path = DIAGNOSTIC_DIR / f"windows-{day}.jsonl"
            suffix = 1
            while path.exists() and path.stat().st_size + len(line) > DIAGNOSTIC_MAX_FILE:
                path = DIAGNOSTIC_DIR / f"windows-{day}-{suffix}.jsonl"
                suffix += 1
            with path.open("ab") as stream:
                stream.write(line)
            prune_diagnostics()
    except Exception:
        pass


def log_exception(task_id: str | None, event: str, exc: BaseException, **fields: Any) -> None:
    log_event(task_id, event, level="ERROR", status="failed", **fields, **_safe_exception(exc))


def prune_diagnostics() -> None:
    now = time.time()
    files = sorted(DIAGNOSTIC_DIR.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
    for path in files:
        if now - path.stat().st_mtime > DIAGNOSTIC_RETENTION_DAYS * 86400:
            path.unlink(missing_ok=True)
    files = sorted(DIAGNOSTIC_DIR.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= DIAGNOSTIC_MAX_TOTAL:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


def diagnostic_records(task_id: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"android": [], "windows": []}
    if not DIAGNOSTIC_DIR.is_dir():
        return result
    prune_diagnostics()
    for path in DIAGNOSTIC_DIR.glob("*.jsonl"):
        source = "android" if path.name.startswith("android-import-") else "windows"
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("task_id") == task_id:
                        result[source].append(item)
        except OSError:
            continue
    for values in result.values():
        values.sort(key=lambda item: str(item.get("timestamp", "")))
    return result


ANDROID_LOG_ALLOWLIST = {
    "timestamp", "task_id", "event", "level", "status", "app_version", "protocol_version", "device_model",
    "action", "intent_mime", "read_grant_flag", "clip_item_count", "image_count", "image_index", "uri_scheme",
    "read_granted", "mime_hint", "mime", "bytes", "bytes_sent", "bytes_total", "elapsed_ms", "http_status",
    "ack_ok", "error_code", "exception_message", "stack", "attempt", "cached_for_retry",
}


def import_android_log_bundle(bundle: bytes) -> int:
    if len(bundle) > 5 * 1024 * 1024:
        raise ValueError("Android 诊断包过大。")
    accepted: list[bytes] = []
    total = 0
    try:
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            for info in archive.infolist():
                if not info.filename.startswith("android/") or not info.filename.endswith(".jsonl"):
                    continue
                if info.file_size > 5 * 1024 * 1024:
                    raise ValueError("Android 日志文件过大。")
                raw = archive.read(info)
                total += len(raw)
                if total > 5 * 1024 * 1024:
                    raise ValueError("Android 日志总量过大。")
                for line in raw.decode("utf-8", "replace").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    clean = {key: value for key, value in row.items() if key in ANDROID_LOG_ALLOWLIST}
                    task = clean.get("task_id")
                    if task is not None:
                        clean["task_id"] = str(uuid.UUID(str(task)))
                    accepted.append((json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError("无法读取 Android 诊断包。") from exc
    if not accepted:
        raise ValueError("诊断包中没有有效的 Android 事件日志。")
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    target = DIAGNOSTIC_DIR / f"android-import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.jsonl"
    with target.open("wb") as stream:
        stream.writelines(accepted)
    prune_diagnostics()
    return len(accepted)


def diagnostic_bundle(task_id: str) -> bytes:
    events = diagnostic_records(task_id)
    manifest = {
        "application": APP_NAME,
        "windows_receiver_version": APP_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "android_events_included": len(events["android"]) > 0,
        "android_event_count": len(events["android"]),
        "windows_event_count": len(events["windows"]),
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("android.jsonl", "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in events["android"]))
        archive.writestr("windows.jsonl", "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in events["windows"]))
    return output.getvalue()


def load_settings() -> dict[str, str]:
    global COPYQ_EXE
    path = APP_DATA / "settings.json"
    saved_path: Path | None = None
    try:
        value = json.loads(path.read_text("utf-8"))
        candidate = value.get("copyqPath") if isinstance(value, dict) else None
        if isinstance(candidate, str) and candidate.strip():
            saved_path = Path(candidate.strip()).expanduser()
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f"接收端设置文件无法读取：{path}")

    candidates = [saved_path] if saved_path is not None and saved_path.is_file() else []
    candidates.extend(copyq_candidates())
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            check_copyq(candidate)
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        COPYQ_EXE = candidate.resolve()
        return {"copyqPath": str(COPYQ_EXE)}
    # Keep a broken saved value visible in Settings so the user can correct it;
    # with no saved value, use a harmless sentinel and retain native clipboard.
    COPYQ_EXE = saved_path if saved_path is not None else Path("copyq.exe")
    return {"copyqPath": str(COPYQ_EXE)}


def copyq_candidates() -> list[Path]:
    """Check bounded Windows install locations; never scan whole drives."""
    candidates: list[Path] = []

    def add(value: str | Path | None) -> None:
        if not value:
            return
        path = Path(str(value).strip().strip('"'))
        if path.name.lower() != "copyq.exe":
            path = path / "copyq.exe"
        if path not in candidates:
            candidates.append(path)

    # App Paths is the most direct registered executable location.
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\App Paths\copyq.exe", 0,
                                    winreg.KEY_READ | view) as key:
                    add(winreg.QueryValueEx(key, "")[0])
            except OSError:
                pass

    # Installed-app metadata covers standard and user-selected installation paths.
    uninstall_roots = (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for uninstall_root in uninstall_roots:
            try:
                with winreg.OpenKey(root, uninstall_root, 0, winreg.KEY_READ) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index), 0, winreg.KEY_READ) as item:
                                display_name = str(winreg.QueryValueEx(item, "DisplayName")[0])
                                if "copyq" not in display_name.casefold():
                                    continue
                                try:
                                    add(winreg.QueryValueEx(item, "InstallLocation")[0])
                                except OSError:
                                    pass
                                try:
                                    add(winreg.QueryValueEx(item, "DisplayIcon")[0].split(",", 1)[0])
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                pass

    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            root = Path(base)
            add(root / "CopyQ")
            add(root / "Programs" / "CopyQ")
    return candidates


def save_settings(settings: dict[str, str]) -> None:
    app_data_dir()
    path = APP_DATA / "settings.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    os.replace(temp, path)


def check_copyq(path: Path) -> str:
    if not path.is_file() or path.name.lower() != "copyq.exe":
        raise ValueError("请选择有效的 CopyQ 程序文件（copyq.exe）。")
    result = subprocess.run([str(path), "--version"], capture_output=True, timeout=8, check=False)
    output = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    if result.returncode or "CopyQ Clipboard Manager" not in output:
        raise ValueError("无法确认这是可用的 CopyQ 命令行程序，请检查路径。")
    return output.splitlines()[0]


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    crypt = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if protect:
        crypt.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        crypt.restype = wintypes.BOOL
        ok = crypt(ctypes.byref(source), "SendToCopyQ paired device secret", None, None, None, 0x1,
                  ctypes.byref(target))
    else:
        crypt.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        crypt.restype = wintypes.BOOL
        ok = crypt(ctypes.byref(source), None, None, None, None, 0x1,
                   ctypes.byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def protect_device_secret(secret: bytes) -> bytes:
    return _dpapi(secret, True)


def unprotect_device_secret(protected: bytes) -> bytes:
    return _dpapi(protected, False)


def app_data_dir() -> Path:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    return APP_DATA


def run_powershell_json(script: str) -> Any:
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference='Stop'; " + script,
    ]
    result = subprocess.run(command, capture_output=True, timeout=12, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "Windows network query failed")
    encoded = result.stdout.decode("ascii", "replace").strip()
    if not encoded:
        return None
    output = base64.b64decode(encoded).decode("utf-8")
    return json.loads(output) if output else None


def discover_lan_endpoint() -> tuple[str, str, int]:
    script = r"""
$payload = @(Get-NetIPConfiguration | ForEach-Object {
  $cfg = $_
  $alias = [string]$cfg.InterfaceAlias
  $desc = [string]$cfg.InterfaceDescription
  $addresses = @($cfg.IPv4Address | ForEach-Object { $_.IPAddress })
  if ($addresses.Count -gt 0 -and $cfg.NetAdapter.Status -eq 'Up' -and
      ($alias + ' ' + $desc) -notmatch '(?i)meta|tun|vpn|wintun|loopback|virtual|bluetooth|teredo') {
    foreach ($address in $addresses) {
      [pscustomobject]@{
        AliasBase64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($alias))
        InterfaceIndex=[int]$cfg.NetAdapter.ifIndex
        Address=$address
        HasGateway=($null -ne $cfg.IPv4DefaultGateway)
      }
    }
  }
}) | ConvertTo-Json -Compress
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
"""
    rows = run_powershell_json(script)
    if isinstance(rows, dict):
        rows = [rows]
    candidates: list[tuple[int, str, str, int]] = []
    for row in rows or []:
        try:
            address = ipaddress.ip_address(row["Address"])
        except (ValueError, KeyError):
            continue
        if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
            # Prefer an interface with a gateway; exclude VPN/tunnel interfaces above.
            try:
                interface_index = int(row["InterfaceIndex"])
                alias = base64.b64decode(row["AliasBase64"]).decode("utf-8")
            except (ValueError, KeyError, TypeError):
                continue
            candidates.append((0 if row.get("HasGateway") else 1, str(row["Address"]), alias, interface_index))
    if candidates:
        candidates.sort()
        _, address, alias, interface_index = candidates[0]
        return address, alias, interface_index
    # Fallback for machines where the PowerShell NetTCPIP module is unavailable.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    finally:
        sock.close()
    script = rf"""
$row = Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{address}' -ErrorAction Stop | Select-Object -First 1
$payload = [pscustomobject]@{{ Alias=$row.InterfaceAlias; InterfaceIndex=[int]$row.InterfaceIndex }} | ConvertTo-Json -Compress
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
"""
    details = run_powershell_json(script)
    if isinstance(details, dict) and details.get("InterfaceIndex"):
        return address, str(details.get("Alias") or "LAN"), int(details["InterfaceIndex"])
    raise RuntimeError("Could not identify the LAN interface for mDNS registration")


def discover_lan_address() -> tuple[str, str]:
    address, alias, _index = discover_lan_endpoint()
    return address, alias


def computer_id_for_fingerprint(fingerprint: str) -> str:
    """Derive the first ID compatibly; persist it so later cert rotation won't change identity."""
    material = f"SendToCopyQ computer id v1\n{fingerprint.lower()}".encode("utf-8")
    return "stpc-" + hashlib.sha256(material).digest()[:16].hex()


def load_or_create_computer_id(fingerprint: str) -> str:
    path = app_data_dir() / "computer-id.txt"
    if path.exists():
        value = path.read_text("ascii").strip()
        if not re.fullmatch(r"stpc-[0-9a-f]{32}", value):
            raise RuntimeError(f"电脑身份文件格式无效，未覆盖文件：{path}")
        return value
    value = computer_id_for_fingerprint(fingerprint)
    temp = path.with_suffix(".tmp")
    temp.write_text(value + "\n", encoding="ascii")
    os.replace(temp, path)
    return value


def certificate_material(bind_ip: str) -> tuple[Path, Path, str]:
    data_dir = app_data_dir()
    cert_path = data_dir / "receiver-cert.pem"
    key_path = data_dir / "receiver-key.pem"
    if not cert_path.exists() or not key_path.exists():
        private_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SendToCopyQ local receiver")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(bind_ip))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(private_key, hashes.SHA256())
        )
        key_path.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_obj = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fingerprint = hashlib.sha256(cert_obj.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_path, key_path, fingerprint


class PairingConflict(Exception):
    """A re-pair request points at two different authorized-device records."""


class State:
    def __init__(self, bind_ip: str, interface: str, fingerprint: str, interface_index: int):
        self.bind_ip = bind_ip
        self.interface = interface
        self.interface_index = interface_index
        self.fingerprint = fingerprint
        self.computer_id = load_or_create_computer_id(fingerprint)
        self.computer_name = clean_device_name(os.environ.get("COMPUTERNAME") or socket.gethostname())
        self.discovery_status = "starting"
        self.copyq_version = "未验证"
        self.lock = threading.RLock()
        self.receive_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pair_codes: dict[str, dict[str, Any]] = {}
        self.used_nonces: dict[str, float] = {}
        self.last_event: dict[str, Any] = {"kind": "ready", "message": "接收端已就绪", "time": time.time()}
        self.last_pairing: dict[str, Any] | None = None
        self.devices_path = app_data_dir() / "paired-devices.json"
        self.devices = self._load_devices()
        if self.devices:
            self.last_event["message"] = f"接收端已就绪，已记住 {len(self.devices)} 台配对设备"

    def received_files(self) -> ReceiveStore:
        configured = APP_DATA / "receive-directory.txt"
        try:
            root = Path(configured.read_text("utf-8"))
        except FileNotFoundError:
            main = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SendToCopyQ").resolve()
            root = default_receive_root() if APP_DATA == main else APP_DATA / "接收文件"
        if not root.is_absolute():
            raise ValueError("接收目录必须是绝对路径。")
        return ReceiveStore(APP_DATA / "received-files.sqlite3", root)

    def _load_devices(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.devices_path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(f"配对设备数据损坏，未覆盖文件：{self.devices_path}")

    def save_devices(self) -> None:
        temp = self.devices_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.devices, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temp, self.devices_path)

    def revoke_device(self, device_id: str) -> bool:
        with self.lock:
            if device_id not in self.devices:
                return False
            original = self.devices
            self.devices = {key: value for key, value in original.items() if key != device_id}
            try:
                self.save_devices()
            except Exception:
                self.devices = original
                raise
            return True

    def issue_pair_code(self) -> tuple[str, str]:
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.pair_codes.clear()
            self.pair_codes[hashlib.sha256(code.encode()).hexdigest()] = {"expires": now + PAIRING_TTL_SECONDS}
        payload = {
            "type": "send-to-copyq-pair",
            "version": 1,
            "computerId": self.computer_id,
            "computerName": self.computer_name,
            "host": self.bind_ip,
            "port": TLS_PORT,
            "pairingCode": code,
            "certificateSha256": self.fingerprint,
        }
        qr = segno.make(json.dumps(payload, separators=(",", ":")), error="m")
        from io import BytesIO
        buffer = BytesIO()
        scale, border = 5, 2
        qr.save(buffer, kind="svg", scale=scale, border=border, xmldecl=False)
        svg = buffer.getvalue().decode("utf-8")
        svg_root = re.search(r"<svg\b[^>]*>", svg)
        if not svg_root:
            raise RuntimeError("Segno did not produce a valid SVG root")
        root_tag = svg_root.group(0)
        width_match = re.search(r'\bwidth="([0-9]+(?:\.[0-9]+)?)(?:px)?"', root_tag)
        height_match = re.search(r'\bheight="([0-9]+(?:\.[0-9]+)?)(?:px)?"', root_tag)
        if not width_match or not height_match:
            raise RuntimeError("Segno SVG is missing numeric dimensions")
        view_box = f'viewBox="0 0 {width_match.group(1)} {height_match.group(1)}"'
        root_tag = re.sub(r'\sviewBox="[^"]*"', "", root_tag)
        svg = svg[:svg_root.start()] + root_tag[:-1] + f" {view_box}>" + svg[svg_root.end():]
        return code, svg

    def claim_pair_code(
        self,
        code: str,
        device_name: str,
        client_instance_id: str | None = None,
        previous_device_id: str | None = None,
    ) -> dict[str, str] | None:
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        now = time.time()
        with self.lock:
            record = self.pair_codes.get(code_hash)
            if not record or record["expires"] < now:
                self.pair_codes.pop(code_hash, None)
                return None
            identity_matches = [
                device_id for device_id, item in self.devices.items()
                if client_instance_id and item.get("clientInstanceId") == client_instance_id
            ]
            previous_exists = bool(previous_device_id and previous_device_id in self.devices)
            if len(identity_matches) > 1:
                raise PairingConflict("手机身份对应多条授权记录，请先在电脑端检查设备列表。")
            identity_id = identity_matches[0] if identity_matches else None
            if identity_id and previous_exists and previous_device_id != identity_id:
                raise PairingConflict("手机身份与原授权记录指向不同设备，请先在电脑端检查设备列表。")

            device_id = identity_id
            if device_id is None and previous_exists and client_instance_id:
                existing_client_id = self.devices[previous_device_id].get("clientInstanceId")
                if existing_client_id and existing_client_id != client_instance_id:
                    raise PairingConflict("原授权记录已绑定到另一部手机，未修改授权。")
                device_id = previous_device_id

            original_devices = self.devices
            updated_devices = {key: dict(value) for key, value in original_devices.items()}
            status = "created"
            if device_id:
                existing = updated_devices[device_id]
                try:
                    secret = unprotect_device_secret(base64.b64decode(existing["secretProtected"], validate=True))
                except Exception as exc:
                    raise RuntimeError("无法读取原授权凭据，已保留现有授权。") from exc
                if len(secret) != 32:
                    raise RuntimeError("原授权凭据格式无效，已保留现有授权。")
                existing["name"] = clean_device_name(device_name)
                if client_instance_id:
                    existing["clientInstanceId"] = client_instance_id
                existing["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                status = "updated"
            else:
                device_id = secrets.token_hex(12)
                secret = secrets.token_bytes(32)
                new_record = {
                    "name": clean_device_name(device_name),
                    "secretProtected": base64.b64encode(protect_device_secret(secret)).decode("ascii"),
                    "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                if client_instance_id:
                    new_record["clientInstanceId"] = client_instance_id
                updated_devices[device_id] = new_record

            self.devices = updated_devices
            try:
                self.save_devices()
            except Exception:
                self.devices = original_devices
                raise
            self.pair_codes.pop(code_hash, None)
            return {
                "deviceId": device_id,
                "secret": base64.b64encode(secret).decode("ascii"),
                "pairingStatus": status,
            }

    def authenticate(self, headers: http.client.HTTPMessage, content_type: str, content_length: int,
                     file_name_header: str | None = None) -> tuple[str, bytes, str, int, int, bool] | None:
        device_id = headers.get("X-Device-Id", "")
        timestamp = headers.get("X-Timestamp", "")
        nonce = headers.get("X-Nonce", "")
        body_hash = headers.get("X-Content-SHA256", "")
        signature = headers.get("X-Signature", "")
        supplied_task_id = headers.get("X-Task-Id", "")
        image_index_text = headers.get("X-Image-Index", "")
        image_count_text = headers.get("X-Image-Count", "")
        metadata_present = bool(supplied_task_id or image_index_text or image_count_text)
        if metadata_present:
            try:
                task_id = str(uuid.UUID(supplied_task_id))
                image_index = int(image_index_text)
                image_count = int(image_count_text)
                if not (1 <= image_index <= image_count <= 20):
                    return None
            except (ValueError, TypeError, AttributeError):
                return None
        else:
            task_id = str(uuid.uuid4())
            image_index, image_count = 1, 1
        if not device_id or not timestamp.isdigit() or len(nonce) < 16 or len(nonce) > 96:
            return None
        try:
            stamp = int(timestamp)
            supplied_hash = bytes.fromhex(body_hash)
        except ValueError:
            return None
        if len(supplied_hash) != 32 or abs(time.time() - stamp) > 120:
            return None
        with self.lock:
            device = self.devices.get(device_id)
            if not device:
                return None
            secret = unprotect_device_secret(base64.b64decode(device["secretProtected"], validate=True))
            nonce_key = device_id + ":" + nonce
            if nonce_key in self.used_nonces:
                return None
            # Task and image headers are transport metadata protected by the pinned TLS
            # connection; the existing request-body HMAC remains wire-compatible.
            canonical = f"{timestamp}\n{nonce}\n{content_type}\n{content_length}\n{body_hash}"
            if file_name_header is not None:
                if not metadata_present:
                    return None
                canonical += f"\n/receive\n{task_id}\n{image_index}\n{image_count}\n{file_name_header}"
            message = canonical.encode("utf-8")
            expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature.lower()):
                return None
            self.used_nonces[nonce_key] = time.time()
            cutoff = time.time() - 300
            self.used_nonces = {key: value for key, value in self.used_nonces.items() if value >= cutoff}
            return device_id, supplied_hash, task_id, image_index, image_count, metadata_present

    def report(self, kind: str, message: str, device: str = "", task_id: str | None = None) -> None:
        with self.lock:
            self.last_event = {"kind": kind, "message": message, "device": device, "taskId": task_id, "time": time.time()}


def clean_device_name(value: str) -> str:
    text = " ".join(str(value).replace("\x00", "").split())[:64]
    return text or "Android device"


def validate_image(data: bytes, content_type: str) -> str | None:
    # Some Android providers append a small trailer to an otherwise valid image.
    # Require the real container signature and terminal marker, then let CopyQ or
    # GDI+ perform decoding before acknowledging clipboard success.
    if content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND\xaeB`\x82" in data[8:]:
        return "image/png"
    if content_type in ("image/jpeg", "image/jpg") and data.startswith(b"\xff\xd8\xff") and b"\xff\xd9" in data[3:]:
        return "image/jpeg"
    return None


def copyq_is_running() -> bool:
    try:
        result = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq copyq.exe", "/FO", "CSV", "/NH"],
                                capture_output=True, timeout=5, check=False)
        return b'"copyq.exe"' in (result.stdout or b"").lower()
    except (OSError, subprocess.SubprocessError):
        return False


def native_clipboard_image_formats() -> list[str]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    if not user32.OpenClipboard(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        available = []
        for value, name in ((2, "CF_BITMAP"), (8, "CF_DIB"), (17, "CF_DIBV5")):
            if user32.IsClipboardFormatAvailable(value):
                available.append(name)
        return available
    finally:
        user32.CloseClipboard()


def copyq_insert_image(data: bytes, mime: str) -> dict[str, Any]:
    """Store exactly one image item. Diagnostic labels must never become clipboard text."""
    if not COPYQ_EXE.is_file():
        raise RuntimeError("CopyQ executable is not installed or configured")
    tab_result = subprocess.run([str(COPYQ_EXE), "config", "clipboard_tab"], capture_output=True, timeout=8)
    if tab_result.returncode:
        raise RuntimeError("Unable to read the CopyQ clipboard tab")
    tab = tab_result.stdout.decode("utf-8", "replace").strip()
    if not tab:
        raise RuntimeError("CopyQ clipboard history tab is disabled")
    commands = [str(COPYQ_EXE), "tab", tab]
    # CopyQ's write command inserts a new image-only item at row 0. Do not `add` a
    # text label first: that creates a separate Phone share text history item.
    write = subprocess.run(commands + ["write", "0", mime, "-"], input=data, capture_output=True, timeout=20)
    if write.returncode:
        raise RuntimeError(f"CopyQ image write failed (exit {write.returncode})")
    stored = subprocess.run(commands + ["read", mime, "0"], capture_output=True, timeout=8)
    if stored.returncode or stored.stdout != data:
        raise RuntimeError("CopyQ history image byte verification failed")
    selected = subprocess.run(commands + ["select", "0"], capture_output=True, timeout=8)
    if selected.returncode:
        raise RuntimeError("CopyQ could not select the image item")
    formats = subprocess.run([str(COPYQ_EXE), "clipboard", "?"], capture_output=True, timeout=8)
    if formats.returncode:
        raise RuntimeError("CopyQ could not inspect the Windows clipboard formats")
    names = formats.stdout.decode("utf-8", "replace").splitlines()
    if not any(name in names for name in (mime, "application/x-qt-image")):
        raise RuntimeError("Selected clipboard does not report a CopyQ image format")
    if "text/plain" in names or "text/unicode" in names:
        raise RuntimeError("Selected clipboard unexpectedly contains text data")
    native_formats = native_clipboard_image_formats()
    if not native_formats:
        raise RuntimeError("Windows native clipboard has no standard image format")
    return {"clipboard_verified": True, "copyq_version": "configured", "clipboard_formats": names,
            "native_formats": native_formats, "write_exit_code": write.returncode,
            "history_read_exit_code": stored.returncode, "select_exit_code": selected.returncode,
            "format_query_exit_code": formats.returncode}


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", wintypes.UINT), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


def _native_dib_payloads(width: int, height: int, bgra: bytes) -> dict[int, bytes]:
    """Packed DIBs, without a BITMAPFILEHEADER. Input is top-down straight BGRA."""
    if width <= 0 or height <= 0 or width * height * 4 > 256 * 1024 * 1024:
        raise ValueError("Invalid or oversized decoded image dimensions")
    if len(bgra) != width * height * 4:
        raise ValueError("Decoded image pixel length mismatch")
    stride = (width * 3 + 3) & ~3
    dib = bytearray(40 + stride * height)
    struct.pack_into("<IiiHHIIiiII", dib, 0, 40, width, height, 1, 24, 0, stride * height, 0, 0, 0, 0)
    for y in range(height):
        row = bgra[y * width * 4:(y + 1) * width * 4]
        output = bytearray(stride)
        output[0:width * 3:3] = row[0::4]
        output[1:width * 3:3] = row[1::4]
        output[2:width * 3:3] = row[2::4]
        # CF_DIB's 24-bit fallback composites transparency onto white.
        for x, alpha in enumerate(row[3::4]):
            if alpha != 255:
                for channel in range(3):
                    offset = x * 3 + channel
                    output[offset] = (output[offset] * alpha + 255 * (255 - alpha) + 127) // 255
        start = 40 + (height - 1 - y) * stride
        dib[start:start + stride] = output
    # An explicit V5 alternative retains alpha for applications that support it.
    v5 = bytearray(124)
    struct.pack_into("<IiiHHIIiiII", v5, 0, 124, width, -height, 1, 32, 3, len(bgra), 0, 0, 0, 0)
    struct.pack_into("<IIIII", v5, 40, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000, 0x73524742)
    struct.pack_into("<I", v5, 108, 4)  # LCS_GM_IMAGES; no embedded profile.
    return {8: bytes(dib), 17: bytes(v5) + bgra}


def _validate_native_dib(data: bytes) -> tuple[int, int, int]:
    if len(data) < 40:
        raise ValueError("Clipboard DIB header is truncated")
    size, width, height, planes, depth, compression, image_size = struct.unpack_from("<IiiHHII", data)
    if width <= 0 or height == 0 or planes != 1:
        raise ValueError("Clipboard DIB dimensions or planes are invalid")
    if size == 40 and depth == 24 and compression == 0 and height > 0:
        required = ((width * 3 + 3) & ~3) * height
    elif size == 124 and depth == 32 and compression == 3 and height < 0 and len(data) >= 124:
        if struct.unpack_from("<IIIII", data, 40) != (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000, 0x73524742):
            raise ValueError("Clipboard DIBV5 masks or color space are invalid")
        required = width * -height * 4
    else:
        raise ValueError("Unexpected clipboard DIB structure")
    if image_size != required or len(data) != size + required:
        raise ValueError("Clipboard DIB pixel data is incomplete")
    return width, abs(height), depth


def _decode_native_pixels(data: bytes) -> tuple[int, int, bytes]:
    """GDI+ decoding only; this function never opens or writes the clipboard."""
    gdip = ctypes.WinDLL("gdiplus.dll")
    shlwapi = ctypes.WinDLL("shlwapi.dll")
    class Rect(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int) for name in ("x", "y", "width", "height")]
    class BitmapData(ctypes.Structure):
        _fields_ = [("width", wintypes.UINT), ("height", wintypes.UINT), ("stride", ctypes.c_int),
                    ("format", ctypes.c_int), ("scan0", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]
    signatures = {
        "GdiplusStartup": ([ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p], ctypes.c_int),
        "GdiplusShutdown": ([ctypes.c_size_t], None),
        "GdipLoadImageFromStream": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
        "GdipGetImageWidth": ([ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)], ctypes.c_int),
        "GdipGetImageHeight": ([ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)], ctypes.c_int),
        "GdipBitmapLockBits": ([ctypes.c_void_p, ctypes.POINTER(Rect), wintypes.UINT, ctypes.c_int, ctypes.POINTER(BitmapData)], ctypes.c_int),
        "GdipBitmapUnlockBits": ([ctypes.c_void_p, ctypes.POINTER(BitmapData)], ctypes.c_int),
        "GdipDisposeImage": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(gdip, name)
        function.argtypes, function.restype = args, result
    def checked(name: str, *args: Any) -> None:
        status = getattr(gdip, name)(*args)
        if status != 0:
            raise RuntimeError(f"{name} failed (GDI+ status {status})")
    token = ctypes.c_size_t()
    checked("GdiplusStartup", ctypes.byref(token), ctypes.byref(_GdiplusStartupInput(1, None, False, False)), None)
    stream = None
    image = ctypes.c_void_p()
    locked = False
    bits = BitmapData()
    try:
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        shlwapi.SHCreateMemStream.argtypes = [ctypes.POINTER(ctypes.c_ubyte), wintypes.UINT]
        shlwapi.SHCreateMemStream.restype = ctypes.c_void_p
        stream = shlwapi.SHCreateMemStream(buffer, len(data))
        if not stream:
            raise RuntimeError("SHCreateMemStream failed")
        checked("GdipLoadImageFromStream", stream, ctypes.byref(image))
        width, height = wintypes.UINT(), wintypes.UINT()
        checked("GdipGetImageWidth", image, ctypes.byref(width))
        checked("GdipGetImageHeight", image, ctypes.byref(height))
        if not width.value or not height.value or width.value * height.value * 4 > 256 * 1024 * 1024:
            raise ValueError("Invalid or oversized decoded image dimensions")
        rect = Rect(0, 0, width.value, height.value)
        checked("GdipBitmapLockBits", image, ctypes.byref(rect), 1, 0x26200A, ctypes.byref(bits))  # Read, 32bppARGB
        locked = True
        if not bits.scan0 or abs(bits.stride) < width.value * 4:
            raise RuntimeError("GDI+ returned an invalid pixel buffer")
        pixels = b"".join(ctypes.string_at(bits.scan0 + y * bits.stride, width.value * 4) for y in range(height.value))
        checked("GdipBitmapUnlockBits", image, ctypes.byref(bits))
        locked = False
        return width.value, height.value, pixels
    finally:
        if locked:
            gdip.GdipBitmapUnlockBits(image, ctypes.byref(bits))
        if image:
            gdip.GdipDisposeImage(image)
        if stream:
            vtable = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(stream)
        gdip.GdiplusShutdown(token)


class _NativeClipboardApi:
    """Typed Win32 calls; no API is invoked on construction except loading DLLs."""
    def __init__(self, task_id: str | None, image_index: int, image_count: int):
        self.task_id, self.image_index, self.image_count = task_id, image_index, image_count
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "OpenClipboard": (self.user, [wintypes.HWND], wintypes.BOOL),
            "EmptyClipboard": (self.user, [], wintypes.BOOL),
            "CloseClipboard": (self.user, [], wintypes.BOOL),
            "SetClipboardData": (self.user, [wintypes.UINT, ctypes.c_void_p], ctypes.c_void_p),
            "GetClipboardData": (self.user, [wintypes.UINT], ctypes.c_void_p),
            "GetClipboardOwner": (self.user, [], wintypes.HWND),
            "GetClipboardSequenceNumber": (self.user, [], wintypes.DWORD),
            "RegisterClipboardFormatW": (self.user, [wintypes.LPCWSTR], wintypes.UINT),
            "GlobalAlloc": (self.kernel, [wintypes.UINT, ctypes.c_size_t], ctypes.c_void_p),
            "GlobalLock": (self.kernel, [ctypes.c_void_p], ctypes.c_void_p),
            "GlobalUnlock": (self.kernel, [ctypes.c_void_p], wintypes.BOOL),
            "GlobalSize": (self.kernel, [ctypes.c_void_p], ctypes.c_size_t),
            "GlobalFree": (self.kernel, [ctypes.c_void_p], ctypes.c_void_p),
        }
        self.functions = {}
        for name, (dll, args, result) in definitions.items():
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
            self.functions[name] = function

    def call(self, name: str, *args: Any, stage: str = "write", required: bool = True) -> Any:
        ctypes.set_last_error(0)
        result = self.functions[name](*args)
        error = ctypes.get_last_error()
        success = bool(result)
        if name == "GlobalUnlock":
            success = bool(result) or error == 0  # zero can mean successfully unlocked.
        elif name == "GlobalFree":
            success = result is None
        elif name in ("GetClipboardOwner", "GetClipboardSequenceNumber"):
            success = True  # interpreted by the caller, not by GetLastError.
        log_event(self.task_id, "NATIVE_CLIPBOARD_API_RESULT", status="ok" if success else "failed",
                  level="INFO" if success else "WARN", api=name, api_result=int(result or 0),
                  win32_error=error if not success else 0, stage=stage,
                  image_index=self.image_index, image_count=self.image_count)
        if required and not success:
            raise RuntimeError(f"{stage}: {name} failed (Win32 {error}: {ctypes.FormatError(error).strip()})")
        return result

    def open(self, hwnd: int, stage: str) -> None:
        for _ in range(10):
            if self.call("OpenClipboard", hwnd, stage=stage, required=False):
                return
            time.sleep(0.05)
        raise RuntimeError(f"{stage}: clipboard remained busy after 10 attempts")

    def allocate(self, data: bytes) -> int:
        handle = self.call("GlobalAlloc", 0x0002, len(data), stage="allocate")  # GMEM_MOVEABLE
        try:
            pointer = self.call("GlobalLock", handle, stage="allocate")
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                self.call("GlobalUnlock", handle, stage="allocate")
            return handle
        except Exception:
            self.call("GlobalFree", handle, stage="cleanup", required=False)
            raise

    def read(self, format_id: int, expected_size: int) -> bytes:
        handle = self.call("GetClipboardData", format_id, stage="readback")
        allocated = self.call("GlobalSize", handle, stage="readback")
        if allocated < expected_size:
            raise RuntimeError("readback: clipboard HGLOBAL is truncated")
        pointer = self.call("GlobalLock", handle, stage="readback")
        try:
            return ctypes.string_at(pointer, expected_size)
        finally:
            self.call("GlobalUnlock", handle, stage="readback")


def _publish_native_dibs(api: _NativeClipboardApi, hwnd: int, payloads: dict[int, bytes]) -> list[str]:
    dimensions = {key: _validate_native_dib(payload) for key, payload in payloads.items()}
    _publish_clipboard_payloads(api, hwnd, payloads)
    for format_id, payload in payloads.items():
        width, height, depth = dimensions[format_id]
        log_event(api.task_id, "NATIVE_CLIPBOARD_READBACK", format_id=format_id,
                  width=width, height=height, bit_depth=depth, dib_bytes=len(payload),
                  exact_bytes=True, image_index=api.image_index, image_count=api.image_count)
    return ["CF_DIB" if value == 8 else "CF_DIBV5" for value in payloads]


def _publish_clipboard_payloads(api: _NativeClipboardApi, hwnd: int, payloads: dict[int, bytes]) -> None:
    """Publish allocated formats together and verify their actual bytes."""
    if not hwnd:
        raise RuntimeError("A valid clipboard owner window is required")
    handles: dict[int, int] = {}
    try:
        # Allocate everything before EmptyClipboard; preserve old clipboard on preparation failure.
        for format_id, payload in payloads.items():
            handles[format_id] = api.allocate(payload)
        api.open(hwnd, "write")
        try:
            api.call("EmptyClipboard")
            for format_id in payloads:
                api.call("SetClipboardData", format_id, handles[format_id])
                del handles[format_id]  # System now owns it. Never GlobalFree it again.
            if api.call("GetClipboardOwner") != hwnd:
                raise RuntimeError("write: clipboard owner did not match the hidden window")
        finally:
            api.call("CloseClipboard", stage="write_close")
        sequence = api.call("GetClipboardSequenceNumber", stage="readback")
        api.open(hwnd, "readback")
        try:
            if api.call("GetClipboardSequenceNumber", stage="readback") != sequence or api.call("GetClipboardOwner", stage="readback") != hwnd:
                raise RuntimeError("readback: clipboard changed before verification")
            for format_id, expected in payloads.items():
                actual = api.read(format_id, len(expected))
                if actual != expected:
                    raise RuntimeError(f"readback: clipboard bytes mismatch for format {format_id}")
        finally:
            api.call("CloseClipboard", stage="readback_close")
    finally:
        for handle in handles.values():
            api.call("GlobalFree", handle, stage="cleanup", required=False)


class _NativeClipboardWorker:
    """One process-lifetime hidden owner window on its creating/message-pumping thread."""
    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.state_lock = threading.Lock()
        self.failure: Exception | None = None
        threading.Thread(target=self._run, name="send-to-copyq-clipboard", daemon=True).start()
        self.ready.wait()
        if self.failure:
            raise self.failure

    def enqueue(self, job: dict[str, Any]) -> None:
        with self.state_lock:
            if self.failure:
                raise self.failure
            self.jobs.put(job)

    def _run(self) -> None:
        try:
            self._run_owner()
            raise RuntimeError("Native clipboard owner thread stopped")
        except Exception as exc:
            with self.state_lock:
                self.failure = exc
                while True:
                    try:
                        job = self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    job["error"] = exc
                    job["done"].set()
            self.ready.set()

    def _run_owner(self) -> None:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user.PeekMessageW.restype = wintypes.BOOL
        user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.restype = ctypes.c_ssize_t
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.DestroyWindow.restype = wintypes.BOOL
        hwnd = None
        try:
            hwnd = user.CreateWindowExW(0, "STATIC", "SendToCopyQ Clipboard Owner", 0, 0, 0, 0, 0, None, None, None, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception as exc:
            self.failure = exc
        finally:
            self.ready.set()
        if not hwnd:
            return
        message = wintypes.MSG()
        try:
            while True:
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
                try:
                    job = self.jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                pixels = None
                try:
                    api = _NativeClipboardApi(job["task_id"], job["image_index"], job["image_count"])
                    if "paths" in job:
                        payload = file_drop_payload(job["paths"])
                        drop_effect = api.call("RegisterClipboardFormatW", "Preferred DropEffect", stage="prepare")
                        _publish_clipboard_payloads(api, hwnd, {15: payload, drop_effect: struct.pack("<I", 1)})
                        job["result"] = ["CF_HDROP"]
                    else:
                        width, height, pixels = _decode_native_pixels(job["data"])
                        log_event(job["task_id"], "NATIVE_CLIPBOARD_OWNER_READY", owner_hwnd=int(hwnd), width=width, height=height)
                        job["result"] = _publish_native_dibs(api, hwnd, _native_dib_payloads(width, height, pixels))
                except Exception as exc:
                    job["error"] = exc
                finally:
                    pixels = None
                    job.pop("data", None)
                    job["done"].set()
                    del job
        finally:
            user.DestroyWindow(hwnd)


_native_clipboard_worker: _NativeClipboardWorker | None = None
_native_clipboard_worker_lock = threading.Lock()


def set_native_clipboard_image(data: bytes, task_id: str | None = None, image_index: int = 1, image_count: int = 1) -> list[str]:
    return _run_clipboard_job({"data": data, "task_id": task_id, "image_index": image_index, "image_count": image_count})


def _run_clipboard_job(job: dict[str, Any]) -> list[str]:
    # Lazy startup: importing/compiling this module never touches the clipboard or creates a window.
    global _native_clipboard_worker
    with _native_clipboard_worker_lock:
        if _native_clipboard_worker is None:
            _native_clipboard_worker = _NativeClipboardWorker()
        worker = _native_clipboard_worker
    job["done"] = threading.Event()
    worker.enqueue(job)
    # Do not time out a queued write: it could otherwise modify the clipboard after a failed ACK.
    job["done"].wait()
    if "error" in job:
        raise job["error"]
    return job["result"]


def copy_received_images(items: list[dict], task: str) -> str:
    images = [item for item in items if item["category"] == "image"]
    if not images:
        return "unchanged"
    paths = [Path(item["path"]) for item in images]
    if len(images) == 1 and images[0]["mime"] in ("image/png", "image/jpeg"):
        with paths[0].open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != images[0]["hash"]:
            raise ValueError("原图已被修改，无法按图片重新复制。")
        write_image_to_clipboard(data, images[0]["mime"], task, 1, 1)
        return "image"
    _run_clipboard_job({"paths": paths, "task_id": task, "image_index": 1, "image_count": len(paths)})
    return "files"


def write_image_to_clipboard(data: bytes, mime: str, task_id: str, image_index: int, image_count: int) -> dict[str, Any]:
    copyq_path_exists = COPYQ_EXE.is_file()
    copyq_running = copyq_is_running() if copyq_path_exists else False
    copyq_version = ""
    if copyq_path_exists:
        try:
            check = subprocess.run([str(COPYQ_EXE), "--version"], capture_output=True, timeout=8, check=False)
            copyq_version = (check.stdout or check.stderr).decode("utf-8", "replace").splitlines()[0]
        except Exception:
            copyq_version = "version query failed"
    log_event(task_id, "COPYQ_DETECTED", status="available" if copyq_path_exists else "unavailable",
              configured_path_exists=copyq_path_exists, running=copyq_running, version=copyq_version,
              image_index=image_index, image_count=image_count)
    log_event(task_id, "CLIPBOARD_WRITE_STARTED", image_index=image_index, image_count=image_count,
              mime=mime, bytes=len(data), backend="CopyQ" if copyq_path_exists and copyq_running else "Windows native")
    if copyq_path_exists and copyq_running:
        log_event(task_id, "COPYQ_WRITE_STARTED", image_index=image_index, image_count=image_count, mime=mime, bytes=len(data))
        try:
            result = copyq_insert_image(data, mime)
            log_event(task_id, "COPYQ_WRITE_RESULT", image_index=image_index, image_count=image_count,
                      status="ok", clipboard_verified=result["clipboard_verified"],
                      clipboard_formats=",".join(result["clipboard_formats"]),
                      native_formats=",".join(result["native_formats"]),
                      write_exit_code=result["write_exit_code"], history_read_exit_code=result["history_read_exit_code"],
                      select_exit_code=result["select_exit_code"],
                      format_query_exit_code=result["format_query_exit_code"])
            log_event(task_id, "NATIVE_CLIPBOARD_WRITE_RESULT", image_index=image_index, backend="CopyQ",
                      native_formats=",".join(result["native_formats"]))
            log_event(task_id, "CLIPBOARD_VERIFY_RESULT", image_index=image_index, image_count=image_count,
                      status="ok", image_format=True, text_format=False, exact_bytes=True,
                      source_bytes_used=True)
            return {"backend": "CopyQ", "copyq_available": True, "copyq_version": copyq_version,
                    "copyq_exit_code": 0, "clipboard_verified": True}
        except Exception as exc:
            log_exception(task_id, "COPYQ_WRITE_RESULT", exc, image_index=image_index, image_count=image_count,
                          copyq_available=True, copyq_version=copyq_version)
    try:
        formats = set_native_clipboard_image(data, task_id, image_index, image_count)
        log_event(task_id, "NATIVE_CLIPBOARD_WRITE_RESULT", image_index=image_index, image_count=image_count,
                  status="ok", clipboard_formats=",".join(formats))
        log_event(task_id, "CLIPBOARD_VERIFY_RESULT", image_index=image_index, image_count=image_count,
                  status="ok", image_format=True, exact_bytes=True, source_bytes_used=True, backend="Windows native")
        return {"backend": "Windows native", "copyq_available": copyq_path_exists,
                "copyq_running": copyq_running, "copyq_version": copyq_version,
                "copyq_exit_code": None, "clipboard_verified": True}
    except Exception as exc:
        log_exception(task_id, "NATIVE_CLIPBOARD_WRITE_RESULT", exc, image_index=image_index, image_count=image_count,
                      copyq_available=copyq_path_exists, copyq_running=copyq_running)
        raise RuntimeError("Windows could not confirm an image in the system clipboard") from exc


class QuietHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SendToCopyQ/0.1"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def reply(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


    def read_json(self, limit: int = 8192) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return None
        if length < 0 or length > limit:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

class TransferHandler(QuietHandler):
    state: State

    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {
                "ok": True,
                "service": APP_NAME,
                "computerId": self.state.computer_id,
                "computerName": self.state.computer_name,
                "protocolVersion": int(PROTOCOL_VERSION),
                "fileReceive": True,
            })
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/pair/claim":
            self.handle_pair_claim()
        elif parsed.path == "/upload":
            self.handle_upload()
        elif parsed.path == "/receive":
            self.handle_file_receive()
        else:
            self.reply(404, {"error": "not found"})

    def handle_pair_claim(self) -> None:
        value = self.read_json()
        if not value:
            self.reply(400, {"error": "invalid request"})
            return
        code = str(value.get("pairingCode", ""))
        device_name = clean_device_name(value.get("deviceName", "Android device"))
        client_instance_id = value.get("clientInstanceId")
        previous_device_id = value.get("previousDeviceId")
        if client_instance_id is not None:
            client_instance_id = str(client_instance_id).lower()
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", client_instance_id):
                self.reply(400, {"error": "手机身份格式无效。"})
                return
        if previous_device_id is not None:
            previous_device_id = str(previous_device_id)
            if not re.fullmatch(r"[0-9a-f]{24}", previous_device_id):
                self.reply(400, {"error": "原授权 ID 格式无效。"})
                return
        if not code or len(code) > 128:
            self.reply(400, {"error": "invalid pairing code"})
            return
        try:
            result = self.state.claim_pair_code(code, device_name, client_instance_id, previous_device_id)
        except PairingConflict as exc:
            identity_hash = hashlib.sha256(client_instance_id.encode()).hexdigest()[:12] if client_instance_id else None
            previous_hash = hashlib.sha256(previous_device_id.encode()).hexdigest()[:12] if previous_device_id else None
            log_event(None, "PAIRING_IDENTITY_CONFLICT", level="WARN", status="failed",
                      client_identity_hash=identity_hash, previous_device_hash=previous_hash)
            self.reply(409, {"error": str(exc)})
            return
        except (RuntimeError, OSError) as exc:
            self.reply(500, {"error": str(exc)})
            return
        if not result:
            self.reply(403, {"error": "pairing code expired or already used"})
            return
        self.state.report("paired", f"已配对设备：{device_name}", device_name)
        with self.state.lock:
            self.state.last_event["pairingStatus"] = result["pairingStatus"]
            self.state.last_pairing = {"status": result["pairingStatus"], "device": device_name, "time": time.time()}
        log_event(None, "PAIRING_CLAIM", status="ok", result=result["pairingStatus"], device_name=device_name,
                  client_identity_hash=hashlib.sha256(client_instance_id.encode()).hexdigest()[:12]
                  if client_instance_id else None)
        print(f"[配对] {device_name} ({result['pairingStatus']})")
        desktop_event("devices")
        self.reply(200, result)

    def handle_file_receive(self) -> None:
        mime = self.headers.get("Content-Type", "").strip().lower()
        encoded_name = self.headers.get("X-File-Name", "")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime):
                raise ValueError("文件类型无效。")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", encoded_name):
                raise ValueError("文件名无效。")
            decoded_name = base64.urlsafe_b64decode(encoded_name + "=" * (-len(encoded_name) % 4)).decode("utf-8")
            name = safe_name(decoded_name)
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "文件名称、大小或类型无效。"})
            return
        maximum = MAX_IMAGE_BYTES if mime in ("image/png", "image/jpeg") else MAX_FILE_BYTES
        if length < 0 or length > maximum or (length == 0 and mime in ("image/png", "image/jpeg")):
            self.reply(413, {"error": "文件超过此类型的接收大小限制。"})
            return
        auth = self.state.authenticate(self.headers, mime, length, encoded_name)
        if not auth:
            self.reply(401, {"error": "设备身份验证失败"})
            return
        device_id, digest, task, ordinal, total, _ = auth
        device = clean_device_name(self.state.devices.get(device_id, {}).get("name", "Android device"))
        category = category_for(name, mime)
        expected_suffixes = {"image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}}
        if mime in expected_suffixes and Path(name).suffix.lower() not in expected_suffixes[mime]:
            name = Path(name).stem + (".png" if mime == "image/png" else ".jpg")
        temporary = None
        try:
            store = self.state.received_files()
            self.connection.settimeout(60)
            temporary = store.receive(self.rfile, length, digest, category)
            if mime in expected_suffixes and not validate_image(temporary.read_bytes(), mime):
                raise ValueError("图片内容与声明的格式不匹配。")
            with self.state.receive_lock, closing(store.connect()) as db:
                with db:
                    db.execute("INSERT OR IGNORE INTO batches(device_id, task, device, total, updated) VALUES (?, ?, ?, ?, ?)",
                               (device_id, task, device, total, time.time()))
                    batch = db.execute("SELECT * FROM batches WHERE device_id=? AND task=?", (device_id, task)).fetchone()
                    if batch["total"] != total:
                        raise ValueError("同一批次的文件数量不一致。")
                    items = store.files(db, batch["id"])
                    existing = next((item for item in items if item["ordinal"] == ordinal), None)
                    if existing:
                        if existing["hash"] != digest.hex() or not Path(existing["path"]).is_file():
                            raise ValueError("已接收的文件内容不一致或文件已移动，请重新分享。")
                    else:
                        path = store.publish(temporary, name)
                        try:
                            db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (batch["id"], ordinal, str(path), mime, category, length, digest.hex()))
                            db.execute("UPDATE batches SET updated=? WHERE id=?", (time.time(), batch["id"]))
                            db.commit()
                        except sqlite3.Error:
                            path.unlink(missing_ok=True)
                            raise
                        items = store.files(db, batch["id"])
                clipboard = batch["clipboard"]
                completed_now = len(items) == total and clipboard is None
                if completed_now:
                    try:
                        clipboard = copy_received_images(items, task)
                    except Exception as exc:
                        log_exception(task, "BATCH_CLIPBOARD_FAILED", exc)
                        clipboard = "failed"
                    with db:
                        db.execute("UPDATE batches SET clipboard=? WHERE id=?", (clipboard, batch["id"]))
                count = len(items)
                image_count = sum(item["category"] == "image" for item in items)
            log_event(task, "FILE_SAVED", file_index=ordinal, file_count=total, category=category, bytes_received=length)
            if completed_now:
                log_event(task, "RECEIVE_FINISHED", image_index=total, image_count=total, receive_mode="batch",
                          device_name=device, backend=clipboard)
                desktop_event("file-complete", task)
            elif not existing:
                desktop_event("files")
        except ValueError as exc:
            self.reply(400, {"error": str(exc)})
            return
        except (OSError, sqlite3.Error) as exc:
            log_event(task, "FILE_RECEIVE_FAILED", level="ERROR", status="failed", error_type=type(exc).__name__,
                      error_code=getattr(exc, "winerror", None) or getattr(exc, "errno", None))
            self.reply(500, {"error": "文件接收未完成，请检查电脑接收目录的权限和可用空间后重试。"})
            return
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.reply(200, {"ok": True, "taskId": task, "savedCount": count, "imageCount": image_count,
                         "clipboard": clipboard or "pending"})

    def handle_upload(self) -> None:
        request_started = time.perf_counter()
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            task_hint = str(uuid.UUID(self.headers.get("X-Task-Id", "")))
        except (ValueError, AttributeError):
            task_hint = None
        log_event(task_hint, "CONNECTION_ACCEPTED", remote_ip=self.client_address[0], remote_port=self.client_address[1],
                  content_type=content_type)
        if content_type not in ("image/png", "image/jpeg"):
            log_event(task_hint, "IMAGE_VALIDATION_FAILED", level="WARN", status="failed", error_code="UNSUPPORTED_MIME",
                      mime=content_type)
            self.reply(415, {"error": "只接受 PNG 或 JPEG 图片"})
            log_event(task_hint, "RECEIVE_FINISHED", level="WARN", status="failed",
                      elapsed_ms=int((time.perf_counter() - request_started) * 1000))
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_IMAGE_BYTES:
            log_event(task_hint, "TRANSFER_FAILED", level="WARN", status="failed", error_code="INVALID_CONTENT_LENGTH",
                      expected_bytes=length)
            self.reply(413, {"error": "图片大小无效或超过 50 MiB"})
            log_event(task_hint, "RECEIVE_FINISHED", level="WARN", status="failed",
                      elapsed_ms=int((time.perf_counter() - request_started) * 1000))
            return
        auth = self.state.authenticate(self.headers, content_type, length)
        if not auth:
            log_event(task_hint, "DEVICE_AUTHENTICATED", level="WARN", status="failed", error_code="AUTHENTICATION_FAILED")
            self.reply(401, {"error": "设备身份验证失败"})
            log_event(task_hint, "RECEIVE_FINISHED", level="WARN", status="failed",
                      elapsed_ms=int((time.perf_counter() - request_started) * 1000))
            return
        device_id, expected_hash, task_id, image_index, image_count, has_task_headers = auth
        device = self.state.devices.get(device_id, {})
        device_name = clean_device_name(device.get("name", "Android device"))
        common = {"device_name": device_name, "device_id_hash": hashlib.sha256(device_id.encode()).hexdigest()[:12],
                  "image_index": image_index, "image_count": image_count, "mime": content_type,
                  "expected_bytes": length, "task_headers_authenticated": has_task_headers}
        log_event(task_id, "DEVICE_AUTHENTICATED", **common)
        body = self.rfile.read(length)
        log_event(task_id, "TRANSFER_RECEIVED", status="ok" if len(body) == length else "failed",
                  actual_bytes=len(body), **common)
        log_event(task_id, "IMAGE_VALIDATION_STARTED", image_bytes=len(body), **common)
        if len(body) != length or not hmac.compare_digest(hashlib.sha256(body).digest(), expected_hash):
            log_event(task_id, "IMAGE_VALIDATION_FAILED", level="ERROR", status="failed", error_code="BODY_HASH_MISMATCH",
                      actual_bytes=len(body), **common)
            log_exception(task_id, "RECEIVE_FINISHED", RuntimeError("Request body length or SHA-256 mismatch"),
                          elapsed_ms=int((time.perf_counter() - request_started) * 1000), **common)
            self.reply(400, {"error": "图片数据校验失败"})
            return
        mime = validate_image(body, content_type)
        if not mime:
            log_event(task_id, "IMAGE_VALIDATION_FAILED", level="ERROR", status="failed", error_code="IMAGE_SIGNATURE_MISMATCH",
                      actual_bytes=len(body), **common)
            self.reply(415, {"error": "图片内容与 PNG/JPEG 类型不匹配"})
            log_event(task_id, "RECEIVE_FINISHED", level="WARN", status="failed",
                      elapsed_ms=int((time.perf_counter() - request_started) * 1000),
                      image_index=image_index, image_count=image_count, bytes_received=len(body))
            return
        log_event(task_id, "IMAGE_VALIDATION_COMPLETED", actual_bytes=len(body), hash_verified=True,
                  format_verified=True, mime=mime, image_index=image_index, image_count=image_count)
        try:
            with self.state.receive_lock:
                clipboard_result = write_image_to_clipboard(body, mime, task_id, image_index, image_count)
        except Exception as exc:
            log_exception(task_id, "CLIPBOARD_VERIFY_RESULT", exc, image_index=image_index, image_count=image_count,
                          mime=mime, bytes=len(body))
            self.state.report("error", f"剪贴板写入失败：{type(exc).__name__}", device_name, task_id)
            try:
                self.reply(500, {"error": "图片已收到，但 Windows 剪贴板未能确认图片格式"})
            finally:
                log_event(task_id, "ACK_SENT", level="ERROR", status="failed", http_status=500,
                          image_index=image_index, image_count=image_count)
                log_event(task_id, "RECEIVE_FINISHED", level="ERROR", status="failed",
                          elapsed_ms=int((time.perf_counter() - request_started) * 1000),
                          image_index=image_index, image_count=image_count, bytes_received=len(body))
            return
        backend = clipboard_result["backend"]
        self.state.report("received", f"已接收 {mime} 图片并写入剪贴板（{backend}）", device_name, task_id)
        print(f"[接收] {device_name} · {mime} · {len(body)} 字节 · {backend} 已确认剪贴板图片")
        ack_sent = False
        try:
            self.reply(200, {"ok": True, "taskId": task_id, "message": "电脑已确认图片写入系统剪贴板"})
            ack_sent = True
            log_event(task_id, "ACK_SENT", image_index=image_index, image_count=image_count, http_status=200,
                      backend=backend, clipboard_verified=True)
        except Exception as exc:
            log_exception(task_id, "ACK_SENT", exc, image_index=image_index, image_count=image_count,
                          http_status=200, backend=backend)
        finally:
            log_event(task_id, "RECEIVE_FINISHED", status="ok" if ack_sent else "failed",
                      image_index=image_index, image_count=image_count,
                      bytes_received=len(body), backend=backend,
                      elapsed_ms=int((time.perf_counter() - request_started) * 1000))
            if ack_sent:
                if image_index == image_count:
                    desktop_event("legacy-complete", task_id)
                else:
                    desktop_event("legacy")
        return


class DashboardHandler(QuietHandler):
    state: State

    def _local_only(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def do_GET(self) -> None:
        if not self._local_only():
            self.reply(403, {"error": "local only"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/state":
            with self.state.lock:
                devices = [
                    {"id": key, "name": item.get("name", "Android device"), "created": item.get("created", "")}
                    for key, item in self.state.devices.items()
                ]
                result = {
                    "appVersion": APP_VERSION,
                    "protocolVersion": PROTOCOL_VERSION,
                    "ip": self.state.bind_ip,
                    "interface": self.state.interface,
                    "interfaceIndex": self.state.interface_index,
                    "lanState": self.state.lan.state_name if getattr(self.state, "lan", None) else "running",
                    "port": TLS_PORT,
                    "copyqPath": str(COPYQ_EXE),
                    "copyqInstalled": COPYQ_EXE.is_file(),
                    "copyqVersion": self.state.copyq_version,
                    "fingerprint": self.state.fingerprint,
                    "computerId": self.state.computer_id,
                    "computerName": self.state.computer_name,
                    "discoveryStatus": self.state.discovery_status,
                    "pairingActive": any(item["expires"] > time.time() for item in self.state.pair_codes.values()),
                    "lastPairing": getattr(self.state, "last_pairing", None),
                    "devices": devices,
                    "lastEvent": self.state.last_event,
                }
            self.reply(200, result)
            return
        if parsed.path == "/api/received":
            try:
                store = self.state.received_files()
                self.reply(200, {"root": str(store.root), "batches": store.recent()})
            except (ValueError, OSError, sqlite3.Error):
                self.reply(500, {"error": "无法读取接收记录。"})
            return
        if parsed.path == "/api/diagnostics":
            task_id = urllib.parse.parse_qs(parsed.query).get("task_id", [""])[0]
            try:
                task_id = str(uuid.UUID(task_id))
            except (ValueError, AttributeError):
                self.reply(400, {"error": "请输入有效的任务 ID。"})
                return
            events = diagnostic_records(task_id)
            self.reply(200, {"taskId": task_id, "android": events["android"], "windows": events["windows"]})
            return
        if parsed.path == "/api/diagnostics/export":
            task_id = urllib.parse.parse_qs(parsed.query).get("task_id", [""])[0]
            try:
                task_id = str(uuid.UUID(task_id))
                bundle = diagnostic_bundle(task_id)
            except (ValueError, AttributeError):
                self.reply(400, {"error": "请输入有效的任务 ID。"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="SendToCopyQ-{task_id}.zip"')
            self.send_header("Content-Length", str(len(bundle)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(bundle)
            return
        if parsed.path in ("/", "/desktop.css", "/desktop.js"):
            asset = "index.html" if parsed.path == "/" else parsed.path[1:]
            page = (RESOURCE_DIR / asset).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {"index.html": "text/html", "desktop.css": "text/css", "desktop.js": "text/javascript"}[asset] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._local_only():
            self.reply(403, {"error": "local only"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/api/local/lan-network", "/api/local/lan-start"):
            lan = getattr(self.state, "lan", None)
            if not lan or not lan.token or not hmac.compare_digest(
                self.headers.get("X-MoryPaste-Lan-Control", "").encode("utf-8"), lan.token.encode("utf-8")
            ):
                self.reply(403, {"error": "本机接收控制请求未获授权。"})
                return
            try:
                if parsed.path == "/api/local/lan-network":
                    self.reply(200, lan.refresh_network())
                else:
                    expected = self.read_json(1024)
                    if expected is None:
                        self.reply(400, {"error": "无效的本机接收控制请求。"})
                        return
                    lan.start(expected)
                    self.reply(200, {"ok": True})
            except Exception as exc:
                log_exception(None, "LAN_START_FAILED", exc)
                self.reply(409, {"error": str(exc) or "无法开始局域网接收，请重试。"})
            return
        if parsed.path == "/api/received/action":
            self.handle_received_action()
            return
        if parsed.path == "/api/pairing":
            if getattr(self.state, "lan", None) and self.state.lan.state_name != "running":
                self.reply(409, {"error": "请先启用局域网接收。"})
                return
            _code, svg = self.state.issue_pair_code()
            self.reply(200, {"svg": svg, "expiresIn": PAIRING_TTL_SECONDS})
            return
        if parsed.path == "/api/diagnostics/import-android":
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length <= 0 or length > 5 * 1024 * 1024:
                    raise ValueError("Android 诊断包大小无效或超过 5 MiB。")
                imported = import_android_log_bundle(self.rfile.read(length))
                self.reply(200, {"ok": True, "importedEvents": imported})
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                self.reply(400, {"error": str(exc) or "Android 诊断包导入失败。"})
            return
        if parsed.path == "/api/settings":
            global COPYQ_EXE
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 4096:
                    raise ValueError("设置请求无效。")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("设置内容无效。")
                candidate = Path(str(value.get("copyqPath", "")).strip()).expanduser()
                version = check_copyq(candidate)
                save_settings({"copyqPath": str(candidate)})
                COPYQ_EXE = candidate
                self.state.copyq_version = version
                self.state.report("ready", f"CopyQ 已配置：{version}")
                self.reply(200, {"ok": True, "copyqPath": str(candidate), "copyqVersion": version})
            except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                self.reply(400, {"error": str(exc) or "CopyQ 检查失败。"})
            return
        if parsed.path == "/api/revoke":
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length <= 0 or length > 2048:
                    raise ValueError("撤销请求大小无效。")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                device_id = value.get("deviceId") if isinstance(value, dict) else None
                if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{24}", device_id):
                    raise ValueError("授权设备 ID 无效。")
                removed = self.state.revoke_device(device_id)
            except (ValueError, UnicodeError) as exc:
                self.reply(400, {"error": str(exc), "code": "INVALID_REVOKE_REQUEST"})
                return
            except Exception as exc:
                log_exception(None, "DEVICE_REVOKE_FAILED", exc)
                self.reply(500, {"error": "授权记录保存失败，原授权已保留，请重试。", "code": "REVOKE_SAVE_FAILED"})
                return
            log_event(None, "DEVICE_REVOKED", status="ok", removed=removed)
            if removed:
                desktop_event("devices")
            self.reply(200, {"ok": True, "revoked": removed, "status": "revoked" if removed else "already_absent"})
            return
        if parsed.path == "/api/quit":
            self.reply(200, {"ok": True})
            self.state.stop_event.set()
            return
        self.reply(404, {"error": "not found"})

    def handle_received_action(self) -> None:
        origin = self.headers.get("Origin")
        if (origin and origin != f"http://127.0.0.1:{DASHBOARD_PORT}") or self.headers.get_content_type() != "application/json":
            self.reply(403, {"error": "仅可在本机客户端中操作接收文件。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("请求无效。")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("请求无效。")
            store = self.state.received_files()
            action = value.get("action")
            folder = None
            if action == "openRoot":
                store.root.mkdir(parents=True, exist_ok=True)
                folder = store.root
            else:
                batch_id = value.get("id")
                if type(batch_id) is not int or action not in ("openFolder", "copyImages"):
                    raise ValueError("接收记录无效。")
                with self.state.receive_lock, closing(store.connect()) as db:
                    batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
                    if batch is None:
                        raise ValueError("已找不到这条接收记录。")
                    items = store.files(db, batch_id)
                    if action == "openFolder":
                        item = next((item for item in items if item["ordinal"] == value.get("ordinal")), None)
                        if not item or not Path(item["path"]).is_file():
                            raise ValueError("这个文件已移动或删除。")
                        folder = Path(item["path"]).parent
                    else:
                        if len(items) != batch["total"]:
                            raise ValueError("还未收齐本批文件。")
                        clipboard = copy_received_images(items, batch["task"])
                        with db:
                            db.execute("UPDATE batches SET clipboard=? WHERE id=?", (clipboard, batch_id))
                        desktop_event("files")
            if folder is not None:
                if value.get("resolveOnly") is True:
                    self.reply(200, {"ok": True, "folderPath": str(folder)})
                    return
                os.startfile(folder)  # Keep the local browser entry point working.
            self.reply(200, {"ok": True})
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            log_event(None, "RECEIPT_ACTION_FAILED", level="WARN", status="failed", error_type=type(exc).__name__)
            self.reply(500, {"error": "操作未完成，请确认文件仍在且可访问。"})


class LanReceiver:
    def __init__(self, state: State, cert_path: Path, key_path: Path, token: str):
        self.state = state
        self.cert_path = cert_path
        self.key_path = key_path
        self.token = token
        self.state_name = "waiting"
        self.lock = threading.Lock()
        self.transfer = None
        self.mdns = None

    def refresh_network(self) -> dict:
        with self.lock:
            if self.transfer is None:
                ip, interface, index = discover_lan_endpoint()
                with self.state.lock:
                    self.state.bind_ip = ip
                    self.state.interface = interface
                    self.state.interface_index = index
            return {"ip": self.state.bind_ip, "interface": self.state.interface,
                    "interfaceIndex": self.state.interface_index}

    def start(self, expected: dict | None = None) -> None:
        with self.lock:
            if self.state.stop_event.is_set():
                raise ValueError("接收程序正在退出。")
            if self.transfer is not None:
                return
            ip, interface, index = self.state.bind_ip, self.state.interface, self.state.interface_index
            if expected is not None:
                endpoint = (expected.get("ip"), expected.get("interface"), expected.get("interfaceIndex"))
                if not ip or endpoint != (ip, interface, index) or endpoint != discover_lan_endpoint():
                    raise ValueError("当前接收网卡或地址已变化，请重试。")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(self.cert_path), str(self.key_path))
            transfer = http.server.ThreadingHTTPServer((ip, TLS_PORT), TransferHandler)
            try:
                transfer.socket = context.wrap_socket(transfer.socket, server_side=True)
                transfer.daemon_threads = True
                threading.Thread(target=transfer.serve_forever, name="send-to-copyq-transfer", daemon=True).start()
            except BaseException:
                transfer.server_close()
                raise
            self.transfer = transfer
            try:
                self.mdns = MdnsRegistration(ip, index, self.state.computer_id, self.state.computer_name, TLS_PORT)
                self.state.discovery_status = "registered" if self.mdns.wait_registered(5.0) else "unconfirmed"
            except Exception as exc:
                self.state.discovery_status = "failed"
                print(f"局域网发现启动失败；接收端仍可通过二维码使用：{exc}")
            self.state_name = "running"
            print(f"接收端已启动：{ip}:{TLS_PORT}（网卡：{interface}）", flush=True)

    def close(self) -> None:
        with self.lock:
            if self.mdns is not None:
                self.mdns.close()
            if self.transfer is not None:
                self.transfer.shutdown()
                self.transfer.server_close()


def serve() -> int:
    global COPYQ_EXE
    lan_token = os.environ.pop("MORYPASTE_LAN_CONTROL", "")
    try:
        load_settings()
        try:
            bind_ip, interface, interface_index = discover_lan_endpoint()
        except Exception as exc:
            if not lan_token:
                raise
            print(f"暂时无法确定接收网卡：{exc}", flush=True)
            bind_ip, interface, interface_index = "", "", 0
        cert_path, key_path, fingerprint = certificate_material(bind_ip or "127.0.0.1")
    except Exception as exc:
        print(f"启动失败：{exc}")
        return 2
    print(f"接收核心实际数据目录：{APP_DATA}", flush=True)
    state = State(bind_ip, interface, fingerprint, interface_index)
    state.lan = LanReceiver(state, cert_path, key_path, lan_token)
    if lan_token:
        state.discovery_status = "waiting"
    try:
        state.copyq_version = check_copyq(COPYQ_EXE)
        print(f"CopyQ：{state.copyq_version}（{COPYQ_EXE}）")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        state.copyq_version = "未配置或不可用"
        state.report("error", f"未找到可用的 CopyQ：{exc}。可在本机控制页设置路径。")
        print(f"CopyQ 尚未配置：{exc}")
    TransferHandler.state = state
    DashboardHandler.state = state
    try:
        dashboard = http.server.ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), DashboardHandler)
        if not lan_token:
            try:
                state.lan.start()
            except Exception:
                dashboard.server_close()
                raise
    except OSError as exc:
        print(f"启动失败：无法监听接收端口或本地界面端口：{exc}")
        return 2
    dashboard.daemon_threads = True
    threading.Thread(target=dashboard.serve_forever, name="send-to-copyq-http", daemon=True).start()
    print(f"本机控制界面：http://127.0.0.1:{DASHBOARD_PORT}/", flush=True)
    try:
        state.stop_event.wait()
    except KeyboardInterrupt:
        state.stop_event.set()
    finally:
        dashboard.shutdown()
        state.lan.close()
        dashboard.server_close()
    print("接收端已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
