from __future__ import annotations

import base64
import io
import json
import re
from copy import deepcopy

import httpx
from PIL import Image

from qian_labor.ai.providers import OpenAIResponsesProvider
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


def _openai_success_response() -> httpx.Response:
    chat_response = _success_response()
    payload = chat_response.json()
    return httpx.Response(
        200,
        json={
            "output_text": payload["choices"][0]["message"]["content"],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


def _openai_provider(handler) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        api_key="synthetic-openai-key-never-real",
        base_url="https://api.openai.com/v1",
        text_model="synthetic-text-model",
        vision_model="synthetic-vision-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_delay_seconds=0,
        max_attempts=1,
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
    assert phone in request_text  # 新契约：内容不遮盖，原文直达通道
    assert result.document_type == "contract"


def test_text_prepared_by_pipeline_is_not_redacted_again_by_openai_provider(monkeypatch) -> None:
    from qian_labor.ai.schemas import ProviderExtractionResult

    # The legacy OpenAI adapter's strict request-schema allowlist is tested
    # separately; keep this regression focused on prepared-content handling.
    monkeypatch.setattr(
        ProviderExtractionResult,
        "model_json_schema",
        classmethod(lambda cls: {"type": "object", "properties": {}}),
    )
    phone = "13912345678"
    prepared = PrivacyBoundary(PEPPER).prepare(
        "完全虚构合同.txt",
        f"完全虚构合同 手机 {phone}".encode(),
        is_image=False,
        external=True,
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _openai_success_response()

    prepared_content = PreparedProviderContent(
        bytes(prepared.content),
        source_context={"parser_context": [{"citation_id": "cite-synthetic-openai"}]},
    )
    result = _openai_provider(handler).extract(prepared.filename, prepared_content)

    request_text = json.dumps(captured["payload"], ensure_ascii=False)
    assert phone in request_text  # 新契约：内容不遮盖，原文直达通道
    assert "cite-synthetic-openai" in request_text
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
    assert sent_bytes == original.getvalue()  # 新契约：图片不打码，原图直达
    assert result.document_type == "contract"


def test_xlsx_citation_survives_wire_schema_and_grounding() -> None:
    from openpyxl import Workbook

    from qian_labor.ai.grounding import ground_result
    from qian_labor.jobs.processing import ProcessingPipeline
    from qian_labor.parsers.registry import ParserRegistry

    book = Workbook()
    sheet = book.active
    sheet.title = "试用期"
    sheet.append(["员工编号", "试用期开始", "试用期结束", "考核记录"])
    for index in range(1, 11):
        sheet.append([f"SYN-{index:03d}", "2026-01-01", "2026-03-01", "有" if index % 2 else "无"])
    buffer = io.BytesIO()
    book.save(buffer)
    content = buffer.getvalue()
    parsed = ParserRegistry().parse("synthetic.xlsx", content)
    inputs = ProcessingPipeline._extraction_inputs("synthetic.xlsx", content, parsed)
    assert len(inputs) == 1
    item = inputs[0]
    prepared = PrivacyBoundary(PEPPER).prepare(item.filename, item.content, is_image=False, external=True)
    calls = []

    def handler(request):
        calls.append(1)
        payload = json.loads(request.content)
        texts = []
        for message in payload["messages"]:
            value = message["content"]
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(part["text"] for part in value if part.get("type") == "text")
        wire = "\n".join(texts)
        row_contexts = []
        for line in wire.splitlines():
            if line.startswith("[row_context ") and line.endswith("]"):
                row_contexts.append(json.loads(line[len("[row_context "):-1]))
        assert row_contexts and all(context.get("excerpt") for context in row_contexts)
        entries = re.findall(r'\[source (\{[^\n]+\})\]\n\[citation_id ([^\]]+)\]\n([^\n]*)', wire)
        by_cell = {(loc["sheet"], loc["row"], str(loc["column"])): (citation, quote)
                   for raw, citation, quote in entries
                   if "column" in (loc := json.loads(raw)) and loc.get("row", 0) > 1}
        response = _success_response().json()
        body = json.loads(response["choices"][0]["message"]["content"])
        template = body["facts"][0]
        body.update(document_type="assessment", employee_name=None, employee_number=None, facts=[])
        for index in range(1, 11):
            for column, fact_type, value in (
                (2, "employment.probation.start_date", "2026-01-01"),
                (3, "employment.probation.end_date", "2026-03-01"),
                (4, "employment.probation.assessment_exists", bool(index % 2)),
            ):
                citation, quote = by_cell[("试用期", index + 1, str(column))]
                fact = deepcopy(template)
                fact.update(employee_id=f"SYN-{index:03d}", fact_type=fact_type,
                            value_type="boolean" if isinstance(value, bool) else "text",
                            value_boolean=value if isinstance(value, bool) else None,
                            value_text=value if isinstance(value, str) else None)
                fact["source"].update(page=None, citation_id=citation, excerpt=quote,
                                      sheet=None, row=None, column=None)
                body["facts"].append(fact)
        response["choices"][0]["message"]["content"] = json.dumps(body, ensure_ascii=False)
        return httpx.Response(200, json=response)

    result = _provider(handler).extract(prepared.filename, prepared.content)
    proofs = ground_result(result, item, "synthetic.xlsx")
    assert len(calls) == 1
    assert len(result.facts) == len(proofs) == 30
    for fact, proof in zip(result.facts, proofs):
        assert proof["status"] == "locally_located", proof
        assert not proof["requires_review"]
        assert fact.source.sheet == "试用期"
        assert fact.source.row == int(fact.employee_id.split("-")[1]) + 1
        expected_column = {"employment.probation.start_date": "2",
                           "employment.probation.end_date": "3",
                           "employment.probation.assessment_exists": "4"}[fact.fact_type]
        assert fact.source.column == expected_column
        assert fact.source.excerpt


def test_citation_spans_and_identifiers_pass_through_without_masking(monkeypatch):
    """新契约：取消发送前遮盖后，正文原样直达（citation 标记与手机号都保留），
    本地标识哈希证据照常计算。"""
    from qian_labor.jobs.processing import ProcessingPipeline
    from qian_labor.parsers.protocols import ParsedBlock, ParsedDocument

    citation = 'cite-4bfe0fe619294154942a561bac8bcab3'
    phone = '13912345678'
    monkeypatch.setattr('qian_labor.jobs.processing.deterministic_citation_id',
                        lambda *args: citation)
    parsed = ParsedDocument(kind='spreadsheet', blocks=[
        ParsedBlock('SYN-001', 'cell', {'sheet': '合成', 'row': 2, 'column': 1}),
        ParsedBlock(f'{phone} [citation_id {citation}]', 'cell',
                    {'sheet': '合成', 'row': 2, 'column': 2}),
    ])
    item = ProcessingPipeline._extraction_inputs('synthetic.xlsx', b'synthetic', parsed)[0]
    prepared = PrivacyBoundary(PEPPER).prepare(item.filename, item.content, is_image=False, external=True)
    wire = prepared.content.decode()
    assert wire == item.content.decode()  # 原样透传，无遮盖改写
    assert phone in wire
    assert all(e.value_hash for e in prepared.identifier_evidence)
