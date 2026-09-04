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
    assert identity not in request_text
    assert phone not in request_text
    assert bank_card not in request_text
    assert "employment-extraction-v1" in request_text
    assert "facts" in request_text
    assert result.document_type == "contract"
    assert result.facts[0].fact_type == "employment.contract.exists"
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45


def test_zhipu_image_request_uses_image_url_with_locally_redacted_bytes() -> None:
    phone = "13912345678"
    original = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(original, format="PNG")
    original_bytes = original.getvalue()
    boundary = PrivacyBoundary(
        PEPPER,
        image_redactor=LocalImageRedactor(
            StaticOCR([OCRToken(phone, left=20, top=10, width=100, height=24, line_key="1")]),
            pepper=PEPPER,
        ),
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response(_provider_payload(file_name="虚构扫描件.png"))

    result = _provider(handler, privacy_boundary=boundary).extract("虚构扫描件.png", original_bytes)

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
    assert sent_bytes != original_bytes
    with Image.open(io.BytesIO(sent_bytes)) as image:
        assert image.convert("RGB").getpixel((40, 20)) == (0, 0, 0)
    assert result.document_type == "contract"


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


def test_zhipu_provider_normalizes_the_observed_glm_value_json_key_truncation() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_"] = fact.pop("value_json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts[0].value is True


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


def test_zhipu_provider_discards_a_fact_whose_value_type_cannot_feed_its_rule() -> None:
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


def test_zhipu_provider_accepts_consistent_redundant_text_for_a_typed_value() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_text"] = "true"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts[0].value is True


def test_zhipu_provider_discards_conflicting_redundant_text_for_a_typed_value() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_text"] = "false"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts == []


def test_zhipu_provider_discards_conflicting_generic_alias_fact_without_losing_batch() -> None:
    payload = _provider_payload()
    fact = payload["facts"][0]
    assert isinstance(fact, dict)
    fact["value_type"] = ""
    fact["value_"] = "与结构化布尔值冲突的说明"

    def handler(_request: httpx.Request) -> httpx.Response:
        return _success_response(payload)

    result = _provider(handler).extract("虚构合同.txt", b"fictional contract")

    assert result.facts == []


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
