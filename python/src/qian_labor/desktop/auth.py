from __future__ import annotations

import hmac

from fastapi import Request

TOKEN_HEADER = "X-Qian-Desktop-Token"


def request_has_valid_token(request: Request, expected_token: str) -> bool:
    provided = request.headers.get(TOKEN_HEADER, "")
    return bool(provided) and hmac.compare_digest(provided, expected_token)
