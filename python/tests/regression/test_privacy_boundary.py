import io

import pytest
from PIL import Image

from qian_labor.security.local_redaction import (
    LocalImageRedactor,
    OCRToken,
    PrivacyBoundary,
    PrivacyBoundaryError,
)
from qian_labor.security.masking import (
    extract_identifier_hashes,
    find_sensitive_identifiers,
    identifier_hash,
    mask_identity,
    mask_sensitive,
)
from qian_labor.settings import Settings


class StaticOCR:
    def __init__(self, tokens: list[OCRToken]):
        self.tokens = tokens

    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        return self.tokens


class BrokenOCR:
    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        raise OSError("synthetic local OCR failure")


def _white_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(output, format="PNG")
    return output.getvalue()


def _valid_identity(serial: str = "123") -> str:
    stem = "640104" + "19900101" + serial
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    return stem + checks[
        sum(int(value) * weight for value, weight in zip(stem, weights, strict=True)) % 11
    ]


def test_masks_sensitive_values():
    value = mask_sensitive("fake-key-abcdefghijklmnop")
    assert "fake-key-abcdefghijklmnop" not in value
    assert mask_identity("FAKE-ID-001") != "FAKE-ID-001"


def test_extracts_stable_type_isolated_hmac_hashes_without_retaining_identifiers():
    identity = _valid_identity()
    phone = "139" + "1234" + "5678"
    bank_card = "6222" + "12345678" + "9012"
    source = f"证件 {identity} 手机 {phone} 银行卡 {bank_card}"

    hashes = extract_identifier_hashes(source, "test-pepper")

    assert hashes == extract_identifier_hashes(source, "test-pepper")
    assert hashes["id_number_hash"] == identifier_hash("id_number", identity, "test-pepper")
    assert hashes["phone_hash"] == identifier_hash("phone", phone, "test-pepper")
    assert hashes["bank_card_hash"] == identifier_hash("bank_card", bank_card, "test-pepper")
    assert len(set(hashes.values())) == 3
    assert all(value not in hashes.values() for value in (identity, phone, bank_card))


def test_masks_filename_and_preserves_small_prefix_and_suffix():
    phone = "139" + "1234" + "5678"
    masked = mask_sensitive(f"离职材料-{phone}.pdf")

    assert phone not in masked
    assert masked.startswith("离职材料-139")
    assert masked.endswith("5678.pdf")


def test_dedicated_hash_pepper_overrides_server_app_secret():
    dedicated = Settings(app_secret="app-secret", pii_hash_pepper="dedicated-pepper")
    fallback = Settings(app_secret="app-secret", pii_hash_pepper="")

    assert dedicated.effective_pii_hash_pepper == "dedicated-pepper"
    assert fallback.effective_pii_hash_pepper == "app-secret"


def test_masks_and_hashes_common_separated_identifier_formats_without_reconstruction():
    identity = _valid_identity()
    separated_identity = f"{identity[:6]}-{identity[6:14]} {identity[14:]}"
    phone = "139" + " " + "1234" + " " + "5678"
    bank_card = "6222" + "-" + "1234" + "-" + "5678" + "-" + "9012"
    source = f"证件 {separated_identity} 手机 {phone} 银行卡 {bank_card}"

    hashes = extract_identifier_hashes(source, "test-pepper")
    masked = mask_sensitive(source)
    compact_masked = masked.replace(" ", "").replace("-", "")

    assert hashes["id_number_hash"] == identifier_hash("id_number", identity, "test-pepper")
    assert hashes["phone_hash"] == identifier_hash("phone", phone.replace(" ", ""), "test-pepper")
    assert hashes["bank_card_hash"] == identifier_hash(
        "bank_card", bank_card.replace("-", ""), "test-pepper"
    )
    assert identity not in compact_masked
    assert phone.replace(" ", "") not in compact_masked
    assert bank_card.replace("-", "") not in compact_masked


def test_rejects_invalid_identity_checksum_instead_of_hashing_as_bank_card():
    identity = _valid_identity()
    invalid = identity[:-1] + ("0" if identity[-1] != "0" else "1")

    assert extract_identifier_hashes(f"证件 {invalid}", "test-pepper") == {}


def test_bounded_scanner_normalizes_common_whitespace_and_preserves_original_spans():
    phone = "139" + "\t  \u3000\n" + "1234" + " - \t" + "5678"
    bank_card = "6222" + "\u3000  -\n" + "1234" + "\t" + "5678" + "  " + "9012"
    source = f"手机[{phone}] 银行卡[{bank_card}]"

    matches = find_sensitive_identifiers(source)
    hashes = extract_identifier_hashes(source, "test-pepper")
    masked = mask_sensitive(source)

    assert [(item.kind, source[item.start : item.end]) for item in matches] == [
        ("phone", phone),
        ("bank_card", bank_card),
    ]
    assert hashes["phone_hash"] == identifier_hash("phone", phone, "test-pepper")
    assert hashes["bank_card_hash"] == identifier_hash("bank_card", bank_card, "test-pepper")
    assert phone not in masked
    assert bank_card not in masked


def test_scanner_fails_privacy_conservative_for_overlong_numeric_candidate():
    overlong = "1" + (" \t\u3000-\n" * 40) + ("2" * 40)

    masked = mask_sensitive(f"材料 {overlong} 结束")

    assert overlong not in masked
    assert extract_identifier_hashes(overlong, "test-pepper") == {}


def test_redacts_sensitive_ocr_boxes_before_image_can_leave_process():
    phone = "139" + "1234" + "5678"
    redactor = LocalImageRedactor(
        StaticOCR([OCRToken(phone, left=20, top=10, width=100, height=24, line_key="1")])
    )

    redacted = redactor.redact(_white_png())

    assert redacted != _white_png()
    with Image.open(io.BytesIO(redacted)) as image:
        assert image.convert("RGB").getpixel((40, 20)) == (0, 0, 0)


def test_redaction_fails_closed_when_local_ocr_is_unavailable():
    with pytest.raises(PrivacyBoundaryError, match="AI_LOCAL_REDACTION_FAILED"):
        LocalImageRedactor(BrokenOCR()).redact(_white_png())


def test_redaction_fails_closed_when_local_ocr_returns_no_tokens():
    with pytest.raises(PrivacyBoundaryError, match="AI_LOCAL_REDACTION_FAILED"):
        LocalImageRedactor(StaticOCR([])).redact(_white_png())


def test_redacts_every_ocr_token_when_sensitive_value_spans_same_line():
    tokens = [
        OCRToken("139", left=10, top=10, width=25, height=20, line_key="1"),
        OCRToken("1234", left=40, top=10, width=35, height=20, line_key="1"),
        OCRToken("5678", left=80, top=10, width=35, height=20, line_key="1"),
    ]

    redacted = LocalImageRedactor(StaticOCR(tokens)).redact(_white_png())

    with Image.open(io.BytesIO(redacted)) as image:
        pixels = image.convert("RGB")
        assert pixels.getpixel((15, 15)) == (0, 0, 0)
        assert pixels.getpixel((45, 15)) == (0, 0, 0)
        assert pixels.getpixel((85, 15)) == (0, 0, 0)


def test_prepared_text_retains_hashed_evidence_with_original_span_locator():
    phone = "139" + "\t \u3000\n" + "1234" + " - " + "5678"
    source = f"员工手机[{phone}]".encode()

    prepared = PrivacyBoundary("test-pepper").prepare(
        "虚构材料.txt", source, is_image=False, external=True
    )

    assert len(prepared.identifier_evidence) == 1
    evidence = prepared.identifier_evidence[0]
    decoded = source.decode()
    assert evidence.field_name == "phone_hash"
    assert evidence.value_hash == identifier_hash("phone", phone, "test-pepper")
    assert decoded[evidence.start : evidence.end] == phone
    assert evidence.locator == {"type": "text", "start": evidence.start, "end": evidence.end}


def test_ocr_cross_token_evidence_keeps_safe_line_and_box_locator():
    phone = "139" + "1234" + "5678"
    tokens = [
        OCRToken("139", left=10, top=10, width=25, height=20, line_key="safe-line-1"),
        OCRToken("\u3000-", left=36, top=10, width=5, height=20, line_key="safe-line-1"),
        OCRToken("1234", left=42, top=10, width=35, height=20, line_key="safe-line-1"),
        OCRToken("5678", left=80, top=10, width=35, height=20, line_key="safe-line-1"),
    ]
    redactor = LocalImageRedactor(StaticOCR(tokens), pepper="test-pepper")

    redacted = redactor.redact_with_metadata(_white_png())

    assert len(redacted.identifier_evidence) == 1
    evidence = redacted.identifier_evidence[0]
    assert evidence.value_hash == identifier_hash("phone", phone, "test-pepper")
    assert evidence.locator["type"] == "ocr"
    assert evidence.locator["line_key"] == "safe-line-1"
    assert evidence.locator["token_indexes"] == [0, 1, 2, 3]
