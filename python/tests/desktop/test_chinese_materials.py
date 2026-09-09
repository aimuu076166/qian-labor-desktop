"""Source-only qualification; never asserts a canned model risk outcome."""
from io import BytesIO
from PIL import Image

from qian_labor.parsers.registry import ParserRegistry
from synthetic_chinese_materials import build_materials, observations, EMPLOYEES, OCR_LINES


def test_chinese_eight_materials_preserve_ten_original_ids_dates_and_true_positions():
    materials = build_materials(); assert len(materials) == 8
    parser = ParserRegistry()
    parsed = {name: parser.parse(name, content) for name, content in materials.items()}
    for observation in observations():
        assert set(observation) == {'filename', 'employee_ids', 'location', 'text'}
        assert set(observation['employee_ids']).issubset(EMPLOYEES)
        if observation['filename'] in {'06-解除通知.png', '07-解除通知扫描.pdf'}:
            continue
        candidates = [block for block in parsed[observation['filename']].blocks
            if all(block.locator.get(key) == value for key, value in observation['location'].items())]
        assert any(observation['text'] in block.text for block in candidates), observation
    roster = ' '.join(block.text for block in parsed['01-员工花名册.csv'].blocks)
    assert all(eid in roster for eid in EMPLOYEES)
    assert parsed['08-嵌图合同待核对.docx'].needs_vision
    assert parsed['07-解除通知扫描.pdf'].needs_vision and not parsed['07-解除通知扫描.pdf'].blocks
    assert parsed['06-解除通知.png'].needs_vision
    with Image.open(BytesIO(materials['06-解除通知.png'])) as image:
        assert image.width >= 1200 and image.height >= 700
    assert any('SYN-010' in text and '2026-09-01' in text for text in OCR_LINES)


def test_embedded_image_has_a_vision_input_not_only_a_warning():
    materials = build_materials()
    parsed = ParserRegistry().parse(
        "08-嵌图合同待核对.docx",
        materials["08-嵌图合同待核对.docx"],
    )
    assert parsed.needs_vision
    assert len(parsed.vision_pages) >= 1
    assert all(page.image_bytes for page in parsed.vision_pages)
    assert any("SYN-001" in block.text for block in parsed.blocks)
