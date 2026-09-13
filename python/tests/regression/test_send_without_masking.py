"""方案第二步：停止材料发送前脱敏。

发送至外部 Provider 的正文与文件名不再遮盖姓名、号码；
本地标识哈希（员工匹配依赖）保持计算；密钥不进入业务日志。
"""
import json

import httpx

from qian_labor.security.local_redaction import PrivacyBoundary

from test_zhipu_provider import (  # noqa: F401
    PEPPER,
    MODEL,
    _provider,
    _provider_payload,
    _success_response,
)


def test_text_body_is_sent_without_masking() -> None:
    """身份证/手机号原文应完整出现在请求正文中（不再遮盖）。"""
    from test_zhipu_provider import _valid_identity

    identity = _valid_identity()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response()

    source = f"完全虚构劳动合同 身份证 {identity} 手机 13912345678".encode()
    _provider(handler).extract("虚构员工合同.txt", source)

    request_text = json.dumps(captured["payload"], ensure_ascii=False)
    assert identity in request_text
    assert "13912345678" in request_text


def test_filename_is_sent_without_masking() -> None:
    """文件名中的员工姓名与手机号不再遮盖。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _success_response()

    _provider(handler).extract("张明远-13912345678-劳动合同.txt", b"synthetic")

    request_text = json.dumps(captured["payload"], ensure_ascii=False)
    assert "张明远" in request_text


def test_local_identifier_hashes_still_computed_for_matching() -> None:
    """放弃发送脱敏后，本地标识哈希仍需计算（员工匹配依赖）。"""
    from test_zhipu_provider import _valid_identity

    boundary = PrivacyBoundary(PEPPER)
    prepared = boundary.prepare(
        "任意文件.txt",
        f"身份证 {_valid_identity()}".encode(),
        is_image=False,
        external=True,
    )
    assert prepared.identifier_hashes, "本地哈希不应随脱敏一起取消"


def test_parser_citation_is_not_an_employee_identifier_but_is_sent_unchanged():
    from qian_labor.security.local_redaction import ParserTextContent
    citation = 'cite-ffcb3f1f1671069261515dbd4baac6c9'
    text = f'[citation_id {citation}]\n合成劳动合同条款'
    left = text.index(citation)
    content = ParserTextContent(text.encode(), ((left, left + len(citation)),))
    prepared = PrivacyBoundary(PEPPER).prepare('synthetic.txt', content, is_image=False, external=True)
    assert prepared.content == content
    assert prepared.identifier_hashes == {}


def test_api_key_never_enters_error_messages() -> None:
    """密钥不进入业务日志或错误消息（方案红线）。"""
    from qian_labor.ai.providers import AIProviderError

    real_key = "real-secret-key-should-never-leak"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "synthetic"})

    provider = _provider(handler)
    provider.api_key = real_key
    try:
        provider.extract("任意.txt", b"synthetic")
        raised = None  # type: ignore[assignment]
    except AIProviderError as error:
        raised = error
    assert raised is not None
    assert real_key not in str(raised)
    if raised.diagnostic is not None:
        assert real_key not in repr(raised.diagnostic.as_dict())
