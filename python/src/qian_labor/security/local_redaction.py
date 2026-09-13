from __future__ import annotations

import csv
import io
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageDraw
from qian_labor.parsers.protocols import ParsedBlock

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


class ParserTextContent(bytes):
    """Raw parser text with exact spans of locally generated citation IDs only."""

    def __new__(cls, value: bytes, citation_spans: tuple[tuple[int, int], ...]):
        instance = super().__new__(cls, value)
        instance.citation_spans = citation_spans
        return instance


class PreparedProviderContent(bytes):
    """Bytes that crossed the local privacy boundary, with optional safe context."""

    def __new__(cls, value: bytes, source_context: dict[str, object] | None = None):
        instance = super().__new__(cls, value)
        instance.source_context = source_context
        return instance


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
    ocr_blocks: tuple[ParsedBlock, ...] = ()


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
    ocr_blocks: tuple[ParsedBlock, ...] = ()


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
            rows = csv.DictReader(
                io.StringIO(completed.stdout.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE
            )
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
        if ocr is None and sys.platform == "darwin" and getattr(sys, "frozen", False):
            from qian_labor.security.macos_ocr import MacOSVisionOCR
            ocr = MacOSVisionOCR()
        self.ocr = ocr if ocr is not None else TesseractOCR()
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
        # Keep only masked local line text. Redacted tokens are fully replaced,
        # including identifiers split across OCR tokens; no raw token persistence.
        lines: dict[str, list[tuple[int, OCRToken]]] = {}
        for index, token in enumerate(tokens):
            lines.setdefault(token.line_key, []).append((index, token))
        blocks = tuple(ParsedBlock(
            text=mask_sensitive(" ".join("[REDACTED]" if index in sensitive_tokens else token.text
                                         for index, token in line)),
            block_type="ocr_line",
            locator={"block": number, "bbox": [min(t.left for _, t in line), min(t.top for _, t in line),
                       max(t.left + t.width for _, t in line), max(t.top + t.height for _, t in line)]},
        ) for number, line in enumerate(lines.values(), start=1))
        return RedactedImage(output.getvalue(), hashes, evidence, blocks)

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
        if image_redactor is None:
            self.image_redactor = LocalImageRedactor(pepper=pepper)
        else:
            # 注入的 redactor 可能未携带 pepper；证据哈希是匹配关键路径，回填边界 pepper。
            if not image_redactor.pepper:
                image_redactor.pepper = pepper
            self.image_redactor = image_redactor

    def prepare(
        self,
        filename: str,
        content: bytes,
        *,
        is_image: bool,
        external: bool,
    ) -> PreparedProviderInput:
        # 方案决策（2026-09）：外部通道为用户配置的智谱官方端点，正文与文件名
        # 不再做遮盖/打码（遮盖曾导致社保号残缺、R10/R11 拿废数据，且扫描件
        # OCR 依赖造成整批材料无法分析）。本地标识哈希保留，仅供员工匹配；
        # 图片路径仍运行 OCR 提取证据哈希，但 OCR 失败不再阻断分析。
        if is_image:
            hashes: dict[str, str] = {}
            evidence: tuple[IdentifierEvidence, ...] = ()
            ocr_blocks: tuple[ParsedBlock, ...] = ()
            try:
                redacted = self.image_redactor.redact_with_metadata(content)
            except PrivacyBoundaryError:
                redacted = None
            if redacted is not None:
                hashes = redacted.identifier_hashes
                evidence = redacted.identifier_evidence
                ocr_blocks = redacted.ocr_blocks
            if self.pepper and not hashes and redacted is None:
                # image_redactor 整体失败（无 OCR 结果）：仅此时用边界 pepper 重跑一次
                # OCR 补证据哈希；redact 成功但 pepper 不匹配时不重复消耗 OCR。
                try:
                    tokens = self.image_redactor.ocr.extract_tokens(content)
                except (PrivacyBoundaryError, AttributeError):
                    tokens = []
                if tokens:
                    _, evidence = LocalImageRedactor._sensitive_token_indexes(
                        tokens, self.pepper
                    )
                    hashes = LocalImageRedactor._unique_hashes(evidence)
            if not external:
                return PreparedProviderInput(filename, content, hashes, evidence, ocr_blocks)
            return PreparedProviderInput(
                filename,
                PreparedProviderContent(content),
                hashes,
                evidence,
                ocr_blocks,
            )
        text = content.decode("utf-8", errors="replace")
        # Parser-generated citation IDs are not employee identifiers. Exclude
        # them only from local hashing, preserving offsets and outgoing bytes.
        spans = content.citation_spans if isinstance(content, ParserTextContent) else ()
        evidence_pieces, start = [], 0
        for left, right in spans:
            evidence_pieces.extend((text[start:left], " " * (right - left)))
            start = right
        evidence_pieces.append(text[start:])
        evidence = self._text_evidence("".join(evidence_pieces)) if self.pepper else ()
        hashes = LocalImageRedactor._unique_hashes(evidence)
        if not external:
            return PreparedProviderInput(filename, content, hashes, evidence)
        return PreparedProviderInput(
            filename,
            PreparedProviderContent(content),
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
