"""本地文件存储适配器（设计 7.3）。

对象键由系统生成，只允许十六进制字符，不接受上传文件名，杜绝路径穿越。
写入先落临时文件、校验哈希后原子改名，读者不会看到写了一半的文件。
"""

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings

_KEY_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class LocalFileStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.objects = self.root / "objects"
        self.tmp = self.root / "tmp"

    @staticmethod
    def new_key() -> str:
        return uuid4().hex

    def _path(self, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("Invalid object key")
        return self.objects / key[:2] / key

    def put(self, key: str, content: bytes, sha256: str) -> StoredObject:
        target = self._path(key)
        if target.exists():
            raise FileExistsError(key)

        self.tmp.mkdir(parents=True, exist_ok=True)
        temporary = self.tmp / f"{key}.{uuid4().hex}.part"
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            # 按写入结果重新计算，确认落盘内容与上传内容一致
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != sha256:
                raise OSError("Stored content does not match expected hash")

            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        return StoredObject(key=key, size_bytes=len(content), sha256=sha256)

    def open(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def stat(self, key: str) -> int | None:
        path = self._path(key)
        return path.stat().st_size if path.exists() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


@lru_cache
def get_storage() -> LocalFileStorage:
    return LocalFileStorage(get_settings().storage_root)
