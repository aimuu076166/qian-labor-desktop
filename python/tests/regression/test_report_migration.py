import json
from pathlib import Path
import sqlite3

import pytest

from qian_labor.database import create_desktop_database
from qian_labor import sqlite_migrations as migration


def frozen(folder, version=6):
    path = folder / "qian-labor.db"
    ddl = json.loads(Path(__file__).with_name(f"frozen_v{version}_schema.json").read_text())
    with sqlite3.connect(path) as connection:
        for sql in sorted(ddl, key=lambda value: 0 if "CREATE TABLE" in value else 1):
            connection.execute(sql)
        connection.execute(f"PRAGMA user_version={version}")
    return path


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6])
def test_frozen_predecessors_upgrade_to_report_schema_and_restart(tmp_path, version):
    frozen(tmp_path, version)
    database = create_desktop_database(tmp_path)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 7
        assert connection.exec_driver_sql("SELECT count(*) FROM report_versions").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM report_generation_requests").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    database.dispose()
    create_desktop_database(tmp_path).dispose()


def test_report_upgrade_failure_preserves_exact_v6_and_private_backup(tmp_path, monkeypatch):
    path = frozen(tmp_path)
    before = path.read_bytes()
    def fail(connection):
        connection.exec_driver_sql("CREATE TABLE synthetic_partial_report(id INTEGER)")
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(migration, "_upgrade_v6", fail, raising=False)
    with pytest.raises(migration.MigrationError, match="DESKTOP_DB_MIGRATION_FAILED"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before
    with sqlite3.connect(tmp_path / migration.RECOVERY_DIRECTORY / "before-v1.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
