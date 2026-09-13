"""Local location checks, not validation of a model's interpretation or legal advice.

Only runs of Unicode whitespace are collapsed to one ASCII space and trimmed.
No punctuation removal, fuzzy matching, case folding or date/value inference.
The proof list is internal pipeline data, never a provider response field.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from qian_labor.ai.schemas import ClauseObservation, ContractAdvisory, ExtractionResult, SourceLocator
from qian_labor.parsers.protocols import ParsedBlock
from qian_labor.security.masking import mask_sensitive

EXTRACTION_VERSION = "parser-grounding-v3"
MAX_TEXT_CHARACTERS = 100_000
PROOF_KEY = "_grounding"
POSITION_KEYS = ("page", "sheet", "row", "column", "cell", "paragraph", "table", "image", "block", "bbox")
CITATION_ID_KEY = "citation_id"
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
    source_file_hash: str | None = None

    def __iter__(self):
        # Preserve tuple unpacking by internal/legacy callers.
        return iter((self.filename, self.content))

    def __getitem__(self, index):
        return (self.filename, self.content)[index]


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", mask_sensitive(text)).strip()


def contains_identity(text: str, identity: str | None) -> bool:
    return bool(identity and re.search(r"(?<![\w-])" + re.escape(identity) + r"(?![\w-])", text))


def citation_location(location: dict) -> dict:
    """Keep only parser position fields in the stable evidence identity."""
    result = {key: location[key] for key in POSITION_KEYS if key in location}
    if "column" in result and result["column"] is not None:
        # Parser blocks use numeric spreadsheet columns; SourceLocator stores
        # the same coordinate as a string. Canonicalize before hashing so the
        # request citation and persisted `_citation_id` are identical.
        result["column"] = str(result["column"])
    return result


def row_only_location(location: dict) -> bool:
    """A row citation locates a record but cannot establish a field column."""
    return "row" in location and location.get("column") is None and location.get("cell") is None


def citation_excerpt_requires_review(source: SourceLocator, block: ParsedBlock) -> bool:
    """An explicit citation with no complete quote cannot confirm field meaning."""
    return source.citation_id is not None and normalized(source.excerpt) != normalized(block.text)


def deterministic_citation_id(file_sha256: str, location: dict, excerpt: str) -> str:
    """Return a deterministic ID for one parser-owned, masked source fragment."""
    canonical_location = json.dumps(
        citation_location(location),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    block_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(
        f"{file_sha256}\0{canonical_location}\0{block_hash}".encode("utf-8")
    ).hexdigest()
    return f"cite-{digest[:32]}"


def _block_citation_id(item: ExtractionInput, block: ParsedBlock) -> str | None:
    if not item.source_file_hash:
        return None
    return deterministic_citation_id(
        item.source_file_hash,
        block.locator,
        mask_sensitive(block.text),
    )


def column_coordinate(value: object) -> int | None:
    """Compare Excel A1 column letters with parser-owned one-based numbers."""
    text = str(value)
    if re.fullmatch(r"[A-Za-z]{1,3}", text):
        number = 0
        for letter in text.upper():
            number = number * 26 + ord(letter) - ord('A') + 1
    elif re.fullmatch(r"[0-9]{1,5}", text):
        number = int(text)
    else:
        return None
    return number if 1 <= number <= 16384 else None


def matching_blocks(source: SourceLocator, item: ExtractionInput, identity: str | None,
                    identities: set[str], diagnostic: dict | None = None) -> list[ParsedBlock]:
    """Shared exact local text/coordinate matcher; never interprets a legal clause."""
    quote = normalized(source.excerpt)
    hints = source.model_dump(exclude_none=True)
    requested_citation = hints.get(CITATION_ID_KEY)
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic['reason'] = 'source_missing' if not quote and not requested_citation else 'excerpt_mismatch'
    source_blocks = [b for b in item.blocks if b.block_type != "header" and b.text.strip()]

    # XLS/XLSX and DOCX tables expose one parser block per cell.  A provider is
    # allowed to quote the complete row instead of one cell, so expose a
    # parser-owned row view for exact matching without replacing the original
    # cell blocks (which remain authoritative for column/cell coordinates).
    row_groups: dict[tuple[object, object, object], list[ParsedBlock]] = {}
    for block in source_blocks:
        locator = block.locator
        if not isinstance(locator.get("row"), int) or "column" not in locator:
            continue
        key = (locator.get("sheet"), locator.get("table"), locator["row"])
        row_groups.setdefault(key, []).append(block)
    for (sheet, table, row), blocks in row_groups.items():
        row_text = " | ".join(block.text for block in blocks)
        locator = {"row": row}
        if sheet is not None:
            locator["sheet"] = sheet
        if table is not None:
            locator["table"] = table
        source_blocks.append(ParsedBlock(row_text, "row", locator))

    if requested_citation is not None:
        # An explicit provider citation is usable only when it maps to a
        # parser-owned block in this exact input. Without a file hash, or with
        # a mismatched excerpt, fail closed instead of using model coordinates.
        cited = [
            block for block in source_blocks
            if _block_citation_id(item, block) == requested_citation
        ]
        diagnostic['reason'] = 'excerpt_mismatch' if cited else 'citation_unknown'
        occurrences = [block for block in cited if not quote or quote in normalized(block.text)]
    else:
        occurrences = [
            block for block in source_blocks
            if quote and quote in normalized(block.text)
        ]

    candidates = []
    for block in occurrences:
        loc = block.locator
        mismatched = []
        for key in POSITION_KEYS:
            if key not in hints or key == 'bbox':
                continue
            same = str(hints[key]) == str(loc.get(key))
            if key == 'column' and 'sheet' in loc:
                column = column_coordinate(hints[key])
                same = column is not None and column == column_coordinate(loc.get(key))
                # Some providers return the column's label, not its coordinate.
                # Only a verified cell citation can disambiguate that label;
                # never infer a column from an employee row or a fuzzy header.
                if column is None and requested_citation is not None:
                    same = bool(loc.get('column') is not None and loc.get('header')
                                and hints[key] == loc['header'])
            if not same:
                mismatched.append(key)
        if mismatched:
            diagnostic.update(reason='position_mismatch', position_keys=mismatched)
            continue
        if "bbox" in hints and list(hints["bbox"]) != loc.get("bbox"):
            diagnostic.update(reason='position_mismatch', position_keys=['bbox'])
            continue
        if "row" in loc and identity:
            cells = [part.strip() for b in item.blocks
                     if (b.locator.get("sheet"), b.locator.get("table"), b.locator.get("row")) ==
                        (loc.get("sheet"), loc.get("table"), loc["row"])
                     for part in b.text.split(" | ")]
            if identity not in cells:
                diagnostic['reason'] = 'identity_mismatch'
                continue
        elif (len(identities) > 1 or len(occurrences) > 1) and not contains_identity(block.text, identity):
            diagnostic['reason'] = 'identity_mismatch'
            continue
        candidates.append(block)

    # Prefer the most precise parser block when the quote is exactly one cell.
    # Otherwise the row-level block is the only valid representation of a
    # complete-row quote.  This prevents one repeated date from yielding both a
    # cell proof and a row proof for the same employee.
    exact_cells = [
        block for block in candidates
        if block.block_type in {"cell", "table_cell"}
        and normalized(block.text) == quote
    ]
    if exact_cells:
        candidates = exact_cells
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
                         "requires_review": row_only_location(location) or citation_excerpt_requires_review(
                           observation.source, block) or len(candidates) > 1 or not identity or
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
        diagnostic = {}
        candidates = matching_blocks(fact.source, item, identity, identities, diagnostic)
        if not candidates:
            fact.source = SourceLocator(file_name=filename)
            fact.needs_human_confirmation = True
            facts.append(fact)
            proofs.append({"version": EXTRACTION_VERSION, "status": "unlocated_needs_review",
                           "diagnostic": diagnostic})
            continue
        for block in candidates:
            location = {k: v for k, v in block.locator.items() if k in POSITION_KEYS}
            if "column" in location:
                location["column"] = str(location["column"])
            located = fact.model_copy(deep=True)
            located.source = SourceLocator(file_name=filename, excerpt=mask_sensitive(block.text), **location)
            located.needs_human_confirmation |= row_only_location(location) or citation_excerpt_requires_review(
                fact.source, block) or len(candidates) > 1 or (
                bool(identity) and "row" not in location and not contains_identity(block.text, identity))
            located.needs_human_confirmation |= semantic_review_required(
                located.fact_type, block.text
            )
            facts.append(located)
            proofs.append({"version": EXTRACTION_VERSION, "status": "locally_located",
                           "requires_review": located.needs_human_confirmation or result.needs_human_confirmation})
    result.facts = facts
    return proofs
