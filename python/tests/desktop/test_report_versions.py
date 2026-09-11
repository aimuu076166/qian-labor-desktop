"""Frozen report/API behavior with synthetic SQLite data and no external provider."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
import hashlib
import json

import pytest
from sqlalchemy import select, text

from test_company_workspaces import api, company, snapshot
from test_current_company import current
from test_effective_facts import prepared
from test_historical_import import setup_history, post
from qian_labor.models.core import AnalysisBatch, EmployeeRecord, SourceLocator, AIUsageRecord


def context(client, url):
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def command(payload, request_id=None):
    c = payload["current_context"]
    return {"request_id": request_id or str(uuid4()), "expected_input_revision": c["input_revision"],
            "expected_result_revision": c["result_revision"], "expected_review_revision": c["review_revision"],
            "expected_context_signature": c["context_signature"]}


def generate(client, url):
    body = command(context(client, url))
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    return body, response.json()


def test_empty_current_is_limited_readonly_context_and_no_generation_drift(api):
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    before = context(client, url)
    assert before["items"] == [] and before["current_context_available"] is True
    assert context(client, url)["current_context"] == before["current_context"]
    body, first = generate(client, url)
    assert first["request"]["outcome"] == "accepted"
    saved = first["snapshot"]
    assert saved["payload"]["report_status"] == "draft"
    assert saved["payload"]["limitations"]
    assert saved["payload"]["assessment_revision"]["availability"] == "none"
    assert context(client, url)["current_context"] == before["current_context"]
    assert client.post(url, json=body).json()["snapshot"]["id"] == saved["id"]
    second_body, second = generate(client, url)
    assert second_body != body and second["snapshot"]["version"] == 2
    assert context(client, url)["current_context"] == before["current_context"]
    with db.session() as s:
        assert len(list(s.scalars(select(AIUsageRecord)))) == 0
        assert s.execute(text("SELECT count(*) FROM report_versions")).scalar_one() == 2
        assert s.execute(text("SELECT count(*) FROM report_generation_requests")).scalar_one() == 2


def test_versions_freeze_evidence_names_and_current_context_without_provider(api, tmp_path, monkeypatch):
    from qian_labor.ai.providers import FakeAIProvider
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    monkeypatch.setattr(FakeAIProvider, "extract", lambda *a, **k: pytest.fail("report called provider"))
    url = base + "/report-versions"
    body, accepted = generate(client, url)
    item = accepted["snapshot"]
    detail = context(client, url + "/" + item["id"])
    assert detail["snapshot"]["payload"]["facts"]
    assert detail["stale"] is False
    with db.session() as s:
        original_bytes = s.execute(text("SELECT payload_json FROM report_versions WHERE id=:id"), {"id": item["id"]}).scalar_one()
        s.get(EmployeeRecord, rec["id"]).department = "合成新部门"
        s.commit()
    changed = context(client, url)
    assert changed["current_context"]["context_signature"] != body["expected_context_signature"]
    repeated = client.post(url, json=body)
    assert repeated.status_code == 200 and repeated.json()["snapshot"]["id"] == item["id"]
    again = context(client, url + "/" + item["id"])
    assert again["snapshot"] == detail["snapshot"] and again["stale"] is True
    with db.session() as s:
        assert s.execute(text("SELECT payload_json FROM report_versions WHERE id=:id"), {"id": item["id"]}).scalar_one() == original_bytes
    assert hashlib.sha256(original_bytes.encode()).hexdigest() == item["content_sha256"]
    assert "真实模型分析" not in original_bytes


def test_exact_request_body_cas_rejection_receipt_and_scope_before_lookup(api):
    client, db = api
    c, other = company(client), company(client)
    aid = current(client, c["id"])["analysis_id"]
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    body = command(context(client, url))
    with db.session() as s:
        s.get(AnalysisBatch, aid).company_display_name = "合成变化"
        s.commit()
    conflict = client.post(url, json=body)
    assert conflict.status_code == 409
    receipt = conflict.json()["request"]
    assert receipt["outcome"] == "rejected" and receipt["error_code"] == "REPORT_VERSION_CONFLICT"
    assert client.get(url + "/requests/" + body["request_id"]).json()["request"] == receipt
    assert client.post(url, json=body).json()["request"] == receipt
    mismatch = {**body, "expected_context_signature": "0" * 64}
    assert client.post(url, json=mismatch).status_code == 409
    for suffix in ("", "/requests/" + body["request_id"]):
        assert client.get(url.replace(c["id"], other["id"]) + suffix).status_code == 404
    assert client.get(url + "/requests/" + str(uuid4())).json()["request"] is None
    assert client.get(url, headers={"X-Qian-Desktop-Token": "wrong"}).status_code == 401


def test_corrupted_current_source_keeps_owned_stored_versions_readable(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    url = base + "/report-versions"
    body, first = generate(client, url)
    saved = first["snapshot"]
    with db.session() as s:
        source = s.scalar(select(SourceLocator).where(SourceLocator.analysis_id == aid))
        source.content_hash = "0" * 64
        s.commit()
    listing = context(client, url)
    assert listing["current_context_available"] is False and listing["current_context"] is None
    assert listing["items"][0]["id"] == saved["id"]
    detail = context(client, url + "/" + saved["id"])
    assert detail["snapshot"] == saved and detail["current_context_available"] is False
    assert client.post(url, json=body).status_code == 200
    failed = client.post(url, json={**body, "request_id": str(uuid4())})
    assert failed.status_code == 409 and failed.json()["request"]["error_code"] == "REPORT_SOURCE_INVALID"


def test_owned_history_deletion_cascades_reports_and_receipts_but_keeps_current_copy(api):
    client, db, aid, target, files, import_url = setup_history(api)
    company_id = import_url.split("/")[3]
    copied_id = post(client, import_url, aid, files[:1]).json()["results"][0]["file_id"]
    from qian_labor.models.core import UploadedFile
    with db.session() as s:
        copy = s.get(UploadedFile, copied_id)
        key, digest = copy.storage_key, copy.sha256
        record_ids = list(s.scalars(select(EmployeeRecord.id)))
    url = f"/api/company-workspaces/{company_id}/analyses/{aid}/report-versions"
    body, result = generate(client, url)
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert client.get(url + "/requests/" + body["request_id"]).status_code == 404
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM report_versions")).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM report_generation_requests")).scalar_one() == 0
        assert all(s.get(EmployeeRecord, rid) is not None for rid in record_ids)
        assert s.get(UploadedFile, copied_id) is not None
    assert hashlib.sha256(client.app.state.import_service.storage.read_bytes(key)).hexdigest() == digest
    assert client.delete(f"/api/analyses/{target}").status_code == 409


def test_all_human_display_fields_are_masked_before_persistence(api):
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    with db.session() as s:
        s.get(AnalysisBatch, aid).company_display_name = "合成企业电话13800138000"
        s.commit()
    _, saved = generate(client, f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions')
    assert "13800138000" not in json.dumps(saved, ensure_ascii=False)


def test_database_immutable_updates_restart_and_duplicate_before_busy(api):
    from sqlalchemy.exc import IntegrityError
    from qian_labor.services.report_versions import ReportVersionService, GenerateReportRequest
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    body, saved = generate(client, url)
    for table, column in [("report_versions", "payload_json"), ("report_generation_requests", "body_json")]:
        with db.session() as s, pytest.raises(IntegrityError, match="REPORT_VERSION_IMMUTABLE"):
            s.execute(text(f"UPDATE {table} SET {column}='{{}}'"))
            s.commit()
    db.engine.dispose()
    service = ReportVersionService(db, client.app.state.processing_queue)
    with client.app.state.processing_queue.mutation(aid):
        repeated, code = service.generate(c["id"], aid, GenerateReportRequest(**body))
        assert code == 200 and repeated == saved
    assert context(client, url + '/' + saved['snapshot']['id'])['snapshot'] == saved['snapshot']


@pytest.mark.parametrize("write", [False, True])
def test_report_capture_is_one_sqlite_snapshot_while_writer_waits(api, monkeypatch, write):
    from qian_labor.services.report import ReportService
    from qian_labor.services.report_versions import ReportVersionService, GenerateReportRequest
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    body = command(context(client, url))
    captured, release, started, committed = Event(), Event(), Event(), Event()
    original = ReportService.get
    def pause(self, analysis_id, *, session=None):
        result = original(self, analysis_id, session=session)
        captured.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(ReportService, 'get', pause)
    service = ReportVersionService(db, client.app.state.processing_queue)
    def read():
        return service.generate(c['id'], aid, GenerateReportRequest(**body)) if write else service.listing(c['id'], aid)
    def update():
        with db.session() as s:
            started.set()
            s.execute(text('BEGIN IMMEDIATE'))
            s.get(AnalysisBatch, aid).company_display_name = '并发合成变化'
            s.commit()
            committed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(read)
        assert captured.wait(5)
        writer = pool.submit(update)
        assert started.wait(5)
        assert not committed.wait(.05)
        release.set()
        output = result.result(5)
        writer.result(5)
    if write:
        assert output[1] == 201
        assert output[0]['snapshot']['context_signature'] == body['expected_context_signature']
    else:
        assert output['current_context']['context_signature'] == body['expected_context_signature']


def test_concurrent_same_uuid_accepts_once_and_different_body_never_adds_version(api):
    client, db = api
    c = company(client)
    aid = current(client, c['id'])['analysis_id']
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    body = command(context(client, url))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body), range(2)))
    assert all(r.status_code in {200, 201, 409} for r in responses)
    assert sum(r.status_code == 201 for r in responses) == 1
    accepted = client.post(url, json=body)
    assert accepted.status_code == 200
    assert client.post(url, json={**body, 'expected_review_revision': '0'*64}).status_code == 409
    assert context(client, url)['total'] == 1


@pytest.mark.parametrize('without_findings', [False, True])
def test_foreign_source_attached_to_owned_fact_refuses_new_generation(api, tmp_path, without_findings):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    body, saved = generate(client, base + '/report-versions')
    foreign, _ = snapshot(db)
    with db.session() as s:
        if without_findings:
            from qian_labor.models.core import RiskFinding
            for finding in s.scalars(select(RiskFinding).where(RiskFinding.analysis_id == aid)):
                s.delete(finding)
        source = s.scalar(select(SourceLocator).where(SourceLocator.analysis_id == aid))
        source.analysis_id = foreign
        s.commit()
    detail = context(client, base + '/report-versions/' + saved['snapshot']['id'])
    assert detail['current_context_available'] is False
    assert detail['snapshot'] == saved['snapshot']


def test_facts_materials_and_review_changes_only_mark_saved_bytes_stale(api, tmp_path):
    from qian_labor.models.core import EmploymentFact, UploadedFile, RiskFinding
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    url = base + '/report-versions'
    body, saved = generate(client, url)
    for model, field, value in [(EmploymentFact, 'normalized_value_json', False),
                                 (UploadedFile, 'status', 'failed'), (RiskFinding, 'summary', '合成复核后文字')]:
        with db.session() as s:
            row = s.scalar(select(model).where(model.analysis_id == aid))
            assert row is not None
            setattr(row, field, value)
            s.commit()
        detail = context(client, url + '/' + saved['snapshot']['id'])
        assert detail['snapshot'] == saved['snapshot'] and detail['stale']
        assert context(client, url)['current_context']['context_signature'] != body['expected_context_signature']


def test_unbound_legacy_is_live_only_and_all_gets_leave_database_unchanged(api):
    client, db = api
    c = company(client)
    aid, _ = snapshot(db)
    assert client.get(f'/api/analyses/{aid}/report').status_code == 200
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    assert client.get(url).status_code == 404
    aid = current(client, c['id'])['analysis_id']
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/report-versions'
    body, saved = generate(client, url)
    import sqlite3
    with sqlite3.connect(db.path) as connection:
        before = list(connection.iterdump())
    for suffix in ['', '/' + saved['snapshot']['id'], '/requests/' + body['request_id']]:
        assert client.get(url + suffix).status_code == 200
    with sqlite3.connect(db.path) as connection:
        assert list(connection.iterdump()) == before


def test_nonfinite_current_fact_does_not_hide_safe_owned_snapshot(api, tmp_path):
    from qian_labor.models.core import EmploymentFact
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    body, saved = generate(client, base + '/report-versions')
    with db.session() as s:
        fact = s.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))
        fact.value_json = float('nan')
        s.commit()
    listing = context(client, base + '/report-versions')
    assert listing['current_context_available'] is False
    assert context(client, base + '/report-versions/' + saved['snapshot']['id'])['snapshot'] == saved['snapshot']
    failed = client.post(base + '/report-versions', json={**body, 'request_id': str(uuid4())})
    assert failed.status_code == 409 and failed.json()['request']['error_code'] == 'REPORT_SOURCE_INVALID'


def add_report_observation(db, aid, location):
    """Synthetic stored observation; no extraction or external model."""
    from qian_labor.models.core import UploadedFile, ContractAdvisoryRun, ContractClauseObservation
    from qian_labor.services.contract_advisory import source_digest
    with db.session() as s:
        file = s.scalar(select(UploadedFile).where(UploadedFile.analysis_id == aid))
        run = ContractAdvisoryRun(analysis_id=aid, file_id=file.id, input_key=str(uuid4()), content_hash=file.sha256,
            contract_version='contract-advisory-v1', execution_status='completed', input_statuses=['completed'])
        s.add(run)
        s.flush()
        observation = ContractClauseObservation(run_id=run.id, analysis_id=aid, file_id=file.id, dedupe_key=str(uuid4()),
            issue='合成条款待核对', checks=['核对合成条款'], next_action='人工复核', unverified_references=[],
            source_location=location, source_excerpt='合成条款', source_hash=source_digest(file.id, location, '合成条款'))
        s.add(observation)
        s.commit()
        return observation.id


@pytest.mark.parametrize('proof', [['malformed-json-proof'], None, 'malformed', 1,
    {'version': 'parser-grounding-v1', 'status': 'locally_located', 'requires_review': 'yes'}])
def test_malformed_advisory_proof_keeps_saved_reports_readable_and_refuses_generation(api, tmp_path, proof):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    body, saved = generate(client, base + '/report-versions')
    with db.session() as s:
        original = s.execute(text('SELECT payload_json FROM report_versions')).scalar_one()
    add_report_observation(db, aid, {'paragraph': 2, '_grounding': proof})
    listing = context(client, base + '/report-versions')
    assert listing['current_context_available'] is False and listing['current_context'] is None
    assert listing['warning'] == 'REPORT_SOURCE_INVALID' and listing['items'][0]['id'] == saved['snapshot']['id']
    detail = context(client, base + '/report-versions/' + saved['snapshot']['id'])
    assert detail['snapshot'] == saved['snapshot'] and detail['stale'] and not detail['current_context_available']
    denied = client.post(base + '/report-versions', json={**body, 'request_id': str(uuid4())})
    assert denied.status_code == 409 and denied.json()['request']['error_code'] == 'REPORT_SOURCE_INVALID'
    assert client.post(base + '/report-versions', json=body).json() == saved
    with db.session() as s:
        assert s.execute(text('SELECT count(*) FROM report_versions')).scalar_one() == 1
        assert s.execute(text('SELECT payload_json FROM report_versions')).scalar_one() == original


def test_forged_current_advisory_citation_refuses_new_report_but_keeps_saved_snapshot(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    body, saved = generate(client, base + '/report-versions')
    add_report_observation(db, aid, {
        'paragraph': 2,
        '_grounding': {'version': 'parser-grounding-v2', 'status': 'locally_located', 'requires_review': False},
        '_citation_id': 'cite-forged',
    })
    listing = context(client, base + '/report-versions')
    assert listing['current_context_available'] is False
    assert client.post(base + '/report-versions', json={**body, 'request_id': str(uuid4())}).json()['request']['error_code'] == 'REPORT_SOURCE_INVALID'
    assert context(client, base + '/report-versions/' + saved['snapshot']['id'])['snapshot'] == saved['snapshot']


def location_report_fixture():
    """Actual service payload shared with the renderer regression, entirely in memory."""
    from contextlib import nullcontext
    from datetime import datetime
    from io import BytesIO
    from types import SimpleNamespace
    from openpyxl import Workbook
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    from qian_labor.database import create_database
    from qian_labor.models.core import CompanyWorkspace, CompanyAnalysisBinding, Employee, UploadedFile, EmploymentFact
    from qian_labor.services.report_versions import ReportVersionService, GenerateReportRequest
    from qian_labor.services.source_provenance import deterministic_citation_id
    db = create_database('sqlite+pysqlite:///:memory:', create_schema=True)
    location = {'sheet': '合成联系人13800138000', 'column': '合成列13900139000', 'row': 2,
        '_grounding': {'version': EXTRACTION_VERSION, 'status': 'locally_located', 'requires_review': True}}
    workbook = Workbook()
    workbook.active.title = location['sheet']
    workbook.active.append([location['column']])
    workbook.active.append(['合成条款'])
    stream = BytesIO()
    workbook.save(stream)
    try:
        with db.session() as s:
            company = CompanyWorkspace(display_name='合成隐私企业')
            analysis = AnalysisBatch(name='合成来源位置', company_display_name='合成隐私企业', status='completed',
                created_at=datetime(2026, 9, 9), assessment_profile='labor_materials_v1')
            s.add_all([company, analysis])
            s.flush()
            s.add(CompanyAnalysisBinding(company_id=company.id, analysis_id=analysis.id, role='historical'))
            employee = Employee(analysis_id=analysis.id, masked_name='合成**', normalized_name='合成**', match_status='confirmed')
            file = UploadedFile(analysis_id=analysis.id, original_filename='synthetic-location.xlsx', storage_key='synthetic/location.xlsx',
                extension='.xlsx', mime_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                size_bytes=len(stream.getvalue()), sha256=hashlib.sha256(stream.getvalue()).hexdigest(), status='processed')
            s.add_all([employee, file])
            s.flush()
            location['_citation_id'] = deterministic_citation_id(file.sha256, location, '合成条款')
            fact = EmploymentFact(id='13800138-0000-4000-8000-000000000001', analysis_id=analysis.id, employee_id=employee.id,
                file_id=file.id, fact_type='employment.contract.exists', value_json=True, normalized_value_json=True,
                extraction_method='synthetic', confidence=1, dedupe_key=str(uuid4()))
            s.add(fact)
            s.flush()
            source = SourceLocator(analysis_id=analysis.id, file_id=file.id, fact_id=fact.id, locator_type='spreadsheet',
                location=location, excerpt='合成条款', content_hash=hashlib.sha256('合成条款'.encode()).hexdigest())
            s.add(source)
            s.commit()
        add_report_observation(db, analysis.id, location)
        service = ReportVersionService(db, SimpleNamespace(mutation=lambda _: nullcontext()))
        state = service.listing(company.id, analysis.id)
        output, status = service.generate(company.id, analysis.id, GenerateReportRequest(**command(state)))
        assert status == 201
        with db.session() as s:
            stored = s.execute(text('SELECT payload_json FROM report_versions')).scalar_one()
            assert json.loads(stored) == output['snapshot']['payload']
            assert hashlib.sha256(stored.encode()).hexdigest() == output['snapshot']['content_sha256']
            assert s.get(SourceLocator, source.id).location == location
        return output
    finally:
        db.dispose()


def test_fact_and_advisory_locations_mask_before_persistence_without_damaging_metadata():
    output = location_report_fixture()
    payload = output['snapshot']['payload']
    for location in [payload['facts'][0]['sources'][0]['location'], payload['advisories'][0]['source']['location']]:
        assert location['sheet'] == '合成联系人138****8000'
        assert location['column'] == '合成列139****9000' and location['row'] == 2
    assert '13800138000' not in json.dumps(output, ensure_ascii=False)
    assert '13900139000' not in json.dumps(output, ensure_ascii=False)
    assert payload['facts'][0]['id'] == '13800138-0000-4000-8000-000000000001'
    assert payload['assessment_revision']['check_date'] == '2026-09-09'
    assert output['snapshot']['input_revision'] == output['request']['expected_input_revision']
    assert output['snapshot']['context_signature'] == output['request']['expected_context_signature']


def test_renderer_fixture_matches_real_service_display_content_and_immutable_hash():
    from pathlib import Path
    from qian_labor.services.report_versions import canonical
    fixture_path = Path(__file__).resolve().parents[3] / 'apps/desktop/tests/fixtures/report-source-privacy.json'
    fixture = json.loads(fixture_path.read_text())
    fresh = location_report_fixture()
    def display_content(value):
        if isinstance(value, list):
            return [display_content(item) for item in value]
        if isinstance(value, dict):
            # IDs, signatures and capture time vary in independently created DBs;
            # all displayed source/fact/advisory fields and assessment axes remain.
            return {key: display_content(item) for key, item in value.items() if
                isinstance(item, (dict, list)) or not (key == 'id' or key == 'generated_at' or
                    key.endswith(('_id', '_ids', '_signature', '_revision')))}
        return value
    assert display_content(fixture['snapshot']['payload']) == display_content(fresh['snapshot']['payload'])
    assert hashlib.sha256(canonical(fixture['snapshot']['payload']).encode()).hexdigest() == fixture['snapshot']['content_sha256']
    assert '13800138000' not in json.dumps(fixture, ensure_ascii=False)
    assert '13900139000' not in json.dumps(fixture, ensure_ascii=False)


if __name__ == '__main__':
    print(json.dumps(location_report_fixture(), ensure_ascii=False))
