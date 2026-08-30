import hashlib
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

import pymupdf as fitz
from PIL import Image

ALLOWED = {
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel", "application/octet-stream"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".pdf": {"application/pdf"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
}


class UploadRejected(ValueError):
    pass


def _validate_office_archive(content: bytes) -> None:
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2_000:
                raise UploadRejected("Office 文件条目过多。")
            compressed = sum(max(entry.compress_size, 1) for entry in entries)
            expanded = sum(entry.file_size for entry in entries)
            if expanded > 100_000_000 or expanded / compressed > 100:
                raise UploadRejected("Office 文件解压规模异常。")
    except BadZipFile as exc:
        raise UploadRejected("Office 文件已损坏。") from exc


def validate_upload(
    filename: str,
    mime_type: str,
    content: bytes,
    max_bytes: int = 15_000_000,
    max_image_pixels: int = 20_000_000,
    max_pdf_pages: int = 100,
) -> str:
    if PurePath(filename).name != filename or filename in {".", ".."}:
        raise UploadRejected("文件名包含不安全路径。")
    extension = os.path.splitext(filename.lower())[1]
    if extension not in ALLOWED or mime_type not in ALLOWED[extension]:
        raise UploadRejected("不支持的文件格式。")
    if not content or len(content) > max_bytes:
        raise UploadRejected("文件为空或超过单文件限制。")
    if content.startswith((b"MZ", b"#!/", b"\x7fELF")):
        raise UploadRejected("不接受可执行文件。")
    signatures = {
        ".pdf": content.startswith(b"%PDF"),
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".jpeg": content.startswith(b"\xff\xd8\xff"),
        ".webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
        ".xlsx": content.startswith(b"PK"),
        ".docx": content.startswith(b"PK"),
        ".xls": content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")),
    }
    if extension in signatures and not signatures[extension]:
        raise UploadRejected("文件内容与扩展名不一致。")
    if extension in {".xlsx", ".docx"}:
        _validate_office_archive(content)
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
                if image.width * image.height > max_image_pixels:
                    raise UploadRejected("图片像素超过限制。")
        except UploadRejected:
            raise
        except Exception as exc:
            raise UploadRejected("图片文件已损坏。") from exc
    if extension == ".pdf":
        try:
            document = fitz.open(stream=content, filetype="pdf")
            if document.page_count > max_pdf_pages:
                raise UploadRejected("PDF 页数超过限制。")
            document.close()
        except UploadRejected:
            raise
        except Exception as exc:
            raise UploadRejected("PDF 文件已损坏。") from exc
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class UploadPolicy:
    max_bytes: int = 15_000_000
    max_batch_bytes: int = 100_000_000
    max_files: int = 100
