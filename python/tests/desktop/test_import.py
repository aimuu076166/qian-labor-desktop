from pathlib import Path

import pytest

from qian_labor.database import create_database
from qian_labor.desktop.import_service import DesktopImportService
from qian_labor.services.analyses import AnalysisService
from qian_labor.storage.local import LocalStorage


def test_import_copies_only_explicitly_selected_files_and_preserves_original(tmp_path: Path) -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    analysis = AnalysisService(database).create("桌面导入测试", "虚构企业")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    selected = source_dir / "roster.csv"
    unselected = source_dir / "not-selected.csv"
    original = "员工编号,姓名\nF-001,虚构甲\n".encode()
    selected.write_bytes(original)
    unselected.write_bytes("员工编号,姓名\nF-999,不应导入\n".encode())

    service = DesktopImportService(database, tmp_path / "app-data")
    imported = service.import_paths(analysis.id, [selected])

    assert len(imported) == 1
    assert imported[0].original_filename == "roster.csv"
    assert selected.read_bytes() == original
    storage = LocalStorage(str(tmp_path / "app-data" / "storage"))
    assert storage.read_bytes(imported[0].storage_key) == original
    assert not any("not-selected" in path.name for path in (tmp_path / "app-data").rglob("*"))


def test_import_rejects_unsupported_explicit_path_without_modifying_source(tmp_path: Path) -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    analysis = AnalysisService(database).create("桌面导入拒绝测试", "虚构企业")
    source = tmp_path / "source.exe"
    original = b"MZ synthetic executable"
    source.write_bytes(original)

    service = DesktopImportService(database, tmp_path / "app-data")

    with pytest.raises(ValueError):
        service.import_paths(analysis.id, [source])
    assert source.read_bytes() == original
