"""Strict IPC adapter for the OCR executable shipped inside the macOS sidecar."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image

from qian_labor.security.local_redaction import OCRToken, PrivacyBoundaryError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_LINES = 10_000


class MacOSVisionOCR:
    def extract_tokens(self, content: bytes) -> list[OCRToken]:
        try:
            if not content or len(content) > MAX_INPUT_BYTES:
                raise ValueError
            bundle = Path(getattr(sys, "_MEIPASS", ""))
            if not bundle.is_absolute():
                raise ValueError
            helper = bundle / "qian-macos-ocr"
            if helper.is_symlink() or not helper.is_file() or not os.access(helper, os.X_OK):
                raise ValueError
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    raise ValueError
                image.verify()
            completed = subprocess.run(
                [str(helper)], input=content, capture_output=True, timeout=30, check=False,
            )
            if completed.returncode != 0 or not 0 < len(completed.stdout) <= MAX_OUTPUT_BYTES:
                raise ValueError
            data = json.loads(completed.stdout)
            if type(data) is not dict or set(data) != {"width", "height", "lines"}:
                raise ValueError
            if (type(data["width"]) is not int or type(data["height"]) is not int
                    or (data["width"], data["height"]) != (width, height)):
                raise ValueError
            lines = data["lines"]
            if type(lines) is not list or not 0 < len(lines) <= MAX_LINES:
                raise ValueError
            tokens = []
            for index, line in enumerate(lines):
                if type(line) is not dict or set(line) != {"text", "left", "top", "width", "height"}:
                    raise ValueError
                text = line["text"]
                if (type(text) is not str or not text.strip() or len(text) > 4096
                        or any(ord(char) < 32 for char in text)):
                    raise ValueError
                if any(type(line[key]) is not int for key in ("left", "top", "width", "height")):
                    raise ValueError
                left, top = line["left"], line["top"]
                box_width, box_height = line["width"], line["height"]
                if (left < 0 or top < 0 or box_width <= 0 or box_height <= 0
                        or left + box_width > width or top + box_height > height):
                    raise ValueError
                tokens.append(OCRToken(text, left, top, box_width, box_height, f"vision-line-{index}"))
            return tokens
        except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError,
                subprocess.SubprocessError, Image.DecompressionBombError):
            raise PrivacyBoundaryError("AI_LOCAL_REDACTION_FAILED") from None
