"""Frozen schema from the pre-migration desktop model; synthetic databases only."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time

import pytest
from sqlalchemy import select

from qian_labor.database import create_database, create_desktop_database
from qian_labor.models.core import RiskFinding

LEGACY_FINGERPRINT = "17458b92cef351752afe795f4502e1a3380d99ac16185043d3074926c075e6a2"
PRIVATE_RECOVERY_PLATFORM = pytest.mark.skipif(os.name == "nt", reason="Windows private backup ACL not implemented")

def legacy_database(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "qian-labor.db"
    with sqlite3.connect(path) as connection:
        for statement in LEGACY_DDL:
            connection.execute(statement)
        rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        assert hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest() == LEGACY_FINGERPRINT
        for table in ("analysis_batches", "uploaded_files", "employees", "employment_facts",
                      "source_locators", "risk_findings", "finding_reviews"):
            values = {}
            for _, name, kind, required, default, primary in connection.execute(f"PRAGMA table_info({table})"):
                if name == "id": value = f"synthetic-{table}"
                elif name == "analysis_id": value = "synthetic-analysis_batches"
                elif name == "employee_id": value = "synthetic-employees"
                elif name == "file_id": value = "synthetic-uploaded_files"
                elif name == "fact_id": value = "synthetic-employment_facts"
                elif name == "finding_id": value = "synthetic-risk_findings"
                elif name == "assessment_status": value = "requires_human_review"
                elif name == "review_status": value = "reviewed"
                elif not required and not primary: value = None
                elif kind in {"INTEGER", "BOOLEAN", "FLOAT"}: value = 0
                elif kind in {"DATE", "DATETIME"}: value = "2026-01-01 00:00:00"
                elif kind == "JSON": value = "[]"
                else: value = "synthetic-preserved-value"
                values[name] = value
            connection.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    return path


def database_snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}


def test_desktop_new_database_is_versioned_with_lifecycle_fields(tmp_path):
    database = create_desktop_database(tmp_path)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar() == 7
            columns = {row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info(risk_findings)")}
            assert columns["is_current"][3:5] == (1, "1")
            assert "retired_at" in columns
            indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(risk_findings)")}
            assert "ix_risk_findings_analysis_current" in indexes
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert not (tmp_path / ".qian-migration-recovery").exists()
    finally:
        database.dispose()


@PRIVATE_RECOVERY_PLATFORM
def test_known_legacy_upgrade_preserves_all_original_rows_and_defaults(tmp_path):
    path = legacy_database(tmp_path)
    before = database_snapshot(path)
    database = create_desktop_database(tmp_path)
    try:
        with database.session() as session:
            finding = session.scalar(select(RiskFinding))
            assert finding.is_current is True and finding.retired_at is None
            assert finding.review_status == "reviewed" and len(finding.reviews) == 1
        after = database_snapshot(path)
        assert {**{table: after[table] for table in before}, "risk_findings": [row[:-2] for row in after["risk_findings"]],
                "analysis_batches": [row[:-1] for row in after["analysis_batches"]]} == before
    finally:
        database.dispose()
    reopened = create_desktop_database(tmp_path)
    reopened.dispose()
    assert not (tmp_path / ".qian-migration-recovery").exists()


@pytest.mark.parametrize("fault", ["future", "extra_table", "extra_index", "extra_trigger", "partial", "v1_lie"])
def test_unknown_future_and_partially_migrated_schema_are_not_modified(tmp_path, fault):
    from qian_labor import sqlite_migrations as migration
    path = legacy_database(tmp_path)
    with sqlite3.connect(path) as connection:
        if fault == "future": connection.execute("PRAGMA user_version=2")
        elif fault == "extra_table": connection.execute("CREATE TABLE unrelated (id INTEGER)")
        elif fault == "extra_index": connection.execute("CREATE INDEX unrelated ON finding_reviews(note)")
        elif fault == "extra_trigger": connection.execute("CREATE TRIGGER unrelated AFTER UPDATE ON finding_reviews BEGIN SELECT 1; END")
        elif fault == "partial": connection.execute("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
        elif fault == "v1_lie": connection.execute("PRAGMA user_version=1")
    original = path.read_bytes()
    with pytest.raises(migration.MigrationError):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == original
    assert not (tmp_path / ".qian-migration-recovery").exists()


@PRIVATE_RECOVERY_PLATFORM
def test_wal_backup_keeps_uncheckpointed_commits_and_failure_blocks_restart(tmp_path, monkeypatch):
    from qian_labor import sqlite_migrations as migration
    path = legacy_database(tmp_path)
    keeper = sqlite3.connect(path)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("UPDATE finding_reviews SET note='synthetic-WAL-only-review'")
    keeper.commit()
    with sqlite3.connect(path.as_uri() + "?immutable=1", uri=True) as base_only:
        assert base_only.execute("SELECT note FROM finding_reviews").fetchone()[0] != "synthetic-WAL-only-review"
    def fail_after_first_alter(connection):
        connection.exec_driver_sql("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
        raise RuntimeError("synthetic-private-failure-detail")
    monkeypatch.setattr(migration, "_upgrade_v0", fail_after_first_alter)
    try:
        with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_FAILED$"):
            create_desktop_database(tmp_path)
        recovery = tmp_path / ".qian-migration-recovery"
        backup = recovery / "before-v1.sqlite3"
        with sqlite3.connect(backup) as restored:
            assert restored.execute("SELECT note FROM finding_reviews").fetchone()[0] == "synthetic-WAL-only-review"
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("PRAGMA user_version").fetchone() == (0,)
        assert not any(row[1] == "is_current" for row in keeper.execute("PRAGMA table_info(risk_findings)"))
        if os.name != "nt":
            assert recovery.stat().st_mode & 0o777 == 0o700
            assert all(item.stat().st_mode & 0o777 == 0o600 for item in recovery.iterdir())
        with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_RECOVERY_REQUIRED$"):
            create_desktop_database(tmp_path)
    finally:
        keeper.close()


@pytest.mark.parametrize("phase", ["backup", "create", "upgrade", "cleanup"])
@PRIVATE_RECOVERY_PLATFORM
def test_faults_fail_closed_and_never_auto_restore(tmp_path, monkeypatch, phase):
    from qian_labor import sqlite_migrations as migration
    if phase != "create": legacy_database(tmp_path)
    def fail(*args):
        raise OSError("synthetic-private-path")
    target = {"backup": "_backup_database", "create": "_create_v1", "upgrade": "_upgrade_v0", "cleanup": "_remove_recovery"}[phase]
    monkeypatch.setattr(migration, target, fail)
    with pytest.raises(migration.MigrationError):
        create_desktop_database(tmp_path)
    if phase != "create":
        assert (tmp_path / ".qian-migration-recovery").exists()
        with sqlite3.connect(tmp_path / "qian-labor.db") as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (7 if phase == "cleanup" else 0,)
        with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_RECOVERY_REQUIRED$"):
            create_desktop_database(tmp_path)


def test_new_schema_ddl_failure_rolls_back_all_tables_and_version(tmp_path, monkeypatch):
    from qian_labor import sqlite_migrations as migration
    def incomplete(connection):
        connection.exec_driver_sql("CREATE TABLE synthetic_partial (id INTEGER)")
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(migration, "_create_v1", incomplete)
    with pytest.raises(migration.MigrationError): create_desktop_database(tmp_path)
    with sqlite3.connect(tmp_path / "qian-labor.db") as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)


@PRIVATE_RECOVERY_PLATFORM
def test_foreign_key_corruption_is_rejected_before_backup(tmp_path):
    from qian_labor import sqlite_migrations as migration
    path = legacy_database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE finding_reviews SET finding_id='synthetic-missing'")
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_INTEGRITY_FAILED$"):
        create_desktop_database(tmp_path)
    assert not (tmp_path / ".qian-migration-recovery").exists()


@pytest.mark.parametrize("kind", ["empty_directory", "file", "symlink"])
@pytest.mark.parametrize("current", [False, True])
def test_any_recovery_residue_blocks_even_new_or_current_schema(tmp_path, kind, current):
    from qian_labor import sqlite_migrations as migration
    if current:
        database = create_desktop_database(tmp_path)
        database.dispose()
    recovery = tmp_path / ".qian-migration-recovery"
    if kind == "empty_directory": recovery.mkdir()
    elif kind == "file": recovery.touch()
    else: recovery.symlink_to(tmp_path / "missing-target")
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_RECOVERY_REQUIRED$"):
        create_desktop_database(tmp_path)
    assert (tmp_path / "qian-labor.db").exists() is current


def _hold_lock(folder, ready, release):
    from qian_labor.sqlite_migrations import migration_lock
    with migration_lock(Path(folder), timeout_seconds=0.2):
        ready.set()
        release.wait(timeout=5)


def test_cross_process_lock_has_bounded_wait(tmp_path):
    from qian_labor import sqlite_migrations as migration
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(timeout=3)
        started = time.monotonic()
        with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_BUSY$"):
            with migration.migration_lock(tmp_path, timeout_seconds=0.1):
                pytest.fail("another process already owns migration lock")
        assert time.monotonic() - started < 1
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive(): process.terminate()
    assert process.exitcode == 0


def test_generic_in_memory_database_remains_compatible():
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    with database.engine.connect() as connection:
        assert "is_current" in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(risk_findings)")}
    database.dispose()


def test_unverified_private_acl_platform_refuses_old_upgrade_but_allows_new_database(tmp_path, monkeypatch):
    from qian_labor import sqlite_migrations as migration
    old_folder = tmp_path / "legacy"
    path = legacy_database(old_folder)
    original = path.read_bytes()
    monkeypatch.setattr(migration, "_supports_private_recovery", lambda: False)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_PLATFORM_UNSUPPORTED$"):
        create_desktop_database(old_folder)
    assert path.read_bytes() == original
    assert not (old_folder / ".qian-migration-recovery").exists()
    fresh = create_desktop_database(tmp_path / "new")
    fresh.dispose()


@pytest.mark.parametrize("stage", ["manifest_unlink", "backup_unlink", "directory_remove"])
@PRIVATE_RECOVERY_PLATFORM
def test_partial_cleanup_keeps_remaining_artifacts_and_blocks_restart(tmp_path, monkeypatch, stage):
    from qian_labor import sqlite_migrations as migration
    legacy_database(tmp_path)
    original_unlink, original_rmdir = Path.unlink, Path.rmdir
    def unlink(path, *args, **kwargs):
        selected = "migration.json" if stage == "manifest_unlink" else "before-v1.sqlite3"
        if stage != "directory_remove" and path.name == selected:
            raise OSError("synthetic unlink failure")
        return original_unlink(path, *args, **kwargs)
    def rmdir(path):
        if stage == "directory_remove" and path.name == ".qian-migration-recovery":
            raise OSError("synthetic rmdir failure")
        return original_rmdir(path)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_FAILED$"):
        create_desktop_database(tmp_path)
    recovery = tmp_path / ".qian-migration-recovery"
    remaining = {path.name for path in recovery.iterdir()}
    assert remaining == ({"before-v1.sqlite3", "migration.json"} if stage == "manifest_unlink"
                         else {"before-v1.sqlite3"} if stage == "backup_unlink" else set())
    with sqlite3.connect(tmp_path / "qian-labor.db") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_RECOVERY_REQUIRED$"):
        create_desktop_database(tmp_path)


def _crash_upgrade(folder):
    from qian_labor import sqlite_migrations as migration
    def crash(connection):
        connection.exec_driver_sql("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
        os._exit(93)
    migration._upgrade_v0 = crash
    create_desktop_database(Path(folder))


@PRIVATE_RECOVERY_PLATFORM
def test_process_interruption_leaves_backup_blocks_startup_and_rolls_back_ddl(tmp_path):
    from qian_labor import sqlite_migrations as migration
    path = legacy_database(tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_upgrade, args=(str(tmp_path),))
    process.start()
    process.join(timeout=5)
    if process.is_alive(): process.terminate(); process.join(timeout=2)
    assert process.exitcode == 93
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_RECOVERY_REQUIRED$"):
        create_desktop_database(tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert not any(row[1] == "is_current" for row in connection.execute("PRAGMA table_info(risk_findings)"))
    backup = tmp_path / ".qian-migration-recovery" / "before-v1.sqlite3"
    with sqlite3.connect(backup) as source, sqlite3.connect(tmp_path / "synthetic-restored.db") as restored:
        source.backup(restored)
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT COUNT(*) FROM finding_reviews").fetchone() == (1,)


def _start_desktop(folder, start, results):
    start.wait(timeout=3)
    try:
        database = create_desktop_database(Path(folder))
        database.dispose()
        results.put("ok")
    except Exception as error:
        results.put(type(error).__name__)


@PRIVATE_RECOVERY_PLATFORM
def test_two_processes_can_start_without_duplicate_upgrade_or_data_loss(tmp_path):
    path = legacy_database(tmp_path)
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_start_desktop, args=(str(tmp_path), start, results)) for _ in range(2)]
    for process in processes: process.start()
    start.set()
    try:
        assert [results.get(timeout=5) for _ in processes] == ["ok", "ok"]
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive(): process.terminate(); process.join(timeout=2)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone() == (1,)
    assert not (tmp_path / ".qian-migration-recovery").exists()


def test_bootstrap_refuses_an_unexpected_temporary_schema_object(tmp_path):
    from qian_labor import sqlite_migrations as migration
    path = legacy_database(tmp_path)
    database = create_database(f"sqlite+pysqlite:///{path}")
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("CREATE TEMP TABLE synthetic_unrelated (id INTEGER)")
            connection.commit()
        with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_SCHEMA_UNSUPPORTED$"):
            migration.bootstrap_desktop_database(database.engine, path)
        assert not (tmp_path / ".qian-migration-recovery").exists()
    finally:
        database.dispose()


@PRIVATE_RECOVERY_PLATFORM
def test_private_file_permission_failure_closes_descriptor(tmp_path, monkeypatch):
    from qian_labor import sqlite_migrations as migration
    descriptors = []
    original_open = os.open
    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor
    def failure(*args):
        raise OSError("synthetic permission failure")
    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fchmod", failure)
    with pytest.raises(OSError):
        migration._exclusive_file(tmp_path / "synthetic-private-file")
    with pytest.raises(OSError):
        os.fstat(descriptors[-1])


# Do not derive the old schema from the current ORM model: that hides upgrade drift.
LEGACY_DDL = [
    "CREATE TABLE ai_usage_records (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tfile_id VARCHAR(36), \n\tprovider VARCHAR(50) NOT NULL, \n\tmodel VARCHAR(100) NOT NULL, \n\toperation VARCHAR(50) NOT NULL, \n\tinput_units INTEGER NOT NULL, \n\toutput_units INTEGER NOT NULL, \n\testimated_cost_usd FLOAT NOT NULL, \n\tlatency_ms INTEGER NOT NULL, \n\tattempt INTEGER NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tidempotency_key VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (analysis_id, idempotency_key), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE\n)",
    "CREATE TABLE analysis_batches (\n\tid VARCHAR(36) NOT NULL, \n\tname VARCHAR(200) NOT NULL, \n\tcompany_display_name VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tis_demo BOOLEAN NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tcompleted_at DATETIME, \n\tpurge_at DATETIME NOT NULL, \n\tdeleted_at DATETIME, \n\tfile_count INTEGER NOT NULL, \n\temployee_count INTEGER NOT NULL, \n\thigh_count INTEGER NOT NULL, \n\tmedium_count INTEGER NOT NULL, \n\tlow_count INTEGER NOT NULL, \n\tinsufficient_data_count INTEGER NOT NULL, \n\tcoverage_rate FLOAT NOT NULL, \n\tprogress INTEGER NOT NULL, \n\tcurrent_stage VARCHAR(64) NOT NULL, \n\tfailure_reason VARCHAR(200), \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE audit_events (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tevent_type VARCHAR(80) NOT NULL, \n\tactor VARCHAR(80) NOT NULL, \n\tmetadata_json JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE\n)",
    "CREATE TABLE deletion_tombstones (\n\tanalysis_id VARCHAR(36) NOT NULL, \n\tdeleted_at DATETIME NOT NULL, \n\tdeletion_reason VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (analysis_id)\n)",
    "CREATE TABLE employee_match_candidates (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tfile_id VARCHAR(36), \n\tcandidate_employee_id VARCHAR(36), \n\textracted_fields JSON NOT NULL, \n\tscore FLOAT NOT NULL, \n\treason VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE, \n\tFOREIGN KEY(candidate_employee_id) REFERENCES employees (id) ON DELETE CASCADE\n)",
    "CREATE TABLE employee_match_decisions (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tcandidate_id VARCHAR(36), \n\tdecision VARCHAR(32) NOT NULL, \n\ttarget_employee_id VARCHAR(36), \n\tcorrected_fields JSON NOT NULL, \n\tactor VARCHAR(80) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_employee_match_decision_candidate UNIQUE (candidate_id), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(candidate_id) REFERENCES employee_match_candidates (id) ON DELETE SET NULL, \n\tFOREIGN KEY(target_employee_id) REFERENCES employees (id) ON DELETE SET NULL\n)",
    "CREATE TABLE employees (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tmasked_name VARCHAR(100) NOT NULL, \n\tnormalized_name VARCHAR(100) NOT NULL, \n\temployee_number VARCHAR(80), \n\tid_number_hash VARCHAR(64), \n\tphone_hash VARCHAR(64), \n\tbank_card_hash VARCHAR(64), \n\tdepartment VARCHAR(100), \n\tjob_title VARCHAR(100), \n\temployment_status VARCHAR(32) NOT NULL, \n\thire_date DATE, \n\ttermination_date DATE, \n\tmatch_status VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (analysis_id, employee_number), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE\n)",
    "CREATE TABLE employment_facts (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\temployee_id VARCHAR(36), \n\tfile_id VARCHAR(36), \n\tfact_type VARCHAR(120) NOT NULL, \n\tvalue_json JSON NOT NULL, \n\tnormalized_value_json JSON NOT NULL, \n\textraction_method VARCHAR(32) NOT NULL, \n\tconfidence FLOAT NOT NULL, \n\tverification_status VARCHAR(32) NOT NULL, \n\tdedupe_key VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (analysis_id, dedupe_key), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE, \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE\n)",
    "CREATE TABLE finding_reviews (\n\tid VARCHAR(36) NOT NULL, \n\tfinding_id VARCHAR(36) NOT NULL, \n\tactor VARCHAR(80) NOT NULL, \n\told_status VARCHAR(32) NOT NULL, \n\tnew_status VARCHAR(32) NOT NULL, \n\tnote VARCHAR(500) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(finding_id) REFERENCES risk_findings (id) ON DELETE CASCADE\n)",
    "CREATE TABLE parsed_blocks (\n\tid VARCHAR(36) NOT NULL, \n\tdocument_id VARCHAR(36) NOT NULL, \n\tposition INTEGER NOT NULL, \n\tblock_type VARCHAR(32) NOT NULL, \n\ttext TEXT NOT NULL, \n\tlocator JSON NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(document_id) REFERENCES parsed_documents (id) ON DELETE CASCADE\n)",
    "CREATE TABLE parsed_documents (\n\tid VARCHAR(36) NOT NULL, \n\tfile_id VARCHAR(36) NOT NULL, \n\tparser_name VARCHAR(80) NOT NULL, \n\tparser_version VARCHAR(32) NOT NULL, \n\tdetected_kind VARCHAR(32) NOT NULL, \n\tneeds_vision BOOLEAN NOT NULL, \n\twarnings JSON NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (file_id), \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE\n)",
    "CREATE TABLE processing_jobs (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tfile_id VARCHAR(36), \n\tjob_type VARCHAR(40) NOT NULL, \n\tinput_hash VARCHAR(64) NOT NULL, \n\tunique_key VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\terror_code VARCHAR(100), \n\tstarted_at DATETIME, \n\tcompleted_at DATETIME, \n\tPRIMARY KEY (id), \n\tUNIQUE (unique_key), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE\n)",
    "CREATE TABLE risk_findings (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\temployee_id VARCHAR(36), \n\trule_id VARCHAR(80) NOT NULL, \n\trule_version VARCHAR(32) NOT NULL, \n\tcategory VARCHAR(50) NOT NULL, \n\tseverity VARCHAR(16) NOT NULL, \n\tassessment_status VARCHAR(32) NOT NULL, \n\treview_status VARCHAR(32) NOT NULL, \n\ttitle VARCHAR(200) NOT NULL, \n\tsummary TEXT NOT NULL, \n\ttrigger_fact_ids JSON NOT NULL, \n\tsource_locator_ids JSON NOT NULL, \n\tmissing_fact_types JSON NOT NULL, \n\tlegal_basis JSON NOT NULL, \n\trecommended_actions JSON NOT NULL, \n\trequires_human_review BOOLEAN NOT NULL, \n\tdue_date DATE, \n\tversion INTEGER NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (analysis_id, employee_id, rule_id, rule_version), \n\tCONSTRAINT ck_risk_findings_assessment_status CHECK (assessment_status IN ('management_reminder', 'confirmed_anomaly', 'suspected_risk', 'insufficient_data', 'requires_human_review')), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE\n)",
    "CREATE TABLE source_locators (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\tfile_id VARCHAR(36) NOT NULL, \n\tfact_id VARCHAR(36), \n\tlocator_type VARCHAR(24) NOT NULL, \n\tlocation JSON NOT NULL, \n\texcerpt TEXT NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(file_id) REFERENCES uploaded_files (id) ON DELETE CASCADE, \n\tFOREIGN KEY(fact_id) REFERENCES employment_facts (id) ON DELETE CASCADE\n)",
    "CREATE TABLE uploaded_files (\n\tid VARCHAR(36) NOT NULL, \n\tanalysis_id VARCHAR(36) NOT NULL, \n\toriginal_filename VARCHAR(255) NOT NULL, \n\tstorage_key VARCHAR(255) NOT NULL, \n\tmime_type VARCHAR(100) NOT NULL, \n\textension VARCHAR(12) NOT NULL, \n\tsize_bytes INTEGER NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tdetected_kind VARCHAR(32) NOT NULL, \n\tclassified_kind VARCHAR(32) NOT NULL, \n\tpage_count INTEGER, \n\tprogress INTEGER NOT NULL, \n\terror_code VARCHAR(80), \n\tpurge_at DATETIME, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (analysis_id, sha256), \n\tFOREIGN KEY(analysis_id) REFERENCES analysis_batches (id) ON DELETE CASCADE, \n\tUNIQUE (storage_key)\n)",
    "CREATE INDEX ix_ai_usage_records_analysis_id ON ai_usage_records (analysis_id)",
    "CREATE INDEX ix_analysis_batches_status ON analysis_batches (status)",
    "CREATE INDEX ix_audit_events_analysis_id ON audit_events (analysis_id)",
    "CREATE INDEX ix_employee_match_candidates_analysis_id ON employee_match_candidates (analysis_id)",
    "CREATE INDEX ix_employee_match_decisions_analysis_id ON employee_match_decisions (analysis_id)",
    "CREATE INDEX ix_employees_analysis_id ON employees (analysis_id)",
    "CREATE INDEX ix_employment_facts_analysis_id ON employment_facts (analysis_id)",
    "CREATE INDEX ix_employment_facts_employee_id ON employment_facts (employee_id)",
    "CREATE INDEX ix_employment_facts_fact_type ON employment_facts (fact_type)",
    "CREATE INDEX ix_finding_reviews_finding_id ON finding_reviews (finding_id)",
    "CREATE INDEX ix_parsed_blocks_document_id ON parsed_blocks (document_id)",
    "CREATE INDEX ix_processing_jobs_analysis_id ON processing_jobs (analysis_id)",
    "CREATE INDEX ix_processing_jobs_status ON processing_jobs (status)",
    "CREATE INDEX ix_risk_findings_analysis_id ON risk_findings (analysis_id)",
    "CREATE INDEX ix_risk_findings_employee_id ON risk_findings (employee_id)",
    "CREATE INDEX ix_risk_findings_rule_id ON risk_findings (rule_id)",
    "CREATE INDEX ix_source_locators_analysis_id ON source_locators (analysis_id)",
    "CREATE INDEX ix_source_locators_file_id ON source_locators (file_id)",
    "CREATE INDEX ix_uploaded_files_analysis_id ON uploaded_files (analysis_id)"
]
