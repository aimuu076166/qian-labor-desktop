import json

import pytest

from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.parsers.protocols import ParsedBlock, ParsedDocument


def test_text_chunks_cover_every_block_without_splitting_block_boundaries():
    blocks = [
        ParsedBlock(f"SYN-{index:03d} " + ("事实段落。" * 1200), "paragraph", {"paragraph": index})
        for index in range(1, 4)
    ]
    parsed = ParsedDocument("docx", blocks)

    inputs = ProcessingPipeline._extraction_inputs("synthetic.docx", b"", parsed)

    assert len(inputs) >= 2
    serialized = [item.content.decode("utf-8") for item in inputs]
    for block in blocks:
        assert sum(block.text in text for text in serialized) == 1
        assert json.dumps(block.locator, ensure_ascii=False) in next(
            text for text in serialized if block.text in text
        )


def test_embedded_vision_input_keeps_parser_owned_context():
    page = type("SyntheticVisionPage", (), {
        "page": 1,
        "media_type": "image/png",
        "image_bytes": b"synthetic-image",
        "width": 10,
        "height": 10,
        "locator": {"paragraph": 2, "image": 1},
    })()
    parsed = ParsedDocument(
        "docx",
        [ParsedBlock("SYN-001 正文", "paragraph", {"paragraph": 1})],
        needs_vision=True,
        vision_pages=[page],
    )

    inputs = ProcessingPipeline._extraction_inputs("embedded.docx", b"", parsed)
    vision = inputs[-1]

    assert vision.filename.endswith("-image-1.png")
    assert vision.content == b"synthetic-image"
    assert vision.blocks[0].locator == {"paragraph": 2, "image": 1}


def test_chunk_boundary_keeps_employee_and_date_in_same_table_row():
    identity = ParsedBlock("SYN-001", "table_cell", {"table": 1, "row": 1, "column": 1})
    value = ParsedBlock("2026-01-01", "table_cell", {"table": 1, "row": 1, "column": 2})
    parsed = ParsedDocument("docx", [
        ParsedBlock("A" * 15_866, "paragraph", {"paragraph": 1}),
        identity,
        value,
    ])

    inputs = ProcessingPipeline._extraction_inputs("synthetic.docx", b"", parsed)

    identity_chunk = next(index for index, item in enumerate(inputs) if identity in item.blocks)
    value_chunk = next(index for index, item in enumerate(inputs) if value in item.blocks)
    assert identity_chunk == value_chunk


@pytest.mark.parametrize(
    ("kind", "row_prefix"),
    [
        ("spreadsheet", {"sheet": "CSV"}),
        ("spreadsheet", {"sheet": "员工"}),
        ("docx", {"table": 1}),
    ],
)
def test_row_atom_is_preserved_for_csv_xlsx_and_docx_table(kind, row_prefix):
    identity = ParsedBlock("SYN-001", "cell" if kind == "spreadsheet" else "table_cell",
                           {**row_prefix, "row": 2, "column": 1})
    value = ParsedBlock("2026-01-01", "cell" if kind == "spreadsheet" else "table_cell",
                        {**row_prefix, "row": 2, "column": 2})
    parsed = ParsedDocument(kind, [
        ParsedBlock("A" * 15_866, "paragraph", {"paragraph": 1}),
        identity,
        value,
    ])

    inputs = ProcessingPipeline._extraction_inputs("synthetic.txt", b"", parsed)

    assert next(index for index, item in enumerate(inputs) if identity in item.blocks) == next(
        index for index, item in enumerate(inputs) if value in item.blocks
    )
