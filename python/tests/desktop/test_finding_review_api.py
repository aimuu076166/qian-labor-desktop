import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm.attributes import flag_modified

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import (
    AnalysisBatch, AuditEvent, Employee, EmploymentFact, FindingReview,
    RiskFinding, SourceLocator, UploadedFile,
)
from qian_labor.services.risk_evaluation import RiskEvaluationService
from qian_labor.services.finding_review import FindingReviewError, FindingReviewService
from qian_labor.rules.registry import RULE_REGISTRY


HEADERS = {"X-Qian-Desktop-Token": "synthetic-review-token"}


@pytest.fixture
def review_case(tmp_path):
    app = create_desktop_app(data_dir=tmp_path, launch_token=HEADERS["X-Qian-Desktop-Token"])
    database = app.state.database
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构复核测试", status="evaluating")
        session.add(analysis)
        session.flush()
        employee = Employee(analysis_id=analysis.id, masked_name="虚构员工",
                            normalized_name="虚构员工", employee_number="SYN-R01")
        file_id = str(uuid4())
        content = b'employee_number,material\nSYN-R01,synthetic-review\n'
        storage_key = f'analyses/{analysis.id}/{file_id}.csv'
        from qian_labor.storage.local import LocalStorage
        LocalStorage(tmp_path / 'storage').save_bytes(content, storage_key)
        uploaded = UploadedFile(id=file_id, analysis_id=analysis.id, original_filename="虚构.csv",
                                storage_key=storage_key, mime_type="text/csv",
                                extension=".csv", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        session.add_all([employee, uploaded])
        session.flush()
        for index, (kind, value) in enumerate([
            ("employment.status", "active"), ("employment.contract.exists", False),
        ]):
            fact = EmploymentFact(analysis_id=analysis.id, employee_id=employee.id,
                                  file_id=uploaded.id, fact_type=kind, value_json=value,
                                  normalized_value_json=value, extraction_method="fixture",
                                  confidence=1, verification_status="confirmed",
                                  dedupe_key=hashlib.sha256(kind.encode()).hexdigest())
            session.add(fact)
            session.flush()
            session.add(SourceLocator(analysis_id=analysis.id, file_id=uploaded.id,
                                      fact_id=fact.id, locator_type="cell", location={"row": index + 1},
                                      excerpt="虚构证据", content_hash="b" * 64))
        session.commit()
        analysis_id, employee_id = analysis.id, employee.id
    findings = RiskEvaluationService(database).evaluate_analysis(analysis_id)
    finding_id = next(item.id for item in findings if item.rule_id == "CONTRACT_MISSING_ACTIVE")
    with TestClient(app) as client:
        yield client, database, analysis_id, employee_id, finding_id


def post_review(client, finding_id, status="reviewed", version=0, note="已核对虚构材料"):
    return client.post(f"/api/findings/{finding_id}/reviews", headers=HEADERS,
                       json={"expected_version": version, "status": status, "note": note})


def stored_state(database, finding_id):
    with database.session() as session:
        finding = session.get(RiskFinding, finding_id)
        return (finding.review_status, finding.version,
                session.scalar(select(func.count()).select_from(FindingReview)),
                session.scalar(select(func.count()).select_from(AuditEvent)))


def test_review_requires_token_and_validates_trimmed_note_without_writes(review_case):
    client, database, _, _, finding_id = review_case
    before = stored_state(database, finding_id)
    response = client.post(f"/api/findings/{finding_id}/reviews",
                           json={"expected_version": 0, "status": "reviewed", "note": "核对"})
    assert response.status_code == 401
    for note in ("", "   ", "字" * 501):
        assert post_review(client, finding_id, note=note).status_code == 422
    for status in ("resolved", "invented"):
        assert post_review(client, finding_id, status=status).status_code == 422
    assert post_review(client, finding_id, version=-1).status_code == 422
    assert stored_state(database, finding_id) == before


def test_review_persists_masked_audit_and_full_detail_then_rejects_stale_post(review_case):
    client, database, _, _, finding_id = review_case
    synthetic_phone = "138" + "0013" + "8000"
    response = post_review(client, finding_id, note=f"  已核查，合成联系电话{synthetic_phone}  ")
    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "reviewed"
    assert payload["version"] == 1
    assert payload["requires_human_review"] is True
    assert payload["missing_fact_types"] == []
    assert payload["legal_basis"] and payload["recommended_actions"]
    assert payload["reviews"][0]["status"] == "reviewed"
    assert payload["reviews"][0]["note"].startswith("已核查")
    assert synthetic_phone not in payload["reviews"][0]["note"]
    before = stored_state(database, finding_id)
    stale = post_review(client, finding_id, status="dismissed")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "DESKTOP_REVIEW_STALE"
    assert stored_state(database, finding_id) == before
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert detail == payload
    with database.session() as session:
        review = session.scalar(select(FindingReview).where(FindingReview.finding_id == finding_id))
        assert (review.old_status, review.new_status, review.actor) == ("open", "reviewed", "local-user")


@pytest.mark.parametrize("status", ["queued", "evaluating", "matching_review", "failed", "deleted"])
def test_busy_or_unavailable_analysis_rejects_review_without_writes(review_case, status):
    client, database, analysis_id, _, finding_id = review_case
    with database.session() as session:
        session.get(AnalysisBatch, analysis_id).status = status
        session.commit()
    before = stored_state(database, finding_id)
    response = post_review(client, finding_id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_REVIEW_UNAVAILABLE"
    assert stored_state(database, finding_id) == before


def test_missing_finding_returns_404_and_partial_analysis_allows_review(review_case):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, "does-not-exist").status_code == 404
    with database.session() as session:
        session.get(AnalysisBatch, analysis_id).status = "partial"
        session.commit()
    assert post_review(client, finding_id).status_code == 200


@pytest.mark.parametrize("status", ["reviewed", "dismissed", "not_applicable", "needs_material", "open"])
def test_review_counts_and_draft_report_preserve_all_decisions(review_case, status):
    client, _, analysis_id, employee_id, finding_id = review_case
    dashboard_url = f"/api/analyses/{analysis_id}/dashboard"
    employee_url = f"/api/analyses/{analysis_id}/employees"
    before = client.get(dashboard_url, headers=HEADERS).json()["overview"]["summary"]
    ledger_before = client.get(employee_url, headers=HEADERS).json()["items"][0]
    assert post_review(client, finding_id, status=status).status_code == 200
    after = client.get(dashboard_url, headers=HEADERS).json()["overview"]["summary"]
    ledger_after = client.get(employee_url, headers=HEADERS).json()["items"][0]
    decrement = int(status not in {"open", "needs_material"})
    assert after["requires_human_review_count"] == before["requires_human_review_count"] - decrement
    assert ledger_after["requires_human_review_count"] == ledger_before["requires_human_review_count"] - decrement
    assert after["high_count"] == (0 if status in {"dismissed", "not_applicable"} else 1)
    report = client.get(f"/api/analyses/{analysis_id}/report", headers=HEADERS).json()
    assert report["report_status"] == "draft"
    finding = next(item for item in report["findings"] if item["id"] == finding_id)
    assert finding["review_status"] == status
    assert finding["reviews"][0]["status"] == status
    assert finding["version"] == 1
    assert finding["legal_basis"] and finding["recommended_actions"]


def test_unchanged_evaluation_preserves_review_and_audit_history(review_case):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id).status_code == 200
    before = stored_state(database, finding_id)
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    assert stored_state(database, finding_id) == before


@pytest.mark.parametrize("change", ["value", "verification", "source"])
def test_changed_contributing_evidence_reopens_review_even_when_risk_result_unchanged(review_case, change):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id).status_code == 200
    with database.session() as session:
        fact = session.scalar(select(EmploymentFact).where(
            EmploymentFact.fact_type == "employment.contract.exists"))
        if change == "value":
            fact.normalized_value_json = 0
            # SQLAlchemy otherwise treats False == 0 as an unchanged JSON value.
            flag_modified(fact, "normalized_value_json")
        elif change == "verification":
            fact.verification_status = "unverified"
        else:
            source = session.scalar(select(SourceLocator).where(SourceLocator.fact_id == fact.id))
            source.location = {"row": 99}
        session.commit()
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert detail["assessment_status"] == "suspected_risk"
    assert detail["review_status"] == "open"
    assert detail["version"] == 2
    assert [item["status"] for item in detail["reviews"]] == ["reviewed", "open"]
    assert post_review(client, finding_id, version=1).status_code == 409


@pytest.mark.parametrize("status", ["dismissed", "not_applicable"])
def test_analysis_cached_counts_match_dashboard_after_review_and_unchanged_evaluation(review_case, status):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id, status=status).status_code == 200
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id).high_count == 0
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id).high_count == 0


def test_concurrent_review_submissions_only_commit_one_version_and_one_decision(review_case):
    _, database, _, _, finding_id = review_case
    barrier = Barrier(2)
    def submit(status):
        barrier.wait()
        try:
            FindingReviewService(database).review(finding_id, expected_version=0,
                                                 status=status, note="合成并发复核")
            return "saved"
        except FindingReviewError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, ["reviewed", "dismissed"]))
    assert sorted(outcomes) == ["DESKTOP_REVIEW_STALE", "saved"]
    state = stored_state(database, finding_id)
    assert state[1:3] == (1, 1)


def test_review_initializes_legacy_signature_and_unchanged_evaluation_preserves_it(review_case):
    client, database, analysis_id, _, finding_id = review_case
    with database.session() as session:
        session.execute(delete(AuditEvent).where(
            AuditEvent.analysis_id == analysis_id,
            AuditEvent.metadata_json["finding_id"].as_string() == finding_id,
        ))
        session.commit()
    assert post_review(client, finding_id).status_code == 200
    before = stored_state(database, finding_id)
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    assert stored_state(database, finding_id) == before


@pytest.mark.parametrize("change_source", [False, True])
def test_legacy_open_finding_establishes_baseline_and_rejects_old_details(review_case, change_source):
    client, database, analysis_id, _, finding_id = review_case
    with database.session() as session:
        session.execute(delete(AuditEvent).where(AuditEvent.analysis_id == analysis_id))
        session.commit()
    original = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    if change_source:
        with database.session() as session:
            source = session.scalar(select(SourceLocator).where(SourceLocator.analysis_id == analysis_id))
            source.location = {"row": 999}
            session.commit()
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    updated = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert updated["version"] == original["version"] + 1
    before = stored_state(database, finding_id)
    assert post_review(client, finding_id, version=original["version"]).status_code == 409
    assert stored_state(database, finding_id) == before
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    assert stored_state(database, finding_id) == before


def test_previously_reviewed_legacy_finding_without_signature_is_conservatively_reopened(review_case):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id).status_code == 200
    with database.session() as session:
        session.execute(delete(AuditEvent).where(AuditEvent.analysis_id == analysis_id))
        session.commit()
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert detail["review_status"] == "open"
    assert detail["version"] == 2
    assert len(detail["reviews"]) == 2


def test_changed_rule_result_reopens_review(review_case, monkeypatch):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id).status_code == 200
    rule = RULE_REGISTRY["R01"]
    monkeypatch.setitem(RULE_REGISTRY, "R01", replace(rule, metadata=replace(rule.metadata, severity="medium")))
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert detail["severity"] == "medium"
    assert detail["review_status"] == "open"
    assert detail["version"] == 2


def test_audit_signature_contains_digest_only_without_copied_source_or_fact_material(review_case):
    _, database, _, _, finding_id = review_case
    with database.session() as session:
        event = session.scalar(select(AuditEvent).where(
            AuditEvent.metadata_json["finding_id"].as_string() == finding_id))
        assert set(event.metadata_json) == {"finding_id", "version", "signature"}
        assert len(event.metadata_json["signature"]) == 64


def test_database_write_lock_returns_review_unavailable_without_decision_writes(review_case):
    client, database, _, _, finding_id = review_case
    before = stored_state(database, finding_id)
    def short_busy_timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=1")
    event.listen(database.engine, "connect", short_busy_timeout)
    with database.engine.connect() as locked:
        locked.exec_driver_sql("BEGIN IMMEDIATE")
        response = post_review(client, finding_id)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "DESKTOP_REVIEW_UNAVAILABLE"
    assert stored_state(database, finding_id) == before


def test_post_returns_committed_snapshot_without_followup_detail_read(review_case, monkeypatch):
    client, database, _, _, finding_id = review_case
    def unavailable_followup_read(*_args, **_kwargs):
        raise AssertionError("A separate post-commit GET can race with a new evaluation")
    monkeypatch.setattr("qian_labor.desktop.app._finding_detail", unavailable_followup_read)
    response = post_review(client, finding_id)
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["review_status"] == "reviewed"
    assert response.json()["sources"]
    assert response.json()["reviews"][0]["status"] == "reviewed"
    assert stored_state(database, finding_id)[1:3] == (1, 1)
