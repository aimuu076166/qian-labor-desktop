"""方案第四步：有上限的格式修复请求与操作闭环。

- 本地可解的格式差异不发 API 请求（由逐条归一化覆盖）。
- 本地不可解时最多一次带诊断反馈的修复请求；仍失败明确报错。
- 修复请求使用同一通道与模型，可从调用次数验证。
"""
import json

import httpx

from qian_labor.ai.providers import AIProviderError
from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider

from test_zhipu_provider import (  # noqa: F401
    PEPPER,
    MODEL,
    _provider,
    _provider_payload,
    _success_response,
)


def _omitted_source_payload() -> dict[str, object]:
    """触发顶层 metadata 错误；单条事实错误现已隔离，不应重跑整份。"""
    payload = _provider_payload()
    payload["document_type"] = "invalid-synthetic-type"
    return payload


def test_repair_request_fires_at_most_once_and_succeeds() -> None:
    """首次 schema 失败 → 一次修复请求（带 hint）→ 成功；总请求次数 = 2。"""
    calls: list[dict[str, object]] = []
    good = _provider_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return _success_response(_omitted_source_payload())
        return _success_response(good)

    result = _provider(handler, max_attempts=1).extract("虚构合同.txt", b"synthetic")
    assert len(calls) == 2
    assert result.facts[0].value is True
    # 修复请求复用同一模型且追加了反馈消息。
    assert calls[1]["model"] == calls[0]["model"] == MODEL
    assert len(calls[1]["messages"]) == len(calls[0]["messages"]) + 1
    assert "未通过校验" in calls[1]["messages"][-1]["content"]


def test_repair_request_fires_only_once_then_fails_explicitly() -> None:
    """修复请求也失败 → 明确 AI_SCHEMA_INVALID，不再第三次调用。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _success_response(_omitted_source_payload())

    with __import__("pytest").raises(AIProviderError, match="AI_SCHEMA_INVALID"):
        _provider(handler, max_attempts=1).extract("虚构合同.txt", b"synthetic")
    assert len(calls) == 2


def test_local_fixable_shapes_never_trigger_extra_request() -> None:
    """本地可修的格式差异（省略键、顶层扩展）→ 单次请求即成功。"""
    calls: list[int] = []
    payload = _provider_payload()
    payload["facts"][0]["source"] = {"file_name": "01.txt", "excerpt": "x"}
    payload["summary"] = "模型附加"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _success_response(payload)

    result = _provider(handler, max_attempts=1).extract("虚构合同.txt", b"synthetic")
    assert len(calls) == 1
    assert len(result.facts) == 1


def test_semantic_empty_failure_gets_no_repair_request() -> None:
    """语义类失败（NO_SUPPORTED_FACTS 场景的前置：全部事实未接收）
    不属于格式修复范围 → 不发修复请求，直接明确失败。"""
    calls: list[int] = []
    payload = _provider_payload()
    payload["facts"][0]["fact_type"] = "synthetic-private-unknown"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _success_response(payload)

    result = _provider(handler, max_attempts=1).extract("虚构合同.txt", b"synthetic")
    assert len(calls) == 1
    assert result.facts == []
    assert result.unreceived == [{"reason": "unsupported_fact_type", "index": "0"}]
