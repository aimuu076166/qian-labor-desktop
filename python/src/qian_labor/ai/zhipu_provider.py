from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from qian_labor.ai.fact_contract import CANONICAL_FACT_TYPES, CANONICAL_FACT_TYPE_SET
from qian_labor.ai.providers import AIProviderError
from qian_labor.ai.schemas import ExtractionResult, ProviderExtractionResult, UsageRecord
from qian_labor.security.local_redaction import (
    PreparedProviderContent,
    PrivacyBoundary,
    PrivacyBoundaryError,
    valid_external_pepper,
)


class ZhipuChatCompletionsProvider:
    """Zhipu/BigModel Chat Completions adapter for provider-neutral extraction."""

    name = "zhipu"
    is_external = True
    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    _RATE_LIMIT_CODES = {
        1113: "AI_ACCOUNT_ARREARS",
        1302: "AI_RATE_LIMIT",
        1305: "AI_PROVIDER_OVERLOADED",
        1308: "AI_QUOTA_EXCEEDED",
        1309: "AI_PLAN_EXPIRED",
    }

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
    ) -> None:
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

    def check_connection(self) -> None:
        """Validate this key/model with one small request and no automatic retry burst."""
        if not self.api_key or not self.text_model or not self.base_url:
            raise AIProviderError("AI_PROVIDER_NOT_CONFIGURED")
        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.text_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": 'Return only this JSON object: {"ok":true}',
                        }
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 16,
                    "stream": False,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise AIProviderError("AI_TIMEOUT") from None
        except httpx.HTTPStatusError as error:
            raise AIProviderError(self._failure_code(error.response)) from None
        except httpx.TransportError:
            raise AIProviderError("AI_PROVIDER_ERROR") from None

    def extract(self, filename: str, content: bytes) -> ExtractionResult:
        extension = Path(filename).suffix.lower()
        is_image = extension in self._IMAGE_EXTENSIONS
        model = self.vision_model if is_image else self.text_model
        if not self.api_key or not model or not self.base_url:
            raise AIProviderError("AI_PROVIDER_NOT_CONFIGURED")
        if self.batch_budget_usd <= 0:
            raise AIProviderError("AI_BUDGET_EXCEEDED")

        if isinstance(content, PreparedProviderContent):
            prepared_filename = filename
            prepared_content = bytes(content)
        else:
            try:
                prepared = self.privacy_boundary.prepare(
                    filename,
                    content,
                    is_image=is_image,
                    external=True,
                )
            except PrivacyBoundaryError:
                raise AIProviderError("AI_LOCAL_REDACTION_FAILED") from None
            prepared_filename = prepared.filename
            prepared_content = prepared.content

        payload = self._request_payload(
            prepared_filename,
            prepared_content,
            model,
            is_image,
        )
        started = time.monotonic()
        response: httpx.Response | None = None
        attempts = 0
        failure_code: str | None = None

        for attempts in range(1, self.max_attempts + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "transient provider response",
                        request=response.request,
                        response=response,
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
                        failure_code = self._failure_code(error.response)
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
    def _failure_code(cls, response: httpx.Response) -> str:
        if response.status_code != 429:
            return "AI_PROVIDER_ERROR"
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            raw_code = error.get("code") if isinstance(error, dict) else None
            business_code = int(raw_code)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "AI_RATE_LIMIT"
        return cls._RATE_LIMIT_CODES.get(business_code, "AI_RATE_LIMIT")

    @staticmethod
    def _request_payload(
        filename: str,
        content: bytes,
        model: str,
        is_image: bool,
    ) -> dict[str, object]:
        schema = ProviderExtractionResult.model_json_schema()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        fact_types = ", ".join(CANONICAL_FACT_TYPES)
        system_prompt = (
            "You extract structured employment facts only; never make legal conclusions. "
            "Preserve uncertainty and source locations. Return one JSON object and no markdown. "
            "Every facts[].fact_type MUST be exactly one of these canonical values and no others: "
            f"{fact_types}. "
            "Do not invent a fact when the material does not support it. "
            "The JSON must satisfy this schema exactly: "
            f"{schema_text}"
        )
        user_content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    f"Filename: {filename}\n"
                    "Extract only facts supported by the material. "
                    "Use null/low confidence instead of guessing."
                ),
            }
        ]
        if is_image:
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            encoded = base64.b64encode(content).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )
        else:
            excerpt = content.decode("utf-8", errors="replace")[:100_000]
            user_content.append({"type": "text", "text": excerpt})

        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }

    @classmethod
    def _validated_result(
        cls,
        response: httpx.Response,
        started: float,
        attempts: int,
    ) -> ExtractionResult | None:
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise TypeError
            first = choices[0]
            if not isinstance(first, dict):
                raise TypeError
            message = first.get("message")
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError
            provider_result = ProviderExtractionResult.model_validate_json(content)
            if any(
                fact.fact_type not in CANONICAL_FACT_TYPE_SET
                for fact in provider_result.facts
            ):
                raise ValueError("AI_FACT_TYPE_UNSUPPORTED")
            result = provider_result.to_extraction_result()
            usage = payload.get("usage", {})
            if not isinstance(usage, dict):
                raise TypeError
            result.usage = UsageRecord(
                input_tokens=cls._usage_token(usage, "prompt_tokens", result.usage.input_tokens),
                output_tokens=cls._usage_token(
                    usage,
                    "completion_tokens",
                    result.usage.output_tokens,
                ),
                estimated_cost_usd=result.usage.estimated_cost_usd,
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )
            return result
        except (
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
