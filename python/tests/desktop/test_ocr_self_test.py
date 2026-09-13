import builtins
import importlib.util
import io
from pathlib import Path
import socket
import sys

from PIL import Image
import pytest

from qian_labor.security.local_redaction import OCRToken, PreparedProviderInput, TesseractOCR

ROOT = Path(__file__).resolve().parents[3]


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("offline_ocr_entrypoint", ROOT / "python/desktop_entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_tokens(self, content):
    with Image.open(io.BytesIO(content)) as image:
        assert image.width >= 500 and image.height >= 180
    return [OCRToken("13912345678", 20, 100, 330, 45, "synthetic-line")]


def test_ocr_self_test_uses_default_boundary_without_files_or_network(monkeypatch, capsys):
    from qian_labor.desktop.ocr_self_test import run
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(TesseractOCR, "extract_tokens", synthetic_tokens)
    def forbidden(*args, **kwargs):
        pytest.fail("offline OCR self-test must not open files or network connections")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert run() == 0
    captured = capsys.readouterr()
    assert captured.out == "LOCAL_OCR=PASS\n"
    assert captured.err == ""


def test_self_test_checks_printed_ink_not_font_advance_whitespace(monkeypatch, capsys):
    from qian_labor.desktop.ocr_self_test import run
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(TesseractOCR, "extract_tokens", lambda *args: [
        OCRToken("13912345678", 25, 109, 224, 27, "synthetic-tight-box"),
    ])
    assert run() == 0
    assert capsys.readouterr().out == "LOCAL_OCR=PASS\n"


@pytest.mark.parametrize("fault", ["no_phone", "ocr_error", "content_changed", "bad_hash", "wrong_box"])
def test_ocr_self_test_failure_never_leaks_details(monkeypatch, capsys, fault):
    from qian_labor.desktop import ocr_self_test
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    def extract(self, content):
        if fault == "ocr_error":
            print("synthetic-private-error", file=sys.stderr)
            print("synthetic-private-error")
            raise RuntimeError("synthetic-private-error")
        if fault == "no_phone": return [OCRToken("SYNTHETIC", 20, 100, 330, 45, "line")]
        if fault == "wrong_box": return [OCRToken("SYNTHETIC", 20, 20, 330, 45, "wrong-line"), OCRToken("13999999999", 20, 100, 330, 45, "wrong-line2")]
        return synthetic_tokens(self, content)
    monkeypatch.setattr(TesseractOCR, "extract_tokens", extract)
    if fault in {"content_changed", "bad_hash"}:
        original_prepare = ocr_self_test.PrivacyBoundary.prepare
        def unchanged(self, filename, content, **kwargs):
            result = original_prepare(self, filename, content, **kwargs)
            if fault == "content_changed":
                # 图片内容被意外改写（应原样透传）→ 自检必须 FAIL。
                return PreparedProviderInput(filename, content + b"x", result.identifier_hashes)
            return PreparedProviderInput(filename, result.content, {"phone_hash": "not-a-valid-hash"})
        monkeypatch.setattr(ocr_self_test.PrivacyBoundary, "prepare", unchanged)
    assert ocr_self_test.run() == 1
    captured = capsys.readouterr()
    assert captured.out == "LOCAL_OCR=FAIL\n"
    assert captured.err == ""


def test_entrypoint_self_test_never_imports_server_or_reads_required_environment(monkeypatch, capsys):
    from qian_labor.desktop import ocr_self_test
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name not in {"uvicorn", "qian_labor.desktop.app"}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["sidecar", "--self-test-local-ocr"])
    monkeypatch.setattr(module, "_required_env", lambda *args: pytest.fail("server environment read"))
    monkeypatch.setattr(ocr_self_test, "run", lambda: print("LOCAL_OCR=PASS") or 0)
    assert module.main() == 0
    assert capsys.readouterr().out == "LOCAL_OCR=PASS\n"


@pytest.mark.parametrize("args", [["--self-test-local-ocr", "synthetic-private-arg"],
                                   ["--self-test-local-ocr", "--self-test-local-ocr"],
                                   ["synthetic-private-arg"]])
def test_entrypoint_rejects_nonexclusive_self_test_arguments(monkeypatch, capsys, args):
    module = load_entrypoint()
    monkeypatch.setattr(sys, "argv", ["sidecar", *args])
    monkeypatch.setattr(module, "_required_env", lambda *args: pytest.fail("server path entered"))
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "LOCAL_OCR=FAIL\n"
    assert captured.err == ""


def test_frozen_macos_missing_helper_cannot_claim_self_test_success(monkeypatch, tmp_path, capsys):
    from qian_labor.desktop.ocr_self_test import run
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(TesseractOCR, "extract_tokens", lambda *args: pytest.fail("PATH fallback"))
    assert run() == 1
    captured = capsys.readouterr()
    assert captured.out == "LOCAL_OCR=FAIL\n"
    assert captured.err == ""
