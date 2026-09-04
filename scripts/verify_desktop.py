#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from desktop_verification import VerificationError, verify_command


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "python" / "desktop_entrypoint.py"


def main() -> int:
    try:
        markers = verify_command([sys.executable, str(ENTRYPOINT)])
    except VerificationError as error:
        print(f"DESKTOP_VERIFY=FAIL:{error.code}", file=sys.stderr)
        return 1
    for marker in markers:
        print(marker)
    print("DESKTOP_VERIFY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
