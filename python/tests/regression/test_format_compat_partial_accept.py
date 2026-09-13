"""方案第三步：智谱响应格式兼容与逐条接收。

核心承诺：单条异常不再作废整份合同。
格式整理规则对应方案表格逐项：补空值、忽略扩展字段、冗余值豁免、
百分数置信度归一化、自造事实类型列为未接收项、JSON 损坏明确失败。
"""
import json

import httpx
import pytest

from qian_labor.ai.providers import AIProviderError
from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider

from test_zhipu_provider import (  # noqa: F401
    PEPPER,
    MODEL,
    _provider,
    _provider_payload,
    _success_response,
)


def _fact(**overrides: object) -> dict[str, object]:
    fact = {
        "employee_id": "F-903",
        "fact_type": "employment.contract.exists",
        "value_type": "boolean",
        "value_text": None,
        "value_integer": None,
        "value_number": None,
        "value_boolean": True,
        "value_string_list": None,
        "value_json": None,
        "confidence": 0.9,
        "source": {
            "file_name": "01-劳动合同书.txt",
            "excerpt": "甲方与乙方签订固定期限劳动合同",
        },
        "needs_human_confirmation": False,
    }
    fact.update(overrides)
    return fact


def _payload(facts: list[dict[str, object]], **top: object) -> dict[str, object]:
    payload = _provider_payload()
    payload["facts"] = facts
    payload.update(top)
    return payload


def _extract(payload: dict[str, object]):
    return _provider(lambda _: _success_response(payload)).extract(
        "虚构合同.txt", b"synthetic"
    )


# ---- 情况1：source 可选键被省略 → 补空值，不拒绝 ----

def test_omitted_source_keys_are_treated_as_null() -> None:
    result = _extract(_payload([_fact()]))
    assert len(result.facts) == 1
    assert result.facts[0].source.page is None
    assert result.facts[0].source.sheet is None
    assert result.facts[0].source.bbox is None


# ---- 情况2：顶层与 source 内扩展字段 → 忽略 ----

def test_top_level_extension_fields_are_ignored() -> None:
    result = _extract(_payload([_fact()], summary="三年期合同", notes="双方法律平等"))
    assert len(result.facts) == 1


def test_source_level_extension_fields_are_ignored() -> None:
    fact = _fact()
    fact["source"]["line"] = 3
    result = _extract(_payload([fact]))
    assert len(result.facts) == 1


# ---- 情况3：冗余载体值：一致接受，矛盾待复核 ----

def test_consistent_redundant_text_is_accepted() -> None:
    result = _extract(_payload([_fact(value_text="true")]))
    assert result.facts[0].value is True
    assert not result.facts[0].needs_human_confirmation


def test_conflicting_redundant_text_becomes_reviewable_not_fatal() -> None:
    payload = _payload([
        _fact(value_text="false"),
        _fact(fact_type="employment.contract.type", value_type="text",
              value_boolean=None, value_text="固定期限"),
    ])
    result = _extract(payload)
    by_type = {fact.fact_type: fact for fact in result.facts}
    assert by_type["employment.contract.exists"].value is True
    assert by_type["employment.contract.exists"].needs_human_confirmation
    assert by_type["employment.contract.type"].value == "固定期限"


# ---- 情况4：置信度百分数 → 明确范围规则归一化 ----

def test_percentage_confidence_is_normalized_with_flag() -> None:
    result = _extract(_payload([_fact(confidence=95)]))
    assert 0 <= result.facts[0].confidence <= 1
    assert result.facts[0].needs_human_confirmation


def test_confidence_above_hundred_is_reviewable_not_fatal() -> None:
    result = _extract(_payload([_fact(confidence=150)]))
    assert 0 <= result.facts[0].confidence <= 1
    assert result.facts[0].needs_human_confirmation


# ---- 情况5：试用期区间文本 → 可靠转换才转，否则待复核 ----

def test_probation_periods_iso_text_is_converted_to_json() -> None:
    fact = _fact(
        fact_type="employment.probation.periods",
        value_type="text",
        value_text="2026-01-01 至 2026-03-01",
        value_boolean=None,
    )
    result = _extract(_payload([fact]))
    assert result.facts[0].value == [["2026-01-01", "2026-03-01"]]


def test_probation_periods_chinese_text_is_reviewable() -> None:
    """中文口语区间无法可靠转换 → 该项待复核，不炸整份。"""
    fact = _fact(
        fact_type="employment.probation.periods",
        value_type="text",
        value_text="试用期两个月，自入职之日起算",
        value_boolean=None,
    )
    result = _extract(_payload([fact]))
    assert result.facts[0].needs_human_confirmation


# ---- 情况6：模型自造事实类型 → 未接收项，不参与规则 ----

def test_invented_fact_type_is_listed_as_unreceived() -> None:
    payload = _payload([
        _fact(),
        _fact(fact_type="employment.contract.term_years", value_type="text",
              value_boolean=None, value_text="3年"),
    ])
    result = _extract(payload)
    assert len(result.facts) == 1
    assert result.facts[0].fact_type == "employment.contract.exists"
    assert result.unreceived == [{
        "reason": "unsupported_fact_type",
        "index": "1",
    }]


# ---- 情况7：JSON 损坏 / 截断 → 明确失败 ----

def test_truncated_json_still_fails_explicitly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": '{"schema_version": "employment-extraction-v1", "doc'},
                "finish_reason": "length",
            }],
            "usage": {},
        })

    with pytest.raises(AIProviderError):
        _provider(handler).extract("虚构合同.txt", b"synthetic")


# ---- 核心承诺：19 好 + 1 坏 → 19 接收 + 1 待复核 ----

def test_nineteen_good_facts_survive_one_bad_fact() -> None:
    good_facts = [
        _fact(fact_type="employment.contract.start_date", value_type="text",
              value_boolean=None, value_text="2026-01-01", source={
                  "file_name": "01.txt", "excerpt": "自2026年1月1日起", "paragraph": n})
        for n in range(1, 20)
    ]
    # 类型声明错但内容可靠可转：text:"false" → boolean False，标记待复核。
    bad = _fact(value_type="text", value_boolean=None, value_text="false",
                value_integer=None)
    payload = _payload([*good_facts, bad])
    result = _extract(payload)
    assert len(result.facts) == 20
    assert result.facts[-1].needs_human_confirmation
    assert result.facts[-1].value is False
    assert result.unreceived == []
