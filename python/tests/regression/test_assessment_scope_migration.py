"""Versioned scope migration against frozen synthetic v0/v1 schemas."""
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from qian_labor.database import create_desktop_database
from qian_labor.services.analyses import AnalysisService
from qian_labor import sqlite_migrations as migration
from test_sqlite_migrations import legacy_database, database_snapshot


@pytest.mark.parametrize("version", [0, 1])
def test_old_analysis_scope_preserved_on_upgrade_and_restart(tmp_path, version):
    path = legacy_database(tmp_path)
    if version == 1:
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE risk_findings ADD COLUMN retired_at DATETIME")
            conn.execute("CREATE INDEX ix_risk_findings_analysis_current ON risk_findings (analysis_id, is_current)")
            conn.execute("PRAGMA user_version=1")
    before = database_snapshot(path)
    db = create_desktop_database(tmp_path)
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == 7
        assert conn.exec_driver_sql("SELECT assessment_profile FROM analysis_batches").scalar() == "legacy_full_v1"
    after = database_snapshot(path)
    after["analysis_batches"] = [row[:-1] for row in after["analysis_batches"]]
    if version == 0:
        after["risk_findings"] = [row[:-2] for row in after["risk_findings"]]
    assert {table: after[table] for table in before} == before
    db.dispose()
    reopened = create_desktop_database(tmp_path)
    reopened.dispose()


def test_fresh_profile_is_immutable_at_sql_boundary(tmp_path):
    db = create_desktop_database(tmp_path)
    analysis = AnalysisService(db).create("合成", "合成", assessment_profile="labor_materials_v1")
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE analysis_batches SET assessment_profile='legacy_full_v1' WHERE id=:id"), {"id": analysis.id})
    assert AnalysisService(db).get(analysis.id).assessment_profile == "labor_materials_v1"


def test_failed_scope_upgrade_retains_backup_and_blocks_restart(tmp_path, monkeypatch):
    path = legacy_database(tmp_path)
    before = database_snapshot(path)
    def fail(conn):
        raise RuntimeError("synthetic-failure")
    monkeypatch.setattr(migration, "_upgrade_v1", fail)
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_MIGRATION_FAILED"):
        create_desktop_database(tmp_path)
    assert database_snapshot(path) == before
    recovery = tmp_path / migration.RECOVERY_DIRECTORY
    assert database_snapshot(recovery / "before-v1.sqlite3") == before
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_RECOVERY_REQUIRED"):
        create_desktop_database(tmp_path)


def test_unknown_schema_version_is_not_modified(tmp_path):
    path = legacy_database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_VERSION_UNSUPPORTED"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before


def test_version_one_without_known_schema_is_not_reinterpreted_as_new(tmp_path):
    path = tmp_path / "qian-labor.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_SCHEMA_UNSUPPORTED"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before
