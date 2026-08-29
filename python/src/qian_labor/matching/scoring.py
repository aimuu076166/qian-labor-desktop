from qian_labor.matching.types import CandidateIdentity, CandidateScore

AUTO_MATCH_THRESHOLD = 0.90
AMBIGUITY_MARGIN = 0.08


def _normalized(value: str | None) -> str:
    return "".join((value or "").casefold().split())


def score_candidate(source: CandidateIdentity, candidate: CandidateIdentity) -> CandidateScore:
    stable_fields = ("id_number_hash", "phone_hash", "bank_card_hash")
    conflict = any(
        getattr(source, field)
        and getattr(candidate, field)
        and getattr(source, field) != getattr(candidate, field)
        for field in stable_fields
    )
    if conflict:
        conflict_reasons = ["stable_identifier_conflict"]
        if _normalized(source.name) and _normalized(source.name) == _normalized(candidate.name):
            conflict_reasons.append("name_exact")
        return CandidateScore(0.35, tuple(conflict_reasons), True)

    if source.id_number_hash and source.id_number_hash == candidate.id_number_hash:
        return CandidateScore(0.99, ("id_number_hash_exact",))
    if source.phone_hash and source.phone_hash == candidate.phone_hash:
        return CandidateScore(0.95, ("phone_hash_exact",))
    if source.bank_card_hash and source.bank_card_hash == candidate.bank_card_hash:
        return CandidateScore(0.97, ("bank_card_hash_exact",))
    if source.employee_number and source.employee_number == candidate.employee_number:
        return CandidateScore(1.0, ("employee_number_exact",))

    score = 0.0
    reasons: list[str] = []
    if _normalized(source.name) and _normalized(source.name) == _normalized(candidate.name):
        score += 0.62
        reasons.append("name_exact")
    if source.department and _normalized(source.department) == _normalized(candidate.department):
        score += 0.18
        reasons.append("department_exact")
    if source.hire_date and source.hire_date == candidate.hire_date:
        score += 0.12
        reasons.append("hire_date_exact")
    return CandidateScore(round(min(score, 0.89), 2), tuple(reasons))
