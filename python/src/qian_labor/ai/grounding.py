"""Local location checks, not validation of a model's interpretation or legal advice.

Only runs of Unicode whitespace are collapsed to one ASCII space and trimmed.
No punctuation removal, fuzzy matching, case folding or date/value inference.
The proof list is internal pipeline data, never a provider response field.
"""
from dataclasses import dataclass
import re

from qian_labor.ai.schemas import ClauseObservation, ContractAdvisory, ExtractionResult, SourceLocator
from qian_labor.parsers.protocols import ParsedBlock
from qian_labor.security.masking import mask_sensitive

EXTRACTION_VERSION = "parser-grounding-v2"
MAX_TEXT_CHARACTERS = 100_000
PROOF_KEY = "_grounding"
POSITION_KEYS = ("page", "sheet", "row", "column", "cell", "paragraph", "table", "image", "block", "bbox")
_PLANNED_LANGUAGE = re.compile(r"拟|计划|待签|草案|意向|预计|将于|尚未|待定")


def semantic_review_required(fact_type: str, source_text: str) -> bool:
    """Keep temporal/role ambiguity out of confirmed rule inputs.

    The provider may report a fact from a sentence that is locally genuine but
    only describes a future plan, a delivery event, or a note.  We retain the
    observation and exact evidence, while forcing the existing human-review
    state instead of treating the sentence as an established event.
    """
    if _PLANNED_LANGUAGE.search(source_text):
        return True
    if fact_type == "employment.termination.occurred":
        if any(marker in source_text for marker in ("送达", "签收")) and not any(
            marker in source_text for marker in ("解除日期", "终止日期", "解除劳动关系", "结束劳动关系")
        ):
            return True
    if fact_type in {"employment.social_insurance.present", "employment.social_insurance.period_matches"}:
        if any(marker in source_text for marker in ("备注", "放弃", "自愿")) and not any(
            marker in source_text for marker in ("缴费记录", "已缴", "已提供")
        ):
            return True
    return False


@dataclass(frozen=True)
class ExtractionInput:
    filename: str
    content: bytes
    blocks: tuple[ParsedBlock, ...] = ()
    page: int | None = None

    def __iter__(self):
        # Preserve tuple unpacking by internal/legacy callers.
        return iter((self.filename, self.content))

    def __getitem__(self, index):
        return (self.filename, self.content)[index]


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", mask_sensitive(text)).strip()


def contains_identity(text: str, identity: str | None) -> bool:
    return bool(identity and re.search(r"(?<![\w-])" + re.escape(identity) + r"(?![\w-])", text))


def matching_blocks(source: SourceLocator, item: ExtractionInput, identity: str | None,
                    identities: set[str]) -> list[ParsedBlock]:
    """Shared exact local text/coordinate matcher; never interprets a legal clause."""
    quote = normalized(source.excerpt)
    hints = source.model_dump(exclude_none=True)
    occurrences = [b for b in item.blocks if b.block_type != "header" and quote and quote in normalized(b.text)]
    candidates = []
    for block in occurrences:
        loc = block.locator
        if any(str(hints[k]) != str(loc.get(k)) for k in POSITION_KEYS if k in hints and k != "bbox"):
            continue
        if "bbox" in hints and list(hints["bbox"]) != loc.get("bbox"):
            continue
        if "row" in loc and identity:
            cells = [part.strip() for b in item.blocks
                     if (b.locator.get("sheet"), b.locator.get("table"), b.locator.get("row")) ==
                        (loc.get("sheet"), loc.get("table"), loc["row"])
                     for part in b.text.split(" | ")]
            if identity not in cells:
                continue
        elif (len(identities) > 1 or len(occurrences) > 1) and not contains_identity(block.text, identity):
            continue
        candidates.append(block)
    return candidates


@dataclass(frozen=True)
class GroundedClause:
    observation: ClauseObservation
    source: SourceLocator
    proof: dict


def ground_advisory(advisory: ContractAdvisory, item: ExtractionInput, filename: str,
                    employee_number: str | None = None) -> list[GroundedClause]:
    rows = []
    identities = {o.employee_number for o in advisory.observations if o.employee_number}
    for observation in advisory.observations:
        identity = observation.employee_number or employee_number
        candidates = matching_blocks(observation.source, item, identity, identities)
        if not candidates:
            rows.append(GroundedClause(observation, SourceLocator(file_name=filename),
                        {"version": EXTRACTION_VERSION, "status": "unlocated_needs_review", "requires_review": True}))
        for block in candidates:
            location = {k: v for k, v in block.locator.items() if k in POSITION_KEYS}
            if "column" in location:
                location["column"] = str(location["column"])
            rows.append(GroundedClause(observation, SourceLocator(file_name=filename,
                        excerpt=mask_sensitive(block.text), **location),
                        {"version": EXTRACTION_VERSION, "status": "locally_located",
                         "requires_review": len(candidates) > 1 or not identity or
                           ("row" not in location and not contains_identity(block.text, identity))}))
    return rows


def ground_result(result: ExtractionResult, item: ExtractionInput, filename: str) -> list[dict]:
    """Replace provider locations with locally matched occurrences, or no evidence.

    Explicit locator hints constrain matches; a false hint cannot be silently
    repaired. Multiple matching blocks are all retained and require human review.
    Rows in spreadsheets/tables are scoped by an exact employee identifier when
    available. A multi-employee result cannot claim an unscoped shared sentence.
    """
    facts = []
    proofs = []
    identities = {f.employee_id for f in result.facts if f.employee_id}
    for fact in result.facts:
        identity = fact.employee_id or result.employee_number
        candidates = matching_blocks(fact.source, item, identity, identities)
        if not candidates:
            fact.source = SourceLocator(file_name=filename)
            fact.needs_human_confirmation = True
            facts.append(fact)
            proofs.append({"version": EXTRACTION_VERSION, "status": "unlocated_needs_review"})
            continue
        for block in candidates:
            location = {k: v for k, v in block.locator.items() if k in POSITION_KEYS}
            if "column" in location:
                location["column"] = str(location["column"])
            located = fact.model_copy(deep=True)
            located.source = SourceLocator(file_name=filename, excerpt=mask_sensitive(block.text), **location)
            located.needs_human_confirmation |= len(candidates) > 1 or (
                bool(identity) and "row" not in location and not contains_identity(block.text, identity))
            located.needs_human_confirmation |= semantic_review_required(
                located.fact_type, block.text
            )
            facts.append(located)
            proofs.append({"version": EXTRACTION_VERSION, "status": "locally_located",
                           "requires_review": located.needs_human_confirmation or result.needs_human_confirmation})
    result.facts = facts
    return proofs
