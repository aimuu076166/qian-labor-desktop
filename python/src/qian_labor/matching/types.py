from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateIdentity:
    name: str = ""
    employee_number: str | None = None
    id_number_hash: str | None = None
    phone_hash: str | None = None
    bank_card_hash: str | None = None
    department: str | None = None
    hire_date: str | None = None


@dataclass(frozen=True)
class CandidateScore:
    score: float
    reasons: tuple[str, ...]
    stable_identifier_conflict: bool = False


@dataclass(frozen=True)
class RankedMatch:
    status: str
    employee_id: str | None
    score: float
    reasons: tuple[str, ...]
    candidates: tuple[tuple[str, CandidateScore], ...]
