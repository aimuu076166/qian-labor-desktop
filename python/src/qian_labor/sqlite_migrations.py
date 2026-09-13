"""Desktop-only, fail-closed SQLite bootstrap. Never restore a backup automatically."""
from __future__ import annotations

from contextlib import closing, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from qian_labor.models.core import Base

SCHEMA_VERSION = 7
RECOVERY_DIRECTORY = ".qian-migration-recovery"
LOCK_TIMEOUT_SECONDS = 2.0
BACKUP_TIMEOUT_SECONDS = 10.0
# Frozen from the complete pre-migration repository schema, including autoindexes.
LEGACY_SCHEMA_SHA256 = "17458b92cef351752afe795f4502e1a3380d99ac16185043d3074926c075e6a2"
V1_SCHEMA_SHA256 = {
    "1ea4934d1c870e36c10d03392cac1f0db2e30fdeb42e81404289b35a69b005ad",  # fresh create_all
    "d72b63f2a8dfac99862032a4b20b94cdef2fddd5a29006228ed3a50f2f0e5e3b",  # two ALTERs
}
V2_SCHEMA_SHA256 = {
    "2e62fc453e6dba10b57eb6733e7b86776cc6a968532148f817839dea3d2057a8",  # fresh
    "45b5c3cbf525689c74953604b3dad76299c30eb55588d60192d1bad9feffbe93",  # v0 / altered v1
    "e41624826a92bd8a48cfeeae00e2de188f70990e2aa39eae0b2fe6977dba56d3",  # fresh v1 upgrade
}
V3_SCHEMA_SHA256 = {
    "c6044244b89131a995849bf9c8073cca6a045588faa42b271cfeb2cd1b502460",  # fresh / fresh v2
    "8aefb786b333f6b0d49cbd5935f25cd60797181115eb871f4c60b56ebe0d3fd5",  # altered legacy
    "399e6bae15b0208e820a9c3bf9995a37b9e353f002f55cf35b3f6c011f5ff069",  # fresh v1 lineage
}
V4_SCHEMA_SHA256 = {
    "19107cd7fca57ad5766a0b3879e470cbca832ddc9adecc76e2bacde686946887",  # fresh / fresh v2/v3
    "326820b691b8e5071fcbd304edaa53b4195a30135f03956468b44d5553f9d0a3",  # altered legacy
    "7cf12ef4f0519490fea823367cd0e3c8bf300a33a6686981ad099ce33fdfb97f",  # fresh v1 lineage
}
V5_SCHEMA_SHA256 = {
    "36a476e4bd14b6c9d5c39faf658dfe3d660b8e4601b6576888ce36a7be240478",  # fresh / fresh v2/v3/v4
    "ca6d9b307b6954d3ff6ad6e85d7855c68b26397141a7b760360606d8d5f753ac",  # altered legacy
    "a4012c72a2e11c0fcf526a83eb5e3bf8760827c4b6c8e40de94152db579988ff",  # fresh v1 lineage
}
V6_SCHEMA_SHA256 = {
    "bc083d492641b99a1d1eac9a6e56cbae09c3ea45bd62b973e003fbcdae720a07",
    "ab5347c9c35dc12d0c5ae768df0ff14127f143f758ac35996c73d6843d897fc1",
    "99dd278a8c957cb4d84dd33938293d2128b4d490ad2224c8da96f81395153f7b",
}
V7_SCHEMA_SHA256 = {
    "0deceadd95773d4a7018f7fdc3793ac8c4374fd4354cd107faf5dda1774bd477",
    "ad1ec86c3f410a70c34cac545a77bd47718cb6886b0ff52fb4135685be5be581",  # fresh
    "f794866c74529d0061ca8a5566f4a72fa35ab2e858e6c1ab00ec82b965decf84",  # altered legacy
}


class MigrationError(RuntimeError):
    """Public message is always a fixed safe code, never a path or SQLite detail."""


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def assert_no_pending_recovery(data_dir: Path) -> None:
    if _exists(data_dir / RECOVERY_DIRECTORY):
        raise MigrationError("DESKTOP_DB_RECOVERY_REQUIRED")


@contextmanager
def migration_lock(data_dir: Path, *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS):
    path = data_dir / ".qian-migration.lock"
    if path.is_symlink():
        raise MigrationError("DESKTOP_DB_MIGRATION_FAILED")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MigrationError("DESKTOP_DB_MIGRATION_FAILED")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
                if time.monotonic() >= deadline:
                    raise MigrationError("DESKTOP_DB_BUSY") from None
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    # Keep the lock inode: unlinking it permits two processes to lock different files.


def _fingerprint(connection: Connection) -> str:
    rows = [list(row) for row in connection.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    )]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _verify_integrity(connection: Connection) -> None:
    if (connection.exec_driver_sql("PRAGMA integrity_check").fetchall() != [("ok",)]
            or connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()):
        raise MigrationError("DESKTOP_DB_INTEGRITY_FAILED")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _exclusive_file(path: Path) -> int:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _supports_private_recovery() -> bool:
    # chmod is not a private DACL guarantee on Windows. Do not create an
    # unverified additional copy of an existing user's database there.
    return os.name != "nt"


def _prepare_recovery(path: Path, from_version: int = 0) -> Path:
    recovery = path.parent / RECOVERY_DIRECTORY
    recovery.mkdir(mode=0o700)
    if os.name != "nt":
        recovery.chmod(0o700)
    with os.fdopen(_exclusive_file(recovery / "migration.json"), "wb") as output:
        output.write((json.dumps({"from_version": from_version, "to_version": SCHEMA_VERSION, "state": "pending"}) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(recovery)
    _sync_directory(path.parent)
    return recovery


def _backup_database(path: Path, backup: Path) -> None:
    os.close(_exclusive_file(backup))
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise MigrationError("DESKTOP_DB_MIGRATION_FAILED")
    # The caller owns BEGIN IMMEDIATE on a DIFFERENT connection. This reader
    # includes committed WAL pages, while other writers cannot change that state.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as reader:
        with closing(sqlite3.connect(backup)) as writer:
            reader.backup(writer, pages=128, progress=progress, sleep=0.025)
            if (writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or writer.execute("PRAGMA foreign_key_check").fetchall()):
                raise MigrationError("DESKTOP_DB_INTEGRITY_FAILED")
    descriptor = os.open(backup, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(backup.parent)


def _create_v1(connection: Connection) -> None:
    Base.metadata.create_all(connection)
    _scope_triggers(connection)
    _binding_triggers(connection)
    _report_triggers(connection)


def _binding_triggers(connection: Connection) -> None:
    connection.exec_driver_sql("CREATE TRIGGER snapshot_binding_consistent BEFORE INSERT ON employee_snapshot_bindings WHEN NOT EXISTS (SELECT 1 FROM employees WHERE id=NEW.snapshot_id AND analysis_id=NEW.analysis_id) BEGIN SELECT RAISE(ABORT, 'WORKSPACE_BINDING_INVALID'); END")
    connection.exec_driver_sql("CREATE TRIGGER snapshot_binding_immutable BEFORE UPDATE ON employee_snapshot_bindings BEGIN SELECT RAISE(ABORT, 'WORKSPACE_BINDING_IMMUTABLE'); END")
    connection.exec_driver_sql("CREATE TRIGGER company_binding_immutable BEFORE UPDATE ON company_analysis_bindings BEGIN SELECT RAISE(ABORT, 'WORKSPACE_BINDING_IMMUTABLE'); END")


def _upgrade_v2(connection: Connection) -> None:
    for name in ("company_workspaces", "employee_records", "company_analysis_bindings", "employee_snapshot_bindings", "workspace_preferences"):
        Base.metadata.tables[name].create(connection)
    _binding_triggers(connection)


def _upgrade_v3(connection: Connection) -> None:
    for name in ("contract_advisory_runs", "contract_clause_observations", "contract_advisory_handlings"):
        Base.metadata.tables[name].create(connection)


def _upgrade_v4(connection: Connection) -> None:
    for name in ("effective_fact_revisions", "assessment_decisions", "assessment_results"):
        Base.metadata.tables[name].create(connection)


def _upgrade_v5(connection: Connection) -> None:
    for name in ("task_runs", "task_requests"):
        Base.metadata.tables[name].create(connection)


def _report_triggers(connection: Connection) -> None:
    for name in ("report_versions", "report_generation_requests"):
        connection.exec_driver_sql(f"CREATE TRIGGER {name}_immutable BEFORE UPDATE ON {name} BEGIN SELECT RAISE(ABORT, 'REPORT_VERSION_IMMUTABLE'); END")


def _upgrade_v6(connection: Connection) -> None:
    for name in ("report_versions", "report_generation_requests"):
        Base.metadata.tables[name].create(connection)
    _report_triggers(connection)


def _scope_triggers(connection: Connection) -> None:
    connection.exec_driver_sql("CREATE TRIGGER assessment_profile_immutable BEFORE UPDATE OF assessment_profile ON analysis_batches WHEN NEW.assessment_profile IS NOT OLD.assessment_profile BEGIN SELECT RAISE(ABORT, 'ASSESSMENT_PROFILE_IMMUTABLE'); END")
    connection.exec_driver_sql("CREATE TRIGGER assessment_profile_supported BEFORE INSERT ON analysis_batches WHEN NEW.assessment_profile NOT IN ('legacy_full_v1','labor_materials_v1') OR NEW.assessment_profile IS NULL BEGIN SELECT RAISE(ABORT, 'ASSESSMENT_PROFILE_UNSUPPORTED'); END")


def _upgrade_v1(connection: Connection) -> None:
    connection.exec_driver_sql("ALTER TABLE analysis_batches ADD COLUMN assessment_profile VARCHAR(40) NOT NULL DEFAULT 'legacy_full_v1'")
    _scope_triggers(connection)


def _upgrade_v0(connection: Connection) -> None:
    connection.exec_driver_sql("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
    connection.exec_driver_sql("ALTER TABLE risk_findings ADD COLUMN retired_at DATETIME")
    connection.exec_driver_sql(
        "CREATE INDEX ix_risk_findings_analysis_current ON risk_findings (analysis_id, is_current)"
    )


def _remove_recovery(recovery: Path) -> None:
    expected = {"migration.json", "before-v1.sqlite3"}
    if {item.name for item in recovery.iterdir()} != expected:
        raise MigrationError("DESKTOP_DB_RECOVERY_REQUIRED")
    for name in expected:
        path = recovery / name
        if path.is_symlink() or not path.is_file():
            raise MigrationError("DESKTOP_DB_RECOVERY_REQUIRED")
    # A failed unlink leaves every remaining artifact in place and blocks startup.
    (recovery / "migration.json").unlink()
    (recovery / "before-v1.sqlite3").unlink()
    _sync_directory(recovery)
    recovery.rmdir()


def bootstrap_desktop_database(engine: Engine, path: Path) -> None:
    try:
        with migration_lock(path.parent):
            assert_no_pending_recovery(path.parent)
            if path.is_symlink() or (_exists(path) and not path.is_file()):
                raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
            if not path.exists():
                os.close(_exclusive_file(path))
            recovery = None
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout=2000")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                if connection.exec_driver_sql("SELECT name FROM sqlite_temp_master LIMIT 1").fetchall():
                    raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
                version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                if version not in {0, 1, 2, 3, 4, 5, 6, SCHEMA_VERSION}:
                    raise MigrationError("DESKTOP_DB_VERSION_UNSUPPORTED")
                fingerprint = _fingerprint(connection)
                if version == SCHEMA_VERSION:
                    if fingerprint not in V7_SCHEMA_SHA256:
                        raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
                    _verify_integrity(connection)
                    connection.rollback()
                    return
                empty = not connection.exec_driver_sql("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                if empty:
                    if version != 0:
                        raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
                    _create_v1(connection)
                else:
                    if not ((version == 0 and fingerprint == LEGACY_SCHEMA_SHA256)
                            or (version == 1 and fingerprint in V1_SCHEMA_SHA256)
                            or (version == 2 and fingerprint in V2_SCHEMA_SHA256)
                            or (version == 3 and fingerprint in V3_SCHEMA_SHA256)
                            or (version == 4 and fingerprint in V4_SCHEMA_SHA256)
                            or (version == 5 and fingerprint in V5_SCHEMA_SHA256)
                            or (version == 6 and fingerprint in V6_SCHEMA_SHA256)):
                        raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
                    if not _supports_private_recovery():
                        raise MigrationError("DESKTOP_DB_MIGRATION_PLATFORM_UNSUPPORTED")
                    _verify_integrity(connection)
                    recovery = _prepare_recovery(path, version)
                    _backup_database(path, recovery / "before-v1.sqlite3")
                    if version == 0:
                        _upgrade_v0(connection)
                    if version < 2:
                        _upgrade_v1(connection)
                    if version < 3:
                        _upgrade_v2(connection)
                    if version < 4:
                        _upgrade_v3(connection)
                    if version < 5:
                        _upgrade_v4(connection)
                    if version < 6:
                        _upgrade_v5(connection)
                    _upgrade_v6(connection)
                if _fingerprint(connection) not in V7_SCHEMA_SHA256:
                    raise MigrationError("DESKTOP_DB_SCHEMA_UNSUPPORTED")
                _verify_integrity(connection)
                connection.exec_driver_sql("PRAGMA user_version=7")
                connection.commit()
            if recovery is not None:
                _remove_recovery(recovery)
    except MigrationError:
        raise
    except (OperationalError, sqlite3.OperationalError) as error:
        original = getattr(error, "orig", error)
        code = getattr(original, "sqlite_errorcode", 0)
        if code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise MigrationError("DESKTOP_DB_BUSY") from None
        raise MigrationError("DESKTOP_DB_MIGRATION_FAILED") from None
    except Exception:
        raise MigrationError("DESKTOP_DB_MIGRATION_FAILED") from None
