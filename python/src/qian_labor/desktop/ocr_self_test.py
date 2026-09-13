"""Offline, memory-only diagnostic for the actual runtime's default OCR boundary."""
from contextlib import redirect_stderr, redirect_stdout
import io

from PIL import Image, ImageChops, ImageDraw, ImageFont

from qian_labor.security.local_redaction import PrivacyBoundary
from qian_labor.security.masking import identifier_hash

PHONE = "13912345678"  # Deliberately synthetic; never read from environment or material.
PEPPER = "synthetic-local-ocr-self-test-only-pepper"


def _check() -> bool:
    with Image.new("RGB", (700, 200), "white") as image:
        font = ImageFont.load_default(size=36)
        draw = ImageDraw.Draw(image)
        draw.text((20, 20), "SYNTHETIC LOCAL OCR", font=font, fill="black")
        draw.text((20, 100), PHONE, font=font, fill="black")
        printed_phone_box = draw.textbbox((20, 100), PHONE, font=font)
        # Font advance includes blank side bearings; measure the actual rendered ink.
        ink = ImageChops.invert(image.crop(printed_phone_box)).getbbox()
        if ink is None:
            return False
        printed_phone_box = (
            printed_phone_box[0] + ink[0], printed_phone_box[1] + ink[1],
            printed_phone_box[0] + ink[2], printed_phone_box[1] + ink[3],
        )
        output = io.BytesIO()
        image.save(output, format="PNG")
    content = output.getvalue()
    boundary = PrivacyBoundary(PEPPER)
    tokens = boundary.image_redactor.ocr.extract_tokens(content)
    phone_tokens = [token for token in tokens if PHONE in token.text.replace(" ", "").replace("-", "")]
    if not phone_tokens:
        return False
    prepared = boundary.prepare("synthetic-ocr-self-test.png", content, is_image=True, external=True)
    if prepared.identifier_hashes.get("phone_hash") != identifier_hash("phone", PHONE, PEPPER):
        return False
    # 新契约：图片不打码。自检改为验证原图完整直达 + 哈希链路正确。
    if bytes(prepared.content) != content:
        return False
    for token in phone_tokens:
        box = (token.left, token.top, token.left + token.width, token.top + token.height)
        if not (0 <= box[0] < box[2] <= 700 and 0 <= box[1] < box[3] <= 200):
            return False
    return True


def run() -> int:
    # Never forward OCR diagnostics, exception messages, or synthetic recognized text.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            passed = _check()
        except Exception:
            passed = False
    print("LOCAL_OCR=PASS" if passed else "LOCAL_OCR=FAIL", flush=True)
    return 0 if passed else 1
