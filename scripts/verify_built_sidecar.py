#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from desktop_verification import VerificationError, verify_command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify one built desktop sidecar executable.")
    parser.add_argument("--binary", type=Path, required=True)
    return parser


def _validate_binary(path: Path) -> None:
    if not path.is_file():
        raise VerificationError("BINARY_MISSING")
    if path.is_symlink():
        raise VerificationError("BINARY_SYMLINK_REJECTED")
    if os.name == "nt":
        if path.suffix.lower() != ".exe":
            raise VerificationError("BINARY_SUFFIX_INVALID")
    elif not os.access(path, os.X_OK):
        raise VerificationError("BINARY_NOT_EXECUTABLE")


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_binary(args.binary)
        markers = verify_command([str(args.binary.resolve())])
    except VerificationError as error:
        print(f"BUILT_SIDECAR_VERIFY=FAIL:{error.code}", file=sys.stderr)
        return 1
    for marker in markers:
        print(marker)
    print("BUILT_SIDECAR_VERIFY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
