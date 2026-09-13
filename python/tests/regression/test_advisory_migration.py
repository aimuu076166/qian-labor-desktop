import json
from pathlib import Path
import sqlite3

import pytest

from qian_labor.database import create_desktop_database
from qian_labor import sqlite_migrations as migration


def frozen_v3(folder):
    path = folder / "qian-labor.db"
    ddl = json.loads(Path(__file__).with_name("frozen_v3_schema.json").read_text())
    with sqlite3.connect(path) as c:
        for sql in sorted(ddl, key=lambda value: 0 if "CREATE TABLE" in value else 1):
            c.execute(sql)
        c.execute("PRAGMA user_version=3")
    return path


def test_v3_additive_advisory_migration_and_restart(tmp_path):
    path = frozen_v3(tmp_path)
    before = path.read_bytes()
    db = create_desktop_database(tmp_path)
    with db.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA user_version").scalar_one() == 7
        assert c.exec_driver_sql("SELECT COUNT(*) FROM contract_clause_observations").scalar_one() == 0
    db.dispose()
    create_desktop_database(tmp_path).dispose()
    assert path.read_bytes() != before


def test_advisory_migration_rolls_back_with_private_backup(tmp_path, monkeypatch):
    path = frozen_v3(tmp_path)
    before = path.read_bytes()
    def fail(c):
        c.exec_driver_sql("CREATE TABLE synthetic_partial (id INTEGER)")
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(migration, "_upgrade_v3", fail, raising=False)
    with pytest.raises(migration.MigrationError, match="^DESKTOP_DB_MIGRATION_FAILED$"):
        create_desktop_database(tmp_path)
    assert path.read_bytes() == before
    with sqlite3.connect(tmp_path / migration.RECOVERY_DIRECTORY / "before-v1.sqlite3") as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
