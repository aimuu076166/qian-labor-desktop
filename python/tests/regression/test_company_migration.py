"""Every frozen shipped SQLite form migrates additively, without adopting data."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import text

from qian_labor.database import create_desktop_database
from qian_labor import sqlite_migrations as migration
from test_sqlite_migrations import legacy_database, database_snapshot


def old_database(folder, form):
    if form in {"v0", "v1-altered", "v2-altered"}:
        path = legacy_database(folder)
        with sqlite3.connect(path) as c:
            if form != "v0":
                c.execute("ALTER TABLE risk_findings ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1")
                c.execute("ALTER TABLE risk_findings ADD COLUMN retired_at DATETIME")
                c.execute("CREATE INDEX ix_risk_findings_analysis_current ON risk_findings (analysis_id, is_current)")
                c.execute("PRAGMA user_version=1")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "qian-labor.db"
        ddl = json.loads(Path(__file__).with_name("frozen_v2_schema.json").read_text())
        with sqlite3.connect(path) as c:
            for sql in sorted(ddl, key=lambda s: 0 if s.startswith("CREATE TABLE") or s.startswith("\nCREATE TABLE") else 1):
                if "CREATE TRIGGER" in sql:
                    continue
                # Frozen v2 before this slice, never regenerated from current models.
                if form != "v2-fresh":
                    sql = sql.replace("\tassessment_profile VARCHAR(40) DEFAULT 'legacy_full_v1' NOT NULL, \n", "")
                c.execute(sql)
            c.execute("PRAGMA user_version=1")
        # Populate fresh-schema lineages with the same frozen legacy FK graph.
        # This verifies real IDs/facts/source/review preservation for every form.
        seed = legacy_database(folder / "synthetic-seed")
        with sqlite3.connect(seed) as source, sqlite3.connect(path) as c:
            for table, rows in database_snapshot(seed).items():
                if not rows:
                    continue
                columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
                c.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
    with sqlite3.connect(path) as c:
        if form.startswith("v2"):
            if form != "v2-fresh":
                c.execute("ALTER TABLE analysis_batches ADD COLUMN assessment_profile VARCHAR(40) NOT NULL DEFAULT 'legacy_full_v1'")
            c.execute("CREATE TRIGGER assessment_profile_immutable BEFORE UPDATE OF assessment_profile ON analysis_batches WHEN NEW.assessment_profile IS NOT OLD.assessment_profile BEGIN SELECT RAISE(ABORT, 'ASSESSMENT_PROFILE_IMMUTABLE'); END")
            c.execute("CREATE TRIGGER assessment_profile_supported BEFORE INSERT ON analysis_batches WHEN NEW.assessment_profile NOT IN ('legacy_full_v1','labor_materials_v1') OR NEW.assessment_profile IS NULL BEGIN SELECT RAISE(ABORT, 'ASSESSMENT_PROFILE_UNSUPPORTED'); END")
            c.execute("PRAGMA user_version=2")
        rows = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        fp = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        expected = {"v0": {migration.LEGACY_SCHEMA_SHA256}, "v1-altered": migration.V1_SCHEMA_SHA256,
                    "v1-fresh": migration.V1_SCHEMA_SHA256, "v2-altered": migration.V2_SCHEMA_SHA256,
                    "v2-fresh": migration.V2_SCHEMA_SHA256, "v2-from-fresh-v1": migration.V2_SCHEMA_SHA256}
        assert fp in expected[form]
    return path


@pytest.mark.parametrize("form", ["v0", "v1-altered", "v1-fresh", "v2-altered", "v2-fresh", "v2-from-fresh-v1"])
def test_all_frozen_forms_preserve_old_tables(form, tmp_path):
    path = old_database(tmp_path, form)
    before = database_snapshot(path)
    db = create_desktop_database(tmp_path)
    after = database_snapshot(path)
    with db.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA user_version").scalar() == 7
    for table, rows in before.items():
        converted = after[table]
        if table == "analysis_batches" and not form.startswith("v2"):
            converted = [row[:-1] for row in converted]
        if table == "risk_findings" and form == "v0":
            converted = [row[:-2] for row in converted]
        assert converted == rows
    assert after["employee_records"] == [] and after["company_analysis_bindings"] == []
    db.dispose()
    create_desktop_database(tmp_path).dispose()


def test_v2_additive_fault_rolls_back_and_preserves_private_backup(tmp_path, monkeypatch):
    path = old_database(tmp_path, "v2-altered")
    before = database_snapshot(path)
    def fail(c):
        c.exec_driver_sql("CREATE TABLE synthetic_partial (id INTEGER)")
        raise RuntimeError("synthetic-detail-must-not-leak")
    monkeypatch.setattr(migration, "_upgrade_v2", fail, raising=False)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_FAILED$"):
        create_desktop_database(tmp_path)
    assert database_snapshot(path) == before
    assert database_snapshot(tmp_path / migration.RECOVERY_DIRECTORY / "before-v1.sqlite3") == before
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_RECOVERY_REQUIRED"):
        create_desktop_database(tmp_path)


def test_one_current_corpus_per_company_at_sql_boundary(tmp_path):
    from sqlalchemy.exc import IntegrityError
    from qian_labor.models.core import AnalysisBatch, CompanyWorkspace
    db = create_desktop_database(tmp_path)
    with db.session() as s:
        company = CompanyWorkspace(display_name="合成企业")
        s.add(company)
        first, second = AnalysisBatch(name="合成1"), AnalysisBatch(name="合成2")
        s.add_all([first, second])
        s.flush()
        # Direct SQL verifies the schema invariant independently of the service.
        s.execute(text("INSERT INTO company_analysis_bindings (analysis_id,company_id,role,created_at) VALUES (:a,:c,'current','2026-01-01')"), {"a": first.id, "c": company.id})
        s.commit()
        with pytest.raises(IntegrityError):
            s.execute(text("INSERT INTO company_analysis_bindings (analysis_id,company_id,role,created_at) VALUES (:a,:c,'current','2026-01-01')"), {"a": second.id, "c": company.id})
    db.dispose()


@pytest.mark.parametrize("version", [2, 3])
def test_unknown_schema_at_known_version_fails_without_rewrite(tmp_path, version):
    if version == 2:
        path = old_database(tmp_path, "v2-fresh")
    else:
        db = create_desktop_database(tmp_path)
        path = db.path
        db.dispose()
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE synthetic_unknown (id INTEGER)")
    before = path.read_bytes()
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_SCHEMA_UNSUPPORTED"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before
