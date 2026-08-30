from __future__ import annotations

import re


PATTERNS = {
    "OPENAI_STYLE_API_KEY": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GITHUB_CLASSIC_TOKEN": re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    "GITHUB_FINE_GRAINED_TOKEN": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "GOOGLE_API_KEY": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "AWS_ACCESS_KEY_ID": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "ZHIPU_KEY_ASSIGNMENT": re.compile(
        rb"(?m)^\s*(?:export\s+)?(?:AI_API_KEY|ZAI_API_KEY|ZHIPU_API_KEY|BIGMODEL_API_KEY|ai_api_key|zai_api_key|zhipu_api_key|bigmodel_api_key)\s*=\s*(?![\"']?(?i:<|\$\{|YOUR_|replace|example|synthetic-))([\"']?[^\s#]{16,})"
    ),
}


def is_binary_content(content: bytes) -> bool:
    return b"\0" in content[:4096]
