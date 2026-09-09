#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import secrets
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

import httpx
from docx import Document

from qian_labor.rules.catalog import RULE_IDS
from qian_labor.rules.registry import RULE_REGISTRY


ROOT = Path(__file__).resolve().parents[1]
READY_PREFIX = "QIAN_DESKTOP_READY="
TERMINAL = {"completed", "matching_review", "partial", "failed"}
PROCESSING_TIMEOUT_SECONDS = 30
MARKERS = (
    "SIDECAR_BOOT=PASS",
    "LOOPBACK_ONLY=PASS",
    "TOKEN_AUTH=PASS",
    "SQLITE_PERSISTENCE=PASS",
    "SYNTHETIC_IMPORT=PASS",
    "FAKE_PROVIDER_PIPELINE=PASS",
    "MATCHING_REVIEW=PASS",
    "EMPLOYEE_LEDGER=PASS",
    "REPORT=PASS",
    "R01_R20_REGRESSION=PASS",
    "SOURCE_TRACE=PASS",
    "DELETE_CLEANUP=PASS",
    "SIDECAR_SHUTDOWN=PASS",
)


class VerificationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunningSidecar:
    process: subprocess.Popen[str]
    base_url: str
    ready_pid: int
    launch_token: str


def _fixture_text() -> str:
    payload = {
        "synthetic_marker": "QIAN_DEMO_20260824",
        "document_type": "contract",
        "employee_number": "F-801",
        "employee_name": "完全虚构验收员工",
        "department": "虚构测试部门",
        "job_title": "虚构岗位",
        "facts": {
            "employment.status": "active",
            "employment.contract.exists": False,
            "employment.identity.match_status": "ambiguous",
            "employment.material_coverage": 0.4,
            "analysis.minimum_core_coverage": 0.4,
        },
    }
    return "QIAN_SYNTHETIC_JSON=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _expected_citation_id(file_sha256: str, location: dict[str, Any], excerpt: str) -> str:
    """Independent source identity oracle for the built-sidecar contract."""
    public_location = {key: value for key, value in location.items()
                       if key not in {"_grounding", "_citation_id"}}
    canonical = json.dumps(public_location, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    block_hash = hashlib.sha256(excerpt.encode()).hexdigest()
    digest = hashlib.sha256(f"{file_sha256}\0{canonical}\0{block_hash}".encode()).hexdigest()
    return f"cite-{digest[:32]}"


def _write_fixture(path: Path) -> None:
    document = Document()
    document.add_heading("完全虚构桌面验收材料", level=1)
    document.add_paragraph(
        _fixture_text()
    )
    document.save(path)


def _popen_options(*, windows_no_window: bool = False) -> dict[str, object]:
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if windows_no_window:
            flags |= subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def _start_sidecar(
    command: Sequence[str],
    data_dir: Path,
    token: str,
    *,
    cwd: Path,
    windows_no_window: bool,
) -> RunningSidecar:
    env = {
        **os.environ,
        "AI_PROVIDER": "fake",
        "AI_API_KEY": "",
        "QIAN_DESKTOP_DATA_DIR": str(data_dir),
        "QIAN_DESKTOP_TOKEN": token,
        "QIAN_DESKTOP_PORT": "0",
    }
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **_popen_options(windows_no_window=windows_no_window),
        )
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise VerificationError("BINARY_START_FAILED") from error

    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            lines.put(line.rstrip("\r\n"))

    threading.Thread(target=read_stdout, daemon=True).start()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationError("EXITED_BEFORE_READY")
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        if not line.startswith(READY_PREFIX):
            continue
        try:
            payload = json.loads(line.split("=", 1)[1])
        except (json.JSONDecodeError, IndexError) as error:
            raise VerificationError("READY_INVALID") from error
        host = payload.get("host")
        port = payload.get("port")
        pid = payload.get("pid")
        if host not in {"127.0.0.1", "::1"}:
            raise VerificationError("NON_LOOPBACK_BIND")
        if not isinstance(port, int) or not 0 < port <= 65535:
            raise VerificationError("READY_INVALID")
        if not isinstance(pid, int) or pid <= 0:
            raise VerificationError("READY_INVALID")
        return RunningSidecar(process, f"http://{host}:{port}", pid, token)
    raise VerificationError("READY_TIMEOUT")


def _pid_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _stop_sidecar(running: RunningSidecar) -> None:
    process = running.process
    shutdown_error: VerificationError | None = None
    if process.poll() is None:
        try:
            response = httpx.post(
                f"{running.base_url}/api/internal/shutdown",
                headers={"X-Qian-Desktop-Token": running.launch_token},
                timeout=3,
            )
            if response.status_code != 202:
                raise VerificationError("SHUTDOWN_REQUEST_FAILED")
            process.wait(timeout=10)
        except (httpx.HTTPError, subprocess.TimeoutExpired, VerificationError):
            shutdown_error = VerificationError("SHUTDOWN_REQUEST_FAILED")
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=10)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_is_alive(running.ready_pid):
        time.sleep(0.05)
    if _pid_is_alive(running.ready_pid):
        raise VerificationError("SHUTDOWN_TIMEOUT")
    if shutdown_error is not None:
        raise shutdown_error


def _poll(client: httpx.Client, analysis_id: str) -> dict[str, object]:
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(f"/api/analyses/{analysis_id}/processing")
        if response.status_code != 200:
            raise VerificationError("PROCESSING_STATUS_FAILED")
        payload = response.json()
        if payload.get("status") in TERMINAL:
            return payload
        time.sleep(0.05)
    raise VerificationError("PROCESSING_TIMEOUT")


def _assert_status(response: httpx.Response, expected: int, code: str) -> None:
    if response.status_code != expected:
        raise VerificationError(code)


def _source_bindings(data_dir: Path) -> dict[str, dict[str, Any]]:
    # All fixture facts share paragraph 2, so filename/location alone cannot detect a
    # wrong-fact citation. Check the persisted links read-only as well.
    try:
        with closing(sqlite3.connect((data_dir / "qian-labor.db").as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("""
                SELECT s.id, s.analysis_id AS source_analysis_id, s.file_id,
                       s.locator_type, s.location, s.excerpt,
                       f.analysis_id AS fact_analysis_id, f.employee_id,
                       f.file_id AS fact_file_id, f.fact_type, f.normalized_value_json,
                       u.analysis_id AS file_analysis_id, u.original_filename AS file_name,
                       u.sha256 AS file_sha256,
                       u.classified_kind, f.verification_status, a.assessment_profile
                FROM source_locators s
                JOIN employment_facts f ON f.id = s.fact_id
                JOIN uploaded_files u ON u.id = s.file_id
                JOIN analysis_batches a ON a.id = f.analysis_id
            """).fetchall()
            bindings = {}
            for row in rows:
                binding = dict(row)
                binding["location"] = json.loads(binding["location"])
                raw_value = binding.pop("normalized_value_json")
                # SQLite JSON columns have numeric affinity: 0.4 can already be
                # a Python float, while JSON strings/booleans remain encoded text.
                binding["value"] = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                bindings[binding["id"]] = binding
            return bindings
    except (sqlite3.Error, ValueError) as error:
        raise VerificationError("SOURCE_BINDING_READ_FAILED") from error


def _verify_source_trace(
    details: Sequence[dict[str, Any]], bindings: dict[str, dict[str, Any]], *,
    analysis_id: str, employee_id: str, fixture_file_id: str, fixture_name: str,
    assessment_profile: str = "legacy_full_v1",
    fixture_excerpt: str | None = None,
) -> str:
    if assessment_profile not in {"legacy_full_v1", "labor_materials_v1"}:
        raise VerificationError("SOURCE_TRACE_INVALID")
    required_by_rule = {rule.metadata.rule_id: set(rule.metadata.required_facts)
                        for rule in RULE_REGISTRY.values()}
    expected_r01 = []
    # Independent fixture oracle; never use the production grounder to validate itself.
    expected_excerpt = fixture_excerpt if fixture_excerpt is not None else _fixture_text()
    public_location = {"paragraph": 2}
    stored_location = {"paragraph": 2, "_grounding": {
        "version": "parser-grounding-v2", "status": "locally_located", "requires_review": False,
    }}
    fixture_sha256 = next((binding.get("file_sha256") for binding in bindings.values()
                           if binding.get("file_id") == fixture_file_id), None)
    expected_citation = (_expected_citation_id(fixture_sha256, stored_location, expected_excerpt)
                         if isinstance(fixture_sha256, str) else None)
    if expected_citation is not None:
        stored_location["_citation_id"] = expected_citation
    for detail in details:
        sources = detail.get("sources", [])
        required = required_by_rule.get(detail.get("rule_id"))
        derived_coverage = assessment_profile == "labor_materials_v1" and detail.get("rule_id") == "MATERIAL_COVERAGE_LOW"
        if derived_coverage:
            # This harness imports one synthetic contract containing the active
            # status and explicit missing-contract fact. Those exact two fields
            # determine its scoped coverage. Never accept arbitrary same-file
            # citations, model percentages, or unrelated review flags instead.
            required = {"employment.status", "employment.contract.exists"}
        if required is None or not isinstance(sources, list):
            raise VerificationError("SOURCE_TRACE_INVALID")
        positive = detail.get("assessment_status") != "insufficient_data"
        if positive and not sources:
            raise VerificationError("SOURCE_TRACE_MISSING")
        contributing = {}
        for source in sources:
            binding = bindings.get(source.get("id"))
            if derived_coverage and (binding is None
                    or binding.get("assessment_profile") != assessment_profile
                    or binding.get("classified_kind") != "contract"
                    or binding.get("verification_status") in {"conflicted", "pending_review", "needs_human_confirmation"}):
                raise VerificationError("SOURCE_TRACE_INVALID")
            if (binding is None or binding["fact_type"] not in required
                    or any(binding[key] != analysis_id for key in
                           ("source_analysis_id", "fact_analysis_id", "file_analysis_id"))
                    or binding["employee_id"] != employee_id
                    or binding["fact_file_id"] != fixture_file_id
                    or any(source.get(key) != expected or binding[key] != expected
                           for key, expected in {
                               "file_id": fixture_file_id, "file_name": fixture_name,
                               "locator_type": "paragraph", "excerpt": expected_excerpt,
                           }.items())
                    or source.get("location") != public_location
                    or (expected_citation is not None and source.get("citation_id") != expected_citation)
                    or source.get("provenance") != "locally_located"
                    or binding["location"] != stored_location):
                raise VerificationError("SOURCE_TRACE_INVALID")
            contributing[binding["fact_type"]] = binding["value"]
        if (positive or derived_coverage) and set(contributing) != required:
            raise VerificationError("SOURCE_TRACE_INVALID")
        if derived_coverage and (detail.get("assessment_status") != "insufficient_data"
                or contributing.get("employment.status") != "active"
                or contributing.get("employment.contract.exists") is not False):
            raise VerificationError("SOURCE_TRACE_INVALID")
        if detail.get("rule_id") == "CONTRACT_MISSING_ACTIVE":
            if (detail.get("assessment_status") != "suspected_risk"
                    or contributing.get("employment.status") != "active"
                    or contributing.get("employment.contract.exists") is not False):
                raise VerificationError("EXPECTED_R01_INVALID")
            expected_r01.append(str(detail["id"]))
    if len(expected_r01) != 1:
        raise VerificationError("EXPECTED_R01_MISSING")
    return expected_r01[0]


def _verify_report_sources(
    report_findings: Sequence[dict[str, Any]], details: Sequence[dict[str, Any]],
) -> None:
    verified = {item["id"]: item for item in details}
    if len(report_findings) != len(verified) or {item["id"] for item in report_findings} != set(verified):
        raise VerificationError("REPORT_INCONSISTENT")
    for item in report_findings:
        detail = verified[item["id"]]
        expected = []
        for source in detail["sources"]:
            fields = ["file_name", "locator_type", "location", "excerpt", "provenance"]
            if "citation_id" in source:
                fields.append("citation_id")
            expected.append({key: source[key] for key in fields})
        if (item.get("sources") != expected or item.get("rule_id") != detail["rule_id"]
                or item.get("assessment_status") != detail["assessment_status"]):
            raise VerificationError("REPORT_INCONSISTENT")


def verify_command(
    command: Sequence[str],
    *,
    token: str | None = None,
    cwd: Path | None = None,
    windows_no_window: bool = False,
) -> tuple[str, ...]:
    if not command or any(not isinstance(value, str) or not value for value in command):
        raise VerificationError("COMMAND_INVALID")
    running: RunningSidecar | None = None
    stopped_processes = 0
    launch_token = token or secrets.token_hex(32)
    try:
        with tempfile.TemporaryDirectory(prefix="qian-desktop-verify-") as temp:
            temp_path = Path(temp)
            data_dir = temp_path / "app-data"
            fixture = temp_path / "fictional-contract.docx"
            _write_fixture(fixture)

            running = _start_sidecar(
                command,
                data_dir,
                launch_token,
                cwd=cwd or ROOT,
                windows_no_window=windows_no_window,
            )
            with httpx.Client(base_url=running.base_url, timeout=5) as unauthenticated:
                _assert_status(unauthenticated.get("/health"), 200, "HEALTH_FAILED")
                _assert_status(unauthenticated.get("/api/status"), 401, "AUTH_BYPASS")
                _assert_status(
                    unauthenticated.get(
                        "/api/status", headers={"X-Qian-Desktop-Token": "incorrect-token"}
                    ),
                    401,
                    "AUTH_BYPASS",
                )

            with httpx.Client(
                base_url=running.base_url,
                headers={"X-Qian-Desktop-Token": launch_token},
                timeout=5,
            ) as client:
                _assert_status(client.get("/api/status"), 200, "TOKEN_AUTH_FAILED")
                created = client.post(
                    "/api/analyses",
                    json={"name": "虚构桌面验收", "company_display_name": "完全虚构企业"},
                )
                _assert_status(created, 201, "ANALYSIS_CREATE_FAILED")
                analysis_id = str(created.json()["id"])
                imported = client.post(
                    f"/api/analyses/{analysis_id}/import-paths",
                    json={"paths": [str(fixture)]},
                )
                _assert_status(imported, 200, "SYNTHETIC_IMPORT_FAILED")
                submitted = client.post(f"/api/analyses/{analysis_id}/process")
                _assert_status(submitted, 202, "PROCESS_SUBMIT_FAILED")
                terminal = _poll(client, analysis_id)
                if terminal.get("status") != "matching_review":
                    raise VerificationError("MATCHING_REVIEW_NOT_REACHED")
                matching = client.get(
                    f"/api/analyses/{analysis_id}/matching-candidates"
                )
                _assert_status(matching, 200, "MATCHING_CANDIDATES_FAILED")
                candidates = matching.json().get("candidates", [])
                if not isinstance(candidates, list) or len(candidates) != 1:
                    raise VerificationError("MATCHING_CANDIDATES_INVALID")
                candidate = candidates[0]
                if not candidate.get("employee_id") or not candidate.get("fact_ids"):
                    raise VerificationError("MATCHING_CANDIDATE_INCOMPLETE")
                decision = client.post(
                    f"/api/analyses/{analysis_id}/matching-decisions",
                    json={
                        "candidate_id": candidate["id"],
                        "decision": "assign",
                        "employee_id": candidate["employee_id"],
                        "fact_ids": candidate["fact_ids"],
                    },
                )
                _assert_status(decision, 200, "MATCHING_DECISION_FAILED")
                terminal = _poll(client, analysis_id)
                if terminal.get("status") != "completed":
                    raise VerificationError("FAKE_PIPELINE_NOT_COMPLETED")
                dashboard = client.get(f"/api/analyses/{analysis_id}/dashboard")
                _assert_status(dashboard, 200, "DASHBOARD_FAILED")
                findings = dashboard.json().get("findings", [])
                if not findings:
                    raise VerificationError("FINDING_MISSING")
                details = []
                for item in findings:
                    finding = client.get(f"/api/findings/{item['id']}")
                    _assert_status(finding, 200, "FINDING_DETAIL_FAILED")
                    details.append(finding.json())
                finding_id = _verify_source_trace(
                    details, _source_bindings(data_dir), analysis_id=analysis_id,
                    employee_id=str(candidate["employee_id"]),
                    fixture_file_id=str(imported.json()["files"][0]["id"]),
                    fixture_name=fixture.name,
                    assessment_profile=created.json().get("assessment_scope", {}).get("identifier"),
                    fixture_excerpt=Document(fixture).paragraphs[1].text,
                )
                ledger = client.get(f"/api/analyses/{analysis_id}/employees")
                _assert_status(ledger, 200, "EMPLOYEE_LEDGER_FAILED")
                ledger_payload = ledger.json()
                if ledger_payload.get("total") != 1 or not ledger_payload.get("items"):
                    raise VerificationError("EMPLOYEE_LEDGER_INVALID")
                employee_id = str(ledger_payload["items"][0]["id"])
                employee_detail = client.get(
                    f"/api/analyses/{analysis_id}/employees/{employee_id}"
                )
                _assert_status(employee_detail, 200, "EMPLOYEE_DETAIL_FAILED")
                if employee_detail.json().get("employee", {}).get("id") != employee_id:
                    raise VerificationError("EMPLOYEE_DETAIL_INVALID")
                report = client.get(f"/api/analyses/{analysis_id}/report")
                _assert_status(report, 200, "REPORT_FAILED")
                report_payload = report.json()
                if (
                    report_payload.get("summary", {}).get("employee_count")
                    != dashboard.json().get("summary", {}).get("employee_count")
                    or len(report_payload.get("employees", [])) != ledger_payload.get("total")
                ):
                    raise VerificationError("REPORT_INCONSISTENT")
                _verify_report_sources(report_payload.get("findings", []), details)

            _stop_sidecar(running)
            stopped_processes += 1
            running = None

            running = _start_sidecar(
                command,
                data_dir,
                launch_token,
                cwd=cwd or ROOT,
                windows_no_window=windows_no_window,
            )
            with httpx.Client(
                base_url=running.base_url,
                headers={"X-Qian-Desktop-Token": launch_token},
                timeout=5,
            ) as client:
                persisted = client.get(f"/api/analyses/{analysis_id}/dashboard")
                _assert_status(persisted, 200, "SQLITE_PERSISTENCE_FAILED")
                _assert_status(
                    client.get(f"/api/analyses/{analysis_id}/employees"),
                    200,
                    "EMPLOYEE_LEDGER_PERSISTENCE_FAILED",
                )
                _assert_status(
                    client.get(f"/api/analyses/{analysis_id}/report"),
                    200,
                    "REPORT_PERSISTENCE_FAILED",
                )
                deleted = client.delete(f"/api/analyses/{analysis_id}")
                _assert_status(deleted, 200, "DELETE_FAILED")
                if deleted.json().get("status") != "deleted":
                    raise VerificationError("DELETE_STATUS_INVALID")

            _stop_sidecar(running)
            stopped_processes += 1
            running = None

            running = _start_sidecar(
                command,
                data_dir,
                launch_token,
                cwd=cwd or ROOT,
                windows_no_window=windows_no_window,
            )
            with httpx.Client(
                base_url=running.base_url,
                headers={"X-Qian-Desktop-Token": launch_token},
                timeout=5,
            ) as client:
                if client.get(f"/api/analyses/{analysis_id}/dashboard").status_code != 404:
                    raise VerificationError("DELETE_NOT_DURABLE")
                if client.get(f"/api/findings/{finding_id}").status_code != 404:
                    raise VerificationError("FINDING_DELETE_NOT_DURABLE")
                if client.get(f"/api/analyses/{analysis_id}/employees").status_code != 404:
                    raise VerificationError("EMPLOYEE_LEDGER_DELETE_NOT_DURABLE")
                if client.get(f"/api/analyses/{analysis_id}/report").status_code != 404:
                    raise VerificationError("REPORT_DELETE_NOT_DURABLE")

            _stop_sidecar(running)
            stopped_processes += 1
            running = None

            if len(RULE_IDS) != 20:
                raise VerificationError("R01_R20_REGRESSION")
            if stopped_processes != 3:
                raise VerificationError("SHUTDOWN_INCOMPLETE")
        return MARKERS
    except VerificationError:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError, OSError) as error:
        raise VerificationError("VERIFICATION_FAILED") from error
    finally:
        if running is not None:
            _stop_sidecar(running)
