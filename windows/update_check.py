"""Read Windows and Android release metadata over HTTPS."""
import hashlib
import hmac
import json
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


MAX_NOTE_LENGTH = 500
MAX_MANIFEST_BYTES = 16 * 1024
RELEASES = "https://github.com/xiaodeng2025/MoryPaste/releases"
WINDOWS_MANIFEST = f"{RELEASES}/latest/download/update.json"
ANDROID_RELEASES = RELEASES
ANDROID_MANIFEST = f"{ANDROID_RELEASES}/download/android-stable/android-update.txt"
ANDROID_FALLBACK_PAGE = f"{ANDROID_RELEASES}/tag/android-v0.5.8"
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_INSTALLER_BYTES = 128 * 1024 * 1024


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5})){2}", value):
        raise ValueError("更新版本号无效。")
    return tuple(map(int, value.split(".")))


def _https_url(value):
    if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("更新地址无效。")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("更新地址必须使用 HTTPS。")
    return value


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _GitHubAssetRedirect(_HttpsRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(_https_url(newurl))
        if destination.hostname not in ASSET_HOSTS or destination.port not in (None, 443):
            raise ValueError("GitHub 下载地址无效。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def android_release_page():
    opener = urllib.request.build_opener(_GitHubAssetRedirect())
    try:
        with opener.open(ANDROID_MANIFEST, timeout=5) as response:
            raw = response.read(256)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and exc.geturl() == ANDROID_MANIFEST:
            return None
        raise
    if len(raw) == 256:
        raise ValueError("Android 更新信息超出大小限制。")
    match = re.fullmatch(r"([1-9][0-9]{0,9})[ \t]+([^\s]+)", raw.decode("utf-8").strip())
    if not match or int(match[1]) > 2147483647:
        raise ValueError("Android 更新信息格式无效。")
    _version(match[2])
    return f"{ANDROID_RELEASES}/tag/android-v{match[2]}"


def check_update(current_version, feed_url):
    if not feed_url:
        return {"status": "unconfigured"}
    request = urllib.request.Request(_https_url(feed_url), headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_GitHubAssetRedirect() if feed_url == WINDOWS_MANIFEST else _HttpsRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("更新信息超出大小限制。")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("发布站点没有返回有效的更新信息。") from exc
    if not isinstance(data, dict):
        raise ValueError("更新信息格式无效。")
    version = data.get("version")
    if _version(version) <= _version(current_version):
        return {"status": "current"}
    page_url = _https_url(data.get("release_page"))
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("更新说明格式无效。")
    result = {"status": "available", "version": version, "page_url": page_url,
              "notes": notes.strip()[:MAX_NOTE_LENGTH]}
    digest = data.get("installer_sha256")
    if feed_url == WINDOWS_MANIFEST and page_url == f"{RELEASES}/tag/v{version}" and isinstance(digest, str):
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            result["installer_sha256"] = digest.lower()
    return result


def verify_installer(path: Path, expected_sha256: str) -> None:
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            sha256.update(block)
    if not hmac.compare_digest(sha256.hexdigest(), expected_sha256):
        raise ValueError("安装包 SHA-256 校验失败。")


def download_installer(version: str, digest: str, progress) -> Path:
    _version(version)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("缺少有效的安装包校验值。")
    filename = f"MoryPaste-{version}-Setup.exe"
    url = f"{RELEASES}/download/v{version}/{filename}"
    opener = urllib.request.build_opener(_GitHubAssetRedirect())
    directory = Path(tempfile.mkdtemp(prefix="MoryPaste-update-"))
    installer = directory / filename
    try:
        with opener.open(url, timeout=12) as response, installer.open("xb") as output:
            total = int(response.headers.get("Content-Length", "0"))
            if total < 0 or total > MAX_INSTALLER_BYTES:
                raise ValueError("安装包超过大小限制。")
            received = 0
            last_update = -1
            while chunk := response.read(128 * 1024):
                received += len(chunk)
                if received > MAX_INSTALLER_BYTES:
                    raise ValueError("安装包超过大小限制。")
                output.write(chunk)
                update = min(100, received * 100 // total) if total else received // (1024 * 1024)
                if update != last_update:
                    progress(update if total else None)
                    last_update = update
        verify_installer(installer, digest)
        # Preserve the same Internet Zone treatment as a browser download.
        installer.with_name(f"{installer.name}:Zone.Identifier").write_text(
            f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={url}\r\n", encoding="ascii", newline=""
        )
        return installer
    except Exception:
        installer.unlink(missing_ok=True)
        directory.rmdir()
        raise
