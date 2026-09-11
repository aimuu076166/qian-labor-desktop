from qian_labor.ai.grounding import ExtractionInput, ground_result
from qian_labor.ai.schemas import ExtractionResult
from qian_labor.parsers.protocols import ParsedBlock


def fact(fact_type: str, excerpt: str, **position):
    return ExtractionResult.model_validate({
        "employee_number": "SYN-001",
        "facts": [{
            "employee_id": "SYN-001",
            "fact_type": fact_type,
            "value": True if fact_type.endswith("exists") else "2027-01-01",
            "confidence": 0.99,
            "source": {"file_name": "model-invented.txt", "excerpt": excerpt, **position},
        }],
    })


def test_planned_renewal_cannot_become_an_established_contract_fact():
    item = ExtractionInput(
        "renewal.pdf",
        b"",
        (ParsedBlock(
            "SYN-001 续签通知：拟于2027-01-01续订劳动合同。",
            "pdf_text",
            {"page": 1, "block": 1},
        ),),
        page=1,
    )
    extracted = fact(
        "employment.contract.exists",
        "SYN-001 续签通知：拟于2027-01-01续订劳动合同。",
        page=1,
        block=1,
    )

    proofs = ground_result(extracted, item, "renewal.pdf")

    assert proofs[0]["status"] == "locally_located"
    assert extracted.facts[0].needs_human_confirmation


def test_same_date_for_another_employee_does_not_ground_a_fact():
    item = ExtractionInput(
        "synthetic.xlsx",
        b"",
        (
            ParsedBlock("SYN-001", "cell", {"sheet": "员工", "row": 1, "column": 1}),
            ParsedBlock("SYN-002", "cell", {"sheet": "员工", "row": 2, "column": 1}),
            ParsedBlock("2026-01-01", "cell", {"sheet": "员工", "row": 2, "column": 2}),
        ),
    )
    extracted = ExtractionResult.model_validate({
        "employee_number": "SYN-001",
        "facts": [{
            "employee_id": "SYN-001",
            "fact_type": "employment.start_date",
            "value": "2026-01-01",
            "confidence": 0.9,
            "source": {"file_name": "model.txt", "excerpt": "2026-01-01", "sheet": "员工", "row": 2, "column": "2"},
        }],
    })

    proofs = ground_result(extracted, item, "synthetic.xlsx")

    assert proofs[0]["status"] == "unlocated_needs_review"
    assert extracted.facts[0].needs_human_confirmation


def test_citation_id_is_deterministic_and_binds_file_position_and_block():
    from qian_labor.services.source_provenance import deterministic_citation_id

    location = {"paragraph": 2, "image": 1, "bbox": [1, 2, 30, 40]}
    first = deterministic_citation_id("a" * 64, location, "SYN-001 合同图像")
    assert first == deterministic_citation_id("a" * 64, dict(location), "SYN-001 合同图像")
    assert first != deterministic_citation_id("b" * 64, location, "SYN-001 合同图像")
    assert first != deterministic_citation_id("a" * 64, {**location, "image": 2}, "SYN-001 合同图像")
    assert first != deterministic_citation_id("a" * 64, location, "SYN-002 合同图像")


def test_current_grounding_requires_a_valid_citation_id():
    import hashlib
    from types import SimpleNamespace
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    from qian_labor.services.effective_facts import valid_source_metadata
    from qian_labor.services.source_provenance import deterministic_citation_id

    file = SimpleNamespace(sha256="a" * 64)
    excerpt = "SYN-001 合同图像"
    location = {"paragraph": 2, "_grounding": {
        "version": EXTRACTION_VERSION, "status": "locally_located", "requires_review": False,
    }}
    location["_citation_id"] = deterministic_citation_id(file.sha256, location, excerpt)
    source = SimpleNamespace(location=location, excerpt=excerpt,
                             content_hash=hashlib.sha256(excerpt.encode()).hexdigest(), file=file)
    assert valid_source_metadata(source)
    source.location["_citation_id"] = "cite-forged"
    assert not valid_source_metadata(source)
    source.location.pop("_citation_id")
    assert not valid_source_metadata(source)
