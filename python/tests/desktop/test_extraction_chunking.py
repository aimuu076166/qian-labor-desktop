import json

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
