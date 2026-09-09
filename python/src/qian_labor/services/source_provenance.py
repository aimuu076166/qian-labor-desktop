"""Safe read-only source projection shared by detail, report and review gates."""
from qian_labor.ai.grounding import EXTRACTION_VERSION, PROOF_KEY
from qian_labor.security.filenames import display_location
from qian_labor.security.masking import mask_sensitive


def provenance(location: dict) -> str:
    if PROOF_KEY not in location:
        return "legacy_unverified"
    proof = location[PROOF_KEY]
    if isinstance(proof, dict) and proof.get("version") == EXTRACTION_VERSION and proof.get("status") == "locally_located":
        return "locally_located"
    return "unlocated_needs_review"


def projected_source(source) -> dict:
    state = provenance(source.location)
    return {
        "locator_type": source.locator_type if state != "unlocated_needs_review" else "document",
        "location": display_location({k: v for k, v in source.location.items() if k != PROOF_KEY}) if state != "unlocated_needs_review" else {},
        "excerpt": mask_sensitive(source.excerpt) if state != "unlocated_needs_review" else "",
        "provenance": state,
    }


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
