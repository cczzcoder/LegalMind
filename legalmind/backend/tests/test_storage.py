"""本地文件存储（设计 7.3）单元测试，不需要数据库。"""

import hashlib

import pytest

from app.adapters.storage import LocalFileStorage


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_put_and_open_round_trip(tmp_path):
    storage = LocalFileStorage(tmp_path)
    key = storage.new_key()
    stored = storage.put(key, b"content", sha(b"content"))

    assert stored.size_bytes == 7
    assert storage.open(key) == b"content"
    assert storage.stat(key) == 7
    # 临时文件已清理
    assert list((tmp_path / "tmp").iterdir()) == []


def test_hash_mismatch_leaves_no_file(tmp_path):
    storage = LocalFileStorage(tmp_path)
    key = storage.new_key()

    with pytest.raises(OSError):
        storage.put(key, b"content", sha(b"other"))

    assert storage.stat(key) is None
    assert list((tmp_path / "tmp").iterdir()) == []


def test_existing_key_is_not_overwritten(tmp_path):
    storage = LocalFileStorage(tmp_path)
    key = storage.new_key()
    storage.put(key, b"first", sha(b"first"))

    with pytest.raises(FileExistsError):
        storage.put(key, b"second", sha(b"second"))
    assert storage.open(key) == b"first"


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "a" * 31, "A" * 32, "../" + "a" * 29, "a" * 32 + "/x", ""],
)
def test_invalid_keys_are_rejected(tmp_path, key):
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.open(key)
    with pytest.raises(ValueError):
        storage.put(key, b"x", sha(b"x"))


def test_delete_is_idempotent(tmp_path):
    storage = LocalFileStorage(tmp_path)
    key = storage.new_key()
    storage.put(key, b"x", sha(b"x"))
    storage.delete(key)
    storage.delete(key)
    assert storage.stat(key) is None
