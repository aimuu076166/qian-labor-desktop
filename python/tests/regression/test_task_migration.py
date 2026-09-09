import json
from pathlib import Path
import sqlite3

import pytest

from qian_labor.database import create_desktop_database
from qian_labor import sqlite_migrations as migration


def frozen_v5(folder):
    path = folder / "qian-labor.db"
    ddl = json.loads(Path(__file__).with_name("frozen_v5_schema.json").read_text())
    with sqlite3.connect(path) as connection:
        for sql in sorted(ddl, key=lambda value: 0 if "CREATE TABLE" in value else 1):
            connection.execute(sql)
        connection.execute("PRAGMA user_version=5")
    return path


def test_actual_v5_additive_task_migration_and_restart(tmp_path):
    frozen_v5(tmp_path)
    db = create_desktop_database(tmp_path)
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 7
        assert connection.exec_driver_sql("SELECT count(*) FROM task_runs").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM effective_fact_revisions").scalar_one() == 0
    db.dispose()
    create_desktop_database(tmp_path).dispose()


def test_task_migration_failure_retains_v5_backup_and_exact_original(tmp_path, monkeypatch):
    path = frozen_v5(tmp_path)
    before = path.read_bytes()
    def fail(connection):
        connection.exec_driver_sql("CREATE TABLE synthetic_partial(id INTEGER)")
        raise RuntimeError("synthetic")
    monkeypatch.setattr(migration, "_upgrade_v5", fail, raising=False)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_FAILED$"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before
    with sqlite3.connect(tmp_path / migration.RECOVERY_DIRECTORY / "before-v1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


def test_populated_actual_v5_effective_lineage_is_unchanged_after_upgrade(tmp_path):
    from uuid import uuid4
    from test_sqlite_migrations import legacy_database, database_snapshot
    from qian_labor.database import create_database
    from qian_labor.models.core import CompanyWorkspace, EmployeeRecord, EffectiveFactRevision, AssessmentDecision, AssessmentResult
    path = frozen_v5(tmp_path)
    seed = legacy_database(tmp_path / "synthetic-seed")
    with sqlite3.connect(seed) as source, sqlite3.connect(path) as destination:
        for table, rows in database_snapshot(seed).items():
            if rows:
                columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
                destination.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
    db = create_database(f"sqlite:///{path}")
    company_id, record_id, revision_id = (str(uuid4()) for _ in range(3))
    with db.session() as session:
        session.add(CompanyWorkspace(id=company_id, display_name="合成v5企业"))
        session.flush()
        session.add(EmployeeRecord(id=record_id, company_id=company_id, masked_name="合成员工", employee_number="SYN-V5"))
        session.flush()
        session.add(EffectiveFactRevision(id=revision_id, fact_id="synthetic-employment_facts",
            analysis_id="synthetic-analysis_batches", company_id=company_id,
            employee_id="synthetic-employees", record_id=record_id, version=1,
            kind="correct", value=False, reason="合成原始修订保留", owner_signature="a" * 64, source_signature="b" * 64))
        session.add(AssessmentDecision(id=str(uuid4()), analysis_id="synthetic-analysis_batches", company_id=company_id,
            scope="check_date", kind="check_date", version=1, value="2026-09-01", reason="合成日期"))
        session.add(AssessmentResult(id=str(uuid4()), analysis_id="synthetic-analysis_batches",
            input_revision="c" * 64, check_date="2026-09-01"))
        session.commit()
    with db.engine.connect() as connection:
        assert migration._fingerprint(connection) in migration.V5_SCHEMA_SHA256
    db.dispose()
    before = database_snapshot(path)
    upgraded = create_desktop_database(tmp_path)
    after = database_snapshot(path)
    assert {table: after[table] for table in before} == before
    assert after["task_runs"] == [] and after["task_requests"] == []
    upgraded.dispose()
    create_desktop_database(tmp_path).dispose()
