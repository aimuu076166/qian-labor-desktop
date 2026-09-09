"""Durable company identity and explicit historical adoption; no extraction or evaluation."""
from contextlib import contextmanager

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from qian_labor.database import Database
from qian_labor.desktop.company_schemas import CompanyView, EmployeeView, SnapshotBindingView
from qian_labor.models.core import (
    AnalysisBatch, CompanyWorkspace, CompanyAnalysisBinding, Employee,
    EmployeeMatchCandidate, EmployeeRecord, EmployeeSnapshotBinding, WorkspacePreference,
    EmploymentFact, SourceLocator, UploadedFile,
)
from qian_labor.services.analyses import AnalysisService
from qian_labor.services.assessment_scope import DESKTOP_PROFILE
from qian_labor.services.dashboard import DashboardService
from qian_labor.security.masking import mask_identity
from qian_labor.sqlite_migrations import assert_no_pending_recovery


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def require_material_mutation(session, analysis_id):
    owner = session.get(CompanyAnalysisBinding, analysis_id)
    if owner is not None and owner.role == "historical":
        raise WorkspaceError("WORKSPACE_HISTORICAL_READ_ONLY")
    return owner


class CompanyWorkspaceService:
    def __init__(self, database: Database):
        self.database = database

    @contextmanager
    def _write(self):
        if self.database.path is not None:
            assert_no_pending_recovery(self.database.path.parent)
        try:
            with self.database.session() as session:
                # SQLite serializes the entire validate/CAS/write transaction.
                session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
        except IntegrityError:
            raise WorkspaceError("WORKSPACE_IDENTITY_CONFLICT") from None
        except OperationalError:
            raise WorkspaceError("DESKTOP_DB_BUSY") from None

    @staticmethod
    def _company(session, company_id):
        item = session.get(CompanyWorkspace, company_id)
        if item is None:
            raise WorkspaceError("WORKSPACE_NOT_FOUND", 404)
        return item

    @classmethod
    def _cas_company(cls, session, company_id, version):
        cls._company(session, company_id)
        if session.execute(update(CompanyWorkspace).where(
            CompanyWorkspace.id == company_id, CompanyWorkspace.version == version,
        ).values(version=CompanyWorkspace.version + 1)).rowcount != 1:
            raise WorkspaceError("WORKSPACE_VERSION_CONFLICT")

    def companies(self):
        with self.database.session() as s:
            return [CompanyView.model_validate(c) for c in s.scalars(select(CompanyWorkspace).order_by(CompanyWorkspace.created_at, CompanyWorkspace.id))]

    def company(self, company_id):
        with self.database.session() as s:
            return CompanyView.model_validate(self._company(s, company_id))

    def create_company(self, request):
        with self._write() as s:
            company = CompanyWorkspace(id=str(request.id), display_name=request.display_name)
            s.add(company)
            s.flush()
            result = CompanyView.model_validate(company)
        return result

    @staticmethod
    def _new_record(s, company_id, request):
        item = EmployeeRecord(id=str(request.id), company_id=company_id,
            masked_name=mask_identity(request.display_name), employee_number=request.employee_number,
            department=request.department, job_title=request.job_title)
        s.add(item)
        s.flush()
        return item

    def create_employee(self, company_id, request):
        with self._write() as s:
            self._cas_company(s, company_id, request.expected_company_version)
            item = self._new_record(s, company_id, request)
            result = EmployeeView.model_validate(item)
        return result

    def _current(self, s, company_id):
        company = self._company(s, company_id)
        owner = s.scalar(select(CompanyAnalysisBinding).where(
            CompanyAnalysisBinding.company_id == company_id, CompanyAnalysisBinding.role == "current"))
        if owner is None:
            return None
        analysis = s.get(AnalysisBatch, owner.analysis_id)
        pending = s.scalar(select(func.count()).select_from(EmployeeMatchCandidate).where(
            EmployeeMatchCandidate.analysis_id == analysis.id, EmployeeMatchCandidate.status == "pending")) or 0
        from qian_labor.services.assessment_state import assessment_metadata
        assessment = assessment_metadata(s, analysis.id)
        return {"analysis_id": analysis.id, "company_id": company_id, "role": "current",
                "assessment_profile": analysis.assessment_profile, "status": analysis.status,
                "current_stage": analysis.current_stage, "progress": analysis.progress,
                "analysis_version": analysis.version, "company_version": company.version,
                "stale": not assessment["fresh"], "assessment_revision": assessment, "pending_identity_count": pending}

    def current_analysis(self, company_id):
        with self.database.session() as s:
            return self._current(s, company_id)

    def analyses(self, company_id, relation, page, page_size, active_analysis_id=None):
        """Advisory history/adoption snapshot; performs no writes or reservations."""
        with self.database.session() as s:
            self._company(s, company_id)
            visible = (
                AnalysisBatch.deleted_at.is_(None),
                AnalysisBatch.status != "deleting",
            )
            if relation == "historical":
                query = select(AnalysisBatch).join(
                    CompanyAnalysisBinding,
                    CompanyAnalysisBinding.analysis_id == AnalysisBatch.id,
                ).where(
                    CompanyAnalysisBinding.company_id == company_id,
                    CompanyAnalysisBinding.role == "historical",
                    *visible,
                )
            else:
                is_bound = exists(select(CompanyAnalysisBinding.analysis_id).where(
                    CompanyAnalysisBinding.analysis_id == AnalysisBatch.id
                ))
                query = select(AnalysisBatch).where(~is_bound, *visible)

            total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
            selected = list(s.scalars(query.order_by(
                AnalysisBatch.created_at.desc(), AnalysisBatch.id.desc()
            ).offset((page - 1) * page_size).limit(page_size)))
            ids = [item.id for item in selected]

            pending_counts = {}
            unresolved_counts = {}
            if ids:
                pending_counts = dict(s.execute(select(
                    EmployeeMatchCandidate.analysis_id, func.count()
                ).where(
                    EmployeeMatchCandidate.analysis_id.in_(ids),
                    EmployeeMatchCandidate.status == "pending",
                ).group_by(EmployeeMatchCandidate.analysis_id)).all())
                unresolved_counts = dict(s.execute(select(
                    Employee.analysis_id, func.count()
                ).where(
                    Employee.analysis_id.in_(ids),
                    Employee.employment_status != "merged",
                    Employee.match_status.not_in({"confirmed", "auto_matched"}),
                ).group_by(Employee.analysis_id)).all())

            items = []
            for item in selected:
                pending = pending_counts.get(item.id, 0)
                if relation == "historical":
                    blocker = "WORKSPACE_ANALYSIS_ALREADY_BOUND"
                elif item.id == active_analysis_id:
                    blocker = "DESKTOP_ANALYSIS_BUSY"
                elif item.status not in {"completed", "partial", "failed"}:
                    blocker = "WORKSPACE_ANALYSIS_NOT_SETTLED"
                elif pending or unresolved_counts.get(item.id, 0):
                    blocker = "WORKSPACE_MATCHING_UNRESOLVED"
                else:
                    blocker = None
                items.append({
                    **AnalysisService.payload(item),
                    "relation": relation,
                    "company_id": company_id if relation == "historical" else None,
                    "pending_identity_count": pending,
                    "can_adopt": relation == "unbound" and blocker is None,
                    "adoption_blocker_code": blocker,
                })
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": (total + page_size - 1) // page_size,
            }

    def create_current_analysis(self, company_id, request):
        with self._write() as s:
            if self._current(s, company_id) is not None:
                raise WorkspaceError("WORKSPACE_CURRENT_ANALYSIS_EXISTS")
            self._cas_company(s, company_id, request.expected_company_version)
            company = self._company(s, company_id)
            analysis = AnalysisBatch(id=str(request.id), name="当前材料库",
                company_display_name=company.display_name, assessment_profile=DESKTOP_PROFILE)
            s.add(analysis)
            s.flush()
            s.add(CompanyAnalysisBinding(analysis_id=analysis.id, company_id=company_id, role="current"))
            from qian_labor.models.core import AssessmentDecision, new_id
            s.add(AssessmentDecision(id=new_id(), analysis_id=analysis.id, company_id=company_id,
                scope="check_date", kind="check_date", version=1, value=analysis.created_at.date().isoformat(),
                reason="建立当前材料库时的核查日期"))
            s.flush()
            return self._current(s, company_id)

    def current(self, company_id, search, page, page_size, severity=None, assessment_state=None):
        filtering = severity is not None or assessment_state is not None
        listing = self.employees(company_id, search, 1 if filtering else page,
                                 page_size, paginate=not filtering)
        with self.database.session() as s:
            current = self._current(s, company_id)
            employees = []
            for record in listing["items"]:
                binding = s.scalar(select(EmployeeSnapshotBinding).where(
                    EmployeeSnapshotBinding.employee_record_id == record.id,
                    EmployeeSnapshotBinding.analysis_id == current["analysis_id"])) if current else None
                snapshot = s.get(Employee, binding.snapshot_id) if binding else None
                supported = binding and s.scalar(select(EmploymentFact.id).join(SourceLocator,
                    SourceLocator.fact_id == EmploymentFact.id).join(UploadedFile,
                    UploadedFile.id == SourceLocator.file_id).where(
                    EmploymentFact.employee_id == binding.snapshot_id,
                    EmploymentFact.analysis_id == current["analysis_id"],
                    SourceLocator.analysis_id == EmploymentFact.analysis_id,
                    SourceLocator.file_id == EmploymentFact.file_id,
                    UploadedFile.analysis_id == EmploymentFact.analysis_id,
                    UploadedFile.classified_kind.not_in({"payroll", "attendance"}),
                ).limit(1))
                state = "pending_evidence" if not supported else (
                    "pending_analysis" if current["stale"] or snapshot.match_status not in {"confirmed", "auto_matched"}
                    else "evaluated")
                employees.append({**record.model_dump(), "current_binding":
                    SnapshotBindingView.model_validate(binding) if binding else None,
                    "snapshot_employee_id": snapshot.id if snapshot else None,
                    "employment_status": snapshot.employment_status if snapshot else None,
                    "assessment_state": state, "assessment": None})
            snapshot_ids = {e["snapshot_employee_id"] for e in employees if e["snapshot_employee_id"]}
            if current and snapshot_ids:
                summaries = {e["id"]: e for e in DashboardService(self.database).employees(
                    current["analysis_id"], employee_ids=snapshot_ids, paginate=False)["items"]}
                for employee in employees:
                    summary = summaries.get(employee["snapshot_employee_id"])
                    if summary:
                        employee["employment_status"] = summary["employment_status"]
                        if employee["assessment_state"] == "evaluated":
                            employee["assessment"] = {key: summary[key] for key in (
                                "risk_counts", "insufficient_data_count", "requires_human_review_count", "material_coverage")}
            if filtering:
                employees = [e for e in employees if
                    (assessment_state is None or e["assessment_state"] == assessment_state) and
                    (severity is None or (e["assessment"] is not None and e["assessment"]["risk_counts"][severity] > 0))]
                total = len(employees)
                employees = employees[(page - 1) * page_size:page * page_size]
                listing.update(total=total, page=page, page_size=page_size,
                               pages=(total + page_size - 1) // page_size)
            return {"company": CompanyView.model_validate(self._company(s, company_id)),
                    "enrolled_employee_count": s.scalar(select(func.count()).select_from(EmployeeRecord).where(EmployeeRecord.company_id == company_id)) or 0,
                    "current_analysis": current, "employees": employees,
                    "pending_identity_count": current["pending_identity_count"] if current else 0,
                    **{key: listing[key] for key in ("total", "page", "page_size", "pages")}}

    def employees(self, company_id, search, page, page_size, *, paginate=True):
        with self.database.session() as s:
            self._company(s, company_id)
            query = select(EmployeeRecord).where(EmployeeRecord.company_id == company_id)
            if search:
                query = query.where(or_(*[column.contains(search, autoescape=True) for column in (
                    EmployeeRecord.masked_name, EmployeeRecord.employee_number,
                    EmployeeRecord.department, EmployeeRecord.job_title)]))
            total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
            query = query.order_by(EmployeeRecord.created_at, EmployeeRecord.id)
            if paginate:
                query = query.offset((page - 1) * page_size).limit(page_size)
            items = s.scalars(query)
            return {"items": [EmployeeView.model_validate(i) for i in items], "total": total,
                    "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}

    def employee(self, company_id, record_id):
        with self.database.session() as s:
            self._company(s, company_id)
            item = s.get(EmployeeRecord, record_id)
            if item is None or item.company_id != company_id:
                raise WorkspaceError("WORKSPACE_EMPLOYEE_NOT_FOUND", 404)
            bindings = list(s.scalars(select(EmployeeSnapshotBinding).where(
                EmployeeSnapshotBinding.employee_record_id == record_id).order_by(EmployeeSnapshotBinding.created_at, EmployeeSnapshotBinding.snapshot_id))
            )
            current = [b for b in bindings if s.get(CompanyAnalysisBinding, b.analysis_id).role == "current"]
            return {**EmployeeView.model_validate(item).model_dump(),
                    "bindings": [SnapshotBindingView.model_validate(b) for b in bindings],
                    "current_binding": SnapshotBindingView.model_validate(current[0]) if current else None,
                    "historical_bindings": [SnapshotBindingView.model_validate(b) for b in bindings if b not in current]}

    def _binding(self, s, company_id, analysis_id):
        company = self._company(s, company_id)
        analysis = s.get(AnalysisBatch, analysis_id)
        if analysis is None or analysis.deleted_at is not None or analysis.status == "deleting":
            raise WorkspaceError("DESKTOP_ANALYSIS_NOT_FOUND", 404)
        owner = s.get(CompanyAnalysisBinding, analysis_id)
        if owner is not None and owner.company_id != company_id:
            raise WorkspaceError("WORKSPACE_CROSS_COMPANY_FORBIDDEN")
        bindings = s.scalars(select(EmployeeSnapshotBinding).where(
            EmployeeSnapshotBinding.analysis_id == analysis_id).order_by(EmployeeSnapshotBinding.snapshot_id))
        return {"company_id": company_id, "analysis_id": analysis_id, "bound": owner is not None,
                "role": owner.role if owner else None,
                "company_version": company.version, "analysis_version": analysis.version,
                "bindings": [SnapshotBindingView.model_validate(b) for b in bindings],
                "excluded_merged_snapshot_ids": list(s.scalars(select(Employee.id).where(
                    Employee.analysis_id == analysis_id, Employee.employment_status == "merged").order_by(Employee.id)))}

    def binding(self, company_id, analysis_id):
        with self.database.session() as s:
            return self._binding(s, company_id, analysis_id)

    def adopt(self, company_id, analysis_id, request):
        """Caller must hold DesktopProcessingQueue.mutation for this analysis."""
        with self._write() as s:
            existing = self._binding(s, company_id, analysis_id)
            if existing["bound"]:
                raise WorkspaceError("WORKSPACE_ANALYSIS_ALREADY_BOUND")
            analysis = s.get(AnalysisBatch, analysis_id)
            if analysis.status not in {"completed", "partial", "failed"}:
                raise WorkspaceError("WORKSPACE_ANALYSIS_NOT_SETTLED")
            snapshots = {e.id: e for e in s.scalars(select(Employee).where(
                Employee.analysis_id == analysis_id, Employee.employment_status != "merged"))}
            if any(e.match_status not in {"confirmed", "auto_matched"} for e in snapshots.values()) or s.scalar(
                select(EmployeeMatchCandidate.id).where(EmployeeMatchCandidate.analysis_id == analysis_id,
                    EmployeeMatchCandidate.status == "pending").limit(1)):
                raise WorkspaceError("WORKSPACE_MATCHING_UNRESOLVED")
            ids = [d.snapshot_id for d in request.decisions]
            if len(ids) != len(set(ids)) or set(ids) != set(snapshots):
                raise WorkspaceError("WORKSPACE_SNAPSHOT_SET_CONFLICT")
            self._cas_company(s, company_id, request.expected_company_version)
            if s.execute(update(AnalysisBatch).where(AnalysisBatch.id == analysis_id,
                AnalysisBatch.version == request.expected_analysis_version).values(version=AnalysisBatch.version + 1)).rowcount != 1:
                raise WorkspaceError("WORKSPACE_ANALYSIS_VERSION_CONFLICT")
            s.add(CompanyAnalysisBinding(analysis_id=analysis_id, company_id=company_id, role="historical"))
            s.flush()
            selected = set()
            for decision in request.decisions:
                if decision.action == "create":
                    target = self._new_record(s, company_id, decision.record)
                else:
                    target = s.get(EmployeeRecord, str(decision.employee_record_id))
                    if target is None or target.company_id != company_id:
                        raise WorkspaceError("WORKSPACE_CROSS_COMPANY_FORBIDDEN")
                    if target.lifecycle_status != "active":
                        raise WorkspaceError("WORKSPACE_EMPLOYEE_ARCHIVED")
                    if target.version != decision.expected_record_version:
                        raise WorkspaceError("WORKSPACE_EMPLOYEE_VERSION_CONFLICT")
                    old_number = (snapshots[decision.snapshot_id].employee_number or "").strip() or None
                    if old_number is not None and target.employee_number is not None and old_number != target.employee_number:
                        raise WorkspaceError("WORKSPACE_EMPLOYEE_NUMBER_MISMATCH")
                    target.version += 1
                if target.id in selected:
                    raise WorkspaceError("WORKSPACE_DUPLICATE_IDENTITY_DECISION")
                selected.add(target.id)
                s.add(EmployeeSnapshotBinding(snapshot_id=decision.snapshot_id, analysis_id=analysis_id,
                    company_id=company_id, employee_record_id=target.id))
            s.flush()
            result = self._binding(s, company_id, analysis_id)
        return result

    def preference(self):
        with self.database.session() as s:
            item = s.get(WorkspacePreference, 1)
            return {"last_company_id": item.last_company_id if item else None, "version": item.version if item else 0}

    def set_preference(self, request):
        with self._write() as s:
            company_id = str(request.last_company_id) if request.last_company_id else None
            if company_id is not None:
                self._company(s, company_id)
            item = s.get(WorkspacePreference, 1)
            if (item.version if item else 0) != request.expected_version:
                raise WorkspaceError("WORKSPACE_PREFERENCE_VERSION_CONFLICT")
            if item is None:
                item = WorkspacePreference(id=1)
                s.add(item)
            item.last_company_id, item.version = company_id, request.expected_version + 1
            result = {"last_company_id": company_id, "version": item.version}
        return result
