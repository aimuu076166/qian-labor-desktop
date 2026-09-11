"""Immutable extraction plus owned, typed human revisions. No provider work."""
from datetime import date
import hashlib
import json
import re
from types import SimpleNamespace
from uuid import UUID
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from qian_labor.ai.fact_contract import FACT_VALUE_TYPES
from qian_labor.ai.grounding import EXTRACTION_VERSION
from qian_labor.models.core import (
    AnalysisBatch, CompanyAnalysisBinding, Employee, EmployeeRecord, EmployeeSnapshotBinding,
    EmploymentFact, SourceLocator, UploadedFile, EffectiveFactRevision, AssessmentDecision,
    EmployeeMatchCandidate, EmployeeMatchDecision,
)
from qian_labor.security.masking import mask_sensitive
from qian_labor.security.filenames import display_filename
from qian_labor.services.source_provenance import CITATION_KEY, deterministic_citation_id, grounding_requires_review, projected_source, current_source_attempt

PENDING = {"conflicted", "pending_review", "needs_human_confirmation"}
CONTRACT_TYPES = {"employment.contract." + suffix for suffix in
                  ("exists", "start_date", "end_date", "type", "employer", "term_readable")}
PROBATION_DATES = {"employment.probation.start_date", "employment.probation.end_date"}
PROBATION_SCALARS = PROBATION_DATES | {"employment.probation.assessment_exists"}
BLOCKED = {name for name in FACT_VALUE_TYPES if name.startswith(("employment.pay.", "employment.attendance"))} | {
    "analysis.minimum_core_coverage", "employment.material_coverage", "employment.identity.match_status",
    "employment.post_termination_record",
}
ENUMS = {
    "employment.status": ["active", "probation", "terminated", "unknown"],
    "employment.contract.type": ["fixed", "fixed_term", "indefinite", "open_ended", "non_fixed", "task", "project", "completion_of_task"],
    "employment.termination.settlement_materials": ["final_pay", "separation_certificate", "handover", "item_handover", "work_handover"],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def fail(code, status=409):
    from qian_labor.services.company_workspaces import WorkspaceError
    raise WorkspaceError(code, status)


def iso_date(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("FACT_VALUE_INVALID")
    date.fromisoformat(value)
    return value


def value_spec(name):
    if name not in FACT_VALUE_TYPES or name in BLOCKED:
        return {"editable": False, "input_type": "read_only"}
    kind = "date" if name.endswith("_date") else "periods" if name.endswith(".periods") else (
        "enum" if name in ENUMS and name != "employment.termination.settlement_materials" else
        "boolean" if FACT_VALUE_TYPES[name] == {"boolean"} else
        "list" if FACT_VALUE_TYPES[name] == {"string_list"} else "text")
    return {"editable": True, "input_type": kind, "options": ENUMS.get(name, []),
            "max_items": 50, "max_length": 200, "nullable": True}


def validate_manual_value(name, value):
    spec = value_spec(name)
    if not spec["editable"]:
        raise ValueError("FACT_TYPE_READ_ONLY")
    if value is None:
        return None
    kind = spec["input_type"]
    if kind == "date":
        return iso_date(value)
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError("FACT_VALUE_INVALID")
        return value
    if kind in {"text", "enum"}:
        if type(value) is not str or not value.strip() or len(value) > 200:
            raise ValueError("FACT_VALUE_INVALID")
        value = mask_sensitive(value.strip())
        if kind == "enum" and value not in spec["options"]:
            raise ValueError("FACT_VALUE_INVALID")
        return value
    if type(value) is not list or len(value) > 50:
        raise ValueError("FACT_VALUE_INVALID")
    if kind == "periods":
        for row in value:
            if type(row) is not list or len(row) != 2 or iso_date(row[0]) > iso_date(row[1]):
                raise ValueError("FACT_VALUE_INVALID")
        return value
    if any(type(item) is not str or not item.strip() or len(item) > 200 or
           (spec["options"] and item not in spec["options"]) for item in value):
        raise ValueError("FACT_VALUE_INVALID")
    return [mask_sensitive(item.strip()) for item in value]


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_version: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def mask_reason(cls, value):
        value = mask_sensitive(value.strip())
        if not value or len(value) > 500:
            raise ValueError("FACT_REASON_INVALID")
        return value


class FactRevisionRequest(ReasonRequest):
    kind: Literal["correct", "confirm"]
    value: Any = None
    expected_owner_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_context_signature: str = Field(default="", max_length=64)


def require_owner(s, company_id, analysis_id, *, write=False):
    owner = s.get(CompanyAnalysisBinding, analysis_id)
    analysis = s.get(AnalysisBatch, analysis_id)
    if not owner or owner.company_id != company_id or not analysis or analysis.deleted_at:
        fail("FACT_OWNERSHIP_INVALID", 404)
    if write and (owner.role != "current" or analysis.assessment_profile != "labor_materials_v1"):
        fail("WORKSPACE_HISTORICAL_READ_ONLY")
    return owner, analysis


def require_record(s, company_id, analysis_id, record_id):
    record = s.get(EmployeeRecord, record_id)
    binding = s.scalar(select(EmployeeSnapshotBinding).where(
        EmployeeSnapshotBinding.analysis_id == analysis_id, EmployeeSnapshotBinding.employee_record_id == record_id))
    if not record or record.company_id != company_id or not binding:
        fail("FACT_OWNERSHIP_INVALID", 404)
    employee = s.get(Employee, binding.snapshot_id)
    if not employee or employee.analysis_id != analysis_id:
        fail("FACT_OWNERSHIP_INVALID")
    return employee


def latest_decision(s, analysis_id, scope):
    return s.scalar(select(AssessmentDecision).where(AssessmentDecision.analysis_id == analysis_id,
        AssessmentDecision.scope == scope).order_by(AssessmentDecision.version.desc()).limit(1))


def valid_source_metadata(source, *, allow_legacy=False):
    if not isinstance(source.location, dict) or source.content_hash != hashlib.sha256(source.excerpt.encode()).hexdigest():
        return False
    if "_grounding" not in source.location:
        return True  # Legacy labels remain honest; human review is separate.
    proof = source.location["_grounding"]
    valid_proof = isinstance(proof, dict) and proof.get("status") in {
        "locally_located", "unlocated_needs_review"} and type(proof.get("requires_review", False)) is bool
    if not valid_proof:
        return False
    if proof.get("version") != EXTRACTION_VERSION:
        return bool(allow_legacy and isinstance(proof.get("version"), str)
                    and proof.get("version").startswith("parser-grounding-v"))
    file = getattr(source, "file", None)
    stored = source.location.get(CITATION_KEY)
    return bool(file and isinstance(stored, str) and stored == deterministic_citation_id(file.sha256, source.location, source.excerpt))


def owned_state(s, fact):
    file = s.get(UploadedFile, fact.file_id) if fact.file_id else None
    owner = s.get(CompanyAnalysisBinding, fact.analysis_id)
    employee = s.get(Employee, fact.employee_id) if fact.employee_id else None
    binding = s.get(EmployeeSnapshotBinding, fact.employee_id) if fact.employee_id else None
    record = s.get(EmployeeRecord, binding.employee_record_id) if binding else None
    sources = list(s.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact.id).order_by(SourceLocator.id)))
    valid = bool(file and file.analysis_id == fact.analysis_id and sources and
                 (not file.parsed_document or file.parsed_document.content_hash == file.sha256) and
                 all(x.analysis_id == fact.analysis_id and x.file_id == fact.file_id and
                     valid_source_metadata(x, allow_legacy=bool(owner and owner.role == "historical")) for x in sources))
    old_sources = sources
    if not owner or owner.role != 'historical':
        sources = current_source_attempt(s, sources)
    owned = bool(owner and employee and employee.analysis_id == fact.analysis_id and binding and
                 binding.analysis_id == fact.analysis_id and record and record.company_id == owner.company_id)
    decisions = []
    for decision, candidate in s.execute(select(EmployeeMatchDecision, EmployeeMatchCandidate).join(
            EmployeeMatchCandidate, EmployeeMatchCandidate.id == EmployeeMatchDecision.candidate_id).where(
            EmployeeMatchDecision.analysis_id == fact.analysis_id)):
        if fact.id in (candidate.extracted_fields or {}).get("fact_ids", []):
            decisions.append([decision.id, decision.decision, decision.target_employee_id])
    owner_sig = digest([fact.analysis_id, owner.company_id if owner else None, fact.id, fact.employee_id,
                        record.id if record else None, sorted(decisions)])
    source_sig = digest([file.id if file else None, file.sha256 if file else None,
                         [[x.id, x.analysis_id, x.file_id, x.locator_type, x.location, x.excerpt, x.content_hash] for x in sources]])
    return SimpleNamespace(valid=valid, owned=owned, owner=owner, record=record, file=file, sources=sources,
                           source_refreshed=sources != old_sources,
                           owner_signature=owner_sig, source_signature=source_sig)


def fact_projection(s, fact):
    state = owned_state(s, fact)
    revision = s.scalar(select(EffectiveFactRevision).where(EffectiveFactRevision.fact_id == fact.id)
        .order_by(EffectiveFactRevision.version.desc()).limit(1))
    accepted = bool(revision and state.valid and state.owned and
                    revision.company_id == state.owner.company_id and revision.analysis_id == fact.analysis_id and
                    revision.employee_id == fact.employee_id and revision.record_id == state.record.id and
                    revision.owner_signature == state.owner_signature and revision.source_signature == state.source_signature)
    data = {column.key: getattr(fact, column.key) for column in EmploymentFact.__table__.columns}
    data.update(original=fact, state=state, revision=revision, human_confirmed=accepted,
                revision_valid=accepted, version=revision.version if revision else 0, context_signature="",
                selected_support=True, basis_pending=False)
    if accepted:
        data.update(normalized_value_json=revision.value, verification_status="human_confirmed" if revision.value is not None else "needs_human_confirmation")
    elif not state.valid or any(grounding_requires_review(source.location) for source in state.sources):
        data["verification_status"] = "needs_human_confirmation"
    elif state.source_refreshed and fact.verification_status == 'needs_human_confirmation':
        # A successful retry can replace extraction uncertainty, never a human revision.
        data["verification_status"] = "unverified"
    return SimpleNamespace(**data)


def contract_dependency(rows, employee_id):
    return digest([[row.id, row.state.owner_signature, row.state.source_signature,
                    row.state.file.classified_kind if row.state.file else None,
                    row.normalized_value_json, row.version] for row in rows
                   if row.employee_id == employee_id and row.fact_type in CONTRACT_TYPES])


def _revert_revision(row):
    row.human_confirmed = row.revision_valid = False
    row.normalized_value_json = row.original.normalized_value_json
    row.verification_status = "needs_human_confirmation"


def _has_stale_grounding(session, fact) -> bool:
    """Keep an explicitly old extraction out of the current projection.

    Old facts and their revisions remain addressable through historical
    analyses and fact history.  A current analysis must not silently keep
    using a fact whose parser-grounding proof names an older extraction
    contract after a new extraction has been persisted.
    """
    sources = session.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact.id))
    return any(
        isinstance(source.location, dict)
        and isinstance(source.location.get("_grounding"), dict)
        and isinstance(source.location["_grounding"].get("version"), str)
        and source.location["_grounding"]["version"].startswith("parser-grounding-v")
        and source.location["_grounding"].get("version") != EXTRACTION_VERSION
        for source in sources
    )


def effective_projection(s, analysis_id, *, employee_ids=None):
    """Acyclic: originals -> contract selection -> date pair -> assessment basis."""
    owner = s.get(CompanyAnalysisBinding, analysis_id)
    query = select(EmploymentFact).where(EmploymentFact.analysis_id == analysis_id)
    if employee_ids is not None:
        query = query.where(EmploymentFact.employee_id.in_(employee_ids))
    facts = list(s.scalars(query.order_by(EmploymentFact.id)))
    if owner and owner.role == "current":
        # Explicit v1 (or otherwise stale) proofs remain in the database for
        # historical display and manual-revision history, but cannot be
        # selected as current assessment input after the grounding upgrade.
        facts = [fact for fact in facts if not _has_stale_grounding(s, fact)]
    rows = [fact_projection(s, fact) for fact in facts]
    for eid in {row.employee_id for row in rows if row.employee_id}:
        own = [row for row in rows if row.employee_id == eid]
        choice = latest_decision(s, analysis_id, "current_contract:" + eid)
        if not choice:
            continue
        valid = choice.dependency_signature == contract_dependency(rows, eid)
        contracts = [row for row in own if row.fact_type in CONTRACT_TYPES]
        selected = [row for row in contracts if row.file_id == choice.value] if valid else []
        context = digest([choice.id, choice.dependency_signature]) if valid else ""
        for row in contracts:
            row.selected_support = row in selected if valid else True
        for row in own:
            if row.fact_type not in PROBATION_SCALARS:
                continue
            row.context_signature = context
            if row.fact_type in PROBATION_DATES:
                if row.revision_valid and row.revision.context_signature != context:
                    _revert_revision(row)
                row.selected_support = valid and (row.file_id == choice.value or row.human_confirmed)
        def unique(name, candidates):
            values = [r.normalized_value_json for r in candidates if r.fact_type == name and r.selected_support]
            if not values or any(r.verification_status in PENDING for r in candidates if r.fact_type == name and r.selected_support):
                return None
            return values[0] if len({digest(v) for v in values}) == 1 else None
        start = unique("employment.contract.start_date", selected)
        end = unique("employment.contract.end_date", selected)
        # None from unique() may mean conflict/review pending, not an open bound.
        end_absent = not any(r.fact_type == "employment.contract.end_date" for r in selected)
        contract_type = unique("employment.contract.type", selected)
        pstart = unique("employment.probation.start_date", own)
        pend = unique("employment.probation.end_date", own)
        try:
            pair_valid = bool(valid and contract_type in ENUMS["employment.contract.type"] and
                              (end is not None or (end_absent and contract_type in {"indefinite", "open_ended", "non_fixed"})) and
                              iso_date(start) <= iso_date(pstart) <= iso_date(pend) and
                              (end is None or iso_date(pend) <= iso_date(end)))
        except (ValueError, TypeError):
            pair_valid = False
        pair_support = [[r.id, r.normalized_value_json, r.version, r.state.source_signature] for r in own
                        if r.fact_type in PROBATION_DATES and r.selected_support]
        assessment_context = digest([context, pair_support]) if pair_valid else ""
        for row in own:
            if row.fact_type == "employment.probation.assessment_exists":
                row.context_signature = assessment_context
                if row.revision_valid and row.revision.context_signature != assessment_context:
                    _revert_revision(row)
                row.selected_support = bool(pair_valid and (row.file_id == choice.value or row.human_confirmed))
            if row.fact_type in PROBATION_SCALARS and not pair_valid:
                row.basis_pending = True
                row.verification_status = "needs_human_confirmation"
    return rows


def revision_payload(revision):
    return None if revision is None else {"id": revision.id, "fact_id": revision.fact_id, "version": revision.version,
        "kind": revision.kind, "value": revision.value, "reason": mask_sensitive(revision.reason),
        "employee_id": revision.employee_id, "record_id": revision.record_id,
        "actor": "local-user", "created_at": revision.created_at.isoformat(),
        "context_signature": revision.context_signature}


def safe_value(value):
    if isinstance(value, str): return mask_sensitive(value)
    if isinstance(value, list): return [safe_value(v) for v in value]
    if isinstance(value, dict): return {mask_sensitive(str(k)): safe_value(v) for k, v in value.items()}
    return value


def public_fact(row, read_only=False):
    state = row.state
    return {"id": row.id, "analysis_id": row.analysis_id, "employee_id": row.employee_id,
        "record_id": state.record.id if state.record else None, "file_id": row.file_id, "fact_type": row.fact_type,
        "filename": display_filename(state.file.original_filename) if state.file else "",
        "original_value": safe_value(row.value_json), "effective_value": safe_value(row.normalized_value_json),
        "verification_status": row.verification_status, "version": row.version,
        "human_confirmed": row.human_confirmed, "revision_valid": row.revision_valid,
        "latest_revision": revision_payload(row.revision), "sources": [
            {"id": x.id, "file_id": x.file_id, **(projected_source(x) if isinstance(x.location, dict) else
                {"locator_type": "document", "location": {}, "excerpt": "", "provenance": "unlocated_needs_review"})} for x in state.sources if
            x.analysis_id == row.analysis_id and x.file_id == row.file_id],
        "owner_signature": state.owner_signature, "source_signature": state.source_signature,
        "context_signature": row.context_signature, "selected_support": row.selected_support,
        "basis_pending": row.basis_pending, "source_valid": state.valid,
        "confirmation_context": "current_contract_period" if row.context_signature else "fact_owner_and_material",
        "read_only": read_only or not state.owned or not state.valid or not value_spec(row.fact_type)["editable"],
        "value_spec": value_spec(row.fact_type)}


class EffectiveFactService:
    def __init__(self, database):
        self.database = database

    def listing(self, company_id, analysis_id, record_id=None, request_id=None, page=1, page_size=20):
        from qian_labor.services.assessment_state import assessment_metadata
        with self.database.session() as s:
            owner, _ = require_owner(s, company_id, analysis_id)
            employee = require_record(s, company_id, analysis_id, record_id) if record_id else None
            rows = effective_projection(s, analysis_id, employee_ids={employee.id} if employee else None)
            rows = [row for row in rows if employee is None or row.employee_id == employee.id]
            request = s.get(EffectiveFactRevision, str(request_id)) if request_id else None
            if request and (request.company_id != company_id or request.analysis_id != analysis_id or
                            (record_id and request.record_id != record_id)):
                fail("FACT_OWNERSHIP_INVALID", 404)
            return {"items": [public_fact(row, owner.role != "current") for row in rows[(page-1)*page_size:page*page_size]],
                    "total": len(rows), "page": page, "page_size": page_size, "pages": (len(rows)+page_size-1)//page_size,
                    "read_only": owner.role != "current", "request_revision": revision_payload(request),
                    "assessment_revision": assessment_metadata(s, analysis_id)}

    def history(self, company_id, analysis_id, fact_id, page=1, page_size=20):
        with self.database.session() as s:
            require_owner(s, company_id, analysis_id)
            fact = s.get(EmploymentFact, fact_id)
            if not fact or fact.analysis_id != analysis_id:
                fail("FACT_OWNERSHIP_INVALID", 404)
            from qian_labor.services.assessment_state import page_rows
            return page_rows(s, select(EffectiveFactRevision).where(EffectiveFactRevision.fact_id == fact_id)
                .order_by(EffectiveFactRevision.version.desc()), page, page_size, revision_payload)

    def revise(self, company_id, analysis_id, fact_id, request):
        from qian_labor.services.company_workspaces import CompanyWorkspaceService
        with CompanyWorkspaceService(self.database)._write() as s:
            require_owner(s, company_id, analysis_id, write=True)
            rows = effective_projection(s, analysis_id)
            row = next((row for row in rows if row.id == fact_id), None)
            if not row or not row.state.owned:
                fail("FACT_OWNERSHIP_INVALID", 404)
            if not row.state.valid:
                fail("FACT_SOURCE_INVALID")
            if (s.get(EffectiveFactRevision, str(request.id)) or row.version != request.expected_version or
                    row.state.owner_signature != request.expected_owner_signature or
                    row.state.source_signature != request.expected_source_signature or
                    row.context_signature != request.expected_context_signature):
                fail("FACT_VERSION_CONFLICT")
            value = row.normalized_value_json if request.kind == "confirm" else request.value
            try:
                if request.kind == "confirm" and "value" in request.model_fields_set:
                    supplied = validate_manual_value(row.fact_type, request.value)
                    if digest(supplied) != digest(value):
                        fail("FACT_CONFIRM_VALUE_CHANGED", 422)
                value = validate_manual_value(row.fact_type, value)
            except (ValueError, TypeError):
                fail("FACT_VALUE_INVALID", 422)
            s.add(EffectiveFactRevision(id=str(request.id), fact_id=fact_id, analysis_id=analysis_id,
                company_id=company_id, employee_id=row.employee_id, record_id=row.state.record.id,
                version=row.version+1, kind=request.kind, value=value, reason=request.reason,
                owner_signature=row.state.owner_signature, source_signature=row.state.source_signature,
                context_signature=row.context_signature))
            s.flush()
            return public_fact(next(r for r in effective_projection(s, analysis_id) if r.id == fact_id))
