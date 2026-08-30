from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from datetime import date, timedelta
from math import isfinite
from typing import Any

from qian_labor.rules.types import (
    AssessmentStatus,
    BasisType,
    ManagementParameter,
    RuleContext,
    RuleMetadata,
    RuleResult,
)


def _date(value: Any) -> date:
    return date.fromisoformat(str(value))


def _value(context: RuleContext, name: str) -> Any:
    return context.facts[name].value


def _calendar_period_end(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = monthrange(year, month)[1]
    if value.day > last_day:
        return date(year, month, last_day)
    return date(year, month, value.day) - timedelta(days=1)


def _inclusive_period_reaches_months(start: date, end: date, months: int) -> bool:
    return end >= _calendar_period_end(start, months)


def _inclusive_period_exceeds_months(start: date, end: date, months: int) -> bool:
    return end > _calendar_period_end(start, months)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _status_applicability(context: RuleContext, applicable_statuses: set[str]) -> bool | None:
    fact = context.facts.get("employment.status")
    if fact is None or fact.conflicted or not isinstance(fact.value, str):
        return None
    status = fact.value.strip().lower()
    if status not in {"active", "probation", "terminated"}:
        return None
    return status in applicable_statuses


def r10_applicability(context: RuleContext) -> bool | None:
    return _status_applicability(context, {"active", "probation"})


def r18_applicability(context: RuleContext) -> bool | None:
    return _status_applicability(context, {"terminated"})


def _result(
    context: RuleContext,
    metadata: RuleMetadata,
    triggered: bool,
    phrase: str,
    *,
    status: AssessmentStatus | None = None,
    missing: tuple[str, ...] = (),
    review: bool | None = None,
    severity: str | None = None,
    basis_type: BasisType | None = None,
    legal_source: tuple[str, ...] | None = None,
    management_parameters: tuple[ManagementParameter, ...] | None = None,
) -> RuleResult:
    facts = [context.facts[name] for name in metadata.required_facts if name in context.facts]
    return RuleResult(
        rule_id=metadata.rule_id,
        rule_version=metadata.version,
        triggered=triggered,
        assessment_status=(status or metadata.assessment_status) if triggered else "not_triggered",
        severity=severity or metadata.severity,
        trigger_fact_ids=tuple(fact.id for fact in facts) if triggered else (),
        source_locator_ids=(
            tuple(dict.fromkeys(source for fact in facts for source in fact.source_locator_ids))
            if triggered
            else ()
        ),
        missing_fact_types=missing,
        message_params={"finding_phrase": phrase},
        requires_human_review=(metadata.requires_human_review if review is None else review)
        if triggered
        else False,
        basis_type=basis_type if triggered else None,
        legal_source=legal_source if triggered else None,
        management_parameters=management_parameters if triggered else None,
    )


def r01(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    hit = _value(context, "employment.status") in {"active", "probation"} and not bool(
        _value(context, "employment.contract.exists")
    )
    return _result(
        context,
        metadata,
        hit,
        "本次材料中未发现书面劳动合同，请核对纸质或电子合同",
    )


def r02(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    remaining = (
        _date(_value(context, "employment.contract.end_date")) - context.analysis_date
    ).days
    hit = _value(context, "employment.status") in {"active", "probation"} and 0 <= remaining <= 30
    return _result(
        context,
        metadata,
        hit,
        f"合同距到期还有 {remaining} 天，已进入系统 30 天提前提醒窗口，不代表违法认定",
    )


def r03(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    hit = (
        _date(_value(context, "employment.contract.end_date")) < context.analysis_date
        and _value(context, "employment.status") in {"active", "probation"}
        and bool(_value(context, "employment.evidence_after_contract_end"))
    )
    return _result(context, metadata, hit, "合同到期后材料仍显示持续用工，请核对续签或终止情况")


def r04(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    entities = {str(item).strip() for item in _value(context, "employment.entities") if item}
    return _result(
        context, metadata, len(entities) > 1, "多份材料中的主体名称不一致，请核对合理原因"
    )


def r05(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    exists = bool(_value(context, "employment.contract.exists"))
    readable = bool(_value(context, "employment.contract.term_readable"))
    return _result(
        context,
        metadata,
        exists and not readable,
        "合同期限字段缺失、模糊或版本冲突，资料不足，暂时无法判断",
        status="insufficient_data",
    )


def r06(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    contract_start = _parse_date(_value(context, "employment.contract.start_date"))
    probation_start = _parse_date(_value(context, "employment.probation.start_date"))
    probation_end = _parse_date(_value(context, "employment.probation.end_date"))
    raw_contract_type = _value(context, "employment.contract.type")
    contract_type = raw_contract_type.strip().lower() if isinstance(raw_contract_type, str) else ""
    task_types = {"task", "project", "completion_of_task"}
    indefinite_types = {"indefinite", "open_ended", "non_fixed"}
    fixed_types = {"fixed", "fixed_term"}
    contract_end_value = _value(context, "employment.contract.end_date")
    contract_end = _parse_date(contract_end_value)

    invalid = (
        contract_start is None
        or probation_start is None
        or probation_end is None
        or contract_type not in task_types | indefinite_types | fixed_types
        or (contract_type not in indefinite_types and contract_end is None)
        or (
            contract_type in indefinite_types
            and contract_end_value not in {None, ""}
            and contract_end is None
        )
    )
    if invalid:
        return _result(
            context,
            metadata,
            True,
            "合同或试用期日期、期限类型无法可靠解析，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
        )

    assert contract_start is not None
    assert probation_start is not None
    assert probation_end is not None
    if (
        probation_end < probation_start
        or probation_start < contract_start
        or (contract_end is not None and contract_end < contract_start)
        or (contract_end is not None and probation_end > contract_end)
    ):
        return _result(
            context,
            metadata,
            True,
            "合同与试用期日期存在逆序或超出合同期限，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
        )

    if contract_type in task_types:
        maximum_months = 0
    elif contract_type in indefinite_types:
        maximum_months = 6
    else:
        assert contract_end is not None
        if not _inclusive_period_reaches_months(contract_start, contract_end, 3):
            maximum_months = 0
        elif not _inclusive_period_reaches_months(contract_start, contract_end, 12):
            maximum_months = 1
        elif not _inclusive_period_reaches_months(contract_start, contract_end, 36):
            maximum_months = 2
        else:
            maximum_months = 6

    exceeds_maximum = maximum_months == 0 or _inclusive_period_exceeds_months(
        probation_start, probation_end, maximum_months
    )
    return _result(
        context,
        metadata,
        exceeds_maximum,
        "试用期期限可能超过日历月边界，请律师复核",
    )


def r07(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    periods = {tuple(item) for item in _value(context, "employment.probation.periods")}
    return _result(
        context, metadata, len(periods) > 1, "多份材料出现不同试用期记录，请排除副本后复核"
    )


def r08(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    remaining = (
        _date(_value(context, "employment.probation.end_date")) - context.analysis_date
    ).days
    has_material = bool(_value(context, "employment.probation.assessment_exists"))
    return _result(
        context,
        metadata,
        0 <= remaining <= 14 and not has_material,
        "试用期已进入系统 14 天提前提醒窗口，请准备或核对考核材料",
        status="management_reminder",
        review=False,
    )


def r09(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    comparable = bool(_value(context, "employment.pay.comparable"))
    contract_wage = float(_value(context, "employment.pay.contract_wage"))
    actual_wage = float(_value(context, "employment.pay.actual_wage"))
    threshold = max(100.0, abs(contract_wage) * 0.1)
    hit = comparable and abs(contract_wage - actual_wage) > threshold
    return _result(
        context,
        metadata,
        hit,
        "合同工资与实际工资超过系统筛查阈值（100 元或 10%），该阈值非法定，请人工核对口径",
    )


def r10(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    raw_status = _value(context, "employment.status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    start_date = _parse_date(_value(context, "employment.start_date"))
    analysis_date = context.analysis_date
    present = _parse_bool(_value(context, "employment.social_insurance.present"))
    period_matches = _parse_bool(_value(context, "employment.social_insurance.period_matches"))
    if (
        status not in {"active", "probation", "terminated"}
        or start_date is None
        or start_date > analysis_date
        or present is None
        or period_matches is None
    ):
        return _result(
            context,
            metadata,
            True,
            "社保核对所需日期或关键值无效，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
            severity="info",
        )

    if status not in {"active", "probation"}:
        return _result(context, metadata, False, "当前人员状态不适用在职社保核对")
    if not period_matches:
        return _result(
            context,
            metadata,
            True,
            "社保清单月份不同，请核对异地参保、退休返聘、非劳动关系或数据上传不完整等可能解释",
            status="management_reminder",
            review=False,
            severity="info",
        )
    if present:
        return _result(context, metadata, False, "目标月份社保清单已匹配")

    days_since_start = (analysis_date - start_date).days
    possible_explanations = "清单月份不同、异地参保、退休返聘、非劳动关系或数据上传不完整"
    if days_since_start <= 30:
        return _result(
            context,
            metadata,
            True,
            f"入职 {days_since_start} 日尚处办理核对窗口，请核对{possible_explanations}",
            status="management_reminder",
            review=False,
            severity="info",
        )
    return _result(
        context,
        metadata,
        True,
        f"入职已 {days_since_start} 日且目标月份社保清单未匹配，请核对{possible_explanations}",
        status="suspected_risk",
        review=True,
        severity="high",
    )


def r11(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    contract_entity = str(_value(context, "employment.contract.employer")).strip()
    insurance_entity = str(_value(context, "employment.social_insurance.entity")).strip()
    explained = bool(_value(context, "employment.entity_mismatch_explained"))
    return _result(
        context,
        metadata,
        contract_entity != insurance_entity and not explained,
        "合同与社保主体不一致，请人工核对",
    )


def r12(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    return _result(
        context,
        metadata,
        bool(_value(context, "employment.social_insurance.waiver_language")),
        "材料出现放弃或补贴替代社保缴纳的语义线索，仅供人工复核",
        status="requires_human_review",
    )


def r13(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    overtime = _parse_number(_value(context, "employment.attendance.overtime_hours"))
    raw_overtime_type = _value(context, "employment.attendance.overtime_type")
    overtime_type = raw_overtime_type.strip().lower() if isinstance(raw_overtime_type, str) else ""
    paid = _parse_bool(_value(context, "employment.pay.overtime_evidence"))
    comp_time = _parse_bool(_value(context, "employment.attendance.comp_time_evidence"))

    if (
        overtime is None
        or overtime < 0
        or paid is None
        or comp_time is None
        or overtime_type not in {"weekday", "rest_day", "statutory_holiday", "unknown"}
    ):
        return _result(
            context,
            metadata,
            True,
            "加班时长、类型或支付补休证据无法可靠解析，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
        )

    if overtime <= 0:
        return _result(context, metadata, False, "本次材料未见正数加班时长")
    if overtime_type == "unknown":
        return _result(
            context,
            metadata,
            True,
            "加班类型不明确，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
        )
    if overtime_type == "rest_day":
        hit = not paid and not comp_time
        phrase = "休息日加班线索未见支付或补休证据，请人工复核"
    else:
        hit = not paid
        phrase = "工作日或法定节假日加班线索未见支付证据，调休不能排除风险，请人工复核"
    return _result(
        context,
        metadata,
        hit,
        phrase,
    )


def r14(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    return _result(
        context,
        metadata,
        bool(_value(context, "employment.attendance_payroll.mismatch")),
        "考勤与工资材料的可比字段存在差异，系统不推测原因",
    )


def r15(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    hit = _value(context, "employment.status") in {"active", "probation"} and not bool(
        _value(context, "employment.attendance.present")
    )
    return _result(
        context,
        metadata,
        hit,
        "在职人员目标月份未发现考勤记录，资料不足，暂时无法判断",
        status="insufficient_data",
    )


def r16(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    hit = _value(context, "employment.status") == "terminated" and bool(
        _value(context, "employment.post_termination_record")
    )
    return _result(context, metadata, hit, "离职后仍有工资、考勤或社保记录，请核对结算周期或状态")


def r17(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    occurred = bool(_value(context, "employment.termination.occurred"))
    complete = bool(_value(context, "employment.termination.notice_exists")) and bool(
        _value(context, "employment.termination.delivery_exists")
    )
    return _result(
        context,
        metadata,
        occurred and not complete,
        "材料显示解除或终止，但本次材料未发现完整通知及送达记录",
        status="requires_human_review",
    )


def r18(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    raw_status = _value(context, "employment.status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    raw_materials = _value(context, "employment.termination.settlement_materials")
    if (
        status not in {"active", "probation", "terminated"}
        or not isinstance(raw_materials, list)
        or any(not isinstance(item, str) or not item.strip() for item in raw_materials)
    ):
        return _result(
            context,
            metadata,
            True,
            "离职材料清单格式无效，资料不足，暂时无法判断",
            status="insufficient_data",
            review=True,
            severity="info",
        )

    if status != "terminated":
        return _result(context, metadata, False, "当前人员状态不适用离职材料核对")

    materials = {item.strip().lower() for item in raw_materials}
    allowed_materials = {
        "final_pay",
        "separation_certificate",
        "handover",
        "item_handover",
        "work_handover",
    }
    unknown_materials = tuple(sorted(materials - allowed_materials))
    if unknown_materials:
        return _result(
            context,
            metadata,
            True,
            "离职材料清单包含未知代码，资料不足，暂时无法判断",
            status="insufficient_data",
            missing=unknown_materials,
            review=True,
            severity="info",
            management_parameters=(),
        )
    core_materials = {"final_pay", "separation_certificate"}
    missing_core = tuple(sorted(core_materials - materials))
    if missing_core:
        return _result(
            context,
            metadata,
            True,
            "工资或应付款结算、解除或终止证明等核心材料不足，不计算补偿，不作终局法律结论",
            status="insufficient_data",
            missing=missing_core,
            review=True,
            severity="info",
            management_parameters=(),
        )

    management_materials = {"handover", "item_handover", "work_handover"}
    if materials.isdisjoint(management_materials):
        return _result(
            context,
            metadata,
            True,
            "核心离职材料已齐，尚缺企业内部物品或工作交接记录",
            status="management_reminder",
            missing=("handover",),
            review=False,
            severity="info",
            basis_type="system_management_parameter",
            legal_source=(),
            management_parameters=metadata.management_parameters,
        )
    return _result(context, metadata, False, "核心离职材料与企业管理交接记录已齐")


def r19(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    return _result(
        context,
        metadata,
        _value(context, "employment.identity.match_status") in {"ambiguous", "unknown"},
        "员工身份归属存在歧义，暂停受影响员工的高风险结论",
        status="insufficient_data",
    )


def r20(context: RuleContext, metadata: RuleMetadata) -> RuleResult:
    employee_coverage = float(_value(context, "employment.material_coverage"))
    core_coverage = float(_value(context, "analysis.minimum_core_coverage"))
    return _result(
        context,
        metadata,
        employee_coverage < 0.5 or core_coverage < 0.6,
        "未达到系统数据质量参数（员工 50%、企业核心 60%），资料不足，暂时无法判断",
        status="insufficient_data",
    )


EVALUATORS: dict[str, Callable[[RuleContext, RuleMetadata], RuleResult]] = {
    f"R{index:02d}": evaluator
    for index, evaluator in enumerate(
        [
            r01,
            r02,
            r03,
            r04,
            r05,
            r06,
            r07,
            r08,
            r09,
            r10,
            r11,
            r12,
            r13,
            r14,
            r15,
            r16,
            r17,
            r18,
            r19,
            r20,
        ],
        start=1,
    )
}

APPLICABILITY: dict[str, Callable[[RuleContext], bool | None]] = {
    "R10": r10_applicability,
    "R18": r18_applicability,
}
