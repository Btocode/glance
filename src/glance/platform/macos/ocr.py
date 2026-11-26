"""Extract text on macOS: Apple's Vision framework (`VNRecognizeTextRequest`), built in, on
the device, no download. The PNG never leaves memory.

Needs PyObjC's Vision and Quartz bindings; `available()` is False without them (ocrs or
tesseract then take over, as on Linux).
"""
from __future__ import annotations

from typing import Any

from glance.log import log_event
from glance.shot.ocr import NOTHING, OcrUnavailable

ACCURATE = 0  # VNRequestTextRecognitionLevelAccurate


def _frameworks() -> tuple[Any, Any] | None:
    try:
        import Quartz
        import Vision
    except ImportError:
        return None
    return Vision, Quartz


def lines_in_reading_order(observations: list[tuple[float, float, str]]) -> list[str]:
    """(x, y-of-top in Vision's bottom-left unit space, text) to lines top to bottom, left to
    right within a line (pure, for the tests)."""
    rows = sorted(observations, key=lambda o: (-round(o[1], 2), o[0]))
    return [text for _x, _y, text in rows if text.strip()]


class VisionEngine:
    name = "vision"

    def available(self) -> bool:
        return _frameworks() is not None

    def needs_download(self) -> bool:
        return False

    def text(self, png: bytes) -> str:
        fw = _frameworks()
        if fw is None:
            raise OcrUnavailable(NOTHING)
        Vision, Quartz = fw
        from Foundation import NSData

        data = NSData.dataWithBytes_length_(png, len(png))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
        if image is None:
            raise OcrUnavailable(NOTHING)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(ACCURATE)
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok, _error = handler.performRequests_error_([request], None)
        if not ok:
            log_event("ocr_failed", engine="vision", kind="perform")
            raise OcrUnavailable(NOTHING)
        found: list[tuple[float, float, str]] = []
        for obs in request.results() or ():
            candidates = obs.topCandidates_(1)
            if not candidates:
                continue
            box = obs.boundingBox()
            found.append((float(box.origin.x), float(box.origin.y + box.size.height),
                          str(candidates[0].string())))
        return "\n".join(lines_in_reading_order(found))
