"""
Screen & webcam capture for JARVIS vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def _looks_blank(img_bytes: bytes) -> tuple[bool, str]:
    """True when a frame is a solid colour.

    A real desktop screenshot - even a lock screen with a clock on it - has
    detail; its pixels scatter widely around the mean. A backend that captured
    nothing produces a uniform frame whose pixels all sit within a couple of
    grey levels of each other. That uniformity is the fingerprint of a capture
    that lied, so it is measured instead of trusted.
    """
    if not _PIL:
        # Without a decoder we cannot judge - assume the frame is real.
        return False, ""
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("L")
        arr = np.asarray(img, dtype=np.uint8)
    except Exception:
        return False, ""
    if arr.size == 0:
        return True, "empty frame"
    std = float(arr.std())
    if std < 2.0:
        return True, f"uniform frame (std={std:.1f})"
    return False, ""


def _capture_screen() -> tuple[bytes, str]:
    """Grab the screen through the Wayland-capable chain, with a blank guard.

    Rung order, measured on Kali GNOME Wayland:
      1. core chain - portal -> GNOME Shell D-Bus -> grim -> legacy tools
         (core.screen_watch / core.kali_compat; the path screen_ai already
         used successfully on this machine)
      2. grim directly
      3. mss - X11-only. On Wayland it grabs the empty XWayland root and
         returns a solid-colour frame (~15 KB PNG), which the model then read
         as "a completely black screen". Kept last as the X11 fallback, and
         every rung's output is measured before use so a blank frame can
         never reach the model again.
    """
    import os as _os
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile

    def _core() -> bytes | None:
        try:
            from core.screen_watch import capture_bytes  # noqa: PLC0415
            data, _frame, _reason = capture_bytes()
            return data
        except Exception:
            return None

    def _grim() -> bytes | None:
        if not _shutil.which("grim"):
            return None
        path = None
        try:
            fd, path = _tempfile.mkstemp(suffix=".png", prefix="jarvis-grim-")
            _os.close(fd)
            r = _subprocess.run(["grim", path], capture_output=True,
                                timeout=10, check=False)
            if r.returncode == 0 and _os.path.exists(path) \
                    and _os.path.getsize(path) > 0:
                with open(path, "rb") as fh:
                    return fh.read()
        except Exception:
            pass
        finally:
            if path:
                try:
                    _os.unlink(path)
                except OSError:
                    pass
        return None

    def _mss() -> bytes | None:
        if not _MSS:
            return None
        try:
            with mss.mss() as sct:
                monitors = sct.monitors      # [0] = all combined, [1..n] = real
                target = monitors[1] if len(monitors) > 1 else monitors[0]
                shot = sct.grab(target)
                return mss.tools.to_png(shot.rgb, shot.size)
        except Exception:
            return None

    rungs: list[tuple[str, object]] = [
        ("portal chain", _core),
        ("grim", _grim),
        ("mss (X11)", _mss),
    ]

    problems: list[str] = []
    for name, grab in rungs:
        try:
            raw = grab()
        except Exception as exc:
            problems.append(f"{name}: {exc.__class__.__name__}")
            continue
        if not raw:
            problems.append(f"{name}: produced nothing")
            continue
        blank, why = _looks_blank(raw)
        if blank:
            problems.append(f"{name}: {why or 'uniform frame'}")
            continue
        return _compress(raw, "PNG")

    raise RuntimeError("screen capture failed - " + "; ".join(problems))


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
