"""Extract text: local OCR of an image held in memory. Nothing goes over the network except,
once, the ocrs models themselves.

Engines, in the order `[shot] ocr = "auto"` tries them:

* macOS: Apple's Vision (`VNRecognizeTextRequest`), built in (glance.platform.macos.ocr).
* `ocrs`, the pure-Rust engine inside glance_core (0.3.0 and later). Its two ONNX models
  (~12 MB) are fetched on first use into Glance's data dir from Glance's own GitHub release
  (upstream's bucket if the release lacks them) and checked against the SHA-256 pins below;
  never at import, never without a pin match.
* `tesseract`, if it is installed (the image goes to it on stdin, never through a file).

`ocrs` or `tesseract` names one engine only.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glance.log import log_event

# Glance's own release carries the models (the core-wheels workflow copies them there from
# upstream after checking these same pins); upstream's bucket is the fallback when the release
# does not have them (404). Either way a file is kept only if it matches its pin.
CORE_TAG = "core-v0.3.0"
MODEL_BASE = f"https://github.com/Btocode/glance/releases/download/{CORE_TAG}"
UPSTREAM_BASE = "https://ocrs-models.s3-accelerate.amazonaws.com"


@dataclass(frozen=True)
class Model:
    name: str     # upstream's file name, and the name in Glance's data dir
    sha256: str
    size: int

    @property
    def asset(self) -> str:
        """The release asset's name (beside the wheels, so prefixed)."""
        return f"ocrs-{self.name}"

    @property
    def url(self) -> str:
        return f"{MODEL_BASE}/{self.asset}"

    @property
    def upstream_url(self) -> str:
        return f"{UPSTREAM_BASE}/{self.name}"


# Pinned 2026-11-26 (ocrs 0.14's examples/download-models.sh). .github/workflows/core-wheels.yml
# reads these pins when it puts the files on the release.
MODELS = (
    Model("text-detection.onnx",
          "a917b23dbd9524b465df7e922641b3ff2981623df4ded5a0234004ef2fee7cfe", 2_499_479),
    Model("text-recognition.onnx",
          "86c145c2edb96c8caed5b1ebb8f44d706408922211451c309c625157dd6061c5", 9_713_177),
)
MAX_MODEL_BYTES = 32 * 1024 * 1024
TESSERACT_TIMEOUT_S = 30.0
# As the installer's curl: give up on a connection that does not open in 15 s or stalls
# (no bytes for 30 s), rather than wait forever.
FETCH_CONNECT_S = 15.0
FETCH_TIMEOUT_S = 30.0


class OcrUnavailable(Exception):
    """No engine could read the image: `reason` says why (for the bar)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


NO_ENGINE = "No text reader here: install tesseract, or Glance's core (glance_core 0.3+)"
FETCH_FAILED = "Could not download the text reader. Check the connection and try again"
NOTHING = "No text found in this shot"


def models_dir() -> Path:
    from glance.paths import data_dir

    return data_dir() / "models" / "ocrs"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


Fetch = Callable[[str, Path], None]  # download url to the given path


class ModelNotFound(OSError):
    """The URL answered 404: the file is not there (try the next source)."""


def http_fetch(url: str, dest: Path) -> None:
    """Stream `url` to `dest` (HTTPS only, size capped). ModelNotFound on a 404."""
    import httpx

    if not url.startswith("https://"):
        raise OSError("refusing a non-HTTPS model URL")
    got = 0
    timeout = httpx.Timeout(FETCH_TIMEOUT_S, connect=FETCH_CONNECT_S)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        if r.status_code == 404:
            raise ModelNotFound(f"HTTP {r.status_code}")
        if r.status_code != 200:
            raise OSError(f"HTTP {r.status_code}")
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                got += len(chunk)
                if got > MAX_MODEL_BYTES:
                    raise OSError("model too large")
                f.write(chunk)


_fetch_lock = threading.Lock()


def ensure_models(directory: Path | None = None, fetch: Fetch = http_fetch) -> tuple[Path, Path]:
    """The two model files, downloaded now if missing or not matching their pins. A file
    that fails its pin is deleted and never loaded. Raises OcrUnavailable(FETCH_FAILED)."""
    directory = directory or models_dir()
    with _fetch_lock:
        paths = []
        for m in MODELS:
            path = directory / m.name
            if path.exists():
                try:
                    if path.stat().st_size == m.size and sha256_file(path) == m.sha256:
                        paths.append(path)
                        continue
                except OSError:
                    pass
                log_event("ocr_model_mismatch", model=m.name)
                path.unlink(missing_ok=True)
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".fetch-", suffix=".part")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                log_event("ocr_model_fetch", model=m.name)
                try:
                    fetch(m.url, tmp)
                except ModelNotFound:  # not on Glance's release: upstream, same pin
                    log_event("ocr_model_fallback", model=m.name)
                    fetch(m.upstream_url, tmp)
                if tmp.stat().st_size != m.size or sha256_file(tmp) != m.sha256:
                    log_event("ocr_model_bad_pin", model=m.name)
                    raise OcrUnavailable(FETCH_FAILED)
                os.replace(tmp, path)
            except OcrUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 - network, disk: the bar says so
                log_event("ocr_model_fetch_failed", model=m.name, kind=type(e).__name__)
                raise OcrUnavailable(FETCH_FAILED) from None
            finally:
                tmp.unlink(missing_ok=True)
            paths.append(path)
        return paths[0], paths[1]


def models_present(directory: Path | None = None) -> bool:
    directory = directory or models_dir()
    return all((directory / m.name).exists() for m in MODELS)


class OcrsEngine:
    """glance_core's `Ocr` (ocrs). The models are fetched on the first `text()`."""

    name = "ocrs"

    def __init__(self, core: Any = None, directory: Path | None = None,
                 fetch: Fetch = http_fetch) -> None:
        self._core = core
        self._dir = directory
        self._fetch = fetch
        self._ocr: Any = None
        self._lock = threading.Lock()

    def _module(self) -> Any:
        if self._core is not None:
            return self._core
        try:
            import glance_core
        except ImportError:
            return None
        return glance_core

    def available(self) -> bool:
        core = self._module()
        return core is not None and hasattr(core, "Ocr")

    def needs_download(self) -> bool:
        return not models_present(self._dir)

    def text(self, png: bytes) -> str:
        core = self._module()
        if core is None or not hasattr(core, "Ocr"):
            raise OcrUnavailable(NO_ENGINE)
        with self._lock:
            if self._ocr is None:
                det, rec = ensure_models(self._dir, self._fetch)
                try:
                    self._ocr = core.Ocr(str(det), str(rec))
                except (OSError, RuntimeError) as e:
                    log_event("ocr_load_failed", kind=type(e).__name__)
                    raise OcrUnavailable(NO_ENGINE) from None
            ocr = self._ocr
        try:
            return str(ocr.png_text(png))
        except (ValueError, RuntimeError) as e:
            log_event("ocr_failed", engine="ocrs", kind=type(e).__name__)
            raise OcrUnavailable(NOTHING) from None


class TesseractEngine:
    name = "tesseract"

    def __init__(self, binary: str | None = None,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self._binary = binary
        self._run = run

    def _path(self) -> str | None:
        return self._binary or shutil.which("tesseract")

    def available(self) -> bool:
        return self._path() is not None

    def needs_download(self) -> bool:
        return False

    def text(self, png: bytes) -> str:
        binary = self._path()
        if binary is None:
            raise OcrUnavailable(NO_ENGINE)
        try:
            done = self._run([binary, "stdin", "stdout"], input=png, capture_output=True,
                             timeout=TESSERACT_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log_event("ocr_failed", engine="tesseract", kind=type(e).__name__)
            raise OcrUnavailable(NOTHING) from None
        if done.returncode != 0:
            log_event("ocr_failed", engine="tesseract", kind="exit")
            raise OcrUnavailable(NOTHING)
        return done.stdout.decode("utf-8", "replace")


def engines(mode: str, platform: str = sys.platform) -> list[Any]:
    """The engines `[shot] ocr = mode` tries, in order (empty for "off")."""
    if mode == "off":
        return []
    if mode == "ocrs":
        return [OcrsEngine()]
    if mode == "tesseract":
        return [TesseractEngine()]
    out: list[Any] = []
    if platform == "darwin":
        from glance.platform.macos.ocr import VisionEngine

        out.append(VisionEngine())
    return [*out, OcrsEngine(), TesseractEngine()]


def tidy(text: str) -> str:
    """Trailing spaces and runs of blank lines gone; the lines themselves as read."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\f", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def extract_text(png: bytes, candidates: list[Any]) -> tuple[str, str]:
    """(text, engine name) from the first engine that is there; OcrUnavailable otherwise
    (NOTHING when an engine ran and found no text)."""
    reason = NO_ENGINE
    for engine in candidates:
        if not engine.available():
            continue
        try:
            text = tidy(engine.text(png))
        except OcrUnavailable as e:
            reason = e.reason  # the next engine (tesseract) may still read it
            continue
        if text.strip():
            return text, engine.name
        reason = NOTHING
    raise OcrUnavailable(reason)
