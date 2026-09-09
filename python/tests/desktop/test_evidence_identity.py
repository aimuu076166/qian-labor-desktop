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
