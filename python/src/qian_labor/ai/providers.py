import base64
import hashlib
import json
import mimetypes
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import ValidationError

from qian_labor.ai.schemas import (
    EmploymentFact,
    ExtractionResult,
    ProviderExtractionResult,
    SourceLocator,
    UsageRecord,
)
from qian_labor.security.local_redaction import (
    PrivacyBoundary,
    PrivacyBoundaryError,
    valid_external_pepper,
)


class AIProviderError(RuntimeError):
    """Safe provider error that never includes source text or credentials."""


class AIProvider(Protocol):
    name: str

    def extract(self, filename: str, content: bytes) -> ExtractionResult: ...


class FakeAIProvider:
    """Deterministic offline provider for tests and the fictional demo."""

    name = "fake"
    is_external = False

    def extract(self, filename: str, content: bytes) -> ExtractionResult:
        synthetic = self._synthetic_payload(content)
        if synthetic:
            employee_number = str(synthetic["employee_number"])
            synthetic_facts = synthetic.get("facts", {})
            if not isinstance(synthetic_facts, dict):
                raise AIProviderError("FAKE_PROVIDER_INVALID_SYNTHETIC_PAYLOAD")
            facts = [
                EmploymentFact(
                    employee_id=employee_number,
                    fact_type=fact_type,
                    value=value,
                    confidence=1,
                    source=SourceLocator(
                        file_name=filename,
                        row=2,
                        excerpt="虚构演示字段",
                    ),
                )
                for fact_type, value in synthetic_facts.items()
            ]
            payload_document_type = cast(
                Literal[
                    "roster",
                    "contract",
                    "attendance",
                    "payroll",
                    "social_insurance",
                    "policy",
                    "assessment",
                    "termination",
                    "notice",
                    "other",
                ],
                synthetic.get("document_type", "other"),
            )
            return ExtractionResult(
                document_type=payload_document_type,
                employee_name=str(synthetic["employee_name"])
                if synthetic.get("employee_name") is not None
                else None,
                employee_number=employee_number,
                department=str(synthetic["department"])
                if synthetic.get("department") is not None
                else None,
                job_title=str(synthetic["job_title"])
                if synthetic.get("job_title") is not None
                else None,
                facts=facts,
                usage=UsageRecord(
                    input_tokens=max(1, len(content) // 4),
                    output_tokens=max(40, len(facts) * 8),
                ),
            )
        lower_name = filename.lower()
        document_type = "other"
        markers = {
            "roster": ("名单", "花名册", "roster"),
            "contract": ("合同", "contract"),
            "attendance": ("考勤", "attendance"),
            "payroll": ("工资", "薪资", "payroll"),
            "social_insurance": ("社保", "insurance"),
            "policy": ("制度", "policy"),
            "assessment": ("考核", "绩效", "assessment"),
            "termination": ("解除", "离职", "termination"),
            "notice": ("通知", "送达", "notice"),
        }
        for kind, keywords in markers.items():
            if any(keyword in lower_name for keyword in keywords):
                document_type = kind
                break

        file_digest = hashlib.sha256(content).hexdigest()
        employee_match = re.search(r"F-\d{3}", filename, flags=re.IGNORECASE)
        synthetic_employee = (
            employee_match.group(0).upper() if employee_match else f"F-{file_digest[:6].upper()}"
        )
        typed_document_type = cast(
            Literal[
                "roster",
                "contract",
                "attendance",
                "payroll",
                "social_insurance",
                "policy",
                "assessment",
                "termination",
                "notice",
                "other",
            ],
            document_type,
        )
        return ExtractionResult(
            document_type=typed_document_type,
            employee_number=synthetic_employee,
            facts=[
                EmploymentFact(
                    employee_id=synthetic_employee,
                    fact_type="document_seen",
                    value=True,
                    confidence=0.99,
                    source=SourceLocator(file_name=filename, page=1, excerpt="虚构演示材料"),
                ),
                EmploymentFact(
                    employee_id=synthetic_employee,
                    fact_type="document_type",
                    value=document_type,
                    confidence=0.98,
                    source=SourceLocator(file_name=filename, page=1, excerpt="按文件名分类"),
                ),
            ],
            usage=UsageRecord(
                input_tokens=max(1, len(content) // 4),
                output_tokens=40,
                estimated_cost_usd=0,
            ),
        )

    @staticmethod
    def _synthetic_payload(content: bytes) -> dict[str, Any] | None:
        candidates: list[str] = []
        text = content.decode("utf-8", errors="ignore")
        candidates.extend(line.strip() for line in text.splitlines())
        if content.startswith(b"\x89PNG"):
            try:
                with Image.open(BytesIO(content)) as image:
                    marker = image.info.get("qian_synthetic_json")
                    if isinstance(marker, str):
                        candidates.append(marker)
            except OSError:
                pass
        for candidate in candidates:
            if candidate.startswith("QIAN_SYNTHETIC_JSON="):
                candidate = candidate.split("=", 1)[1]
            if not candidate.startswith("{") or "employee_number" not in candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if payload.get("synthetic_marker") == "QIAN_DEMO_20260824":
                return payload
        return None


class OpenAIResponsesProvider:
    name = "openai-responses"
    is_external = True
    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    _SUPPORTED_SCHEMA_KEYWORDS = frozenset(
        {
            "$defs",
            "$ref",
            "additionalProperties",
            "anyOf",
            "const",
            "description",
            "enum",
            "items",
            "maximum",
            "maxItems",
            "minimum",
            "minItems",
            "properties",
            "required",
            "title",
            "type",
        }
    )

    def __init__(
        self,
        api_key: str,
        base_url: str,
        text_model: str,
        vision_model: str,
        timeout: float = 30,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
        batch_budget_usd: float = 5.0,
        privacy_boundary: PrivacyBoundary | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.text_model = text_model
        self.vision_model = vision_model
        self.timeout = timeout
        self.client = client or httpx.Client(timeout=timeout)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = retry_delay_seconds
        self.batch_budget_usd = batch_budget_usd
        if privacy_boundary is None or not valid_external_pepper(privacy_boundary.pepper):
            raise AIProviderError("AI_PRIVACY_CONFIG_INVALID")
        self.privacy_boundary = privacy_boundary

    def extract(self, filename: str, content: bytes) -> ExtractionResult:
        extension = Path(filename).suffix.lower()
        is_image = extension in self._IMAGE_EXTENSIONS
        model = self.vision_model if is_image else self.text_model
        if not self.api_key or not model:
            raise AIProviderError("AI_PROVIDER_NOT_CONFIGURED")
        if self.batch_budget_usd <= 0:
            raise AIProviderError("AI_BUDGET_EXCEEDED")

        idempotency_key = str(uuid4())
        redaction_failed = False
        try:
            prepared = self.privacy_boundary.prepare(
                filename,
                content,
                is_image=is_image,
                external=True,
            )
        except PrivacyBoundaryError:
            redaction_failed = True
            prepared = None
        if redaction_failed or prepared is None:
            raise AIProviderError("AI_LOCAL_REDACTION_FAILED") from None
        payload = self._request_payload(prepared.filename, prepared.content, model, is_image)
        started = time.monotonic()
        response: httpx.Response | None = None
        attempts = 0
        failure_code: str | None = None

        for attempts in range(1, self.max_attempts + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": idempotency_key,
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "transient provider response", request=response.request, response=response
                    )
                response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = not isinstance(error, httpx.HTTPStatusError) or (
                    error.response.status_code == 429 or error.response.status_code >= 500
                )
                if attempts >= self.max_attempts or not retryable:
                    if isinstance(error, httpx.TimeoutException):
                        failure_code = "AI_TIMEOUT"
                    elif (
                        isinstance(error, httpx.HTTPStatusError)
                        and error.response.status_code == 429
                    ):
                        failure_code = "AI_RATE_LIMIT"
                    else:
                        failure_code = "AI_PROVIDER_ERROR"
                    break
                if self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds * (2 ** (attempts - 1)))

        if failure_code is not None:
            raise AIProviderError(failure_code) from None
        if response is None:
            raise AIProviderError("AI_PROVIDER_ERROR") from None

        result = self._validated_result(response, started, attempts)
        if result is None:
            raise AIProviderError("AI_SCHEMA_INVALID") from None
        if result.usage.estimated_cost_usd > self.batch_budget_usd:
            raise AIProviderError("AI_BUDGET_EXCEEDED")
        return result

    @classmethod
    def _validated_result(
        cls, response: httpx.Response, started: float, attempts: int
    ) -> ExtractionResult | None:
        try:
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise TypeError
            output_text = response_payload.get("output_text") or cls._find_output_text(
                response_payload
            )
            if not isinstance(output_text, str):
                raise TypeError
            provider_result = ProviderExtractionResult.model_validate_json(output_text)
            result = provider_result.to_extraction_result()
            usage = response_payload.get("usage", {})
            if not isinstance(usage, dict):
                raise TypeError
            result.usage = UsageRecord(
                input_tokens=cls._usage_token(usage, "input_tokens", result.usage.input_tokens),
                output_tokens=cls._usage_token(usage, "output_tokens", result.usage.output_tokens),
                estimated_cost_usd=result.usage.estimated_cost_usd,
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )
            return result
        except (
            AIProviderError,
            AttributeError,
            json.JSONDecodeError,
            OverflowError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            return None

    @staticmethod
    def _usage_token(usage: dict[str, object], field: str, default: int) -> int:
        value = usage.get(field, default)
        if type(value) is not int or not 0 <= value <= 1_000_000_000:
            raise ValueError
        return value

    @staticmethod
    def _find_output_text(payload: dict[str, object]) -> str:
        output = payload.get("output", [])
        if not isinstance(output, list):
            raise AIProviderError("AI_SCHEMA_INVALID")
        for item in output:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content", [])
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        return text
        raise AIProviderError("AI_SCHEMA_INVALID")

    @staticmethod
    def _request_payload(
        filename: str, content: bytes, model: str, is_image: bool
    ) -> dict[str, object]:
        prompt = (
            "Extract structured employment facts from the provided employment document. "
            "Do not make legal conclusions. Preserve uncertainty. "
            "Return null when a field cannot be reliably determined. "
            "Preserve source locations. "
            f"Filename: {filename}"
        )
        input_content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        if is_image:
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            encoded = base64.b64encode(content).decode("ascii")
            input_content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                    "detail": "high",
                }
            )
        else:
            excerpt = content.decode("utf-8", errors="replace")[:100_000]
            input_content.append({"type": "input_text", "text": excerpt})

        schema = ProviderExtractionResult.model_json_schema()
        OpenAIResponsesProvider._make_strict_schema(schema)
        OpenAIResponsesProvider._validate_strict_schema(schema)
        return {
            "model": model,
            "store": False,
            "input": [{"role": "user", "content": input_content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "employment_extraction_v1",
                    "schema": schema,
                    "strict": True,
                }
            },
        }

    @staticmethod
    def _make_strict_schema(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                OpenAIResponsesProvider._make_strict_schema(item)
            return
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        for value in tuple(node.values()):
            OpenAIResponsesProvider._make_strict_schema(value)
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                node["properties"] = properties
            node["additionalProperties"] = False
            node["required"] = list(properties)

    @classmethod
    def _validate_strict_schema(cls, node: object) -> None:
        if not isinstance(node, dict) or not node or set(node) - cls._SUPPORTED_SCHEMA_KEYWORDS:
            raise AIProviderError("AI_SCHEMA_INVALID") from None

        is_object = node.get("type") == "object" or "properties" in node
        if is_object:
            properties = node.get("properties")
            required = node.get("required")
            if (
                not isinstance(properties, dict)
                or node.get("additionalProperties") is not False
                or not isinstance(required, list)
                or set(required) != set(properties)
            ):
                raise AIProviderError("AI_SCHEMA_INVALID") from None

        if node.get("type") == "array" and not isinstance(node.get("items"), dict):
            raise AIProviderError("AI_SCHEMA_INVALID") from None

        for container_key in ("$defs", "properties"):
            container = node.get(container_key)
            if container is None:
                continue
            if not isinstance(container, dict):
                raise AIProviderError("AI_SCHEMA_INVALID") from None
            for child in container.values():
                cls._validate_strict_schema(child)

        any_of = node.get("anyOf")
        if any_of is not None:
            if not isinstance(any_of, list) or not any_of:
                raise AIProviderError("AI_SCHEMA_INVALID") from None
            for child in any_of:
                cls._validate_strict_schema(child)

        items = node.get("items")
        if items is not None:
            cls._validate_strict_schema(items)
