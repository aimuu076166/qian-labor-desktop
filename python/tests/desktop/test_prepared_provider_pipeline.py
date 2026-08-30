from __future__ import annotations

import base64
import io
import json

import httpx
from PIL import Image

from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from qian_labor.security.local_redaction import (
    LocalImageRedactor,
    OCRToken,
    PreparedProviderContent,
    PrivacyBoundary,
)

PEPPER = "prepared-provider-test-pepper-32-characters-minimum"
MODEL = "glm-5.3-flash"


class StaticOCR:
    def __init__(self, tokens: list[OCRToken]):
        self.tokens = tokens

    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        return self.tokens


class RejectSecondPreparation:
    pepper = PEPPER

    def prepare(self, *args, **kwargs):
        raise AssertionError("provider attempted a second privacy-boundary pass")


def _success_response() -> httpx.Response:
    provider_payload = {
        "schema_version": "employment-extraction-v1",
        "document_type": "contract",
        "employee_name": "完全虚构员工",
        "employee_number": "F-912",
        "department": None,
        "job_title": None,
        "needs_human_confirmation": False,
        "facts": [
            {
                "employee_id": "F-912",
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
                    "file_name": "已脱敏材料",
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
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(provider_payload, ensure_ascii=False),
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


def _provider(handler) -> ZhipuChatCompletionsProvider:
    return ZhipuChatCompletionsProvider(
        api_key="synthetic-zhipu-key-never-real",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        text_model=MODEL,
        vision_model=MODEL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_delay_seconds=0,
        privacy_boundary=RejectSecondPreparation(),
    )


def test_text_prepared_by_pipeline_is_not_redacted_again_by_zhipu_provider() -> None:
    phone = "13912345678"
    prepared = PrivacyBoundary(PEPPER).prepare(
        "完全虚构合同.txt",
        f"完全虚构合同 手机 {phone}".encode(),
        is_image=False,
        external=True,
    )
    assert isinstance(prepared.content, PreparedProviderContent)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response()

    result = _provider(handler).extract(prepared.filename, prepared.content)

    request_text = json.dumps(captured["payload"], ensure_ascii=False)
    assert phone not in request_text
    assert result.document_type == "contract"


def test_image_prepared_by_pipeline_is_not_ocr_redacted_again_by_zhipu_provider() -> None:
    phone = "13912345678"
    original = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(original, format="PNG")
    boundary = PrivacyBoundary(
        PEPPER,
        image_redactor=LocalImageRedactor(
            StaticOCR([OCRToken(phone, left=20, top=10, width=100, height=24, line_key="1")]),
            pepper=PEPPER,
        ),
    )
    prepared = boundary.prepare(
        "完全虚构扫描件.png",
        original.getvalue(),
        is_image=True,
        external=True,
    )
    assert isinstance(prepared.content, PreparedProviderContent)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response()

    result = _provider(handler).extract(prepared.filename, prepared.content)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    user_content = payload["messages"][-1]["content"]
    image_block = next(item for item in user_content if item["type"] == "image_url")
    sent_bytes = base64.b64decode(image_block["image_url"]["url"].split(",", 1)[1])
    assert sent_bytes == bytes(prepared.content)
    assert sent_bytes != original.getvalue()
    assert result.document_type == "contract"
