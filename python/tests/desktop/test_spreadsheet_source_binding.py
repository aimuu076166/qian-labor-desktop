from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from qian_labor.ai.schemas import EmploymentFact, ExtractionResult, SourceLocator
from qian_labor.desktop.app import create_desktop_app
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import SourceLocator as StoredSource
from qian_labor.parsers.protocols import ParsedBlock, ParsedDocument
from qian_labor.parsers.registry import ParserRegistry
from qian_labor.storage.local import LocalStorage


def result(employee_id: str | None = "QA-907") -> ExtractionResult:
    return ExtractionResult(facts=[EmploymentFact(
        employee_id=employee_id,
        fact_type="employment.contract.exists",
        value=False,
        confidence=0.9,
        source=SourceLocator(file_name="invented.csv", row=1, column="wrong", excerpt="模型改写的说明"),
    )])


def test_provider_input_keeps_parser_owned_spreadsheet_coordinates() -> None:
    content = "员工编号,合同存在\nQA-907,false\n".encode()
    parsed = ParserRegistry().parse("synthetic.csv", content)
    text = ProcessingPipeline._extraction_inputs("synthetic.csv", content, parsed)[0][1].decode()
    assert '"sheet": "CSV"' in text
    assert '"row": 2' in text


def test_model_row_one_is_rebound_to_actual_data_row_and_original_excerpt() -> None:
    parsed = ParserRegistry().parse("synthetic.csv", "员工编号,合同存在\nQA-907,false\n".encode())
    extracted = result()
    ProcessingPipeline._bind_spreadsheet_sources(extracted, parsed, "synthetic.csv")
    source = extracted.facts[0].source
    assert (source.file_name, source.sheet, source.row) == ("synthetic.csv", "CSV", 2)
    assert source.excerpt == "QA-907 | false"
    assert source.column is None


def test_employee_identifier_selects_actual_row_not_model_claim_or_substring() -> None:
    parsed = ParserRegistry().parse("synthetic.csv", "员工编号,合同存在\nQA-9070,true\nQA-907,false\n".encode())
    extracted = result()
    ProcessingPipeline._bind_spreadsheet_sources(extracted, parsed, "synthetic.csv")
    assert extracted.facts[0].source.row == 3


def test_ambiguous_rows_do_not_publish_unverified_coordinates() -> None:
    parsed = ParserRegistry().parse("synthetic.csv", "员工编号,合同存在\nQA-907,true\nQA-907,false\n".encode())
    extracted = result()
    ProcessingPipeline._bind_spreadsheet_sources(extracted, parsed, "synthetic.csv")
    assert extracted.facts[0].source.row is None
    assert extracted.facts[0].source.excerpt == ""
    assert extracted.facts[0].needs_human_confirmation


def test_excel_cells_are_grouped_by_sheet_and_row() -> None:
    parsed = ParsedDocument("spreadsheet", [
        ParsedBlock("QA-9070", "cell", {"sheet": "工资", "row": 2, "column": 1}),
        ParsedBlock("QA-907", "cell", {"sheet": "合同", "row": 7, "column": 1}),
        ParsedBlock("false", "cell", {"sheet": "合同", "row": 7, "column": 2}),
    ])
    extracted = result()
    ProcessingPipeline._bind_spreadsheet_sources(extracted, parsed, "synthetic.xlsx")
    assert (extracted.facts[0].source.sheet, extracted.facts[0].source.row) == ("合同", 7)
    assert extracted.facts[0].source.excerpt == "QA-907 | false"


def test_pipeline_persists_parser_coordinates_instead_of_model_coordinates(tmp_path: Path) -> None:
    class WrongRowProvider:
        name = "source-binding-test"
        is_external = False

        def extract(self, filename: str, content: bytes) -> ExtractionResult:
            return result()

    token = "synthetic-source-binding-token"
    headers = {"X-Qian-Desktop-Token": token}
    source = tmp_path / "synthetic.csv"
    source.write_text("员工编号,合同存在\nQA-907,false\n", encoding="utf-8")
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=token)
    with TestClient(app) as client:
        analysis_id = client.post("/api/analyses", headers=headers, json={
            "name": "虚构来源核验", "company_display_name": "虚构企业",
        }).json()["id"]
        assert client.post(f"/api/analyses/{analysis_id}/import-paths", headers=headers,
                           json={"paths": [str(source)]}).status_code == 200
        ProcessingPipeline(app.state.database, LocalStorage(str(app.state.storage_root)),
                           WrongRowProvider()).process(analysis_id)
        with app.state.database.session() as session:
            stored = session.scalar(select(StoredSource).where(StoredSource.analysis_id == analysis_id))
            assert stored is not None
            assert stored.location == {"sheet": "CSV", "row": 2}
            assert stored.excerpt == "QA-907 | false"
