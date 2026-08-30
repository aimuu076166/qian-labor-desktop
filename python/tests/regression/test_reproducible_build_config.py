from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "apps" / "desktop" / "src-tauri" / "Cargo.toml"
LOCKFILE = MANIFEST.with_name("Cargo.lock")
TOOLCHAIN = ROOT / "rust-toolchain.toml"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_desktop_rust_release_inputs_are_tracked_and_resolvable() -> None:
    lockfile_relative = LOCKFILE.relative_to(ROOT).as_posix()

    assert LOCKFILE.is_file(), "desktop Cargo.lock must be committed"

    ignored = _git("check-ignore", "-q", "--", lockfile_relative)
    assert ignored.returncode == 1, "desktop Cargo.lock must not be ignored"

    tracked = _git("ls-files", "--error-unmatch", "--", lockfile_relative)
    assert tracked.returncode == 0, tracked.stderr

    metadata = subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(MANIFEST),
            "--locked",
            "--no-deps",
            "--format-version=1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert metadata.returncode == 0, metadata.stderr
    packages = json.loads(metadata.stdout)["packages"]
    assert any(
        package["name"] == "qian-labor-desktop"
        and Path(package["manifest_path"]).resolve() == MANIFEST.resolve()
        for package in packages
    )

    assert TOOLCHAIN.is_file(), "rust-toolchain.toml must pin the release toolchain"
    toolchain = tomllib.loads(TOOLCHAIN.read_text(encoding="utf-8"))["toolchain"]
    assert toolchain["channel"] == "1.98.0"
    assert toolchain["profile"] == "minimal"
    assert toolchain["components"] == ["rustfmt"]
