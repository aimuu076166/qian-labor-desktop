from __future__ import annotations

from qian_labor.ai.providers import (
    AIProvider,
    AIProviderError,
    FakeAIProvider,
    OpenAIResponsesProvider,
)
from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from qian_labor.security.local_redaction import PrivacyBoundary, valid_external_pepper
from qian_labor.settings import Settings

OPENAI_BASE_URL = "https://api.openai.com/v1"
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_DEFAULT_TEXT_MODEL = "glm-5.2"
ZHIPU_DEFAULT_VISION_MODEL = "glm-4.6v"


def _external_privacy_boundary(settings: Settings) -> PrivacyBoundary:
    if (
        not valid_external_pepper(settings.pii_hash_pepper)
        or settings.pii_hash_pepper == settings.app_secret
    ):
        raise AIProviderError("AI_PRIVACY_CONFIG_INVALID")
    return PrivacyBoundary(settings.pii_hash_pepper)


def provider_from_settings(settings: Settings) -> AIProvider:
    """Build the selected provider without leaking provider defaults into business code."""

    provider_name = settings.ai_provider.strip().lower()
    if provider_name == "fake":
        return FakeAIProvider()

    privacy_boundary = _external_privacy_boundary(settings)

    if provider_name in {"zhipu", "glm", "bigmodel"}:
        # Settings historically defaulted to OpenAI's endpoint. Treat that untouched
        # legacy default as "not explicitly configured" when Zhipu is selected.
        configured_base = settings.ai_base_url.strip()
        base_url = (
            ZHIPU_BASE_URL
            if configured_base in {"", OPENAI_BASE_URL}
            else configured_base
        )
        text_model = settings.ai_text_model.strip() or ZHIPU_DEFAULT_TEXT_MODEL
        vision_model = settings.ai_vision_model.strip() or ZHIPU_DEFAULT_VISION_MODEL
        return ZhipuChatCompletionsProvider(
            settings.ai_api_key,
            base_url,
            text_model,
            vision_model,
            batch_budget_usd=settings.ai_batch_budget_usd,
            privacy_boundary=privacy_boundary,
        )

    if provider_name in {"openai", "openai-responses"}:
        return OpenAIResponsesProvider(
            settings.ai_api_key,
            settings.ai_base_url.strip() or OPENAI_BASE_URL,
            settings.ai_text_model,
            settings.ai_vision_model,
            batch_budget_usd=settings.ai_batch_budget_usd,
            privacy_boundary=privacy_boundary,
        )

    raise RuntimeError("AI_PROVIDER_UNSUPPORTED")
