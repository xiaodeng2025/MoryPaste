"""Persistent receive receipts and bounded, verified file storage."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import re
import sqlite3
import struct
import tempfile
import uuid
from contextlib import closing
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO


MAX_FILE_BYTES = 4 * 1024**3
CATEGORIES = {"image": "图片", "video": "视频", "document": "文档", "other": "其他"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".3gp"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt", ".md", ".odt", ".ods", ".rtf"}


def default_receive_root() -> Path:
    # FOLDERID_Downloads, including Explorer's configured folder redirection.
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
    target = ctypes.c_wchar_p()
    try:
        shell = ctypes.windll.shell32
        get_folder = shell.SHGetKnownFolderPath
        get_folder.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        get_folder.restype = ctypes.c_long
        if get_folder(folder_id, 0, None, ctypes.byref(target)) == 0 and target.value:
            return Path(target.value) / "MoryPaste 接收"
    finally:
        if target:
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            free(target)
    return Path.home() / "Downloads" / "MoryPaste 接收"


def safe_name(value: str) -> str:
    value = re.split(r"[/\\]", value)[-1]
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "_", value).strip(" .")
    stem, suffix = Path(value).stem, Path(value).suffix
    if re.fullmatch(r"(?i)(con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9¹²³])", stem.split(".")[0].rstrip()):
        stem = "_" + stem
    return (stem[:90] or "接收文件") + suffix[:20].rstrip(" .")


def category_for(name: str, mime: str) -> str:
    extension = Path(name).suffix.lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if extension in DOCUMENT_EXTENSIONS or mime in ("application/pdf", "text/plain"):
        return "document"
    if mime == "application/octet-stream":
        if extension in IMAGE_EXTENSIONS:
            return "image"
        if extension in VIDEO_EXTENSIONS:
            return "video"
    return "other"


def file_drop_payload(paths: list[Path]) -> bytes:
    if not paths or any(not path.is_absolute() or not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("部分图片已移动或删除，请在接收目录中确认。")
    # DROPFILES followed by a double-NUL-terminated list of UTF-16 paths.
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(map(str, paths)) + "\0\0").encode("utf-16le")


class ReceiveStore:
    def __init__(self, database: Path, root: Path):
        self.database, self.root = database, root

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, task TEXT NOT NULL,
                    device TEXT NOT NULL, total INTEGER NOT NULL, updated REAL NOT NULL,
                    clipboard TEXT, UNIQUE(device_id, task)
                );
                CREATE TABLE IF NOT EXISTS files (
                    batch INTEGER NOT NULL, ordinal INTEGER NOT NULL, path TEXT NOT NULL,
                    mime TEXT NOT NULL, category TEXT NOT NULL, size INTEGER NOT NULL, hash TEXT NOT NULL,
                    PRIMARY KEY(batch, ordinal)
                );
            """)
            return db
        except Exception:
            db.close()
            raise

    def receive(self, source: BinaryIO, length: int, digest: bytes, category: str) -> Path:
        directory = self.root / CATEGORIES[category]
        directory.mkdir(parents=True, exist_ok=True)
        # Refuse a category junction that redirects received files outside the chosen root.
        if directory.resolve().parent != self.root.resolve():
            raise OSError("接收分类目录指向接收位置之外。")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".morypaste-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                summed = hashlib.sha256()
                remaining = length
                while remaining:
                    chunk = source.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise ValueError("文件传输未完成。")
                    output.write(chunk)
                    summed.update(chunk)
                    remaining -= len(chunk)
                if not hmac.compare_digest(summed.digest(), digest):
                    raise ValueError("文件完整性校验失败。")
            return temporary
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def publish(temporary: Path, name: str) -> Path:
        named = Path(safe_name(name))
        for index in range(10000):
            filename = named.name if not index else f"{named.stem} ({index + 1}){named.suffix}"
            target = temporary.with_name(filename)
            try:
                # On Windows rename never overwrites an existing destination.
                return temporary.rename(target)
            except FileExistsError:
                continue
        raise OSError("同名文件过多，请整理接收目录后重试。")

    @staticmethod
    def files(db: sqlite3.Connection, batch_id: int) -> list[dict]:
        return [dict(item) for item in db.execute(
            "SELECT ordinal, path, mime, category, size, hash FROM files WHERE batch=? ORDER BY ordinal", (batch_id,))]

    def recent(self, limit: int = 4) -> list[dict]:
        if not self.database.exists():
            return []
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM batches ORDER BY updated DESC, id DESC LIMIT ?", (limit,)).fetchall()
            return [
                {"id": row["id"], "device": row["device"], "total": row["total"], "updated": row["updated"],
                 "clipboard": row["clipboard"], "files": [
                     {**{key: item[key] for key in ("ordinal", "category", "size")},
                      "name": Path(item["path"]).name}
                     for item in self.files(db, row["id"])]}
                for row in rows
            ]
