"""Synthetic native OCR contracts; no customer documents or network requests."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont
import pytest

from qian_labor.security.local_redaction import (
    LocalImageRedactor, PrivacyBoundary, PrivacyBoundaryError, TesseractOCR,
)

ROOT = Path(__file__).resolve().parents[3]


def png():
    output = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(output, format="PNG")
    return output.getvalue()


def payload():
    return {"width": 160, "height": 80, "lines": [
        {"text": '虚构 "13912345678"', "left": 20, "top": 10, "width": 100, "height": 24},
    ]}


def bundled_helper(monkeypatch, tmp_path):
    helper = tmp_path / "qian-macos-ocr"
    helper.touch()
    helper.chmod(0o700)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return helper


def test_native_adapter_uses_only_absolute_bundled_helper(monkeypatch, tmp_path):
    from qian_labor.security.macos_ocr import MacOSVisionOCR
    helper = bundled_helper(monkeypatch, tmp_path)
    def run(command, **kwargs):
        assert command == [str(helper)]
        assert kwargs == {"input": png(), "capture_output": True, "timeout": 30, "check": False}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload()).encode(), b"")
    monkeypatch.setattr(subprocess, "run", run)
    tokens = MacOSVisionOCR().extract_tokens(png())
    assert len(tokens) == 1
    assert (tokens[0].text, tokens[0].left, tokens[0].top, tokens[0].width, tokens[0].height) == (
        '虚构 "13912345678"', 20, 10, 100, 24,
    )


@pytest.mark.parametrize("kind", ["empty", "dimension", "negative", "overflow", "float", "bool",
                                      "text", "extra", "missing", "not_json", "oversized"])
def test_native_adapter_rejects_invalid_output(monkeypatch, tmp_path, kind):
    from qian_labor.security.macos_ocr import MacOSVisionOCR
    bundled_helper(monkeypatch, tmp_path)
    data = copy.deepcopy(payload())
    if kind == "empty": data["lines"] = []
    elif kind == "dimension": data["width"] = 161
    elif kind == "negative": data["lines"][0]["left"] = -1
    elif kind == "overflow": data["lines"][0]["width"] = 160
    elif kind == "float": data["lines"][0]["top"] = 10.0
    elif kind == "bool": data["lines"][0]["width"] = True
    elif kind == "text": data["lines"][0]["text"] = " "
    elif kind == "extra": data["lines"][0]["unexpected"] = 1
    elif kind == "missing": del data["lines"][0]["top"]
    output = json.dumps(data).encode()
    if kind == "not_json": output = b"synthetic-damaged-response"
    if kind == "oversized": output = b" " * (4 * 1024 * 1024 + 1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, b""))
    with pytest.raises(PrivacyBoundaryError, match="^AI_LOCAL_REDACTION_FAILED$"):
        MacOSVisionOCR().extract_tokens(png())


@pytest.mark.parametrize("kind", ["missing", "symlink", "relative", "timeout", "exit", "bad_image", "oversized_input"])
def test_native_adapter_fails_closed_without_fallback(monkeypatch, tmp_path, kind):
    from qian_labor.security.macos_ocr import MacOSVisionOCR
    helper = bundled_helper(monkeypatch, tmp_path)
    if kind == "missing": helper.unlink()
    if kind == "symlink":
        helper.unlink()
        helper.symlink_to(tmp_path / "different-helper")
    if kind == "relative": monkeypatch.setattr(sys, "_MEIPASS", "relative-dir")
    def run(*args, **kwargs):
        if kind == "timeout": raise subprocess.TimeoutExpired("safe-helper", 30)
        if kind == "exit": return subprocess.CompletedProcess(args, 1, b"", b"synthetic-private-detail")
        pytest.fail("invalid local input/path must not execute any helper or PATH fallback")
    monkeypatch.setattr(subprocess, "run", run)
    content = b"bad image" if kind == "bad_image" else png()
    if kind == "oversized_input": content = b"x" * (20 * 1024 * 1024 + 1)
    with pytest.raises(PrivacyBoundaryError, match="^AI_LOCAL_REDACTION_FAILED$"):
        MacOSVisionOCR().extract_tokens(content)


@pytest.mark.parametrize("frozen,platform,native", [(True, "darwin", True), (False, "darwin", False),
                                                   (True, "win32", False), (False, "linux", False)])
def test_frozen_macos_selector_does_not_use_homebrew(monkeypatch, frozen, platform, native):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    ocr = LocalImageRedactor().ocr
    assert (type(ocr).__name__ == "MacOSVisionOCR") is native
    assert isinstance(ocr, TesseractOCR) is (not native)


def build_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("native_test_build_sidecar", ROOT / "scripts/build_sidecar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_build_uses_temporary_cache_and_explicit_macos_target(monkeypatch, tmp_path):
    module = build_module(monkeypatch)
    def run(command, **kwargs):
        assert command[:2] == ["xcrun", "swiftc"]
        assert command[command.index("-target") + 1] == "arm64-apple-macos11.0"
        assert Path(command[command.index("-module-cache-path") + 1]).is_relative_to(tmp_path)
        assert str(ROOT / "python/native/macos_ocr.swift") in command
        Path(command[command.index("-o") + 1]).touch()
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", run)
    assert module.build_macos_ocr(tmp_path, "aarch64-apple-darwin") == tmp_path / "qian-macos-ocr"


@pytest.mark.parametrize("triple,native", [("aarch64-apple-darwin", True), ("x86_64-pc-windows-msvc", False)])
def test_sidecar_build_bundles_native_helper_only_for_macos(monkeypatch, tmp_path, triple, native):
    module = build_module(monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "BINARIES", tmp_path / "binaries")
    monkeypatch.setattr(module, "write_windows_ico", lambda *args: None)
    monkeypatch.setattr(module, "target_triple", lambda: triple)
    helper = tmp_path / "qian-macos-ocr"
    calls = []
    monkeypatch.setattr(module, "build_macos_ocr", lambda *args: calls.append(args) or helper)
    def run(command, **kwargs):
        assert ("--add-binary" in command) is native
        if native: assert command[command.index("--add-binary") + 1] == f"{helper}:."
        dist = Path(command[command.index("--distpath") + 1])
        dist.mkdir()
        (dist / ("qian-sidecar" if native else "qian-sidecar.exe")).touch()
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", run)
    assert module.build().is_file()
    assert bool(calls) is native


@pytest.fixture(scope="module")
def compiled_native_helper():
    if sys.platform != "darwin" or not shutil.which("xcrun"):
        pytest.skip("native Vision verification needs macOS build tools")
    with tempfile.TemporaryDirectory(prefix="qian-native-ocr-test-") as temporary:
        folder = Path(temporary)
        architecture = "arm64" if os.uname().machine == "arm64" else "x86_64"
        helper = folder / "qian-macos-ocr"
        result = subprocess.run([
            "/usr/bin/xcrun", "swiftc", "-O", "-target", f"{architecture}-apple-macos11.0",
            "-module-cache-path", str(folder / "module-cache"),
            str(ROOT / "python/native/macos_ocr.swift"), "-o", str(helper),
        ], capture_output=True, timeout=120)
        assert result.returncode == 0, "native helper compilation failed"
        yield helper


@pytest.mark.parametrize("orientation", [1, 6])
def test_native_vision_real_synthetic_image_with_empty_path(monkeypatch, compiled_native_helper, orientation):
    from qian_labor.security.macos_ocr import MacOSVisionOCR
    monkeypatch.setattr(sys, "_MEIPASS", str(compiled_native_helper.parent), raising=False)
    monkeypatch.setenv("PATH", "")
    image = Image.new("RGB", (1000, 260), "white")
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 36)
    draw = ImageDraw.Draw(image)
    draw.text((30, 35), "完全虚构测试 SYNTHETIC ONLY", font=font, fill="black")
    draw.text((30, 130), "手机号 13912345678", font=font, fill="black")
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    image.save(output, format="PNG", exif=exif)
    tokens = MacOSVisionOCR().extract_tokens(output.getvalue())
    assert any("虚构" in token.text for token in tokens)
    assert any("SYNTHETIC" in token.text for token in tokens)
    phone = next(token for token in tokens if "13912345678" in token.text.replace(" ", ""))
    assert 120 <= phone.top <= 160  # Original raster coordinates, not EXIF-rotated.
    prepared = PrivacyBoundary("synthetic-native-pepper", LocalImageRedactor(MacOSVisionOCR())).prepare(
        "synthetic-native.png", output.getvalue(), is_image=True, external=True,
    )
    with Image.open(io.BytesIO(prepared.content)) as redacted:
        assert redacted.crop((phone.left, phone.top, phone.left + phone.width,
                              phone.top + phone.height)).getextrema() == ((0, 0), (0, 0), (0, 0))


@pytest.mark.parametrize("content", [b"synthetic invalid image", b"", b"x" * (20 * 1024 * 1024 + 1), png()],
                         ids=["bad-image", "empty-input", "oversized-input", "blank-image"])
def test_native_vision_bad_image_only_returns_fixed_error(monkeypatch, compiled_native_helper, content):
    monkeypatch.setenv("PATH", "")
    result = subprocess.run([str(compiled_native_helper)], input=content,
                            capture_output=True, timeout=30)
    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr == b"AI_LOCAL_REDACTION_FAILED\n"


def test_native_vision_links_only_system_libraries(compiled_native_helper):
    result = subprocess.run(["/usr/bin/otool", "-L", str(compiled_native_helper)],
                            capture_output=True, text=True, check=True)
    dependencies = [line.strip().split(" (", 1)[0] for line in result.stdout.splitlines()[1:]]
    assert dependencies and all(path.startswith(("/System/Library/", "/usr/lib/")) for path in dependencies)
    assert any("Vision.framework" in path for path in dependencies)
    build_info = subprocess.run(["/usr/bin/otool", "-l", str(compiled_native_helper)],
                                capture_output=True, text=True, check=True).stdout
    assert "minos 11.0" in build_info
