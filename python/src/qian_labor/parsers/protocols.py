from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    block_type: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class VisionPage:
    page: int
    media_type: str
    image_bytes: bytes
    width: int
    height: int
    # Optional parser-owned context for formats (such as DOCX) that have an
    # embedded image but no physical page coordinate.  Consumers must treat
    # this as a coarse source hint, never as a fabricated page number.
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    kind: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    needs_vision: bool = False
    vision_pages: list[VisionPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DocumentParser(Protocol):
    def parse(self, filename: str, content: bytes) -> ParsedDocument: ...
