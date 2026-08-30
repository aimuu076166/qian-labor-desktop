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


@dataclass(frozen=True)
class ParsedDocument:
    kind: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    needs_vision: bool = False
    vision_pages: list[VisionPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DocumentParser(Protocol):
    def parse(self, filename: str, content: bytes) -> ParsedDocument: ...
