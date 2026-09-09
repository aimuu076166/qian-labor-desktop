from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import select

from qian_labor.database import create_database
from qian_labor.models.core import (
    AnalysisBatch, Employee, EmploymentFact, RiskFinding, SourceLocator, UploadedFile,
)
from qian_labor.services.risk_evaluation import RiskEvaluationError, RiskEvaluationService


@contextmanager
def stored_facts(specs):
    """Synthetic facts: (type, JSON value, verification status, has source)."""
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构事实归并测试", status="evaluating")
        session.add(analysis)
        session.flush()
        employee = Employee(
            analysis_id=analysis.id, employee_number="SYN-001",
            masked_name="虚构员工", normalized_name="虚构员工", match_status="confirmed",
        )
        uploaded = UploadedFile(
            analysis_id=analysis.id, original_filename="虚构事实.csv",
            storage_key="synthetic/facts.csv", mime_type="text/csv", extension=".csv",
            size_bytes=1, sha256="a" * 64,
        )
        session.add_all([employee, uploaded])
        session.flush()
        facts = []
        for index, (fact_type, value, status, sourced) in enumerate(specs):
            fact = EmploymentFact(
                id=f"fact-{index:02d}", analysis_id=analysis.id, employee_id=employee.id,
                file_id=uploaded.id, fact_type=fact_type, value_json=deepcopy(value),
                normalized_value_json=deepcopy(value), extraction_method="fixture",
                confidence=1, verification_status=status, dedupe_key=f"synthetic-{index}",
            )
            session.add(fact)
            session.flush()
            facts.append(fact)
            if sourced:
                session.add(SourceLocator(
                    id=f"source-{index:02d}", analysis_id=analysis.id, file_id=uploaded.id,
                    fact_id=fact.id, locator_type="cell", location={"row": index + 1},
                    excerpt="虚构测试字段", content_hash="b" * 64,
                ))
        session.commit()
        yield database, session, analysis, facts


@pytest.mark.parametrize("left,right", [
    (5000, 5000), (False, False),
    (5000, 5000.0), (0, -0.0),
    ({"wage": [5000, {"bonus": 100.0}]}, {"wage": [5000.0, {"bonus": 100}]}),
    ({"a": [1, False], "b": None}, {"b": None, "a": [1, False]}),
    ([["2026-01-01", "2026-01-31"]], [["2026-01-01", "2026-01-31"]]),
])
def test_equivalent_typed_json_coalesces_every_source_without_mutating_facts(left, right):
    specs = [("employment.pay.actual_wage", value, "confirmed", True) for value in (left, right)]
    with stored_facts(specs) as (_, session, _, facts):
        before = [(deepcopy(f.value_json), deepcopy(f.normalized_value_json), f.verification_status)
                  for f in facts]
        forward = RiskEvaluationService._context_facts(session, facts)
        backward = RiskEvaluationService._context_facts(session, list(reversed(facts)))
        assert forward == backward
        result = forward["employment.pay.actual_wage"]
        assert result.value == left
        assert not result.conflicted
        assert result.source_locator_ids == ("source-00", "source-01")
        session.expire_all()
        assert [(f.value_json, f.normalized_value_json, f.verification_status) for f in facts] == before


@pytest.mark.parametrize("left,right", [
    (5000, 6000), (True, 1), (False, 0), (5000, "5000"),
    (True, 1.0), (5000, 5000.000000001), (9007199254740993, 9007199254740992.0),
    ([1, 2], [2, 1]), ({"a": True}, {"a": 1}), ([True], [1]),
])
def test_conflicting_values_are_gated_in_both_input_orders(left, right):
    with stored_facts([("employment.pay.actual_wage", v, "confirmed", True)
                       for v in (left, right)]) as (_, session, _, facts):
        forward = RiskEvaluationService._context_facts(session, facts)
        backward = RiskEvaluationService._context_facts(session, list(reversed(facts)))
        assert forward == backward
        result = forward["employment.pay.actual_wage"]
        assert result.conflicted
        assert result.value is None
        assert result.source_locator_ids == ("source-00", "source-01")


@pytest.mark.parametrize("status", ["conflicted", "pending_review", "needs_human_confirmation"])
def test_confirmed_duplicate_does_not_resolve_review_gate(status):
    with stored_facts([
        ("employment.contract.exists", False, status, True),
        ("employment.contract.exists", False, "confirmed", True),
    ]) as (_, session, _, facts):
        for ordered in (facts, list(reversed(facts))):
            result = RiskEvaluationService._context_facts(session, ordered)["employment.contract.exists"]
            assert result.conflicted
            assert result.value is None
            assert result.source_locator_ids == ("source-00", "source-01")


def test_missing_data_does_not_borrow_unrelated_first_fact_evidence():
    with stored_facts([("employment.pay.actual_wage", 5000, "confirmed", True)]) as (
        database, _, analysis, _,
    ):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        r01 = next(f for f in findings if f.rule_id == "CONTRACT_MISSING_ACTIVE")
        assert r01.assessment_status == "insufficient_data"
        assert r01.trigger_fact_ids == []
        assert r01.source_locator_ids == []
        r19 = next(f for f in findings if f.rule_id == "EMPLOYEE_IDENTITY_AMBIGUOUS")
        assert r19.assessment_status == "insufficient_data"
        assert r19.source_locator_ids == []


@pytest.mark.parametrize("source_status,source_contract", [(False, False), (True, False)])
def test_positive_finding_without_sources_for_its_required_facts_fails_closed(
    source_status, source_contract,
):
    with stored_facts([
        ("employment.pay.actual_wage", 5000, "confirmed", True),
        ("employment.status", "active", "confirmed", source_status),
        ("employment.contract.exists", False, "confirmed", source_contract),
    ]) as (database, session, analysis, _):
        with pytest.raises(RiskEvaluationError, match="FINDING_SOURCE_REQUIRED"):
            RiskEvaluationService(database).evaluate_analysis(analysis.id)
        assert session.scalar(select(RiskFinding)) is None


def test_conflicting_identity_is_not_overridden_by_confirmed_employee_match():
    with stored_facts([
        ("employment.identity.match_status", "confirmed", "confirmed", True),
        ("employment.identity.match_status", "ambiguous", "confirmed", True),
    ]) as (database, _, analysis, _):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        identity = next(f for f in findings if f.rule_id == "EMPLOYEE_IDENTITY_AMBIGUOUS")
        assert identity.assessment_status == "requires_human_review"
        assert set(identity.source_locator_ids) == {"source-00", "source-01"}


def test_unambiguous_identity_uses_existing_canonical_matcher_decision():
    with stored_facts([
        ("employment.identity.match_status", "ambiguous", "unverified", True),
    ]) as (database, _, analysis, facts):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        assert all(f.rule_id != "EMPLOYEE_IDENTITY_AMBIGUOUS" for f in findings)
        assert facts[0].normalized_value_json == "ambiguous"
        assert facts[0].verification_status == "unverified"


def test_conflicting_material_coverage_does_not_publish_arbitrary_aggregate():
    with stored_facts([
        ("employment.material_coverage", 0.4, "confirmed", True),
        ("employment.material_coverage", 0.9, "confirmed", True),
        ("analysis.minimum_core_coverage", 0.8, "confirmed", True),
    ]) as (database, session, analysis, _):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        coverage = next(f for f in findings if f.rule_id == "MATERIAL_COVERAGE_LOW")
        assert coverage.assessment_status == "requires_human_review"
        session.refresh(analysis)
        assert analysis.coverage_rate == 0


@pytest.mark.parametrize("values", [(5000, 6000), (6000, 5000)])
def test_disagreeing_wages_produce_review_instead_of_arbitrary_risk_or_clearance(values):
    with stored_facts([
        ("employment.pay.contract_wage", 5000, "confirmed", True),
        ("employment.pay.comparable", True, "confirmed", True),
        *(('employment.pay.actual_wage', value, 'confirmed', True) for value in values),
    ]) as (database, _, analysis, _):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        wage = next(f for f in findings if f.rule_id == "PAY_CONTRACT_ACTUAL_MISMATCH")
        assert wage.assessment_status == "requires_human_review"
        assert wage.requires_human_review
        assert wage.trigger_fact_ids == ["fact-00", "fact-01", "fact-02", "fact-03"]
        assert wage.source_locator_ids == ["source-00", "source-01", "source-02", "source-03"]


def test_equivalent_facts_remain_traceable_in_positive_finding():
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, "confirmed", True),
        ("employment.contract.exists", False, "unverified", True),
    ]) as (database, _, analysis, _):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        contract = next(f for f in findings if f.rule_id == "CONTRACT_MISSING_ACTIVE")
        assert contract.assessment_status == "suspected_risk"
        assert contract.trigger_fact_ids == ["fact-00", "fact-01", "fact-02"]
        assert contract.source_locator_ids == ["source-00", "source-01", "source-02"]


@pytest.mark.parametrize("status", ["pending_review", "needs_human_confirmation"])
def test_pending_fact_reports_review_without_asserting_proven_conflict(status):
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, status, True),
    ]) as (database, _, analysis, _):
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        contract = next(f for f in findings if f.rule_id == "CONTRACT_MISSING_ACTIVE")
        assert contract.assessment_status == "requires_human_review"
        assert "尚未核实" in contract.summary


def test_positive_finding_rejects_source_from_another_analysis():
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, "confirmed", True),
    ]) as (database, session, analysis, _):
        other = AnalysisBatch(name="另一虚构分析", status="evaluating")
        session.add(other)
        session.flush()
        source = session.get(SourceLocator, "source-01")
        source.analysis_id = other.id
        session.commit()
        with pytest.raises(RiskEvaluationError, match="FINDING_SOURCE_REQUIRED"):
            RiskEvaluationService(database).evaluate_analysis(analysis.id)


def additional_uploaded_file(session, analysis_id):
    uploaded = UploadedFile(
        analysis_id=analysis_id, original_filename="虚构补充事实.csv",
        storage_key="synthetic/additional-facts.csv", mime_type="text/csv",
        extension=".csv", size_bytes=1, sha256="c" * 64,
    )
    session.add(uploaded)
    session.flush()
    return uploaded


def test_equivalent_facts_from_two_uploaded_files_retain_both_sources():
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, "confirmed", True),
        ("employment.contract.exists", False, "unverified", True),
    ]) as (database, session, analysis, facts):
        uploaded = additional_uploaded_file(session, analysis.id)
        facts[2].file_id = uploaded.id
        session.get(SourceLocator, "source-02").file_id = uploaded.id
        session.commit()
        assert facts[1].file_id != facts[2].file_id

        resolved = RiskEvaluationService._context_facts(session, facts)["employment.contract.exists"]
        assert not resolved.conflicted
        assert resolved.value is False
        assert resolved.source_locator_ids == ("source-01", "source-02")
        findings = RiskEvaluationService(database).evaluate_analysis(analysis.id)
        contract = next(f for f in findings if f.rule_id == "CONTRACT_MISSING_ACTIVE")
        assert contract.assessment_status == "suspected_risk"
        assert contract.source_locator_ids == ["source-00", "source-01", "source-02"]
        assert contract.trigger_fact_ids == ["fact-00", "fact-01", "fact-02"]


def test_positive_finding_rejects_locator_file_that_differs_from_its_fact_file():
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, "confirmed", True),
    ]) as (database, session, analysis, facts):
        uploaded = additional_uploaded_file(session, analysis.id)
        source = session.get(SourceLocator, "source-01")
        source.file_id = uploaded.id
        session.commit()
        assert source.analysis_id == facts[1].analysis_id == uploaded.analysis_id
        assert source.fact_id == facts[1].id
        assert source.file_id != facts[1].file_id

        with pytest.raises(RiskEvaluationError, match="FINDING_SOURCE_REQUIRED"):
            RiskEvaluationService(database).evaluate_analysis(analysis.id)
        assert session.scalar(select(RiskFinding)) is None


def test_positive_finding_rejects_uploaded_file_owned_by_another_analysis():
    with stored_facts([
        ("employment.status", "active", "confirmed", True),
        ("employment.contract.exists", False, "confirmed", True),
    ]) as (database, session, analysis, facts):
        other = AnalysisBatch(name="另一虚构文件所属分析", status="evaluating")
        session.add(other)
        session.flush()
        source = session.get(SourceLocator, "source-01")
        uploaded = session.get(UploadedFile, source.file_id)
        uploaded.analysis_id = other.id
        session.commit()
        assert source.analysis_id == facts[1].analysis_id == analysis.id
        assert source.fact_id == facts[1].id
        assert source.file_id == facts[1].file_id == uploaded.id
        assert uploaded.analysis_id != analysis.id

        with pytest.raises(RiskEvaluationError, match="FINDING_SOURCE_REQUIRED"):
            RiskEvaluationService(database).evaluate_analysis(analysis.id)
        assert session.scalar(select(RiskFinding)) is None


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), float("-inf"),
    {"wages": [float("nan")]}, (5000,), {5000}, {1: "non-string JSON key"},
])
def test_nonfinite_or_unsupported_values_cannot_be_accepted_as_resolved_facts(value):
    with stored_facts([
        ("employment.pay.actual_wage", 5000, "confirmed", True),
    ]) as (_, session, _, facts):
        # Exercise the evaluation boundary before an unsupported in-memory value
        # could reach SQLAlchemy's JSON serializer; the stored original stays intact.
        facts[0].normalized_value_json = value
        with session.no_autoflush:
            result = RiskEvaluationService._context_facts(session, facts)["employment.pay.actual_wage"]
        assert result.conflicted
        assert result.value is None
        assert result.source_locator_ids == ("source-00",)
