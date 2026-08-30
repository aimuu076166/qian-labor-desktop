import json
from datetime import date
from pathlib import Path

from qian_labor.rules.base import RiskRule
from qian_labor.rules.catalog import RULE_VERSION
from qian_labor.rules.implementations import APPLICABILITY, EVALUATORS
from qian_labor.rules.types import ManagementParameter, RuleMetadata

ASSESSMENT_STATUSES = {
    "management_reminder",
    "confirmed_anomaly",
    "suspected_risk",
    "insufficient_data",
    "requires_human_review",
}
BASIS_TYPES = {"legal_basis", "system_management_parameter"}


def _catalog_date(payload: dict, field: str) -> str:
    value = payload.get(field)
    if value is None:
        raise ValueError("RULE_CATALOG_DATE_REQUIRED")
    if not isinstance(value, str):
        raise ValueError("RULE_CATALOG_DATE_INVALID")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("RULE_CATALOG_DATE_INVALID") from error
    return value


def load_registry(catalog_path: Path | None = None) -> dict[str, RiskRule]:
    path = catalog_path or (Path(__file__).parent / "catalog" / "cn_labor_mvp_1_0_0.yaml")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["catalog_version"] != RULE_VERSION:
        raise ValueError("RULE_CATALOG_VERSION_UNSUPPORTED")
    effective_date = _catalog_date(payload, "effective_date")
    last_verified_at = _catalog_date(payload, "last_verified_at")
    registry: dict[str, RiskRule] = {}
    seen_ids: set[str] = set()
    for item in payload["rules"]:
        code = item["code"]
        if code in registry or item["rule_id"] in seen_ids:
            raise ValueError("RULE_CATALOG_DUPLICATE")
        if code not in EVALUATORS:
            raise ValueError("RULE_IMPLEMENTATION_UNKNOWN")
        if not item["legal_source"]:
            raise ValueError("RULE_LEGAL_SOURCE_REQUIRED")
        if item["assessment_status"] not in ASSESSMENT_STATUSES:
            raise ValueError("RULE_ASSESSMENT_STATUS_UNSUPPORTED")
        basis_type = item.get("basis_type", "legal_basis")
        if basis_type not in BASIS_TYPES:
            raise ValueError("RULE_BASIS_TYPE_UNSUPPORTED")
        management_parameters = tuple(
            ManagementParameter(
                basis_type="system_management_parameter",
                name=parameter["name"],
                value=parameter["value"],
                unit=parameter["unit"],
                description=parameter["description"],
            )
            for parameter in item.get("management_parameters", [])
        )
        metadata = RuleMetadata(
            rule_id=item["rule_id"],
            version=RULE_VERSION,
            name=item["name"],
            category=item["category"],
            applicable_region="CN",
            effective_date=effective_date,
            basis_type=basis_type,
            legal_source=tuple(item["legal_source"]),
            management_parameters=management_parameters,
            last_verified_at=last_verified_at,
            required_facts=tuple(item["required_facts"]),
            severity=item["severity"],
            assessment_status=item["assessment_status"],
            requires_human_review=item["requires_human_review"],
            recommended_action="核对原始材料、事实口径及适用地区，并由有权限人员复核",
        )
        registry[code] = RiskRule(metadata, EVALUATORS[code], APPLICABILITY.get(code))
        seen_ids.add(item["rule_id"])
    if len(registry) != 20:
        raise ValueError("RULE_CATALOG_INCOMPLETE")
    return registry


RULE_REGISTRY = load_registry()
