from dataclasses import replace
from sqlalchemy import func, select

from qian_labor.models.core import AnalysisBatch, EmployeeMatchCandidate, EmploymentFact, FindingReview, RiskFinding
from qian_labor.rules.registry import RULE_REGISTRY
from qian_labor.services.risk_evaluation import RiskEvaluationService
from test_finding_review_api import HEADERS, post_review, review_case


def set_contract(database, analysis_id, value):
    with database.session() as session:
        fact = session.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == analysis_id,
            EmploymentFact.fact_type == "employment.contract.exists"))
        fact.value_json = fact.normalized_value_json = value
        session.commit()


def snapshot(database, finding_id):
    with database.session() as session:
        finding = session.get(RiskFinding, finding_id)
        assert finding is not None, "evaluation must not delete the finding and cascade-delete reviews"
        return finding.is_current, finding.retired_at, finding.version, finding.review_status, [
            (review.id, review.actor, review.note, review.created_at) for review in finding.reviews]


def test_no_longer_triggered_finding_retires_without_erasing_review_and_is_idempotent(review_case):
    client, database, analysis_id, _, finding_id = review_case
    assert post_review(client, finding_id).status_code == 200
    before = snapshot(database, finding_id)
    set_contract(database, analysis_id, True)
    evaluator = RiskEvaluationService(database)
    evaluator.evaluate_analysis(analysis_id)
    after = snapshot(database, finding_id)
    assert after[0] is False and after[1] is not None
    assert after[2] == before[2] + 1 and after[3:] == before[3:]
    evaluator.evaluate_analysis(analysis_id)
    assert snapshot(database, finding_id) == after
    assert post_review(client, finding_id, version=after[2]).json()["detail"]["code"] == "DESKTOP_REVIEW_UNAVAILABLE"
    assert snapshot(database, finding_id) == after


def test_retired_records_are_read_only_and_excluded_from_current_views_and_counts(review_case):
    client, database, analysis_id, employee_id, finding_id = review_case
    post_review(client, finding_id)
    set_contract(database, analysis_id, True)
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    base = f"/api/analyses/{analysis_id}"
    for path in (base + "/dashboard", base + f"/employees/{employee_id}", base + "/report"):
        payload = client.get(path, headers=HEADERS).json()
        assert finding_id not in {item["id"] for item in payload["findings"]}
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["is_current"] is False and len(detail.json()["reviews"]) == 1
    history = client.get(base + f"/employees/{employee_id}", headers=HEADERS).json()["retired_findings"]
    assert finding_id in {item["id"] for item in history}
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id).high_count == 0


def test_retrigger_same_evidence_reuses_identity_but_requires_fresh_review(review_case):
    client, database, analysis_id, _, finding_id = review_case
    post_review(client, finding_id)
    before = snapshot(database, finding_id)
    set_contract(database, analysis_id, True)
    evaluator = RiskEvaluationService(database)
    evaluator.evaluate_analysis(analysis_id)
    set_contract(database, analysis_id, False)
    evaluator.evaluate_analysis(analysis_id)
    after = snapshot(database, finding_id)
    assert after[:4] == (True, None, before[2] + 2, "open")
    assert after[4][:1] == before[4] and len(after[4]) == 2
    evaluator.evaluate_analysis(analysis_id)
    assert snapshot(database, finding_id) == after


def test_matching_gate_retires_business_findings_without_erasing_their_reviews(review_case):
    client, database, analysis_id, _, finding_id = review_case
    post_review(client, finding_id)
    before = snapshot(database, finding_id)
    with database.session() as session:
        session.add(EmployeeMatchCandidate(analysis_id=analysis_id, reason="synthetic", status="pending"))
        session.commit()
    RiskEvaluationService(database).evaluate_data_quality(analysis_id)
    after = snapshot(database, finding_id)
    assert after[0] is False and after[4] == before[4]
    assert client.get(f"/api/findings/{finding_id}", headers=HEADERS).status_code == 404


def test_rule_upgrade_preserves_reviewed_old_version_as_retired(review_case, monkeypatch):
    client, database, analysis_id, _, finding_id = review_case
    post_review(client, finding_id)
    before = snapshot(database, finding_id)
    rule = RULE_REGISTRY["R01"]
    monkeypatch.setitem(RULE_REGISTRY, "R01", replace(rule, metadata=replace(rule.metadata, version="synthetic-next")))
    findings = RiskEvaluationService(database).evaluate_analysis(analysis_id)
    current = next(item for item in findings if item.rule_id == "CONTRACT_MISSING_ACTIVE")
    assert current.id != finding_id and current.is_current is True
    after = snapshot(database, finding_id)
    assert after[0] is False and after[4] == before[4]


def test_explicit_analysis_deletion_removes_current_and_retired_reviews(review_case):
    client, database, analysis_id, _, finding_id = review_case
    from qian_labor.models.core import UploadedFile
    with database.session() as session:
        uploaded = session.scalar(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id))
        owned = database.path.parent / 'storage' / uploaded.storage_key
    assert owned.read_bytes() == b'employee_number,material\nSYN-R01,synthetic-review\n'
    unrelated = owned.parent.parent / 'synthetic-unrelated.txt'
    unrelated.write_bytes(b'synthetic unrelated bytes')
    post_review(client, finding_id)
    set_contract(database, analysis_id, True)
    RiskEvaluationService(database).evaluate_analysis(analysis_id)
    assert snapshot(database, finding_id)[0] is False
    assert client.delete(f"/api/analyses/{analysis_id}", headers=HEADERS).status_code == 200
    assert not owned.exists()
    assert unrelated.read_bytes() == b'synthetic unrelated bytes'
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RiskFinding)) == 0
        assert session.scalar(select(func.count()).select_from(FindingReview)) == 0


def test_pending_migration_backup_prevents_deletion_success_or_material_removal(review_case):
    client, database, analysis_id, _, finding_id = review_case
    marker = database.path.parent / ".qian-migration-recovery"
    marker.mkdir(mode=0o700)
    with database.session() as session:
        from qian_labor.models.core import UploadedFile
        uploaded = session.scalar(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id))
        stored_path = database.path.parent / "storage" / uploaded.storage_key
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text("synthetic only")
    response = client.delete(f"/api/analyses/{analysis_id}", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_DB_RECOVERY_REQUIRED"
    assert stored_path.read_text() == "synthetic only"
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id) is not None
        assert session.get(RiskFinding, finding_id) is not None
