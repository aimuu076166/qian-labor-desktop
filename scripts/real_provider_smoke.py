#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path


def _not_run(reason: str) -> int:
    print("REAL_PROVIDER_SMOKE=NOT_RUN")
    print(f"REASON={reason}")
    return 0


def _safe_failure_code(error: Exception) -> str:
    value = str(error)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", value) else type(error).__name__


def _valid_identity(serial: str = "951") -> str:
    stem = "640104" + "19900101" + serial
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    return stem + checks[
        sum(int(value) * weight for value, weight in zip(stem, weights, strict=True)) % 11
    ]


def _write_synthetic_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "员工编号",
                "姓名",
                "在职状态",
                "劳动合同存在",
                "合同开始日期",
                "合同结束日期",
                "合同期限可读",
                "身份匹配状态",
                "员工材料覆盖率",
                "核心材料覆盖率",
                "手机号",
                "身份证号",
                "银行卡号",
            ]
        )
        writer.writerow(
            [
                "F-951",
                "完全虚构GLM验收员工",
                "active",
                "false",
                "2026-01-01",
                "2026-12-31",
                "true",
                "confirmed",
                "0.40",
                "0.40",
                "13912345678",
                _valid_identity(),
                "6222123456789012",
            ]
        )


def _count_sourced_facts(fact_ids: list[str | None]) -> int:
    return len({fact_id for fact_id in fact_ids if fact_id})


def main() -> int:
    api_key = os.getenv("REAL_PROVIDER_SMOKE_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key:
        return _not_run("AI_API_KEY_MISSING")

    pepper = os.getenv("REAL_PROVIDER_SMOKE_PII_HASH_PEPPER") or os.getenv(
        "PII_HASH_PEPPER"
    )
    if not pepper:
        return _not_run("PII_HASH_PEPPER_MISSING")

    # Delay application imports until after the no-key path so CI can prove NOT_RUN
    # without attempting any external-provider setup.
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from qian_labor.ai.fact_contract import CANONICAL_FACT_TYPE_SET
    from qian_labor.desktop.app import create_desktop_app
    from qian_labor.models.core import (
        AIUsageRecord,
        EmploymentFact,
        RiskFinding,
        SourceLocator,
    )
    from qian_labor.rules.catalog import RULE_IDS
    from qian_labor.security.local_redaction import valid_external_pepper
    from qian_labor.settings import Settings

    if not valid_external_pepper(pepper):
        return _not_run("PII_HASH_PEPPER_INVALID")

    try:
        max_calls = int(os.getenv("REAL_PROVIDER_SMOKE_MAX_CALLS", "4"))
        budget = float(os.getenv("REAL_PROVIDER_SMOKE_BUDGET_USD", "5"))
        if not 1 <= max_calls <= 10 or not 0 < budget <= 25:
            raise ValueError("SMOKE_LIMIT_INVALID")

        settings = Settings(
            app_secret="real-provider-smoke-separate-app-secret",
            pii_hash_pepper=pepper,
            ai_provider="zhipu",
            ai_api_key=api_key,
            ai_base_url=os.getenv("REAL_PROVIDER_SMOKE_BASE_URL", ""),
            ai_text_model=os.getenv("REAL_PROVIDER_SMOKE_MODEL", ""),
            ai_vision_model=os.getenv("REAL_PROVIDER_SMOKE_VISION_MODEL", ""),
            ai_batch_budget_usd=budget,
            ai_max_calls_per_analysis=max_calls,
        )

        with tempfile.TemporaryDirectory(prefix="qian-real-provider-smoke-") as temporary:
            root = Path(temporary)
            fixture = root / "完全虚构-GLM-验收花名册.csv"
            _write_synthetic_csv(fixture)
            token = secrets.token_hex(32)
            app = create_desktop_app(
                data_dir=root / "app-data",
                launch_token=token,
                settings=settings,
            )
            headers = {"X-Qian-Desktop-Token": token}

            with TestClient(app) as client:
                created = client.post(
                    "/api/analyses",
                    headers=headers,
                    json={
                        "name": "完全虚构GLM真实模型验收",
                        "company_display_name": "完全虚构验收企业",
                    },
                )
                if created.status_code != 201:
                    raise RuntimeError("ANALYSIS_CREATE_FAILED")
                analysis_id = str(created.json()["id"])

                imported = client.post(
                    f"/api/analyses/{analysis_id}/import-paths",
                    headers=headers,
                    json={"paths": [str(fixture)]},
                )
                if imported.status_code != 200:
                    raise RuntimeError("SYNTHETIC_IMPORT_FAILED")

                submitted = client.post(
                    f"/api/analyses/{analysis_id}/process",
                    headers=headers,
                )
                if submitted.status_code != 202:
                    raise RuntimeError("PROCESS_SUBMIT_FAILED")

                deadline = time.monotonic() + 120
                terminal: dict[str, object] | None = None
                while time.monotonic() < deadline:
                    status_response = client.get(
                        f"/api/analyses/{analysis_id}/processing",
                        headers=headers,
                    )
                    if status_response.status_code != 200:
                        raise RuntimeError("PROCESS_STATUS_FAILED")
                    payload = status_response.json()
                    if payload.get("status") in {
                        "completed",
                        "matching_review",
                        "partial",
                        "failed",
                    }:
                        terminal = payload
                        break
                    time.sleep(0.2)
                if terminal is None:
                    raise RuntimeError("PROCESS_TIMEOUT")
                if terminal.get("status") not in {"completed", "matching_review"}:
                    raise RuntimeError("REAL_PROVIDER_PIPELINE_FAILED")

                database = app.state.database
                with database.session() as session:
                    facts = list(
                        session.scalars(
                            select(EmploymentFact).where(
                                EmploymentFact.analysis_id == analysis_id
                            )
                        )
                    )
                    usages = list(
                        session.scalars(
                            select(AIUsageRecord).where(
                                AIUsageRecord.analysis_id == analysis_id,
                                AIUsageRecord.provider == "zhipu",
                                AIUsageRecord.status == "succeeded",
                            )
                        )
                    )
                    findings = list(
                        session.scalars(
                            select(RiskFinding).where(
                                RiskFinding.analysis_id == analysis_id
                            )
                        )
                    )
                    source_fact_ids = list(
                        session.scalars(
                            select(SourceLocator.fact_id).where(
                                SourceLocator.analysis_id == analysis_id,
                                SourceLocator.fact_id.is_not(None),
                            )
                        )
                    )

                source_count = _count_sourced_facts(source_fact_ids)
                if not facts:
                    raise RuntimeError("STRUCTURED_FACTS_MISSING")
                if not usages or len(usages) > max_calls:
                    raise RuntimeError("REAL_PROVIDER_USAGE_INVALID")
                if any(fact.fact_type not in CANONICAL_FACT_TYPE_SET for fact in facts):
                    raise RuntimeError("FACT_CONTRACT_INVALID")
                if any(finding.rule_id not in RULE_IDS for finding in findings):
                    raise RuntimeError("R01_R20_BOUNDARY_INVALID")
                if source_count <= 0:
                    raise RuntimeError("SOURCE_TRACE_MISSING")

                deleted = client.delete(
                    f"/api/analyses/{analysis_id}",
                    headers=headers,
                )
                if deleted.status_code != 200 or deleted.json().get("status") != "deleted":
                    raise RuntimeError("DELETE_FAILED")

        model = settings.ai_text_model.strip() or "glm-5.3-flash"
        for marker in (
            "REAL_PROVIDER_SMOKE=PASS",
            "PROVIDER=zhipu",
            f"MODEL={model}",
            "TEXT_INPUT=PASS",
            "STRUCTURED_OUTPUT=PASS",
            "FACT_CONTRACT=PASS",
            "R01_R20_BOUNDARY=PASS",
            "SOURCE_TRACE=PASS",
            "DELETE_CLEANUP=PASS",
            "IMAGE_INPUT=NOT_RUN",
        ):
            print(marker)
        return 0
    except Exception as error:
        print(f"REAL_PROVIDER_SMOKE=FAIL:{_safe_failure_code(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
