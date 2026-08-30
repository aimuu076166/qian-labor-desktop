from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageDraw

from qian_labor.security.masking import (
    HashedIdentifier,
    extract_identifier_evidence,
    find_privacy_identifiers,
    mask_sensitive,
)

_PUBLIC_PEPPERS = {
    "development-only-secret",
    "local-synthetic-only",
    "replace-for-local-only",
}


def valid_external_pepper(pepper: str) -> bool:
    return len(pepper) >= 32 and pepper not in _PUBLIC_PEPPERS


class PrivacyBoundaryError(RuntimeError):
    """A safe local privacy error with no source data in its message."""


class PreparedProviderContent(bytes):
    """Internal marker for bytes that already crossed the local privacy boundary."""


@dataclass(frozen=True)
class OCRToken:
    text: str
    left: int
    top: int
    width: int
    height: int
    line_key: str


@dataclass(frozen=True)
class RedactedImage:
    content: bytes
    identifier_hashes: dict[str, str]
    identifier_evidence: tuple[IdentifierEvidence, ...] = ()


@dataclass(frozen=True)
class IdentifierEvidence:
    field_name: str
    value_hash: str
    start: int
    end: int
    locator: dict[str, object]


@dataclass(frozen=True)
class PreparedProviderInput:
    filename: str
    content: bytes
    identifier_hashes: dict[str, str]
    identifier_evidence: tuple[IdentifierEvidence, ...] = ()


class LocalOCR(Protocol):
    def extract_tokens(self, content: bytes) -> list[OCRToken]: ...


class TesseractOCR:
    def __init__(self, *, timeout_seconds: float = 30) -> None:
        self.timeout_seconds = timeout_seconds

    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        try:
            completed = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "tsv"],
                input=content,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED") from None
        if completed.returncode != 0:
            raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED") from None
        try:
            rows = csv.DictReader(io.StringIO(completed.stdout.decode("utf-8")))
            return [
                OCRToken(
                    text=row["text"],
                    left=int(row["left"]),
                    top=int(row["top"]),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    line_key="-".join(
                        row[key] for key in ("page_num", "block_num", "par_num", "line_num")
                    ),
                )
                for row in rows
                if row.get("text", "").strip()
            ]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED") from None


class LocalImageRedactor:
    def __init__(self, ocr: LocalOCR | None = None, *, pepper: str = "") -> None:
        self.ocr = ocr or TesseractOCR()
        self.pepper = pepper

    def redact(self, content: bytes) -> bytes:
        return self.redact_with_metadata(content).content

    def redact_with_metadata(self, content: bytes) -> RedactedImage:
        try:
            tokens = self.ocr.extract_tokens(content)
            if not tokens:
                raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED")
            sensitive_tokens, evidence = self._sensitive_token_indexes(tokens, self.pepper)
            with Image.open(io.BytesIO(content)) as source:
                output_format = (
                    source.format if source.format in {"PNG", "JPEG", "WEBP"} else "PNG"
                )
                image = source.convert("RGB")
                if sensitive_tokens:
                    draw = ImageDraw.Draw(image)
                    for index in sensitive_tokens:
                        token = tokens[index]
                        padding = 3
                        draw.rectangle(
                            (
                                max(0, token.left - padding),
                                max(0, token.top - padding),
                                min(image.width, token.left + token.width + padding),
                                min(image.height, token.top + token.height + padding),
                            ),
                            fill="black",
                        )
                output = io.BytesIO()
                image.save(output, format=output_format)
        except PrivacyBoundaryError:
            raise
        except (OSError, ValueError):
            raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED") from None
        hashes = self._unique_hashes(evidence)
        return RedactedImage(output.getvalue(), hashes, evidence)

    @staticmethod
    def _sensitive_token_indexes(
        tokens: list[OCRToken], pepper: str
    ) -> tuple[set[int], tuple[IdentifierEvidence, ...]]:
        grouped: dict[str, list[tuple[int, OCRToken]]] = {}
        for index, token in enumerate(tokens):
            grouped.setdefault(token.line_key, []).append((index, token))
        sensitive_indexes: set[int] = set()
        evidence: list[IdentifierEvidence] = []
        for line_key, items in grouped.items():
            combined = ""
            spans: list[tuple[int, int, int]] = []
            for index, token in items:
                start = len(combined)
                combined += token.text
                spans.append((index, start, len(combined)))
            for match in find_privacy_identifiers(combined):
                token_indexes = [
                    index for index, start, end in spans if match.start < end and match.end > start
                ]
                sensitive_indexes.update(token_indexes)
            if pepper:
                for item in extract_identifier_evidence(combined, pepper):
                    token_indexes = [
                        index
                        for index, start, end in spans
                        if item.start < end and item.end > start
                    ]
                    evidence.append(
                        IdentifierEvidence(
                            field_name=item.field_name,
                            value_hash=item.value_hash,
                            start=item.start,
                            end=item.end,
                            locator={
                                "type": "ocr",
                                "line_key": line_key,
                                "token_indexes": token_indexes,
                            },
                        )
                    )
        return sensitive_indexes, tuple(evidence)

    @staticmethod
    def _unique_hashes(evidence: tuple[IdentifierEvidence, ...]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for item in evidence:
            hashes.setdefault(item.field_name, item.value_hash)
        return hashes


class PrivacyBoundary:
    def __init__(self, pepper: str, image_redactor: LocalImageRedactor | None = None) -> None:
        self.pepper = pepper
        self.image_redactor = image_redactor or LocalImageRedactor(pepper=pepper)

    def prepare(
        self,
        filename: str,
        content: bytes,
        *,
        is_image: bool,
        external: bool,
    ) -> PreparedProviderInput:
        if is_image:
            if not external:
                return PreparedProviderInput(filename, content, {})
            redacted = self.image_redactor.redact_with_metadata(content)
            return PreparedProviderInput(
                mask_sensitive(filename),
                PreparedProviderContent(redacted.content),
                redacted.identifier_hashes,
                redacted.identifier_evidence,
            )
        text = content.decode("utf-8", errors="replace")
        evidence = self._text_evidence(text) if self.pepper else ()
        hashes = LocalImageRedactor._unique_hashes(evidence)
        if not external:
            return PreparedProviderInput(filename, content, hashes, evidence)
        return PreparedProviderInput(
            mask_sensitive(filename),
            PreparedProviderContent(mask_sensitive(text).encode("utf-8")),
            hashes,
            evidence,
        )

    def _text_evidence(self, text: str) -> tuple[IdentifierEvidence, ...]:
        return tuple(
            self._with_text_locator(item)
            for item in extract_identifier_evidence(text, self.pepper)
        )

    @staticmethod
    def _with_text_locator(item: HashedIdentifier) -> IdentifierEvidence:
        return IdentifierEvidence(
            field_name=item.field_name,
            value_hash=item.value_hash,
            start=item.start,
            end=item.end,
            locator={"type": "text", "start": item.start, "end": item.end},
        )
