"""Backup tooling: the sqlite3 backup-API copy, and shell syntax of backup/restore.sh.

The end-to-end Docker drill is in docs/BACKUP_RESTORE.md; these run offline.
"""
import runpy
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts import sqlite_backup

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _seed(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, user_id TEXT)")
    db.executemany("INSERT INTO audit_log (user_id) VALUES (?)", [("bob",), ("ceo",)])
    db.execute("CREATE TABLE permission_state (id INTEGER PRIMARY KEY, revision INTEGER)")
    db.execute("INSERT INTO permission_state VALUES (1, 7)")
    db.commit()
    db.close()


def _dump(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return list(db.iterdump())
    finally:
        db.close()


def test_backup_copy_is_identical_and_openable(tmp_path):
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    sqlite_backup.backup(src, dst)
    assert _dump(dst) == _dump(src)
    db = sqlite3.connect(dst)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("SELECT revision FROM permission_state").fetchone()[0] == 7
    db.close()


def test_backup_sees_committed_writes_while_source_connection_is_open(tmp_path):
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    live = sqlite3.connect(src)
    live.execute("INSERT INTO audit_log (user_id) VALUES ('sec')")
    live.commit()
    live.execute("INSERT INTO audit_log (user_id) VALUES ('uncommitted')")  # open write txn
    sqlite_backup.backup(src, dst)
    live.rollback()
    live.close()
    rows = sqlite3.connect(dst).execute("SELECT user_id FROM audit_log ORDER BY id").fetchall()
    assert rows == [("bob",), ("ceo",), ("sec",)]


def test_missing_source_is_refused_not_created(tmp_path):
    src = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        sqlite_backup.backup(src, tmp_path / "copy.db")
    assert not src.exists() and not (tmp_path / "copy.db").exists()


def test_existing_destination_is_not_overwritten(tmp_path):
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    dst.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        sqlite_backup.backup(src, dst)
    assert dst.read_bytes() == b"keep me"


def test_corrupt_copy_is_deleted_and_reported(tmp_path, monkeypatch):
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    real_connect = sqlite3.connect

    class Source:
        def backup(self, target):
            Path(dst).write_bytes(b"half a page")

        def close(self):
            pass

    class Target:
        def execute(self, sql):
            assert sql == "PRAGMA integrity_check"
            return real_connect(":memory:").execute("SELECT 'row 1 missing from index'")

        def close(self):
            pass

    monkeypatch.setattr(sqlite_backup.sqlite3, "connect",
                        lambda path, uri=False: Source() if uri else Target())
    with pytest.raises(RuntimeError, match="row 1 missing from index"):
        sqlite_backup.backup(src, dst)
    assert not dst.exists()


def test_cli_usage_and_success(tmp_path, capsys):
    assert sqlite_backup.main([]) == 2
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    assert sqlite_backup.main([str(src), str(dst)]) == 0
    assert "sqlite backup ok" in capsys.readouterr().err
    assert _dump(dst) == _dump(src)


def test_runs_as_a_script(tmp_path, monkeypatch):
    src, dst = tmp_path / "rag.db", tmp_path / "copy.db"
    _seed(src)
    monkeypatch.setattr("sys.argv", ["sqlite_backup.py", str(src), str(dst)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPTS / "sqlite_backup.py"), run_name="__main__")
    assert exc.value.code == 0 and dst.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("name", ["backup.sh", "restore.sh"])
def test_shell_scripts_parse(name):
    result = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_restore_refuses_missing_manifest_before_touching_docker(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rag.db").write_bytes(b"x")
    result = subprocess.run(["bash", str(SCRIPTS / "restore.sh"), str(tmp_path), "unused-volume"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "MANIFEST.sha256 is missing" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_restore_refuses_checksum_mismatch_before_touching_docker(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rag.db").write_bytes(b"tampered")
    (tmp_path / "backup-info.txt").write_text("image=x\n")
    (tmp_path / "MANIFEST.sha256").write_text(
        f"{'0' * 64}  backup-info.txt\n{'0' * 64}  data/rag.db\n")
    result = subprocess.run(["bash", str(SCRIPTS / "restore.sh"), str(tmp_path), "unused-volume"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "REFUSING: checksum mismatch" in result.stderr
