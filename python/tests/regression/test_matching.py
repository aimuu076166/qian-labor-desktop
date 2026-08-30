from qian_labor.matching.scoring import CandidateIdentity, score_candidate
from qian_labor.matching.service import match_employee, rank_candidates


def test_employee_number_exact_match_has_highest_score() -> None:
    source = CandidateIdentity(name="虚构张华", employee_number="NX-001", hire_date="2025-03-01")
    candidate = CandidateIdentity(name="虚构张华", employee_number="NX-001", hire_date="2025-04-01")

    result = score_candidate(source, candidate)

    assert result.score == 1.0
    assert result.reasons == ("employee_number_exact",)


def test_same_name_with_conflicting_stable_ids_is_ambiguous() -> None:
    source = CandidateIdentity(name="虚构王伟", employee_number="NX-002", id_number_hash="a" * 64)
    candidate = CandidateIdentity(
        name="虚构王伟", employee_number="NX-009", id_number_hash="b" * 64
    )

    result = score_candidate(source, candidate)

    assert result.score < 0.5
    assert "stable_identifier_conflict" in result.reasons


def test_close_name_only_candidates_require_review() -> None:
    source = CandidateIdentity(name="虚构同名", department="制造一部")
    result = rank_candidates(
        source,
        [
            ("one", CandidateIdentity(name="虚构同名", department="制造一部")),
            ("two", CandidateIdentity(name="虚构同名", department="制造一部")),
        ],
    )
    assert result.status == "ambiguous"


def test_bank_card_hash_exact_is_a_strong_match_signal() -> None:
    source = CandidateIdentity(name="虚构员工", bank_card_hash="c" * 64)
    candidate = CandidateIdentity(name="另一遮蔽姓名", bank_card_hash="c" * 64)

    result = score_candidate(source, candidate)

    assert result.score >= 0.9
    assert result.reasons == ("bank_card_hash_exact",)


def test_bank_card_hash_conflict_requires_review_even_when_employee_number_matches() -> None:
    source = CandidateIdentity(employee_number="F-901", bank_card_hash="c" * 64)
    candidate = CandidateIdentity(employee_number="F-901", bank_card_hash="d" * 64)

    result = score_candidate(source, candidate)

    assert result.stable_identifier_conflict is True
    assert result.score < 0.5


def test_exact_stable_hash_outweighs_changed_employee_number() -> None:
    stable_hash = "e" * 64
    source = CandidateIdentity(employee_number="F-NEW", id_number_hash=stable_hash)
    candidate = CandidateIdentity(employee_number="F-OLD", id_number_hash=stable_hash)

    result = score_candidate(source, candidate)

    assert result.stable_identifier_conflict is False
    assert result.score >= 0.9
    assert result.reasons == ("id_number_hash_exact",)


def test_equal_name_candidates_require_human_confirmation() -> None:
    result = match_employee(
        [
            {"employee_no": "F-001", "name": "虚构甲"},
            {"employee_no": "F-002", "name": "虚构甲"},
        ],
        {"name": "虚构甲"},
    )
    assert result["status"] in {"ambiguous", "unknown"}


def test_stable_employee_number_wins() -> None:
    result = match_employee(
        [{"employee_no": "F-001", "name": "虚构甲"}],
        {"employee_no": "F-001", "name": "虚构甲"},
    )
    assert result["status"] == "auto_matched"


def test_bank_card_hash_is_available_to_compatibility_matcher() -> None:
    result = match_employee(
        [{"id": "one", "name": "虚构甲", "bank_card_hash": "b" * 64}],
        {"name": "虚构乙", "bank_card_hash": "b" * 64},
    )

    assert result["status"] == "auto_matched"
    assert result["reasons"] == ("bank_card_hash_exact",)
