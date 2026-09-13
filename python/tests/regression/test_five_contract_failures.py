"""五份真实合同场景的修复后行为（方案第一步证据 + 第三步修复的翻转验证）。

第一步固化的失败形态，在第三步格式兼容修复后应全部转为可接收行为。
控制组测试保持不变：规范输出路径不得被破坏。
"""
import json


def _probation_periods_text_fact() -> dict[str, object]:
    return {
        "employee_id": "F-903",
        "fact_type": "employment.probation.periods",
        "value_type": "text",
        "value_text": "2026年1月1日至2026年3月1日，共2个月",
        "value_integer": None,
        "value_number": None,
        "value_boolean": None,
        "value_string_list": None,
        "value_json": None,
        "confidence": 0.9,
        "source": {
            "file_name": "01-劳动合同书（固定期限3年）.txt",
            "page": None,
            "row": None,
            "column": None,
            "sheet": None,
            "paragraph": 12,
            "bbox": None,
            "excerpt": "试用期自2026年1月1日起至2026年3月1日止",
        },
        "needs_human_confirmation": False,
    }


def _contract_exists_fact(**source_overrides) -> dict[str, object]:
    source = {
        "file_name": "04-劳动合同书（无固定期限）.txt",
        "page": None,
        "row": None,
        "column": None,
        "sheet": None,
        "paragraph": 1,
        "bbox": None,
        "excerpt": "甲方与乙方签订无固定期限劳动合同",
    }
    source.update(source_overrides)
    return {
        "employee_id": "F-903",
        "fact_type": "employment.contract.exists",
        "value_type": "boolean",
        "value_text": None,
        "value_integer": None,
        "value_number": None,
        "value_boolean": True,
        "value_string_list": None,
        "value_json": None,
        "confidence": 0.95,
        "source": source,
        "needs_human_confirmation": False,
    }


def _extract(payload: dict[str, object]):
    from test_zhipu_provider import (  # noqa: F401
        _provider,
        _provider_payload,
        _success_response,
    )
    return _provider(lambda _: _success_response(payload)).extract(
        "虚构合同.txt", b"synthetic contract body"
    )


def _payload(facts: list[dict[str, object]], **top: object) -> dict[str, object]:
    from test_zhipu_provider import _provider_payload
    payload = _provider_payload()
    payload["facts"] = facts
    payload.update(top)
    return payload


def test_contract_1_3_probation_periods_text_now_converts() -> None:
    """合同1-3原文场景：periods 文本 → 可接收行为（转换或待复核），不再整份失败。"""
    result = _extract(_payload([_probation_periods_text_fact()]))
    assert len(result.facts) == 1
    # "2026年1月1日至2026年3月1日，共2个月" 含后缀月份说明，无法无损解析为区间：
    # 按方案降级为显式缺失 + 待复核，保留人工入口，而不是拒绝整份。
    assert result.facts[0].needs_human_confirmation


def test_contract_1_3_probation_text_plus_omitted_source_now_passes() -> None:
    """合同1-3变体：periods 文本 + source 省略键，现在同样可接收。"""
    fact = _probation_periods_text_fact()
    fact["source"] = {
        "file_name": "01-劳动合同书（固定期限3年）.txt",
        "excerpt": "试用期自2026年1月1日起至2026年3月1日止",
    }
    result = _extract(_payload([fact]))
    assert len(result.facts) == 1
    assert result.facts[0].source.page is None


def test_contract_4_5_omitted_source_keys_now_pass() -> None:
    """合同4-5主凶场景：模型省略 source 可选键 → 补空值照常接收。"""
    result = _extract(_payload([_contract_exists_fact()]))
    assert len(result.facts) == 1
    assert result.facts[0].value is True


def test_contract_4_5_top_level_summary_now_ignored() -> None:
    """合同4-5变体：顶层 summary 扩展键 → 忽略，不击杀整份。"""
    result = _extract(
        _payload([_contract_exists_fact()], summary="本合同为无固定期限劳动合同")
    )
    assert len(result.facts) == 1


def test_nested_source_extra_field_now_ignored() -> None:
    """source 内扩展键 → 忽略。"""
    fact = _contract_exists_fact(line=3)
    result = _extract(_payload([fact]))
    assert len(result.facts) == 1


def test_confidence_percentage_now_normalized() -> None:
    """置信度 95 → 归一化为 0.95 并标记待复核，不拒绝。"""
    fact = _contract_exists_fact()
    fact["confidence"] = 95
    result = _extract(_payload([fact]))
    assert len(result.facts) == 1
    assert result.facts[0].confidence == 0.95
    assert result.facts[0].needs_human_confirmation


def test_one_bad_fact_no_longer_kills_nineteen_good_facts() -> None:
    """核心承诺翻转：20条中1条类型不符 → 19条接收 + 未接收项记录。"""
    good = [_contract_exists_fact(paragraph=n) for n in range(2, 20)]
    bad = _probation_periods_text_fact()
    result = _extract(_payload([_contract_exists_fact(), *good, bad]))
    assert len(result.facts) == 20
    assert any(fact.needs_human_confirmation for fact in result.facts)


def test_control_group_compliant_payload_still_passes() -> None:
    """控制组：规范载荷保持通过。"""
    periods = _probation_periods_text_fact()
    periods.update(
        value_type="json",
        value_text=None,
        value_json=json.dumps(
            [{"start": "2026-01-01", "end": "2026-03-01", "months": 2}],
            ensure_ascii=False,
        ),
    )
    result = _extract(_payload([_contract_exists_fact(), periods]))
    assert len(result.facts) == 2
