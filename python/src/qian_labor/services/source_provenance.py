"""Safe read-only source projection shared by detail, report and review gates."""
import hashlib
import json

from qian_labor.ai.grounding import EXTRACTION_VERSION, PROOF_KEY
from qian_labor.security.filenames import display_location
from qian_labor.security.masking import mask_sensitive

CITATION_KEY = "_citation_id"


def deterministic_citation_id(file_sha256: str, location: dict, excerpt: str) -> str:
    """Return a stable identity for one parser-owned evidence fragment.

    The file digest, complete real locator, and block-content digest are all
    required.  No provider-supplied identifier participates in the identity.
    """
    public_location = {key: value for key, value in location.items()
                       if key not in {PROOF_KEY, CITATION_KEY}}
    block_hash = hashlib.sha256(excerpt.encode()).hexdigest()
    canonical = json.dumps(public_location, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    digest = hashlib.sha256(f"{file_sha256}\0{canonical}\0{block_hash}".encode()).hexdigest()
    return f"cite-{digest[:32]}"


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


def uncertain_grounded_fact_ids(session, analysis_id: str) -> set[str]:
    from sqlalchemy import select
    from qian_labor.models.core import EmploymentFact, SourceLocator, UploadedFile
    rows = session.execute(select(SourceLocator.fact_id, SourceLocator.location).join(
        EmploymentFact, EmploymentFact.id == SourceLocator.fact_id).join(
        UploadedFile, UploadedFile.id == SourceLocator.file_id).where(
        SourceLocator.analysis_id == analysis_id, EmploymentFact.analysis_id == analysis_id,
        UploadedFile.analysis_id == analysis_id, SourceLocator.file_id == EmploymentFact.file_id))
    return {fid for fid, location in rows if grounding_requires_review(location)}
