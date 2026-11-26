"""Extract text: the OCR engines (ocrs in glance_core, tesseract), the pinned models fetched once, never at import, and one tiny generated image read for real when the models are given."""
from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from glance.shot import ocr
from glance.shot.image import encode_png
from tests.shot_fakes import FakeEngine, decode_png


def fake_models(tmp_path, monkeypatch, blobs: dict[str, bytes]):
    models = tuple(ocr.Model(name, hashlib.sha256(b).hexdigest(), len(b))
                   for name, b in blobs.items())
    monkeypatch.setattr(ocr, "MODELS", models)
    return models


def test_models_are_fetched_once_and_pinned(tmp_path, monkeypatch):
    blobs = {"text-detection.onnx": b"det" * 10, "text-recognition.onnx": b"rec" * 10}
    fake_models(tmp_path, monkeypatch, blobs)
    fetched = []

    def fetch(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(blobs[url.rsplit("/", 1)[1].removeprefix("ocrs-")])

    det, _rec = ocr.ensure_models(tmp_path / "m", fetch)
    assert det.read_bytes() == blobs["text-detection.onnx"]
    assert fetched == [f"{ocr.MODEL_BASE}/ocrs-text-detection.onnx",
                       f"{ocr.MODEL_BASE}/ocrs-text-recognition.onnx"]
    ocr.ensure_models(tmp_path / "m", fetch)
    assert len(fetched) == 2  # present and matching: not fetched again
    det.write_bytes(b"tampered" * 4)
    ocr.ensure_models(tmp_path / "m", fetch)
    assert len(fetched) == 3 and det.read_bytes() == blobs["text-detection.onnx"]
    assert not list((tmp_path / "m").glob("*.part"))


def test_a_model_that_fails_its_pin_is_never_kept(tmp_path, monkeypatch):
    fake_models(tmp_path, monkeypatch, {"a.onnx": b"good", "b.onnx": b"good2"})

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"evil")

    with pytest.raises(ocr.OcrUnavailable) as e:
        ocr.ensure_models(tmp_path / "m", fetch)
    assert e.value.reason == ocr.FETCH_FAILED
    assert not any((tmp_path / "m").iterdir())

    def offline(url: str, dest: Path) -> None:
        raise httpx.ConnectError("offline")

    with pytest.raises(ocr.OcrUnavailable):
        ocr.ensure_models(tmp_path / "m", offline)


def test_the_real_pins_are_sha256():
    assert [m.name for m in ocr.MODELS] == ["text-detection.onnx", "text-recognition.onnx"]
    assert all(len(m.sha256) == 64 and int(m.sha256, 16) for m in ocr.MODELS)
    assert all(m.url.startswith("https://") for m in ocr.MODELS)


def test_models_come_from_glances_release_and_upstream_only_on_a_404(tmp_path, monkeypatch):
    assert [m.url for m in ocr.MODELS] == [
        "https://github.com/Btocode/glance/releases/download/core-v0.3.0/ocrs-text-detection.onnx",
        ("https://github.com/Btocode/glance/releases/download/core-v0.3.0/"
         "ocrs-text-recognition.onnx")]
    assert ocr.MODELS[0].upstream_url == (
        "https://ocrs-models.s3-accelerate.amazonaws.com/text-detection.onnx")
    blobs = {"text-detection.onnx": b"det" * 10, "text-recognition.onnx": b"rec" * 10}
    fake_models(tmp_path, monkeypatch, blobs)
    fetched = []

    def fetch(url: str, dest: Path) -> None:
        fetched.append(url)
        if url.startswith(ocr.MODEL_BASE):
            raise ocr.ModelNotFound("HTTP 404")
        dest.write_bytes(b"evil" if "recognition" in url else blobs[url.rsplit("/", 1)[1]])

    with pytest.raises(ocr.OcrUnavailable):  # upstream's file fails its pin: not kept
        ocr.ensure_models(tmp_path / "m", fetch)
    assert fetched == [f"{ocr.MODEL_BASE}/ocrs-text-detection.onnx",
                       f"{ocr.UPSTREAM_BASE}/text-detection.onnx",
                       f"{ocr.MODEL_BASE}/ocrs-text-recognition.onnx",
                       f"{ocr.UPSTREAM_BASE}/text-recognition.onnx"]
    assert sorted(p.name for p in (tmp_path / "m").iterdir()) == ["text-detection.onnx"]

    def down(url: str, dest: Path) -> None:  # not a 404: no fallback
        fetched.append(url)
        raise OSError("HTTP 503")

    fetched.clear()
    with pytest.raises(ocr.OcrUnavailable):
        ocr.ensure_models(tmp_path / "m", down)
    assert fetched == [f"{ocr.MODEL_BASE}/ocrs-text-recognition.onnx"]


def test_nothing_is_fetched_at_import():
    import importlib

    import glance.shot.ocr as mod

    calls = []
    orig = httpx.stream
    try:
        httpx.stream = lambda *a, **k: calls.append(a)  # type: ignore[assignment]
        importlib.reload(mod)
        mod.engines("auto", platform="linux")
    finally:
        httpx.stream = orig  # type: ignore[assignment]
        importlib.reload(mod)
    assert calls == []


class FakeCore:
    """glance_core with an `Ocr` that 'reads' the PNG it gets: decodes our tiny test image
    and spells out its grey levels."""

    def __init__(self) -> None:
        self.loaded: list[tuple[str, str]] = []

    def Ocr(self, det: str, rec: str):  # the class name in glance_core
        self.loaded.append((det, rec))
        outer = self

        class _O:
            def png_text(self, png: bytes) -> str:
                outer.last = png
                w, h, _ch, px = decode_png(png)
                return f"{w}x{h} first={px[0]}\n\n"

        return _O()


def test_ocrs_engine_loads_once_and_reads(tmp_path, monkeypatch):
    blobs = {"text-detection.onnx": b"d", "text-recognition.onnx": b"r"}
    fake_models(tmp_path, monkeypatch, blobs)
    core = FakeCore()
    eng = ocr.OcrsEngine(core, tmp_path / "m",
                         lambda url, dest: dest.write_bytes(
                             blobs[url.rsplit("/", 1)[1].removeprefix("ocrs-")]))
    assert eng.available() and eng.needs_download()
    png = encode_png(bytes([7, 8, 9, 10]), 2, 2, channels=1)
    text, name = ocr.extract_text(png, [eng])
    assert (text, name) == ("2x2 first=7", "ocrs")
    ocr.extract_text(png, [eng])
    assert len(core.loaded) == 1 and not eng.needs_download()


def test_extract_text_falls_through_engines():
    png = b"png"
    assert ocr.extract_text(png, [FakeEngine("a", available=False), FakeEngine("b", "hi  \n")]) \
        == ("hi", "b")
    assert ocr.extract_text(png, [FakeEngine("a", error=ocr.FETCH_FAILED),
                                  FakeEngine("t", "x\n\n\ny")]) == ("x\n\ny", "t")
    with pytest.raises(ocr.OcrUnavailable) as e:
        ocr.extract_text(png, [FakeEngine("a", "   ")])
    assert e.value.reason == ocr.NOTHING
    with pytest.raises(ocr.OcrUnavailable) as e:
        ocr.extract_text(png, [])
    assert e.value.reason == ocr.NO_ENGINE


def test_tesseract_gets_the_image_on_stdin():
    seen = {}

    class Done:
        returncode, stdout = 0, b"Hello\n"

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return Done()

    t = ocr.TesseractEngine("/usr/bin/tesseract", run)
    assert t.text(b"PNG") == "Hello\n"
    assert seen["argv"] == ["/usr/bin/tesseract", "stdin", "stdout"] and seen["input"] == b"PNG"


def test_engines_by_mode():
    assert ocr.engines("off") == []
    assert [e.name for e in ocr.engines("auto", platform="linux")] == ["ocrs", "tesseract"]
    assert [e.name for e in ocr.engines("tesseract")] == ["tesseract"]


def test_real_ocrs_on_a_tiny_image_when_present():
    """glance_core 0.3 with the models already in Glance's data dir (never fetched here)."""
    core = pytest.importorskip("glance_core")
    if not hasattr(core, "Ocr"):
        pytest.skip("glance_core < 0.3")
    import os

    models = os.environ.get("GLANCE_TEST_OCR_MODELS")
    if not models:
        pytest.skip("GLANCE_TEST_OCR_MODELS not set")
    cairo = pytest.importorskip("cairo")
    import io

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 420, 60)
    cr = cairo.Context(surface)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.set_source_rgb(0, 0, 0)
    cr.select_font_face("DejaVu Sans")
    cr.set_font_size(28)
    cr.move_to(12, 40)
    cr.show_text("Hello from Glance")
    buf = io.BytesIO()
    surface.write_to_png(buf)
    o = core.Ocr(f"{models}/text-detection.onnx", f"{models}/text-recognition.onnx")
    assert o.png_text(buf.getvalue()).strip() == "Hello from Glance"
