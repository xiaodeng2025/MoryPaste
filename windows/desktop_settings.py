"""Desktop preferences and opt-in, current-user Windows login startup."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import winreg
from pathlib import Path

APP_VERSION = "0.6.13"
STARTUP_NAME = "SendToCopyQ.lnk"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"


class DesktopSettings:
    def __init__(self, data_dir: Path, base_dir: Path, startup_dir: Path | None = None):
        self.path = data_dir / "desktop-settings.json"
        self.base_dir = base_dir
        self.startup_dir = startup_dir or Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        self.lock = threading.Lock()

    def preferences(self) -> dict:
        try:
            saved = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            saved = {}
        return {"notifications": saved.get("notifications") is True}

    def set_notifications(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("通知开关无效")
        with self.lock:
            prefs = self.preferences()
            prefs["notifications"] = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), "utf-8")
            temp.replace(self.path)
        return prefs

    @staticmethod
    def _approval() -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, STARTUP_NAME)
            if kind == winreg.REG_BINARY and len(value) >= 4:
                state = int.from_bytes(value[:4], "little")
                return {2: "enabled", 3: "system_disabled"}.get(state, "unknown")
            return "unknown"
        except FileNotFoundError:
            return "unregistered"
        except OSError:
            return "unknown"

    def startup_state(self) -> dict:
        exists = (self.startup_dir / STARTUP_NAME).is_file()
        approval = self._approval()
        state = "off" if not exists else ("registered" if approval == "unregistered" else approval)
        descriptions = {
            "off": "未设置登录启动",
            "registered": "已添加启动项；Windows 尚未报告开关状态",
            "enabled": "已开启，登录后自动启动并驻留系统托盘",
            "system_disabled": "已被 Windows 禁用；请在启动应用中重新启用",
            "unknown": "启动项存在，无法可靠读取 Windows 开关状态",
        }
        return {"state": state, "registered": exists, "enabled": state == "enabled", "description": descriptions[state]}

    def set_startup(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("登录启动开关无效")
        with self.lock:
            shortcut = self.startup_dir / STARTUP_NAME
            if not enabled:
                shortcut.unlink(missing_ok=True)
                return self.startup_state()
            # Never rewrite StartupApproved or re-create an existing disabled entry.
            if shortcut.exists():
                return self.startup_state()
            if self._approval() == "system_disabled":
                raise ValueError("Windows 已禁用此启动项，请在 Windows 启动应用中手动启用。")
            self.startup_dir.mkdir(parents=True, exist_ok=True)
            if getattr(sys, "frozen", False):
                target, arguments = str(Path(sys.executable)), "--start-hidden"
            else:
                target = str(Path(sys.prefix) / "Scripts/pythonw.exe")
                arguments = subprocess.list2cmdline([str(self.base_dir / "desktop_app.py"), "--start-hidden"])
            # Values are passed through environment variables, not interpolated shell code.
            env = os.environ.copy()
            env.update(STCQ_SHORTCUT=str(shortcut), STCQ_TARGET=target, STCQ_ARGUMENTS=arguments,
                       STCQ_WORKDIR=str(self.base_dir), STCQ_ICON=str(self.base_dir / "desktop_resources/sendtocopyq.ico"))
            script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:STCQ_SHORTCUT); $s.TargetPath=$env:STCQ_TARGET; $s.Arguments=$env:STCQ_ARGUMENTS; $s.WorkingDirectory=$env:STCQ_WORKDIR; $s.IconLocation=$env:STCQ_ICON; $s.Description='SendToCopyQ'; $s.Save()"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    env=env, capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or not shortcut.is_file():
                raise OSError("无法创建当前用户登录启动快捷方式")
            return self.startup_state()
