"""Safe read-only source projection shared by detail, report and review gates."""
from qian_labor.ai.grounding import (
    EXTRACTION_VERSION,
    PROOF_KEY,
    deterministic_citation_id,
)
from qian_labor.security.filenames import display_location
from qian_labor.security.masking import mask_sensitive

CITATION_KEY = "_citation_id"


def provenance(location: dict) -> str:
    if PROOF_KEY not in location:
        return "legacy_unverified"
    proof = location[PROOF_KEY]
    if isinstance(proof, dict) and proof.get("version") == EXTRACTION_VERSION and proof.get("status") == "locally_located":
        return "locally_located"
    return "unlocated_needs_review"


def projected_source(source) -> dict:
    state = provenance(source.location)
    citation = None
    file = getattr(source, "file", None)
    stored = source.location.get(CITATION_KEY) if isinstance(source.location, dict) else None
    citation_valid = state != "unlocated_needs_review"
    if state != "unlocated_needs_review" and isinstance(source.location, dict):
        proof = source.location.get(PROOF_KEY)
        if isinstance(proof, dict) and proof.get("version") == EXTRACTION_VERSION:
            expected = deterministic_citation_id(file.sha256, source.location, source.excerpt) if file is not None else None
            citation_valid = isinstance(stored, str) and expected is not None and stored == expected
            if citation_valid:
                citation = stored
    if not citation_valid:
        state = "unlocated_needs_review"
    payload = {
        "locator_type": source.locator_type if state != "unlocated_needs_review" else "document",
        "location": display_location({k: v for k, v in source.location.items() if k not in {PROOF_KEY, CITATION_KEY}}) if state != "unlocated_needs_review" else {},
        "excerpt": mask_sensitive(source.excerpt) if state != "unlocated_needs_review" else "",
        "provenance": state,
    }
    if citation is not None:
        payload["citation_id"] = citation
    return payload


def grounding_requires_review(location: dict) -> bool:
    """Legacy fact semantics stay intact; new unlocated/ambiguous proof is uncertain."""
    return PROOF_KEY in location and (provenance(location) != "locally_located"
        or location[PROOF_KEY].get("requires_review", False) is not False)


def current_source_attempt(session, sources):
    """Use source IDs from the latest Excel completion, without rewriting evidence."""
    if not sources:
        return sources
    from sqlalchemy import select
    from qian_labor.models.core import AuditEvent
    first = sources[0]
    event = session.scalar(select(AuditEvent).where(
        AuditEvent.analysis_id == first.analysis_id,
        AuditEvent.event_type == 'extraction_grounding_completed',
        AuditEvent.metadata_json['file_id'].as_string() == first.file_id,
    ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1))
    ids = event.metadata_json.get('source_ids') if event else None
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        return sources
    selected = [source for source in sources if source.id in ids]
    return selected or sources


def uncertain_grounded_fact_ids(session, analysis_id: str) -> set[str]:
    from sqlalchemy import select
    from qian_labor.models.core import CompanyAnalysisBinding, EmploymentFact, SourceLocator, UploadedFile
    rows = session.scalars(select(SourceLocator).join(
        EmploymentFact, EmploymentFact.id == SourceLocator.fact_id).join(
        UploadedFile, UploadedFile.id == SourceLocator.file_id).where(
        SourceLocator.analysis_id == analysis_id, EmploymentFact.analysis_id == analysis_id,
        UploadedFile.analysis_id == analysis_id, SourceLocator.file_id == EmploymentFact.file_id))
    grouped = {}
    for source in rows:
        grouped.setdefault(source.fact_id, []).append(source)
    owner = session.get(CompanyAnalysisBinding, analysis_id)
    return {fid for fid, sources in grouped.items() if any(grounding_requires_review(source.location)
            for source in (sources if owner and owner.role == 'historical' else current_source_attempt(session, sources)))}
