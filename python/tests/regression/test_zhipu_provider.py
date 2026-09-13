import base64
import io
import json

import httpx
import pytest
from PIL import Image

from qian_labor.ai.provider_factory import provider_from_settings
from qian_labor.ai.providers import AIProviderError, OpenAIResponsesProvider
from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from qian_labor.security.local_redaction import LocalImageRedactor, OCRToken, PrivacyBoundary
from qian_labor.settings import Settings


PEPPER = "desktop-zhipu-test-pepper-32-characters-minimum"
MODEL = "glm-5.3-flash"


class StaticOCR:
    def __init__(self, tokens: list[OCRToken]):
        self.tokens = tokens

    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        return self.tokens


def _valid_identity(serial: str = "123") -> str:
    stem = "640104" + "19900101" + serial
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    return stem + checks[
        sum(int(value) * weight for value, weight in zip(stem, weights, strict=True)) % 11
    ]


def _provider_payload(*, file_name: str = "虚构劳动合同.txt") -> dict[str, object]:
    return {
        "schema_version": "employment-extraction-v1",
        "document_type": "contract",
        "employee_name": "完全虚构员工",
        "employee_number": "F-903",
        "department": "虚构部门",
        "job_title": "虚构岗位",
        "needs_human_confirmation": False,
        "facts": [
            {
                "employee_id": "F-903",
                "fact_type": "employment.contract.exists",
                "value_type": "boolean",
                "value_text": None,
                "value_integer": None,
                "value_number": None,
                "value_boolean": True,
                "value_string_list": None,
                "value_json": None,
                "confidence": 0.99,
                "source": {
                    "file_name": file_name,
                    "page": 1,
                    "row": None,
                    "column": None,
                    "sheet": None,
                    "paragraph": None,
                    "excerpt": "已脱敏的虚构来源",
                    "bbox": None,
                },
                "needs_human_confirmation": False,
            }
        ],
    }


def _success_response(payload: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-synthetic",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload or _provider_payload(), ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
            },
        },
    )


def _provider(
    handler,
    *,
    privacy_boundary: PrivacyBoundary | None = None,
    max_attempts: int = 2,
):
    return ZhipuChatCompletionsProvider(
        api_key="synthetic-zhipu-key-never-real",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        text_model=MODEL,
        vision_model=MODEL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=max_attempts,
        retry_delay_seconds=0,
        privacy_boundary=privacy_boundary or PrivacyBoundary(PEPPER),
    )


def test_zhipu_text_request_uses_chat_completions_json_mode_and_local_redaction() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return _success_response()

    identity = _valid_identity()
    phone = "13912345678"
    bank_card = "6222123456789012"
    source = (
        f"完全虚构劳动合同 身份证 {identity} 手机 {phone} 银行卡 {bank_card} "
        "合同期限 2026-01-01 至 2026-12-31"
    ).encode()

    result = _provider(handler).extract("虚构员工-13912345678-合同.txt", source)

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert captured["authorization"] == "Bearer synthetic-zhipu-key-never-real"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == MODEL
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    request_text = json.dumps(payload, ensure_ascii=False)
    # 方案决策（2026-09）：正文不再脱敏，标识符原文直达智谱官方通道。
    assert identity in request_text
    assert phone in request_text
    assert bank_card in request_text
    assert "employment-extraction-v1" in request_text
    assert "facts" in request_text
    assert result.document_type == "contract"
    assert result.facts[0].fact_type == "employment.contract.exists"
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45


def test_zhipu_image_request_sends_original_bytes_without_local_redaction() -> None:
    phone = "13912345678"
    original = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(original, format="PNG")
    original_bytes = original.getvalue()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response(_provider_payload(file_name="虚构扫描件.png"))

    result = _provider(handler).extract("虚构扫描件.png", original_bytes)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    content = messages[-1]["content"]
    assert isinstance(content, list)
    image_blocks = [item for item in content if item.get("type") == "image_url"]
    assert len(image_blocks) == 1
    data_url = image_blocks[0]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    sent_bytes = base64.b64decode(data_url.split(",", 1)[1])
    # 不再本地打码：原图字节直达通道。
    assert sent_bytes == original_bytes
    assert result.document_type == "contract"


def test_zhipu_image_request_preserves_parser_context_and_local_ocr_ids() -> None:
    request = ZhipuChatCompletionsProvider._request_payload(
        "嵌图合同-image-1.png",
        b"synthetic-redacted-image",
        MODEL,
        True,
        source_context={
            "parser_context": [{
                "locator": {"paragraph": 2, "image": 1},
                "citation_id": "cite-synthetic-image-context",
            }],
            "ocr_blocks": [{
                "locator": {"paragraph": 2, "image": 1, "block": 1},
                "citation_id": "cite-synthetic-ocr-context",
                "text": "SYN-001 signed contract",
            }],
        },
    )

    user_text = request["messages"][1]["content"][0]["text"]

    assert "\"paragraph\":2" in user_text
    assert "\"image\":1" in user_text
    assert "cite-synthetic-image-context" in user_text
    assert "cite-synthetic-ocr-context" in user_text
    assert "SYN-001 signed contract" in user_text


def test_zhipu_provider_rejects_invalid_json_contract() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "{\"wrong\":true}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    with pytest.raises(AIProviderError, match="AI_SCHEMA_INVALID"):
        _provider(handler).extract("虚构合同.txt", b"fictional contract")


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ('{"synthetic-secret-marker": 1 "next": 2}', "missing_comma"),
        ('{"synthetic-secret-marker" 1}', "missing_colon"),
        ('{"synthetic-secret-marker": "unfinished}', "unterminated_string"),
        ('{"synthetic-secret-marker": "bad\\x"}', "invalid_escape"),
        ('{"synthetic-secret-marker": "bad\nline"}', "control_character"),
        ('{"synthetic-secret-marker": 1,}', "property_name"),
        ('{"synthetic-secret-marker": }', "expected_value"),
        ('{} synthetic-secret-marker', "extra_data"),
        ('```json\n{"synthetic-secret-marker": 1}\n```', "markdown_fence"),
    ],
)
def test_invalid_json_diagnostic_has_only_safe_kind_and_position(content: str, kind: str) -> None:
    payload = _success_response().json()
    payload["choices"][0]["message"]["content"] = content
    with pytest.raises(json.JSONDecodeError) as expected:
        json.loads(content)
    with pytest.raises(AIProviderError, match="AI_SCHEMA_INVALID") as caught:
        _provider(lambda _request: httpx.Response(200, json=payload)).extract(
            "synthetic.txt", b"fictional contract"
        )
    diagnostic = caught.value.diagnostic.as_dict()
    assert diagnostic.get("json_error_kind") == kind
    assert diagnostic.get("json_error_position") == expected.value.pos
    assert "synthetic-secret-marker" not in repr(diagnostic)
    assert diagnostic["validation_type"] == "invalid_json"


def test_zhipu_provider_rejects_parseable_json_when_generation_is_truncated() -> None:
    payload = _success_response().json()
    assert isinstance(payload, dict)
    choices = payload["choices"]
    assert isinstance(choices, list)
    choices[0]["finish_reason"] = "length"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(AIProviderError, match="AI_SCHEMA_INVALID"):
        _provider(handler).extract("虚构合同.txt", b"fictional contract")


def test_zhipu_diagnostic_marks_truncated_json_as_incomplete_without_response_text() -> None:
    payload = _success_response().json()
    assert isinstance(payload, dict)
    payload["choices"][0]["finish_reason"] = "length"
    payload["choices"][0]["message"]["content"] = '{"synthetic-person-text":"synthetic-secret-marker"}'

    with pytest.raises(AIProviderError) as caught:
        _provider(lambda _request: httpx.Response(200, json=payload)).extract(
            "虚构合同.txt", b"synthetic-person-text"
        )

    error = caught.value
    assert error.diagnostic.category == "incomplete"
    assert error.diagnostic.finish_reason == "length"
    assert "synthetic-secret-marker" not in repr(error)
    assert "synthetic-person-text" not in repr(error.diagnostic.as_dict())


def test_zhipu_provider_rejects_unknown_finish_reason_without_echoing_it() -> None:
    payload = _success_response().json()
    assert isinstance(payload, dict)
    payload["choices"][0]["finish_reason"] = "synthetic-unknown-finish"

    with pytest.raises(AIProviderError) as caught:
        _provider(lambda _request: httpx.Response(200, json=payload), max_attempts=1).extract(
            "虚构合同.txt", b"synthetic-person-text"
        )

    diagnostic = caught.value.diagnostic
    assert diagnostic.category == "incomplete"
    assert diagnostic.validation_type == "unsupported"
    assert diagnostic.path == "choices[0]"
    assert "synthetic-unknown-finish" not in repr(diagnostic.as_dict())


@pytest.mark.parametrize(
    ("content", "category", "validation_type", "path"),
    [
        ("", "response", "empty", "choices[0].message.content"),
        ("[]", "response", "not_object", "choices[0].message.content"),
    ],
)
def test_zhipu_diagnostic_classifies_safe_response_failures(
    content: str, category: str, validation_type: str, path: str
) -> None:
    payload = _success_response().json()
    assert isinstance(payload, dict)
    if content == "":
        payload["choices"][0]["message"]["content"] = ""
    elif content == "[]":
        payload["choices"][0]["message"]["content"] = content
    else:
        invalid = _provider_payload()
        invalid["facts"][0]["fact_type"] = "synthetic-secret-marker"
        payload["choices"][0]["message"]["content"] = json.dumps(invalid)

    with pytest.raises(AIProviderError) as caught:
        _provider(lambda _request: httpx.Response(200, json=payload), max_attempts=1).extract(
            "虚构合同.txt", b"synthetic-person-text"
        )

    diagnostic = caught.value.diagnostic
    assert diagnostic.category == category
    assert diagnostic.validation_type == validation_type
    assert diagnostic.path == path
    assert "synthetic-secret-marker" not in repr(diagnostic.as_dict())


def test_zhipu_diagnostic_conflicting_value_fields_become_reviewable() -> None:
    """新契约：矛盾载体 → 保留主值+待复核；诊断永不携带值/原文。"""
    payload = _provider_payload()
    payload["facts"][0]["value_text"] = "synthetic-person-text"
    result = _provider(lambda _request: _success_response(payload), max_attempts=1).extract(
        "虚构合同.txt", b"synthetic-person-text"
    )
    assert result.facts[0].value is True
    assert result.facts[0].needs_human_confirmation
    assert "synthetic-person-text" not in repr(result)


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "http"), (429, "http"), (500, "http")],
)
def test_zhipu_diagnostic_marks_http_status_without_error_body(status: int, category: str) -> None:
    with pytest.raises(AIProviderError) as caught:
        _provider(
            lambda _request: httpx.Response(
                status, json={"error": {"message": "synthetic-secret-marker"}}
            ),
            max_attempts=1,
        ).extract("虚构合同.txt", b"synthetic-person-text")
    diagnostic = caught.value.diagnostic
    assert diagnostic.category == category
    assert diagnostic.status_code == status
    assert "synthetic-secret-marker" not in repr(diagnostic.as_dict())


def test_zhipu_diagnostic_marks_timeout_and_does_not_retry_read_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("synthetic-person-text", request=request)

    with pytest.raises(AIProviderError) as caught:
        _provider(handler, max_attempts=3).extract("虚构合同.txt", b"synthetic-person-text")
    assert attempts == 1
    diagnostic = caught.value.diagnostic
    assert diagnostic.category == "timeout"
    assert diagnostic.attempt == 1
    assert "synthetic-person-text" not in repr(diagnostic.as_dict())


def test_zhipu_request_sets_a_bounded_output_budget_for_contract_and_spreadsheet() -> None:
    request = ZhipuChatCompletionsProvider._request_payload(
        "synthetic.xlsx-part-1.txt", b"SYN-001 | 2026-01-01", MODEL, False
    )
    # A live multi-employee response using the default effort hit the old
    # 8192-token cap. Keep thinking enabled, with bounded headroom and low effort.
    assert request["max_tokens"] == 32768
    assert request["reasoning_effort"] == "low"
    assert request["thinking"] == {"type": "enabled"}


def test_zhipu_provider_normalizes_the_observed_glm_value_json_key_truncation() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_"] = fact.pop("value_json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts[0].value is True


def test_zhipu_provider_accepts_parser_cell_and_block_locations() -> None:
    payload = _provider_payload()
    source = payload["facts"][0]["source"]
    assert isinstance(source, dict)
    source.update(cell="B2", block=1)

    result = _provider(lambda _request: _success_response(payload)).extract(
        "虚构合同.txt", b"fictional contract"
    )

    assert result.facts[0].source.cell == "B2"
    assert result.facts[0].source.block == 1


def test_zhipu_provider_recovers_observed_generic_value_with_blank_value_type() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_type"] = ""
    fact["value_"] = fact["value_boolean"]
    fact["value_boolean"] = None

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts[0].value is True


def test_zhipu_provider_rejects_a_fact_whose_value_type_cannot_feed_its_rule() -> None:
    """类型无法喂给规则 → 不进入结果；唯一事实被拒时明确失败而非空成功。"""
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact.update(
        {
            "fact_type": "employment.material_coverage",
            "value_type": "text",
            "value_text": "材料包含合同信息，但没有可计算的覆盖率",
            "value_boolean": None,
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")
    assert result.facts == []
    assert result.unreceived == [{"reason": "unconvertible_value_type", "fact_type": "employment.material_coverage", "index": "0"}]


def test_unconvertible_value_facts_do_not_leak_content_or_block_good_facts() -> None:
    """两条类型不符事实 → 记入未接收项；好事实照常接收；内容零泄露。"""
    from copy import deepcopy
    payload = _provider_payload()
    bad = deepcopy(payload["facts"][0])
    bad.update(fact_type="employment.probation.assessment_exists", value_type="text",
               value_text="synthetic-private-value", value_boolean=None)
    bad["source"]["excerpt"] = "synthetic-private-excerpt"
    payload["facts"].extend([bad, deepcopy(bad)])
    result = _provider(lambda _: _success_response(payload)).extract(
        "synthetic.txt", b"synthetic"
    )
    assert len(result.facts) == 1  # 第一条好事实保留
    assert result.unreceived == [
        {"reason": "unconvertible_value_type",
         "fact_type": "employment.probation.assessment_exists", "index": "1"},
        {"reason": "unconvertible_value_type",
         "fact_type": "employment.probation.assessment_exists", "index": "2"},
    ]
    assert "synthetic-private" not in repr(result.unreceived)


def test_unknown_fact_type_is_not_copied_anywhere() -> None:
    """自造事实类型 → 未接收项只记录安全原因和索引，不复制任意类型名。"""
    payload = _provider_payload()
    payload["facts"][0]["fact_type"] = "synthetic-private-unknown-field"
    try:
        result = _provider(lambda _: _success_response(payload)).extract(
            "synthetic.txt", b"synthetic"
        )
    except AIProviderError as error:
        assert "synthetic-private" not in repr(error)
        assert "synthetic-private" not in repr(error.diagnostic.as_dict() if error.diagnostic else {})
    else:
        assert result.facts == []
        assert result.unreceived == [{
            "reason": "unsupported_fact_type",
            "index": "0",
        }]


def test_zhipu_provider_accepts_consistent_redundant_text_for_a_typed_value() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_text"] = "true"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts[0].value is True


def test_zhipu_provider_keeps_declared_value_when_redundant_text_conflicts() -> None:
    """新契约：冗余文本与声明载体矛盾 → 保留主值并标记待复核，不作废整份。"""
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_text"] = "false"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")
    assert result.facts[0].value is True
    assert result.facts[0].needs_human_confirmation


def test_zhipu_provider_accepts_generic_alias_when_typed_value_present() -> None:
    """新契约：声明载体在而泛型别名冲突 → 保留声明值，不作废。"""
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_type"] = ""
    fact["value_"] = "与结构化布尔值冲突的说明"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")
    assert result.facts[0].value is True
    assert result.facts[0].needs_human_confirmation


def test_zhipu_provider_keeps_both_facts_when_redundant_text_conflicts() -> None:
    """新契约：两条事实冗余矛盾 → 双双保留，矛盾条待复核，不静默丢部分。"""
    payload = _provider_payload()
    valid = payload["facts"][0]
    payload["facts"].append({**valid, "value_text": "false"})
    result = _provider(lambda _: _success_response(payload)).extract("虚构合同.txt", b"synthetic")
    assert len(result.facts) == 2
    assert result.facts[0].needs_human_confirmation is False
    assert result.facts[1].needs_human_confirmation


def test_zhipu_provider_preserves_explicit_unknown_fact_as_reviewable_missing_evidence() -> None:
    payload = _provider_payload()
    payload["facts"][0].update(value_type="null", value_boolean=None)
    result = _provider(lambda _: _success_response(payload)).extract("虚构合同.txt", b"synthetic")
    assert len(result.facts) == 1
    assert result.facts[0].value is None
    assert result.facts[0].needs_human_confirmation


def test_zhipu_provider_empty_valid_response_is_not_a_successful_extraction() -> None:
    payload = _provider_payload()
    payload["facts"] = []
    with pytest.raises(AIProviderError, match="AI_NO_SUPPORTED_FACTS"):
        _provider(lambda _: _success_response(payload)).extract("虚构合同.txt", b"synthetic")


@pytest.mark.parametrize("updates", [
    {"fact_type": "employment.pay.actual_wage", "value_type": "number", "value_boolean": None, "value_number": True},
    {"value_boolean": 1},
    {"value_boolean": None, "value_": 1},
    {"fact_type": "employment.pay.actual_wage", "value_type": "number", "value_boolean": None,
     "value_number": 9007199254740993},
    {"fact_type": "employment.pay.actual_wage", "value_type": "number", "value_boolean": None,
     "value_number": 9007199254740992.0, "value_text": "9007199254740993"},
])
def test_zhipu_provider_never_silently_accepts_type_coercion_or_lossy_aliases(updates) -> None:
    """跨类型强转与有损数值别名永不静默通过：要么明确失败，要么强制人工复核。"""
    payload = _provider_payload()
    payload["facts"][0].update(updates)
    try:
        result = _provider(lambda _: _success_response(payload)).extract(
            "虚构合同.txt", b"synthetic"
        )
    except AIProviderError as error:
        assert str(error) in {"AI_SCHEMA_INVALID", "AI_NO_SUPPORTED_FACTS"}
    else:
        for fact in result.facts:
            assert fact.needs_human_confirmation, "可疑值不得无标记进入规则判断"


def test_zhipu_prompt_requires_one_populated_typed_value_and_preserves_explicit_facts() -> None:
    request = ZhipuChatCompletionsProvider._request_payload("synthetic.txt", b"synthetic", MODEL, False)
    prompt = request["messages"][0]["content"]
    assert "All unused value_* fields must be null" in prompt
    assert "Do not omit an explicitly supported canonical fact" in prompt


def test_zhipu_provider_retries_rate_limit_without_leaking_response_body() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {"message": "synthetic secret body"}})
        return _success_response()

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert attempts == 2
    assert result.usage.attempts == 2


def test_zhipu_provider_does_not_repeat_a_full_extraction_after_read_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with pytest.raises(AIProviderError, match="^AI_TIMEOUT$"):
        _provider(handler, max_attempts=3).extract("虚构合同.txt", b"fictional contract")

    assert attempts == 1


def test_zhipu_connection_check_uses_one_small_request_without_extraction_schema() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": '{"ok":true}'}}
                ]
            },
        )

    _provider(handler).check_connection()

    assert len(captured) == 1
    assert captured[0]["model"] == MODEL
    assert captured[0]["max_tokens"] <= 32
    assert "employment-extraction-v1" not in json.dumps(captured[0], ensure_ascii=False)


@pytest.mark.parametrize(
    ("business_code", "stable_code"),
    [
        (1113, "AI_ACCOUNT_ARREARS"),
        (1302, "AI_RATE_LIMIT"),
        (1305, "AI_PROVIDER_OVERLOADED"),
        (1308, "AI_QUOTA_EXCEEDED"),
        (1309, "AI_PLAN_EXPIRED"),
    ],
)
def test_zhipu_429_business_codes_are_not_all_reported_as_rate_limit(
    business_code: int,
    stable_code: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": business_code, "message": "never expose this body"}},
        )

    with pytest.raises(AIProviderError, match=f"^{stable_code}$"):
        _provider(handler, max_attempts=1).check_connection()


def test_provider_factory_defaults_zhipu_to_glm_5_3_flash_and_official_base() -> None:
    settings = Settings(
        app_secret="separate-app-secret",
        pii_hash_pepper=PEPPER,
        ai_provider="zhipu",
        ai_api_key="synthetic-zhipu-key-never-real",
        ai_text_model="",
        ai_vision_model="",
    )

    provider = provider_from_settings(settings)

    assert isinstance(provider, ZhipuChatCompletionsProvider)
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert provider.text_model == "glm-5.3-flash"
    assert provider.vision_model == "glm-5.3-flash"
    assert provider.timeout == 180
    assert provider.max_attempts == 1


def test_provider_factory_keeps_openai_official_default_when_base_url_is_blank() -> None:
    settings = Settings(
        app_secret="separate-app-secret",
        pii_hash_pepper=PEPPER,
        ai_provider="openai-responses",
        ai_api_key="synthetic-openai-key-never-real",
        ai_base_url="",
        ai_text_model="synthetic-text-model",
        ai_vision_model="synthetic-vision-model",
    )

    provider = provider_from_settings(settings)

    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.base_url == "https://api.openai.com/v1"
