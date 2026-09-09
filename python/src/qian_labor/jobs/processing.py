from __future__ import annotations
from qian_labor.security.filenames import display_filename

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from qian_labor.jobs.control import ProcessingStopped, adapter_call, checkpoint, commit_boundary

from sqlalchemy import func, select

from qian_labor.ai.providers import (
    AIProvider,
    AIDiagnostic,
    AIProviderError,
    FakeAIProvider,
    OpenAIResponsesProvider,
)
from qian_labor.ai.schemas import ExtractionResult, SourceLocator
from qian_labor.ai.grounding import EXTRACTION_VERSION, MAX_TEXT_CHARACTERS, PROOF_KEY, ExtractionInput, ground_result
from qian_labor.database import Database, create_database
from qian_labor.matching.scoring import score_candidate
from qian_labor.matching.types import CandidateIdentity
from qian_labor.models.core import (
    AIUsageRecord,
    AnalysisBatch,
    Employee,
    EmployeeMatchCandidate,
    EmploymentFact,
    ProcessingJob,
    UploadedFile,
    CompanyAnalysisBinding, EmployeeSnapshotBinding,
    AuditEvent,
)
from qian_labor.models.core import (
    ParsedBlock as ParsedBlockModel,
)
from qian_labor.models.core import (
    ParsedDocument as ParsedDocumentModel,
)
from qian_labor.models.core import (
    SourceLocator as SourceLocatorModel,
)
from qian_labor.parsers.protocols import ParsedDocument, ParsedBlock
from qian_labor.parsers.registry import ParserRegistry
from qian_labor.security.local_redaction import (
    IdentifierEvidence,
    PreparedProviderInput,
    PrivacyBoundary,
    PrivacyBoundaryError,
    valid_external_pepper,
)
from qian_labor.security.masking import mask_identity, mask_sensitive
from qian_labor.services.risk_evaluation import RiskEvaluationService
from qian_labor.services.company_workspaces import require_material_mutation
from qian_labor.services.source_provenance import CITATION_KEY, deterministic_citation_id
from qian_labor.settings import Settings
from qian_labor.storage.local import LocalStorage


# Keep the provider request small enough that the JSON contract and advisory
# can finish within one response.  The limit applies to serialized parser
# blocks, not to an arbitrary slice of UTF-8 bytes, so a source block is never
# silently split or dropped.
MAX_EXTRACTION_CHUNK_CHARACTERS = 16_000


def provider_from_settings(settings: Settings) -> AIProvider:
    if settings.ai_provider == "fake":
        return FakeAIProvider()
    if settings.ai_provider in {"openai", "openai-responses"}:
        if (
            not valid_external_pepper(settings.pii_hash_pepper)
            or settings.pii_hash_pepper == settings.app_secret
        ):
            raise AIProviderError("AI_PRIVACY_CONFIG_INVALID")
        return OpenAIResponsesProvider(
            settings.ai_api_key,
            settings.ai_base_url,
            settings.ai_text_model,
            settings.ai_vision_model,
            batch_budget_usd=settings.ai_batch_budget_usd,
            privacy_boundary=PrivacyBoundary(settings.pii_hash_pepper),
        )
    raise RuntimeError("AI_PROVIDER_UNSUPPORTED")


class ProcessingPipeline:
    def __init__(
        self,
        database: Database,
        storage: LocalStorage,
        provider: AIProvider | None = None,
        parser_registry: ParserRegistry | None = None,
        privacy_boundary: PrivacyBoundary | None = None,
        max_provider_calls: int = 100,
    ) -> None:
        self.database = database
        self.storage = storage
        self.provider = provider or FakeAIProvider()
        self.parsers = parser_registry or ParserRegistry()
        self.privacy_boundary = privacy_boundary or PrivacyBoundary("")
        self.max_provider_calls = max(1, max_provider_calls)
        self._provider_calls = 0
        self._estimated_cost_usd = 0.0

    def process(self, analysis_id: str) -> dict[str, Any]:
        checkpoint()
        with self.database.session() as session:
            require_material_mutation(session, analysis_id)
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            if analysis.status == "completed":
                return self._status_payload(session, analysis)
            analysis.status = "parsing"
            analysis.current_stage = "parsing"
            analysis.progress = 5
            analysis.failure_reason = None
            files = list(
                session.scalars(
                    select(UploadedFile)
                    .where(UploadedFile.analysis_id == analysis_id)
                    .order_by(UploadedFile.created_at)
                )
            )
            session.commit()

        failures = 0
        incomplete = 0
        failure_codes: list[str] = []
        for uploaded_file in files:
            try:
                checkpoint()
                incomplete += bool(self._process_file(analysis_id, uploaded_file.id))
            except ProcessingStopped:
                raise
            except PrivacyBoundaryError:
                failures += 1
                failure_codes.append("AI_LOCAL_REDACTION_FAILED")
                self._mark_file_failed(uploaded_file.id, "AI_LOCAL_REDACTION_FAILED",
                                        AIDiagnostic(category="privacy"))
            except AIProviderError as error:
                failures += 1
                error_code = str(error)
                if not re.fullmatch(r"AI_[A-Z0-9_]+", error_code):
                    error_code = "PROCESSING_FILE_FAILED"
                failure_codes.append(error_code)
                self._mark_file_failed(uploaded_file.id, error_code, error.diagnostic)
            except ValueError as error:
                failures += 1
                error_code = 'SPREADSHEET_DIMENSION_LIMIT' if str(error) == 'SPREADSHEET_DIMENSION_LIMIT' else 'PROCESSING_FILE_FAILED'
                failure_codes.append(error_code)
                self._mark_file_failed(uploaded_file.id, error_code,
                                        AIDiagnostic(category="schema", validation_type="invalid_value"))
            except Exception:
                failures += 1
                failure_codes.append("PROCESSING_FILE_FAILED")
                self._mark_file_failed(uploaded_file.id, "PROCESSING_FILE_FAILED",
                                        AIDiagnostic(category="provider"))

        checkpoint()
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            pending_matches = int(
                session.scalar(
                    select(func.count())
                    .select_from(EmployeeMatchCandidate)
                    .where(
                        EmployeeMatchCandidate.analysis_id == analysis_id,
                        EmployeeMatchCandidate.status == "pending",
                    )
                )
                or 0
            )
            all_files_failed = bool(files) and failures == len(files)
            analysis.status = (
                "failed"
                if all_files_failed
                else "matching_review"
                if pending_matches
                else "evaluating"
            )
            analysis.current_stage = analysis.status
            analysis.progress = 100 if all_files_failed else 90 if pending_matches else 92
            analysis.failure_reason = failure_codes[0] if failures else None
            analysis.employee_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(Employee)
                    .where(Employee.analysis_id == analysis_id)
                )
                or 0
            )
            session.commit()
            session.refresh(analysis)
            if all_files_failed:
                return self._status_payload(session, analysis)

        evaluator = RiskEvaluationService(self.database)
        checkpoint()
        if pending_matches:
            evaluator.evaluate_data_quality(analysis_id)
        else:
            evaluator.evaluate_analysis(analysis_id)
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            if (failures or incomplete) and analysis.status == "completed":
                analysis.status = "partial"
                analysis.current_stage = "partial"
            if analysis.status == "completed":
                analysis.completed_at = datetime.now(UTC)
            session.commit()
            return self._status_payload(session, analysis)

    def _process_file(self, analysis_id: str, file_id: str) -> bool:
        with self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            if uploaded_file is None:
                raise KeyError(file_id)
            content = self.storage.read_bytes(uploaded_file.storage_key)

        parsed = self._parse(analysis_id, file_id, content)
        checkpoint()
        reused = self._extract(analysis_id, file_id, content, parsed)
        if reused:
            # The cache read session has closed before taking the commit guard.
            # Reuse restores file state, never cached facts/sources/advisory IDs.
            with commit_boundary(), self.database.session() as session:
                uploaded_file = session.get(UploadedFile, file_id)
                uploaded_file.status = "partial" if parsed.warnings else "processed"
                uploaded_file.error_code = "PROCESSING_INCOMPLETE" if parsed.warnings else None
                uploaded_file.progress = 100
                uploaded_file.detected_kind = parsed.kind
                session.commit()
            self._refresh_analysis_progress(analysis_id)
        return bool(parsed.warnings)

    @classmethod
    def cached_extraction(cls, session, analysis_id, uploaded_file, provider):
        key = cls._job_key(analysis_id, uploaded_file.id, "extract", uploaded_file.sha256)
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.unique_key == key))
        if job is None or job.status != "succeeded":
            return False
        has_facts = session.scalar(select(EmploymentFact.id).where(
            EmploymentFact.analysis_id == analysis_id, EmploymentFact.file_id == uploaded_file.id).limit(1))
        from qian_labor.models.core import ContractAdvisoryRun
        advisory_run = session.scalar(select(ContractAdvisoryRun).where(
            ContractAdvisoryRun.analysis_id == analysis_id, ContractAdvisoryRun.file_id == uploaded_file.id,
            ContractAdvisoryRun.input_key == f"{key}:attempt:{job.attempts}"))
        upgrade_advisory = bool(getattr(provider, "supports_contract_advisory", False)) and (
            advisory_run is None or any(status in {"unreadable", "not_executed"} for status in advisory_run.input_statuses))
        return bool((has_facts or advisory_run) and not upgrade_advisory)

    def _parse(self, analysis_id: str, file_id: str, content: bytes) -> ParsedDocument:
        with self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            if uploaded_file is None:
                raise KeyError(file_id)
            key = self._job_key(analysis_id, file_id, "parse", uploaded_file.sha256)
            job = session.scalar(select(ProcessingJob).where(ProcessingJob.unique_key == key))
            if job and job.status == "succeeded":
                return self.parsers.parse(uploaded_file.original_filename, content)
            if job is None:
                job = ProcessingJob(
                    analysis_id=analysis_id,
                    file_id=file_id,
                    job_type="parse",
                    input_hash=uploaded_file.sha256,
                    unique_key=key,
                    attempts=0,
                )
                session.add(job)
            job.status = "running"
            job.error_code = None
            job.attempts += 1
            job.started_at = datetime.now(UTC)
            uploaded_file.status = "parsing"
            uploaded_file.progress = 15
            session.commit()
        self._refresh_analysis_progress(analysis_id)

        parsed = self.parsers.parse(uploaded_file.original_filename, content)
        with self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            job = session.scalar(select(ProcessingJob).where(ProcessingJob.unique_key == key))
            if uploaded_file is None or job is None:
                raise KeyError(file_id)
            existing = session.scalar(
                select(ParsedDocumentModel).where(ParsedDocumentModel.file_id == file_id)
            )
            if existing is None:
                document = ParsedDocumentModel(
                    file_id=file_id,
                    parser_name=parsed.kind,
                    detected_kind=parsed.kind,
                    needs_vision=parsed.needs_vision,
                    warnings=parsed.warnings,
                    content_hash=hashlib.sha256(content).hexdigest(),
                )
                session.add(document)
                session.flush()
                for position, block in enumerate(parsed.blocks):
                    session.add(
                        ParsedBlockModel(
                            document_id=document.id,
                            position=position,
                            block_type=block.block_type,
                            text=mask_sensitive(block.text),
                            locator=block.locator,
                            content_hash=hashlib.sha256(block.text.encode()).hexdigest(),
                        )
                    )
            uploaded_file.detected_kind = parsed.kind
            uploaded_file.page_count = (
                max(
                    [page.page for page in parsed.vision_pages if isinstance(page.page, int) and page.page > 0]
                    + [int(block.locator.get("page", 0)) for block in parsed.blocks]
                    + [0]
                )
                or None
            )
            uploaded_file.progress = 45
            job.status = "succeeded"
            job.completed_at = datetime.now(UTC)
            session.commit()
        self._refresh_analysis_progress(analysis_id)
        return parsed

    def _extract(
        self, analysis_id: str, file_id: str, content: bytes, parsed: ParsedDocument
    ) -> bool:
        with self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            if uploaded_file is None:
                raise KeyError(file_id)
            key = self._job_key(analysis_id, file_id, "extract", uploaded_file.sha256)
            job = session.scalar(select(ProcessingJob).where(ProcessingJob.unique_key == key))
            if self.cached_extraction(session, analysis_id, uploaded_file, self.provider):
                return True
            if job is None:
                job = ProcessingJob(
                    analysis_id=analysis_id,
                    file_id=file_id,
                    job_type="extract",
                    input_hash=uploaded_file.sha256,
                    unique_key=key,
                    attempts=0,
                )
                session.add(job)
            job.status = "running"
            job.attempts += 1
            job.started_at = datetime.now(UTC)
            uploaded_file.status = "extracting"
            uploaded_file.progress = 55
            uploaded_file.error_code = None
            session.commit()
            original_filename = uploaded_file.original_filename
            job_attempt = job.attempts
            persisted_calls = int(
                session.scalar(
                    select(func.count())
                    .select_from(AIUsageRecord)
                    .where(
                        AIUsageRecord.analysis_id == analysis_id,
                        AIUsageRecord.operation == "extraction",
                    )
                )
                or 0
            )
            persisted_cost = float(
                session.scalar(
                    select(func.sum(AIUsageRecord.estimated_cost_usd)).where(
                        AIUsageRecord.analysis_id == analysis_id,
                        AIUsageRecord.operation == "extraction",
                    )
                )
                or 0
            )
        self._refresh_analysis_progress(analysis_id)

        inputs = self._extraction_inputs(original_filename, content, parsed)
        # Check every text input before the first provider call for this file.
        if any(Path(item.filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
               and len(item.content.decode("utf-8", errors="replace")) > MAX_TEXT_CHARACTERS for item in inputs):
            raise AIProviderError("AI_TEXT_LIMIT_EXCEEDED")
        self._provider_calls = max(self._provider_calls, persisted_calls)
        self._estimated_cost_usd = max(self._estimated_cost_usd, persisted_cost)
        if self._provider_calls + len(inputs) > self.max_provider_calls:
            raise AIProviderError("AI_CALL_LIMIT_EXCEEDED")
        budget = getattr(self.provider, "batch_budget_usd", None)
        if isinstance(budget, (int, float)) and self._estimated_cost_usd >= budget:
            raise AIProviderError("AI_BUDGET_EXCEEDED")
        external = bool(getattr(self.provider, "is_external", self.provider.name != "fake"))
        prepared_inputs = [
            self.privacy_boundary.prepare(
                filename,
                payload,
                is_image=Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"},
                external=external,
            )
            for filename, payload in inputs
        ]
        results: list[tuple[ExtractionResult, PreparedProviderInput, list[dict]]] = []
        advisory_grounding = []
        for index, item in enumerate(prepared_inputs):
            usage_key = hashlib.sha256(f"{key}:usage:{job_attempt}:{index}".encode()).hexdigest()
            with adapter_call(lambda: self._begin_usage_record(
                    analysis_id, file_id, usage_key, attempt=job_attempt)):
                self._provider_calls += 1
                result = self.provider.extract(item.filename, item.content)
                # Returned usage remains durable even when cancellation discards facts.
                self._complete_usage_record(usage_key, result)
            checkpoint()
            context = inputs[index]
            if item.ocr_blocks:
                image_hints: dict[str, Any] = {}
                for block in context.blocks:
                    if block.block_type == "image_context":
                        image_hints.update(block.locator)
                page_hint = {"page": context.page} if isinstance(context.page, int) and context.page > 0 else {}
                ocr_blocks = tuple(
                    ParsedBlock(b.text, b.block_type, {**image_hints, **b.locator, **page_hint})
                    for b in item.ocr_blocks
                )
                image_context = tuple(
                    block for block in context.blocks if block.block_type == "image_context"
                )
                context = ExtractionInput(
                    context.filename,
                    context.content,
                    ocr_blocks + image_context,
                    context.page,
                )
            grounding = ground_result(result, context, original_filename)
            from qian_labor.ai.grounding import ground_advisory
            advisory_grounding.append(ground_advisory(result.contract_advisory, context, original_filename,
                result.employee_number) if result.contract_advisory else [])
            projected_cost = self._estimated_cost_usd + result.usage.estimated_cost_usd
            self._estimated_cost_usd = projected_cost
            if isinstance(budget, (int, float)) and projected_cost > budget:
                raise AIProviderError("AI_BUDGET_EXCEEDED")
            results.append((result, item, grounding))

        if not any(result.facts or result.contract_advisory is not None for result, _, _ in results):
            raise AIProviderError("AI_NO_SUPPORTED_FACTS")

        with commit_boundary(), self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            job = session.scalar(select(ProcessingJob).where(ProcessingJob.unique_key == key))
            if uploaded_file is None or job is None:
                raise KeyError(file_id)
            assignments = []
            for result, prepared, grounding in results:
                assignments.append(self._persist_result(session, analysis_id, uploaded_file, result, prepared=prepared, grounding=grounding))
            from qian_labor.services.contract_advisory import persist_run
            persist_run(session, analysis_id, uploaded_file, f"{key}:attempt:{job_attempt}", results, advisory_grounding, assignments,
                        has_warnings=bool(parsed.warnings))
            document_types = [result.document_type for result, _, _ in results]
            uploaded_file.classified_kind = next(
                (kind for kind in document_types if kind != "other"),
                document_types[0] if document_types else "other",
            )
            uploaded_file.status = "partial" if parsed.warnings else "processed"
            uploaded_file.error_code = "PROCESSING_INCOMPLETE" if parsed.warnings else None
            uploaded_file.progress = 100
            uploaded_file.detected_kind = parsed.kind
            job.status = "succeeded"
            job.completed_at = datetime.now(UTC)
            session.commit()
        self._refresh_analysis_progress(analysis_id)
        return False

    def _begin_usage_record(
        self,
        analysis_id: str,
        file_id: str,
        idempotency_key: str,
        *,
        attempt: int,
    ) -> None:
        with self.database.session() as session:
            if session.scalar(
                select(AIUsageRecord).where(AIUsageRecord.idempotency_key == idempotency_key)
            ):
                return
            session.add(
                AIUsageRecord(
                    analysis_id=analysis_id,
                    file_id=file_id,
                    provider=self.provider.name,
                    model="server-configured" if self.provider.name != "fake" else "fake",
                    operation="extraction",
                    attempt=attempt,
                    status="running",
                    idempotency_key=idempotency_key,
                )
            )
            session.commit()

    def _complete_usage_record(self, idempotency_key: str, result: ExtractionResult) -> None:
        with self.database.session() as session:
            usage = session.scalar(
                select(AIUsageRecord).where(AIUsageRecord.idempotency_key == idempotency_key)
            )
            if usage is None:
                raise RuntimeError("AI_USAGE_RECORD_MISSING")
            usage.input_units = result.usage.input_tokens
            usage.output_units = result.usage.output_tokens
            usage.estimated_cost_usd = result.usage.estimated_cost_usd
            usage.latency_ms = result.usage.latency_ms
            usage.status = "succeeded"
            session.commit()

    @staticmethod
    def _extraction_inputs(
        original_filename: str, content: bytes, parsed: ParsedDocument
    ) -> list[ExtractionInput]:
        if parsed.kind == "image":
            return [ExtractionInput(original_filename, content, page=1)]

        inputs: list[ExtractionInput] = []
        blocks_by_page: dict[int, list] = {}
        document_blocks: list = []
        for block in parsed.blocks:
            page = block.locator.get("page")
            if isinstance(page, int) and page > 0:
                blocks_by_page.setdefault(page, []).append(block)
            else:
                document_blocks.append(block)

        def payload(blocks):
            return "\n".join(f"[source {json.dumps(b.locator, ensure_ascii=False)}]\n{b.text}" for b in blocks).encode()

        def atomic_units(blocks: list[ParsedBlock]) -> list[tuple[ParsedBlock, ...]]:
            """Keep table/spreadsheet rows together before applying the budget.

            Parser blocks are often individual cells.  Splitting at that level
            can separate an employee identifier from the date or value that
            proves it belongs to the same employee.  A row key is deliberately
            scoped by sheet and table so equal row numbers in different source
            regions cannot be merged.
            """
            units: list[list[ParsedBlock]] = []
            row_units: dict[tuple[object, object, object], int] = {}
            for block in blocks:
                locator = block.locator
                row = locator.get("row")
                is_row_block = block.block_type != "header" and isinstance(row, int) and (
                    "sheet" in locator or "table" in locator
                )
                if not is_row_block:
                    units.append([block])
                    continue
                key = (locator.get("sheet"), locator.get("table"), row)
                unit_index = row_units.get(key)
                if unit_index is None:
                    row_units[key] = len(units)
                    units.append([block])
                else:
                    units[unit_index].append(block)
            return [tuple(unit) for unit in units]

        def chunk(blocks: list[ParsedBlock]) -> list[tuple[ParsedBlock, ...]]:
            chunks: list[tuple[ParsedBlock, ...]] = []
            current: list[ParsedBlock] = []
            for unit in atomic_units(blocks):
                single = payload(list(unit))
                if len(single.decode("utf-8", errors="replace")) > MAX_EXTRACTION_CHUNK_CHARACTERS:
                    raise AIProviderError("AI_TEXT_LIMIT_EXCEEDED")
                candidate = payload([*current, *unit])
                if current and len(candidate.decode("utf-8", errors="replace")) > MAX_EXTRACTION_CHUNK_CHARACTERS:
                    chunks.append(tuple(current))
                    current = []
                current.extend(unit)
            if current:
                chunks.append(tuple(current))
            return chunks

        for page, texts in sorted(blocks_by_page.items()):
            page_chunks = chunk(texts)
            for part, blocks in enumerate(page_chunks, start=1):
                suffix = f"-part-{part}" if len(page_chunks) > 1 else ""
                inputs.append(ExtractionInput(
                    f"{original_filename}-page-{page}{suffix}.txt",
                    payload(list(blocks)),
                    blocks,
                    page,
                ))
        if document_blocks:
            document_chunks = chunk(document_blocks)
            base = (
                f"{original_filename}-document"
                if blocks_by_page or parsed.vision_pages
                else original_filename
            )
            for part, blocks in enumerate(document_chunks, start=1):
                if len(document_chunks) == 1 and not blocks_by_page and not parsed.vision_pages:
                    filename = base
                else:
                    suffix = f"-part-{part}" if len(document_chunks) > 1 else ""
                    filename = f"{base}{suffix}.txt"
                inputs.append(ExtractionInput(filename, payload(list(blocks)), blocks))
        for page in parsed.vision_pages:
            locator = getattr(page, "locator", {}) or {}
            image_index = locator.get("image")
            if image_index is not None:
                filename = f"{original_filename}-image-{image_index}.png"
            else:
                filename = f"{original_filename}-page-{page.page}.png"
            context = (
                (ParsedBlock("", "image_context", dict(locator)),)
                if locator
                else ()
            )
            inputs.append(ExtractionInput(filename, page.image_bytes, context, page.page))
        if not inputs:
            raise AIProviderError("AI_NO_SUPPORTED_FACTS")
        return inputs

    @staticmethod
    def _bind_spreadsheet_sources(
        result: ExtractionResult, parsed: ParsedDocument, filename: str
    ) -> None:
        """Use parser-owned row evidence, never a model-invented coordinate/excerpt."""
        if parsed.kind != "spreadsheet":
            return
        rows: dict[tuple[str, int], list[str]] = {}
        for block in parsed.blocks:
            row = block.locator.get("row")
            if block.block_type == "header" or not isinstance(row, int) or not block.text.strip():
                continue
            key = (str(block.locator.get("sheet", "")), row)
            rows.setdefault(key, []).extend(part.strip() for part in block.text.split(" | "))

        for fact in result.facts:
            identity = fact.employee_id or result.employee_number
            candidates = [
                key for key, cells in rows.items() if identity and identity in cells
            ]
            if not identity and len(rows) == 1:
                candidates = list(rows)
            # An exact, sufficiently long original excerpt can disambiguate rows.
            excerpt = fact.source.excerpt.strip()
            if len(candidates) != 1 and len(excerpt) >= 12:
                matching = [
                    key for key in (candidates or list(rows))
                    if excerpt in " | ".join(rows[key])
                ]
                if len(matching) == 1:
                    candidates = matching
            if len(candidates) == 1:
                sheet, row = candidates[0]
                fact.source = SourceLocator(
                    file_name=filename,
                    sheet=sheet or None,
                    row=row,
                    excerpt=" | ".join(rows[candidates[0]]),
                )
            else:
                fact.source = SourceLocator(file_name=filename)
                fact.needs_human_confirmation = True

    def _persist_result(
        self,
        session: Any,
        analysis_id: str,
        uploaded_file: UploadedFile,
        result: ExtractionResult,
        prepared: PreparedProviderInput | None = None,
        grounding: list[dict] | None = None,
    ) -> tuple:
        evidence = prepared.identifier_evidence if prepared else ()
        local_hashes = prepared.identifier_hashes if prepared else {}
        hash_values = self._hash_values(evidence, local_hashes)
        unique_hashes = {
            field: next(iter(values)) for field, values in hash_values.items() if len(values) == 1
        }
        employee_ids = {
            value
            for value in [result.employee_number, *(fact.employee_id for fact in result.facts)]
            if value
        }
        # Canonical facts retain their own result identity even when a separate
        # advisory names another employee. Clause-only inputs can still use the
        # existing explicit matching flow without fabricating employment facts.
        if not result.facts and result.contract_advisory:
            employee_ids.update(o.employee_number for o in result.contract_advisory.observations if o.employee_number)
        employee_number = next(iter(employee_ids)) if len(employee_ids) == 1 else None
        employee = None
        pending_reason = None
        if len(employee_ids) > 1:
            pending_reason = "multiple_employee_ids"
        elif any(len(values) > 1 for values in hash_values.values()):
            pending_reason = "multiple_identifier_values"

        employees: list[Employee] = []
        scored: list[tuple[Employee, Any]] = []
        if pending_reason is None and (employee_number or unique_hashes):
            employees = list(
                session.scalars(
                    select(Employee).where(
                        Employee.analysis_id == analysis_id,
                        Employee.employment_status != "merged",
                    )
                )
            )
            source = CandidateIdentity(employee_number=employee_number, **unique_hashes)
            scored = [
                (
                    item,
                    score_candidate(
                        source,
                        CandidateIdentity(
                            employee_number=item.employee_number,
                            id_number_hash=item.id_number_hash,
                            phone_hash=item.phone_hash,
                            bank_card_hash=item.bank_card_hash,
                        ),
                    ),
                )
                for item in employees
            ]
        exact_hash_matches = [
            (item, score)
            for item, score in scored
            if not score.stable_identifier_conflict
            and any(reason.endswith("_hash_exact") for reason in score.reasons)
        ]
        number_match = next(
            (
                pair
                for pair in scored
                if employee_number and pair[0].employee_number == employee_number
            ),
            None,
        )
        touched_candidates: list[EmployeeMatchCandidate] = []
        source_scope_key = self._candidate_source_scope_key(result, prepared)
        workspace_owner = session.get(CompanyAnalysisBinding, analysis_id)
        is_current = workspace_owner is not None and workspace_owner.role == "current"

        if pending_reason:
            if pending_reason != "multiple_employee_ids":
                self._ensure_match_candidate(
                    session,
                    analysis_id,
                    uploaded_file.id,
                    None,
                    extracted_fields=self._evidence_payload(evidence, employee_ids),
                    reason=pending_reason,
                    score=0,
                    touched_candidates=touched_candidates,
                    source_scope_key=source_scope_key,
                )
        elif len(exact_hash_matches) > 1:
            for target, target_score in exact_hash_matches:
                target.match_status = "ambiguous"
                self._ensure_match_candidate(
                    session,
                    analysis_id,
                    uploaded_file.id,
                    target,
                    extracted_fields=self._evidence_payload(evidence, employee_ids),
                    reason="multiple_exact_hash_candidates",
                    score=target_score.score,
                    touched_candidates=touched_candidates,
                    source_scope_key=source_scope_key,
                )
        elif len(exact_hash_matches) == 1:
            hash_employee, hash_score = exact_hash_matches[0]
            if number_match is not None and number_match[0].id != hash_employee.id:
                for target, target_score in (number_match, (hash_employee, hash_score)):
                    target.match_status = "ambiguous"
                    self._ensure_match_candidate(
                        session,
                        analysis_id,
                        uploaded_file.id,
                        target,
                        extracted_fields=self._evidence_payload(evidence, employee_ids),
                        reason="employee_number_hash_disagreement",
                        score=target_score.score,
                        touched_candidates=touched_candidates,
                        source_scope_key=source_scope_key,
                    )
            elif (
                employee_number
                and hash_employee.employee_number
                and employee_number != hash_employee.employee_number
            ):
                hash_employee.match_status = "ambiguous"
                self._ensure_match_candidate(
                    session,
                    analysis_id,
                    uploaded_file.id,
                    hash_employee,
                    extracted_fields=self._evidence_payload(evidence, employee_ids),
                    reason="employee_number_changed",
                    score=hash_score.score,
                    touched_candidates=touched_candidates,
                    source_scope_key=source_scope_key,
                )
            else:
                employee = hash_employee
                self._reconcile_local_hashes(employee, unique_hashes)
        elif number_match is not None and number_match[1].stable_identifier_conflict:
            target = number_match[0]
            target.match_status = "ambiguous"
            self._ensure_match_candidate(
                session,
                analysis_id,
                uploaded_file.id,
                target,
                extracted_fields=self._evidence_payload(evidence, employee_ids),
                reason="stable_identifier_conflict",
                score=number_match[1].score,
                touched_candidates=touched_candidates,
                source_scope_key=source_scope_key,
            )
        elif number_match is not None and unique_hashes:
            target = number_match[0]
            target.match_status = "ambiguous"
            self._ensure_match_candidate(
                session,
                analysis_id,
                uploaded_file.id,
                target,
                extracted_fields=self._evidence_payload(evidence, employee_ids),
                reason="stable_hash_confirmation_required",
                score=number_match[1].score,
                touched_candidates=touched_candidates,
                source_scope_key=source_scope_key,
            )
        elif number_match is not None:
            employee = number_match[0]
        elif employee_number and is_current:
            self._ensure_match_candidate(
                session, analysis_id, uploaded_file.id, None,
                extracted_fields=self._evidence_payload(evidence, employee_ids),
                reason="workspace_identity_confirmation_required", score=0,
                touched_candidates=touched_candidates, source_scope_key=source_scope_key,
            )
        elif employee_number:
            display_name = result.employee_name or employee_number
            employee = Employee(
                analysis_id=analysis_id,
                masked_name=mask_identity(display_name),
                normalized_name=mask_identity(display_name),
                employee_number=employee_number,
                id_number_hash=unique_hashes.get("id_number_hash"),
                phone_hash=unique_hashes.get("phone_hash"),
                bank_card_hash=unique_hashes.get("bank_card_hash"),
                department=result.department,
                job_title=result.job_title,
                match_status="auto_matched",
            )
            session.add(employee)
            session.flush()
        else:
            self._ensure_match_candidate(
                session,
                analysis_id,
                uploaded_file.id,
                None,
                extracted_fields=self._evidence_payload(evidence, employee_ids),
                reason="unknown_employee_identity",
                score=0,
                touched_candidates=touched_candidates,
                source_scope_key=source_scope_key,
            )

        if employee is not None and is_current and session.get(EmployeeSnapshotBinding, employee.id) is None:
            self._ensure_match_candidate(
                session, analysis_id, uploaded_file.id, employee,
                extracted_fields=self._evidence_payload(evidence, employee_ids),
                reason="workspace_identity_confirmation_required", score=0,
                touched_candidates=touched_candidates, source_scope_key=source_scope_key,
            )
            employee = None

        identity_status = next(
            (
                str(fact.value)
                for fact in result.facts
                if fact.fact_type == "employment.identity.match_status"
            ),
            None,
        )
        if employee and identity_status in {"ambiguous", "unknown"}:
            matched_employee = employee
            matched_employee.match_status = identity_status
            self._ensure_match_candidate(
                session,
                analysis_id,
                uploaded_file.id,
                matched_employee,
                extracted_fields={"masked_name": matched_employee.masked_name},
                reason="identity_ambiguous",
                score=0.82,
                touched_candidates=touched_candidates,
                source_scope_key=source_scope_key,
            )

        persisted_fact_ids: list[str] = []
        fact_ids_by_source_employee: dict[str, list[str]] = {}
        # A confirmed matching decision changes employee ownership, but never the
        # original extraction identity. Reuse only explicitly scoped prior facts.
        decided_candidates = list(session.scalars(select(EmployeeMatchCandidate).where(
            EmployeeMatchCandidate.analysis_id == analysis_id,
            EmployeeMatchCandidate.file_id == uploaded_file.id,
            EmployeeMatchCandidate.status.in_({"confirmed", "unmatched"}),
        )))
        decided_fact_ids = {fid for candidate in decided_candidates
                            for fid in (candidate.extracted_fields or {}).get("fact_ids", [])}
        previous_employee_ids = {"", *(candidate.candidate_employee_id or "" for candidate in decided_candidates)}
        for fact_index, fact in enumerate(result.facts):
            value_text = json.dumps(fact.value, ensure_ascii=False, sort_keys=True)
            dedupe_key = hashlib.sha256(
                f"{analysis_id}:{uploaded_file.id}:{employee.id if employee else ''}:"
                f"{source_scope_key}:{EXTRACTION_VERSION}:{fact.employee_id or ''}:"
                f"{fact.fact_type}:{value_text}".encode()
            ).hexdigest()
            existing_fact = session.scalar(
                select(EmploymentFact).where(
                    EmploymentFact.analysis_id == analysis_id,
                    EmploymentFact.dedupe_key == dedupe_key,
                )
            )
            if existing_fact is None and decided_fact_ids:
                previous_keys = {hashlib.sha256(
                    f"{analysis_id}:{uploaded_file.id}:{previous_employee_id}:"
                    f"{source_scope_key}:{EXTRACTION_VERSION}:{fact.employee_id or ''}:"
                    f"{fact.fact_type}:{value_text}".encode()
                ).hexdigest() for previous_employee_id in previous_employee_ids}
                existing_fact = session.scalar(select(EmploymentFact).where(
                    EmploymentFact.analysis_id == analysis_id,
                    EmploymentFact.file_id == uploaded_file.id,
                    EmploymentFact.id.in_(decided_fact_ids),
                    EmploymentFact.dedupe_key.in_(previous_keys),
                ).order_by(EmploymentFact.created_at, EmploymentFact.id))
            stored_fact = existing_fact
            if stored_fact is None:
                stored_fact = EmploymentFact(
                    analysis_id=analysis_id,
                    employee_id=employee.id if employee else None,
                    file_id=uploaded_file.id,
                    fact_type=fact.fact_type,
                    value_json=fact.value,
                    normalized_value_json=fact.value,
                    extraction_method=self.provider.name,
                    confidence=fact.confidence,
                    verification_status=(
                        "needs_human_confirmation"
                        if fact.needs_human_confirmation or result.needs_human_confirmation
                        else "unverified"
                    ),
                    dedupe_key=dedupe_key,
                )
                session.add(stored_fact)
                session.flush()
            persisted_fact_ids.append(stored_fact.id)
            fact_identity = fact.employee_id or result.employee_number
            if fact_identity:
                fact_ids_by_source_employee.setdefault(fact_identity, []).append(stored_fact.id)
            location = fact.source.model_dump(exclude={"file_name", "excerpt"}, exclude_none=True)
            if grounding is not None:
                location[PROOF_KEY] = grounding[fact_index]
            excerpt = mask_sensitive(fact.source.excerpt)
            # The provider cannot choose this identity.  It is derived only
            # after local grounding has supplied the real parser position.
            location[CITATION_KEY] = deterministic_citation_id(
                uploaded_file.sha256, location, excerpt
            )
            locator_type = self._locator_type(location)
            location_key = json.dumps(location, sort_keys=True, ensure_ascii=False)
            existing_sources = session.scalars(select(SourceLocatorModel).where(
                SourceLocatorModel.analysis_id == analysis_id,
                SourceLocatorModel.file_id == uploaded_file.id,
                SourceLocatorModel.fact_id == stored_fact.id,
                SourceLocatorModel.locator_type == locator_type,
                SourceLocatorModel.excerpt == excerpt,
            ))
            if any(json.dumps(source.location, sort_keys=True, ensure_ascii=False) == location_key
                   for source in existing_sources):
                continue
            session.add(
                SourceLocatorModel(
                    analysis_id=analysis_id,
                    file_id=uploaded_file.id,
                    fact_id=stored_fact.id,
                    locator_type=locator_type,
                    location=location,
                    excerpt=excerpt,
                    content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                )
            )

        if pending_reason == "multiple_employee_ids":
            for source_employee_id in sorted(employee_ids):
                self._ensure_match_candidate(
                    session,
                    analysis_id,
                    uploaded_file.id,
                    None,
                    extracted_fields={
                        "employee_ids": [source_employee_id],
                        "fact_ids": fact_ids_by_source_employee.get(source_employee_id, []),
                    },
                    reason=pending_reason,
                    score=0,
                    source_employee_id=source_employee_id,
                    touched_candidates=touched_candidates,
                    source_scope_key=source_scope_key,
                )
        else:
            for pending_candidate in touched_candidates:
                fields = dict(pending_candidate.extracted_fields or {})
                existing_scope = fields.get("fact_ids", [])
                fields["fact_ids"] = list(
                    dict.fromkeys(
                        [
                            *(existing_scope if isinstance(existing_scope, list) else []),
                            *persisted_fact_ids,
                        ]
                    )
                )
                pending_candidate.extracted_fields = fields
        self._consolidate_candidate_fact_scopes(session, analysis_id, uploaded_file.id)
        session.flush()
        return employee, touched_candidates

    @staticmethod
    def _hash_values(
        evidence: tuple[IdentifierEvidence, ...], fallback: dict[str, str]
    ) -> dict[str, set[str]]:
        values: dict[str, set[str]] = {}
        for item in evidence:
            values.setdefault(item.field_name, set()).add(item.value_hash)
        for field, value in fallback.items():
            values.setdefault(field, set()).add(value)
        return values

    @staticmethod
    def _evidence_payload(
        evidence: tuple[IdentifierEvidence, ...], employee_ids: set[str]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "employee_ids": sorted(employee_ids),
            "identifier_evidence": [
                {
                    "field_name": item.field_name,
                    "value_hash": item.value_hash,
                    "locator": item.locator,
                }
                for item in evidence
            ],
        }
        values: dict[str, set[str]] = {}
        for item in evidence:
            values.setdefault(item.field_name, set()).add(item.value_hash)
        payload.update(
            {field: next(iter(items)) for field, items in values.items() if len(items) == 1}
        )
        return payload

    @staticmethod
    def _reconcile_local_hashes(
        employee: Employee, local_hashes: dict[str, str]
    ) -> tuple[str, ...]:
        conflicts: list[str] = []
        for field in ("id_number_hash", "phone_hash", "bank_card_hash"):
            incoming = local_hashes.get(field)
            if not incoming:
                continue
            current = getattr(employee, field)
            if current is None:
                setattr(employee, field, incoming)
            elif current != incoming:
                conflicts.append(field)
        return tuple(conflicts)

    @staticmethod
    def _ensure_match_candidate(
        session: Any,
        analysis_id: str,
        file_id: str,
        employee: Employee | None,
        *,
        extracted_fields: dict[str, Any],
        reason: str,
        score: float,
        source_employee_id: str | None = None,
        touched_candidates: list[EmployeeMatchCandidate] | None = None,
        source_scope_key: str | None = None,
    ) -> EmployeeMatchCandidate:
        existing_candidates = list(
            session.scalars(
                select(EmployeeMatchCandidate).where(
                    EmployeeMatchCandidate.analysis_id == analysis_id,
                    EmployeeMatchCandidate.file_id == file_id,
                    EmployeeMatchCandidate.candidate_employee_id
                    == (employee.id if employee is not None else None),
                )
            )
        )
        existing = next(
            (
                item
                for item in existing_candidates
                if (
                    source_employee_id is not None
                    and (item.extracted_fields or {}).get("employee_ids") == [source_employee_id]
                )
                or (
                    source_employee_id is None
                    and (item.extracted_fields or {}).get("source_scope_key") == source_scope_key
                )
            ),
            None,
        )
        if existing is None:
            fields = dict(extracted_fields)
            if source_employee_id is None and source_scope_key is not None:
                fields["source_scope_key"] = source_scope_key
            existing = EmployeeMatchCandidate(
                analysis_id=analysis_id,
                file_id=file_id,
                candidate_employee_id=employee.id if employee is not None else None,
                extracted_fields=fields,
                score=score,
                reason=reason,
                status="pending",
            )
            session.add(existing)
        else:
            merged_fields = dict(existing.extracted_fields or {})
            existing_scope = merged_fields.get("fact_ids", [])
            incoming_scope = extracted_fields.get("fact_ids", [])
            merged_fields.update(extracted_fields)
            merged_fields["fact_ids"] = list(
                dict.fromkeys(
                    [
                        *(existing_scope if isinstance(existing_scope, list) else []),
                        *(incoming_scope if isinstance(incoming_scope, list) else []),
                    ]
                )
            )
            existing.extracted_fields = merged_fields
        if touched_candidates is not None and existing not in touched_candidates:
            touched_candidates.append(existing)
        return existing

    @staticmethod
    def _candidate_source_scope_key(
        result: ExtractionResult, prepared: PreparedProviderInput | None
    ) -> str:
        if prepared is not None:
            return prepared.filename
        pages = sorted(
            {str(fact.source.page) for fact in result.facts if fact.source.page is not None}
        )
        return "pages:" + ",".join(pages) if pages else "document"

    @staticmethod
    def _consolidate_candidate_fact_scopes(session: Any, analysis_id: str, file_id: str) -> None:
        candidates = list(
            session.scalars(
                select(EmployeeMatchCandidate).where(
                    EmployeeMatchCandidate.analysis_id == analysis_id,
                    EmployeeMatchCandidate.file_id == file_id,
                    EmployeeMatchCandidate.status == "pending",
                )
            )
        )
        grouped: dict[tuple[str, ...], list[EmployeeMatchCandidate]] = {}
        for candidate in candidates:
            fact_ids = (candidate.extracted_fields or {}).get("fact_ids")
            if not isinstance(fact_ids, list) or not fact_ids or not all(
                isinstance(fact_id, str) for fact_id in fact_ids
            ):
                continue
            grouped.setdefault(tuple(sorted(set(fact_ids))), []).append(candidate)
        for alternatives in grouped.values():
            if len(alternatives) < 2:
                continue
            alternatives.sort(key=lambda item: (-item.score, item.id))
            for duplicate in alternatives[1:]:
                session.delete(duplicate)

    def _mark_file_failed(self, file_id: str, error_code: str,
                          diagnostic: AIDiagnostic | None = None) -> None:
        with self.database.session() as session:
            uploaded_file = session.get(UploadedFile, file_id)
            if uploaded_file:
                uploaded_file.status = "failed"
                uploaded_file.error_code = error_code
                jobs = session.scalars(
                    select(ProcessingJob).where(
                        ProcessingJob.file_id == file_id, ProcessingJob.status == "running"
                    )
                )
                for job in jobs:
                    job.status = "failed"
                    job.error_code = error_code
                    job.completed_at = datetime.now(UTC)
                usages = session.scalars(
                    select(AIUsageRecord).where(
                        AIUsageRecord.file_id == file_id,
                        AIUsageRecord.status == "running",
                    )
                )
                for usage in usages:
                    usage.status = "failed"
                session.add(AuditEvent(
                    analysis_id=uploaded_file.analysis_id,
                    event_type="processing_failed",
                    actor="system",
                    metadata_json={
                        "file_id": file_id,
                        "error_code": error_code,
                        "diagnostic": diagnostic.as_dict() if diagnostic else {},
                    },
                ))
                session.commit()
            analysis_id = uploaded_file.analysis_id if uploaded_file else None
        if analysis_id:
            self._refresh_analysis_progress(analysis_id)

    def _refresh_analysis_progress(self, analysis_id: str) -> None:
        """Publish durable per-file progress while a batch is still running."""
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None or analysis.status in {
                "completed", "partial", "failed", "cancelled", "interrupted", "matching_review"
            }:
                return
            files = list(session.scalars(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id)))
            if not files:
                return
            analysis.progress = max(5, min(89, round(sum(file.progress for file in files) / len(files))))
            if any(file.status == "extracting" for file in files):
                analysis.current_stage = "extracting"
            elif any(file.status == "parsing" for file in files):
                analysis.current_stage = "parsing"
            elif any(file.status in {"uploaded", "interrupted"} for file in files):
                analysis.current_stage = "queued"
            session.commit()

    @staticmethod
    def _job_key(analysis_id: str, file_id: str, job_type: str, input_hash: str) -> str:
        version = f":{EXTRACTION_VERSION}:contract-advisory-v1" if job_type == "extract" else ""
        return f"{analysis_id}:{file_id}:{job_type}:{input_hash}{version}"

    @staticmethod
    def _locator_type(location: dict[str, Any]) -> str:
        if "table" in location:
            return "table_cell"
        if "page" in location:
            return "page"
        if "sheet" in location or "row" in location:
            return "cell"
        if "paragraph" in location:
            return "paragraph"
        return "document"

    @staticmethod
    def _status_payload(session: Any, analysis: AnalysisBatch) -> dict[str, Any]:
        files = list(
            session.scalars(
                select(UploadedFile)
                .where(UploadedFile.analysis_id == analysis.id)
                .order_by(UploadedFile.created_at)
            )
        )
        diagnostics: dict[str, tuple[str | None, dict[str, Any]]] = {}
        for event in session.scalars(select(AuditEvent).where(
            AuditEvent.analysis_id == analysis.id,
            AuditEvent.event_type == "processing_failed",
        ).order_by(AuditEvent.created_at, AuditEvent.id)):
            metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
            file_id = metadata.get("file_id")
            diagnostic = metadata.get("diagnostic")
            if isinstance(file_id, str) and isinstance(diagnostic, dict):
                diagnostics[file_id] = (metadata.get("error_code"), diagnostic)
        return {
            "analysis_id": analysis.id,
            "status": analysis.status,
            "progress": analysis.progress,
            "current_stage": analysis.current_stage,
            "files": [
                {
                    "id": item.id,
                    "filename": display_filename(item.original_filename),
                    "status": item.status,
                    "progress": item.progress,
                    "error_code": item.error_code,
                    "error_diagnostic": (diagnostics[item.id][1]
                        if item.error_code and item.id in diagnostics and diagnostics[item.id][0] == item.error_code else None),
                }
                for item in files
            ],
        }


def run_analysis_job(database_url: str, storage_root: str, analysis_id: str) -> dict[str, Any]:
    settings = Settings()
    database = create_database(database_url)
    return ProcessingPipeline(
        database,
        LocalStorage(storage_root),
        provider_from_settings(settings),
        privacy_boundary=PrivacyBoundary(settings.effective_pii_hash_pepper),
        max_provider_calls=settings.ai_max_calls_per_analysis,
    ).process(analysis_id)
