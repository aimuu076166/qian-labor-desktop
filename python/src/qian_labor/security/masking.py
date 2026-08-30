import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

MAX_IDENTIFIER_SPAN = 128


@dataclass(frozen=True)
class IdentifierMatch:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class HashedIdentifier:
    field_name: str
    value_hash: str
    start: int
    end: int


def _normalized_identifier(value: str) -> str:
    normalized: list[str] = []
    for char in value:
        if _is_identifier_separator(char):
            continue
        digit = _decimal_digit(char)
        normalized.append(digit if digit is not None else char.upper())
    return "".join(normalized)


def _has_valid_identity_shape(value: str) -> bool:
    if len(value) != 18 or not value[:17].isdigit() or value[-1] not in "0123456789X":
        return False
    try:
        date.fromisoformat(f"{value[6:10]}-{value[10:12]}-{value[12:14]}")
    except ValueError:
        return False
    return value[:6] != "000000"


def _valid_identity(value: str) -> bool:
    if not _has_valid_identity_shape(value):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    expected = checks[
        sum(int(digit) * weight for digit, weight in zip(value[:17], weights, strict=True)) % 11
    ]
    return value[-1] == expected


def _valid_candidate(kind: str, value: str) -> bool:
    normalized = _normalized_identifier(value)
    if kind == "id_number":
        return _valid_identity(normalized)
    if kind == "phone":
        return (
            len(normalized) == 11
            and normalized.isdigit()
            and normalized[0] == "1"
            and normalized[1] in "3456789"
        )
    if not 16 <= len(normalized) <= 19 or not normalized.isdigit():
        return False
    return not (len(normalized) == 18 and _has_valid_identity_shape(normalized))


def _decimal_digit(char: str) -> str | None:
    try:
        return str(unicodedata.decimal(char))
    except (TypeError, ValueError):
        return None


def _is_identifier_separator(char: str) -> bool:
    return char in "-－—–().（）·/／:：" or char.isspace()


def _privacy_kind(normalized: str, *, overflow: bool, signal_count: int) -> str | None:
    if overflow or signal_count > 19:
        return "opaque_identifier" if signal_count >= 11 else None
    if (
        len(normalized) == 18
        and normalized[:17].isdigit()
        and normalized[-1] in "0123456789X"
        and (normalized[-1] == "X" or _has_valid_identity_shape(normalized))
    ):
        return "id_number"
    if (
        len(normalized) == 11
        and normalized[:2].isdigit()
        and normalized[0] == "1"
        and normalized[1] in "3456789"
    ):
        return "phone"
    if 16 <= len(normalized) <= 19 and normalized.isdigit() and normalized[0] != "0":
        return "bank_card"
    return None


def _scan_privacy_identifiers(text: str) -> list[IdentifierMatch]:
    matches: list[IdentifierMatch] = []
    index = 0
    while index < len(text):
        start_digit = _decimal_digit(text[index])
        if start_digit is None or start_digit == "0":
            index += 1
            continue
        start = index
        cursor = index
        last_signal_end = index
        normalized_chars: list[str] = []
        signal_ends: list[int] = []
        signal_count = 0
        overflow = False
        while cursor < len(text):
            char = text[cursor]
            digit = _decimal_digit(char)
            if digit is not None or char in "Xx":
                signal_count += 1
                if len(normalized_chars) < 20:
                    normalized_chars.append(digit if digit is not None else char.upper())
                    signal_ends.append(cursor + 1)
                else:
                    overflow = True
                last_signal_end = cursor + 1
            elif not _is_identifier_separator(char):
                break
            if cursor - start + 1 > MAX_IDENTIFIER_SPAN:
                overflow = True
            cursor += 1
        normalized = "".join(normalized_chars)
        kind = _privacy_kind(normalized, overflow=overflow, signal_count=signal_count)
        if kind is not None:
            matches.append(
                IdentifierMatch(kind, text[start:last_signal_end], start, last_signal_end)
            )
            index = max(cursor, index + 1)
        else:
            prefix_match = None
            for prefix_length in (11, 18, 16, 17, 19):
                if len(normalized) < prefix_length:
                    continue
                prefix_kind = _privacy_kind(
                    normalized[:prefix_length],
                    overflow=False,
                    signal_count=prefix_length,
                )
                if prefix_kind is not None:
                    prefix_end = signal_ends[prefix_length - 1]
                    prefix_match = IdentifierMatch(
                        prefix_kind,
                        text[start:prefix_end],
                        start,
                        prefix_end,
                    )
                    break
            if prefix_match is not None:
                matches.append(prefix_match)
                index = prefix_match.end
            else:
                index = start + 1
    return matches


def find_sensitive_identifiers(text: str) -> list[IdentifierMatch]:
    return [
        item
        for item in _scan_privacy_identifiers(text)
        if item.kind != "opaque_identifier" and _valid_candidate(item.kind, item.value)
    ]


def find_privacy_identifiers(text: str) -> list[IdentifierMatch]:
    return _scan_privacy_identifiers(text)


def identifier_hash(kind: str, value: str, pepper: str) -> str:
    if not pepper:
        raise ValueError("PII_HASH_PEPPER_NOT_CONFIGURED")
    normalized = _normalized_identifier(value)
    return hmac.new(
        pepper.encode("utf-8"),
        f"{kind}:{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()


def extract_identifier_hashes(text: str, pepper: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for item in extract_identifier_evidence(text, pepper):
        hashes.setdefault(item.field_name, item.value_hash)
    return hashes


def extract_identifier_evidence(text: str, pepper: str) -> tuple[HashedIdentifier, ...]:
    field_names = {
        "id_number": "id_number_hash",
        "phone": "phone_hash",
        "bank_card": "bank_card_hash",
    }
    return tuple(
        HashedIdentifier(
            field_name=field_names[item.kind],
            value_hash=identifier_hash(item.kind, item.value, pepper),
            start=item.start,
            end=item.end,
        )
        for item in find_sensitive_identifiers(text)
    )


def mask_identity(value: str) -> str:
    return value[:2] + "*" * (max(0, len(value) - 4)) + value[-2:] if len(value) > 4 else "***"


def _masked_identifier(item: IdentifierMatch) -> str:
    normalized = _normalized_identifier(item.value)
    if item.kind == "opaque_identifier":
        return "[REDACTED_IDENTIFIER]"
    if item.kind == "phone":
        return f"{normalized[:3]}****{normalized[-4:]}"
    if item.kind == "bank_card":
        return f"{normalized[:4]}********{normalized[-4:]}"
    return mask_identity(normalized)


def mask_sensitive(text: str) -> str:
    text = re.sub(
        r"(?i)data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[a-z0-9+/=]{32,}",
        "[REDACTED_IMAGE_BASE64]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:raw_document|request_body|document_body|image_base64)\s*[:=]\s*\S+",
        "[REDACTED_REQUEST_CONTENT]",
        text,
    )
    text = re.sub(
        r"(?im)^.*(?:工资表|薪资表|payroll).*,.*$",
        "[REDACTED_PAYROLL_ROW]",
        text,
    )
    for item in reversed(find_privacy_identifiers(text)):
        text = text[: item.start] + _masked_identifier(item) + text[item.end :]
    text = re.sub(r"(?i)((?:sk-|fake-key-)[A-Za-z0-9_-]{12,})", "[REDACTED_API_KEY]", text)
    return text
