import json

import httpx
import pytest

from qian_labor.ai.fact_contract import CANONICAL_FACT_TYPES, FACT_VALUE_TYPES
from qian_labor.ai.providers import AIProviderError
from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from qian_labor.rules.registry import RULE_REGISTRY
from qian_labor.security.local_redaction import PrivacyBoundary


PEPPER = "desktop-fact-contract-pepper-32-characters-minimum"


def test_canonical_fact_contract_matches_exact_r01_r20_required_fact_union() -> None:
    expected = {
        fact_type
        for rule in RULE_REGISTRY.values()
        for fact_type in rule.metadata.required_facts
    }
    assert set(CANONICAL_FACT_TYPES) == expected
    assert set(FACT_VALUE_TYPES) == expected


def test_zhipu_prompt_lists_canonical_fact_types() -> None:
    payload = ZhipuChatCompletionsProvider._request_payload(
        "虚构合同.txt",
        b"fictional contract",
        "glm-5.3-flash",
        False,
    )
    request_text = json.dumps(payload, ensure_ascii=False)
    for fact_type in CANONICAL_FACT_TYPES:
        assert fact_type in request_text


def test_zhipu_rejects_unknown_fact_type_even_when_json_schema_shape_is_valid() -> None:
    response_payload = {
        "schema_version": "employment-extraction-v1",
        "document_type": "contract",
        "employee_name": "完全虚构员工",
        "employee_number": "F-990",
        "department": None,
        "job_title": None,
        "needs_human_confirmation": False,
        "facts": [
            {
                "employee_id": "F-990",
                "fact_type": "contract_end_date",
                "value_type": "text",
                "value_text": "2026-12-31",
                "value_integer": None,
                "value_number": None,
                "value_boolean": None,
                "value_string_list": None,
                "value_json": None,
                "confidence": 0.9,
                "source": {
                    "file_name": "虚构合同.txt",
                    "page": None,
                    "row": 1,
                    "column": None,
                    "sheet": None,
                    "paragraph": None,
                    "excerpt": "虚构来源",
                    "bbox": None,
                },
                "needs_human_confirmation": False,
            }
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(response_payload, ensure_ascii=False),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    provider = ZhipuChatCompletionsProvider(
        api_key="synthetic-key-never-real",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        text_model="glm-5.3-flash",
        vision_model="glm-5.3-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        privacy_boundary=PrivacyBoundary(PEPPER),
    )

    with pytest.raises(AIProviderError, match="AI_SCHEMA_INVALID"):
        provider.extract("虚构合同.txt", b"fictional contract")
