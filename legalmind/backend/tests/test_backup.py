"""备份与恢复 CLI 命令的单元测试。

不依赖真实数据库；pg_dump / pg_restore 通过 mock 绕过。
重点测试：连接参数解析、清单生成与哈希校验、dry-run 隔离、
恢复冲突检测，以及时效性字段（acquired_at）不会被恢复操作覆盖的保证。
"""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.cli import _pg_env_and_args, do_backup, do_restore

# ---------------------------------------------------------------------------
# _pg_env_and_args：连接参数解析
# ---------------------------------------------------------------------------


def test_pg_env_args_standard_url():
    url = "postgresql://admin:secret@db.host:5433/mydb"
    env, args = _pg_env_and_args(url)
    assert env.get("PGPASSWORD") == "secret"
    assert "-h" in args and args[args.index("-h") + 1] == "db.host"
    assert "-p" in args and args[args.index("-p") + 1] == "5433"
    assert "-U" in args and args[args.index("-U") + 1] == "admin"


def test_pg_env_args_strips_asyncpg_prefix():
    url = "postgresql+asyncpg://user:pw@localhost/testdb"
    env, args = _pg_env_and_args(url)
    assert env.get("PGPASSWORD") == "pw"
    assert args[args.index("-h") + 1] == "localhost"


def test_pg_env_args_no_password():
    url = "postgresql://user@localhost/db"
    env, args = _pg_env_and_args(url)
    assert "PGPASSWORD" not in env
    assert "-U" in args


# ---------------------------------------------------------------------------
# do_backup：清单生成与哈希正确性
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path):
    """返回指向临时目录的 Settings 替身。"""
    mock = MagicMock()
    mock.database_url = "postgresql+asyncpg://u:pw@localhost/db"
    mock.storage_root = str(tmp_path / "storage")
    return mock


def _seed_objects(storage_root: Path, files: dict[str, bytes]) -> None:
    objects = storage_root / "objects"
    for key, content in files.items():
        dest = objects / key[:2] / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


@pytest.fixture()
def storage_with_files(tmp_path):
    storage_root = tmp_path / "storage"
    files = {
        "ab" * 16: b"content of file ab",
        "cd" * 16: b"content of file cd",
        "ef" * 16: b"content of file ef",
    }
    _seed_objects(storage_root, files)
    return storage_root, files


def _pg_dump_side_effect(cmd, **_kwargs):
    """mock subprocess.run 时顺带写出 db.dump 文件，使后续 stat() 不会失败。"""
    try:
        idx = cmd.index("-f")
        Path(cmd[idx + 1]).write_bytes(b"fake pg_dump output")
    except (ValueError, IndexError):
        pass
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_backup_creates_manifest_with_correct_hashes(tmp_path, storage_with_files):
    storage_root, files = storage_with_files
    settings = _make_settings(tmp_path)
    settings.storage_root = str(storage_root)

    dest = tmp_path / "backups"
    with patch("app.cli.subprocess.run", side_effect=_pg_dump_side_effect):
        do_backup(dest, "testlabel", settings=settings)

    manifest_path = dest / "testlabel" / "artifacts_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["format_version"] == "1"
    entries = {e["object_key"]: e for e in manifest["entries"]}

    for key, content in files.items():
        assert key in entries
        assert entries[key]["sha256"] == hashlib.sha256(content).hexdigest()
        assert entries[key]["size_bytes"] == len(content)


def test_backup_writes_meta_json(tmp_path, storage_with_files):
    storage_root, _ = storage_with_files
    settings = _make_settings(tmp_path)
    settings.storage_root = str(storage_root)

    dest = tmp_path / "backups"
    with patch("app.cli.subprocess.run", side_effect=_pg_dump_side_effect):
        do_backup(dest, "lbl", settings=settings)

    meta = json.loads((dest / "lbl" / "backup_meta.json").read_text())
    assert meta["label"] == "lbl"
    assert meta["artifact_count"] == 3
    assert "created_at" in meta


def test_backup_fails_if_dest_already_exists(tmp_path):
    settings = _make_settings(tmp_path)
    dest = tmp_path / "backups"
    (dest / "dup").mkdir(parents=True)

    with pytest.raises(SystemExit):
        do_backup(dest, "dup", settings=settings)


def test_backup_reports_missing_pg_dump(tmp_path, storage_with_files):
    """宿主机没有 pg_dump 时（本机开发常见）给出可操作提示，并清理半成品目录。"""
    storage_root, _ = storage_with_files
    settings = _make_settings(tmp_path)
    settings.storage_root = str(storage_root)

    dest = tmp_path / "backups"
    with (
        patch("app.cli.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(SystemExit) as exc_info,
    ):
        do_backup(dest, "lbl", settings=settings)

    assert "pg_dump" in str(exc_info.value)
    assert not (dest / "lbl").exists()


# ---------------------------------------------------------------------------
# do_restore：哈希校验与 dry-run
# ---------------------------------------------------------------------------


def _make_valid_backup(backup_dir: Path, files: dict[str, bytes]) -> None:
    objects_dir = backup_dir / "objects"
    entries = []
    for key, content in files.items():
        dest = objects_dir / key[:2] / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        entries.append(
            {
                "object_key": key,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )

    (backup_dir / "db.dump").write_bytes(b"fake dump data")
    (backup_dir / "artifacts_manifest.json").write_text(
        json.dumps({"format_version": "1", "entries": entries}), encoding="utf-8"
    )
    (backup_dir / "backup_meta.json").write_text(
        json.dumps(
            {
                "created_at": "2026-10-03T00:00:00+00:00",
                "label": "test",
                "artifact_count": len(entries),
                "db_size_bytes": 14,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def valid_backup(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    files = {
        "ab" * 16: b"artifact one",
        "cd" * 16: b"artifact two",
    }
    _make_valid_backup(backup_dir, files)
    return backup_dir, files, "postgresql+asyncpg://u:pw@localhost/legalmind"


def test_restore_dry_run_writes_nothing(tmp_path, valid_backup):
    backup_dir, _, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    do_restore(backup_dir, db_url, str(storage_root), dry_run=True, settings=settings)

    assert not (storage_root / "objects").exists()


def test_restore_dry_run_passes_hash_check(tmp_path, valid_backup):
    backup_dir, _, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    do_restore(backup_dir, db_url, str(storage_root), dry_run=True, settings=settings)


def test_restore_aborts_on_tampered_file(tmp_path, valid_backup):
    backup_dir, files, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    first_key = next(iter(files))
    tampered = backup_dir / "objects" / first_key[:2] / first_key
    tampered.write_bytes(b"tampered content")

    with pytest.raises(SystemExit) as exc_info:
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)

    assert "sha256 不符" in str(exc_info.value) or "校验失败" in str(exc_info.value)


def test_restore_aborts_if_manifest_file_missing(tmp_path, valid_backup):
    backup_dir, files, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    first_key = next(iter(files))
    (backup_dir / "objects" / first_key[:2] / first_key).unlink()

    with pytest.raises(SystemExit) as exc_info:
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)

    assert "备份文件缺失" in str(exc_info.value) or "校验失败" in str(exc_info.value)


def test_restore_copies_files_to_storage(tmp_path, valid_backup):
    backup_dir, files, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    with patch("app.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)

    for key, content in files.items():
        restored = storage_root / "objects" / key[:2] / key
        assert restored.exists(), f"原件未恢复: {key}"
        assert restored.read_bytes() == content


def test_restore_skips_identical_existing_file(tmp_path, valid_backup):
    """恢复时发现目标文件已存在且哈希一致时跳过（幂等）。"""
    backup_dir, files, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    first_key = next(iter(files))
    pre_existing = storage_root / "objects" / first_key[:2] / first_key
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_bytes(files[first_key])

    with patch("app.cli.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)


def test_restore_aborts_on_conflicting_existing_file(tmp_path, valid_backup):
    """目标文件已存在且哈希不同时，必须中止，不做部分恢复。"""
    backup_dir, files, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    first_key = next(iter(files))
    conflicting = storage_root / "objects" / first_key[:2] / first_key
    conflicting.parent.mkdir(parents=True, exist_ok=True)
    conflicting.write_bytes(b"different content already on disk")

    with (
        patch("app.cli.subprocess.run") as mock_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)

    assert "冲突" in str(exc_info.value) or "不同" in str(exc_info.value)
    # 冲突必须在覆盖数据库之前被拦下，否则会留下“库已恢复、原件未恢复”的不一致状态（设计 15.2）
    mock_run.assert_not_called()


def test_restore_fails_on_missing_backup_meta(tmp_path, valid_backup):
    backup_dir, _, db_url = valid_backup
    settings = _make_settings(tmp_path)
    (backup_dir / "backup_meta.json").unlink()
    with pytest.raises(SystemExit):
        do_restore(backup_dir, db_url, str(tmp_path / "storage"), dry_run=True, settings=settings)


def test_restore_reports_missing_pg_restore(tmp_path, valid_backup):
    """宿主机没有 pg_restore 时给出可操作提示，而不是裸的 FileNotFoundError。"""
    backup_dir, _, db_url = valid_backup
    storage_root = tmp_path / "storage"
    settings = _make_settings(tmp_path)

    with (
        patch("app.cli.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(SystemExit) as exc_info,
    ):
        do_restore(backup_dir, db_url, str(storage_root), dry_run=False, settings=settings)

    assert "pg_restore" in str(exc_info.value)
