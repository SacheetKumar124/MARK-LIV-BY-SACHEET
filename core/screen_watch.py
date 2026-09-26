"""Seeing the screen safely: capture, dedupe, redaction, retention.

This is the layer between "the desktop has pixels" and "the model gets an
image". It exists because looking at a screen repeatedly is expensive, and
because sending a whole desktop to a model is a privacy decision that should be
made in code rather than in a prompt.

The four jobs:

1. **Capture** through whatever route this session actually authorises.
   Verified on this box: xdg-desktop-portal returns a real PNG on GNOME Wayland
   in ~0.75 s; ``mss`` is the fallback for X11 and for a portal-less desktop.
2. **Refuse to re-analyse the same frame.** An average hash plus Hamming
   distance decides "the screen has not meaningfully changed", which is what
   makes a watching loop affordable: a static screen costs one hash, not one
   model call.
3. **Redact before sending.** Configured rectangles are painted out and
   sensitive applications (password managers, banking, private browsing) are
   refused outright -- the check lives here, not in the prompt, so a badly
   behaved model cannot talk its way past it.
4. **Keep a little history, then clean up after itself.** Frames are stored with
   a retention cap so a loop left running for a day cannot fill the disk.

Everything returns honest values and never raises past the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core import vision_client

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "screen_ai.json"
FRAME_DIR = BASE_DIR / "memory" / "screens"

__all__ = [
    "Capture", "capture", "capture_bytes", "ahash", "hamming", "same_screen",
    "privacy_verdict", "redact_regions", "describe", "read_text", "locate_text",
    "recent_frames", "cleanup", "active_window_title", "screen_size",
    "self_test",
]


# ── configuration ────────────────────────────────────────────────────────────

def _config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _capture_config() -> dict:
    value = _config().get("capture")
    return value if isinstance(value, dict) else {}


def _privacy_config() -> dict:
    value = _config().get("privacy")
    return value if isinstance(value, dict) else {}


def _frame_limit() -> int:
    return max(1, int(_capture_config().get("keep_frames", 12) or 12))


# ── frame retention on every capture ────────────────────────────────────────
#
# cleanup() below was only ever called by the watching loop, and only on a tick.
# But every *on-demand* capture writes a file too — a screenshot the user asked
# for, each verification frame in the automation loops — and nothing pruned
# those. Measured on this machine: 92 frames, 26 MB, while the module docstring
# promised "a retention cap so a loop left running for a day cannot fill the
# disk". The cap has to be enforced where the files are actually created.
#
# Throttled, because a verification loop can take several frames back-to-back
# and re-reading the directory for each of them buys nothing.
_LAST_CLEANUP = 0.0
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_EVERY_SECONDS = 10.0


def _maybe_cleanup() -> None:
    """Enforce the frame cap after writing a frame. Never raises."""
    global _LAST_CLEANUP
    now = time.time()
    with _CLEANUP_LOCK:
        if now - _LAST_CLEANUP < _CLEANUP_EVERY_SECONDS:
            return
        _LAST_CLEANUP = now
    try:
        cleanup()
    except Exception:                                          # pragma: no cover
        pass


# ── capture ──────────────────────────────────────────────────────────────────

@dataclass
class Capture:
    """One frame of the desktop, plus what we know about it."""

    path: str
    width: int = 0
    height: int = 0
    ahash: int = 0
    checksum: str = ""
    taken_at: float = 0.0
    source: str = ""
    duration: float = 0.0
    redacted: int = 0
    notes: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": f"{self.width}x{self.height}",
            "source": self.source,
            "seconds": round(self.duration, 2),
            "redacted_regions": self.redacted,
            "checksum": self.checksum,
            "notes": self.notes,
        }


def screen_size() -> tuple[int, int]:
    """Primary screen size in pixels, or ``(0, 0)`` when unknown."""
    try:
        from core import kali_compat  # noqa: PLC0415
        ok, path = kali_compat.screenshot()
        if ok:
            try:
                from PIL import Image  # noqa: PLC0415
                with Image.open(path) as image:
                    size = (int(image.width), int(image.height))
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return size
            except Exception:
                return 0, 0
    except Exception:
        pass
    try:
        import mss  # noqa: PLC0415
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return int(monitor["width"]), int(monitor["height"])
    except Exception:
        return 0, 0


def _shot_path(suffix: str = "png") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = f"{int(time.time() * 1000) % 1000:03d}"
    return str(FRAME_DIR / f"frame-{stamp}-{unique}.{suffix}")


def _capture_via_portal(target: str) -> tuple[bool, str]:
    """Preferred route on GNOME Wayland (portal writes the file itself)."""
    try:
        from core import kali_compat  # noqa: PLC0415
        return kali_compat.screenshot(target)
    except Exception as exc:
        return False, f"portal path failed: {exc}"


def _capture_via_mss(target: str, region: Optional[tuple[int, int, int, int]]) -> tuple[bool, str]:
    """Fallback for X11 sessions and desktops without a portal."""
    try:
        import mss  # noqa: PLC0415
        import mss.tools  # noqa: PLC0415
    except Exception as exc:
        return False, f"mss unavailable ({exc.__class__.__name__})"
    try:
        with mss.mss() as sct:
            if region:
                left, top, width, height = region
                box = {"left": int(left), "top": int(top),
                       "width": int(width), "height": int(height)}
            else:
                box = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(box)
            mss.tools.to_png(shot.rgb, shot.size, output=target)
        return (os.path.exists(target), "mss" if os.path.exists(target) else "mss produced nothing")
    except Exception as exc:
        return False, f"mss failed: {exc}"


def _capture_via_grim(target: str) -> tuple[bool, str]:
    if shutil.which("grim") is None:
        return False, "grim not installed"
    result = subprocess.run(["grim", target], capture_output=True, text=True,
                            timeout=10, check=False)
    return (result.returncode == 0 and os.path.exists(target),
            "grim" if result.returncode == 0 else (result.stderr or "grim failed")[:120])


# A tiny cache so a burst of callers in one turn (capture -> analyse -> verify)
# does not take three screenshots of a screen that has not moved.
_LAST_LOCK = threading.RLock()
_LAST_CAPTURE: Optional[Capture] = None


def capture(
    region: Optional[tuple[int, int, int, int]] = None,
    reuse_seconds: float = 0.0,
    destination: Optional[str] = None,
) -> tuple[Optional[Capture], str]:
    """Grab the screen. Returns ``(capture, reason-if-it-failed)``.

    ``reuse_seconds`` reuses a frame younger than that, which is how verification
    steps stay honest: with ``0`` you always get a fresh frame.
    """
    global _LAST_CAPTURE
    with _LAST_LOCK:
        if _LAST_CAPTURE and _LAST_CAPTURE.exists:
            age = time.time() - _LAST_CAPTURE.taken_at
            if reuse_seconds and age <= float(reuse_seconds):
                return _LAST_CAPTURE, ""
            # Two photographs of the same unchanged desktop, a fraction of a
            # second apart, are never what anybody meant. Without this, three
            # tools checking the screen in the same second produce three frames
            # and the user sees a storm; the newest one is just as true.
            if age <= MIN_FRESH_GAP_SECONDS:
                return _LAST_CAPTURE, ""

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    target = destination or _shot_path()
    started = time.monotonic()
    problems: list[str] = []

    ok, detail = _capture_via_portal(target)
    if not ok:
        problems.append(str(detail)[:140])
        if region:
            ok, detail = _capture_via_mss(target, region)
            if not ok:
                problems.append(str(detail)[:140])
        if not ok:
            ok, detail = _capture_via_grim(target)
            if not ok:
                problems.append(str(detail)[:140])

    if not ok or not os.path.exists(target):
        return None, "screenshot failed: " + "; ".join(problems)

    frame = Capture(
        path=target,
        taken_at=time.time(),
        duration=time.monotonic() - started,
        source=str(detail or "screenshot"),
    )
    frame.checksum = _file_checksum(target)
    width, height, hash_value = _measure(target)
    frame.width, frame.height, frame.ahash = width, height, hash_value

    if region:
        frame.notes = f"region {region}"

    masked = redact_regions(frame)
    if masked:
        frame.redacted = masked
        frame.checksum = _file_checksum(target)
        width, height, hash_value = _measure(target)
        frame.width, frame.height, frame.ahash = width, height, hash_value

    with _LAST_LOCK:
        _LAST_CAPTURE = frame
    _maybe_cleanup()
    return frame, ""


def capture_bytes(reuse_seconds: float = 0.0) -> tuple[Optional[bytes], Optional[Capture], str]:
    """Capture and return raw bytes ready for the model."""
    frame, reason = capture(reuse_seconds=reuse_seconds)
    if frame is None:
        return None, None, reason
    try:
        return Path(frame.path).read_bytes(), frame, ""
    except OSError as exc:
        return None, frame, f"could not read the screenshot: {exc}"


def _file_checksum(path: str) -> str:
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _measure(path: str) -> tuple[int, int, int]:
    """(width, height, average-hash) for a PNG on disk."""
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(path) as image:
            width, height = int(image.width), int(image.height)
        return width, height, ahash(path)
    except Exception:
        return 0, 0, 0


# ── perceptual hashing (change detection, not cryptography) ───────────────────

def ahash(source: Any, size: int = 8) -> int:
    """Average hash of an image: a 64-bit fingerprint robust to tiny changes.

    Known blind spot, deliberate: a *uniform* frame hashes to all-ones at any
    brightness, because every pixel equals the average. So a screen fading or a
    solid colour changing would read as "unchanged". Real desktops are never
    uniform (there is always text or a panel), and the checksum comparison in
    :func:`same_screen` catches byte-level differences first, but a caller that
    needs strict change detection on near-blank content should compare checksums
    rather than hashes.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return 0
    try:
        if hasattr(source, "convert"):
            image = source.convert("L")
        else:
            image = Image.open(source).convert("L")
        small = image.resize((size, size), Image.BILINEAR)
        pixels = list(small.getdata())
    except Exception:
        return 0
    if not pixels:
        return 0
    average = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= average:
            bits |= 1 << index
    return bits


def hamming(first: int, second: int) -> int:
    """Number of differing bits between two hashes."""
    return bin(int(first) ^ int(second)).count("1")


def same_screen(first: Capture, second: Capture, threshold: int = 6) -> bool:
    """True when two frames are close enough to be the same view."""
    if not first or not second or not first.ahash or not second.ahash:
        return False
    if first.checksum and first.checksum == second.checksum:
        return True
    if first.checksum and second.checksum and first.ahash == second.ahash:
        # Identical hashes but different bytes: near-blank/uniform content, where
        # the hash is blind by construction. Treat as changed rather than miss a
        # real difference; the cost is one extra model call, the alternative is
        # acting on a stale screen.
        return False
    return hamming(first.ahash, second.ahash) <= int(threshold)


# ── privacy ──────────────────────────────────────────────────────────────────

def _blocked_patterns() -> list[str]:
    configured = _privacy_config().get("blocked_apps")
    defaults = [
        "keepass", "keepassxc", "bitwarden", "1password", "lastpass", "enpass",
        "seahorse", "gnome-keyring", "password", "keychain",
        "bank", "banking", "paypal", "binance", "coinbase", "wise", "stripe",
        "private browsing", "incognito", "inprivate",
        "authenticator", "2fa", "otp",
    ]
    patterns = [str(p).casefold() for p in (configured if isinstance(configured, list) else defaults)]
    return [p for p in patterns if p.strip()]


def privacy_verdict(app: str = "", title: str = "", visible_text: str = "") -> tuple[bool, str]:
    """Should this screen be sent to a model? ``(allowed, reason)``.

    Checked in code rather than by asking the model, because this is exactly the
    decision a model should not be trusted to make about its own input.
    """
    config = _privacy_config()
    if config.get("block_sensitive_apps", True) is False:
        return True, ""
    haystack = " ".join([app or "", title or "", visible_text[:400] or ""]).casefold()
    if not haystack.strip():
        return True, ""
    for pattern in _blocked_patterns():
        if pattern and pattern in haystack:
            return False, (
                f"'{pattern}' detected on screen — refusing to analyse or reply "
                "around it (privacy.blocked_apps)"
            )
    return True, ""


def redact_regions(frame: Capture) -> int:
    """Paint configured rectangles black. Returns how many were applied.

    Redaction happens on the file that is about to be sent, so what the model
    receives genuinely does not contain those pixels -- this is not a promise in
    a prompt.
    """
    regions = _privacy_config().get("redact_regions")
    if not isinstance(regions, list) or not regions:
        return 0
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except Exception:
        return 0
    applied = 0
    try:
        with Image.open(frame.path) as image:
            canvas = image.convert("RGB")
            draw = ImageDraw.Draw(canvas)
            for entry in regions:
                if not isinstance(entry, dict):
                    continue
                try:
                    box = (
                        int(entry.get("left", 0)), int(entry.get("top", 0)),
                        int(entry.get("left", 0)) + int(entry.get("width", 0)),
                        int(entry.get("top", 0)) + int(entry.get("height", 0)),
                    )
                except (TypeError, ValueError):
                    continue
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                draw.rectangle(box, fill=(0, 0, 0))
                applied += 1
            if applied:
                canvas.save(frame.path, format="PNG")
    except Exception:
        return 0
    return applied


# ── active window ────────────────────────────────────────────────────────────

def active_window_title() -> str:
    """Title of the focused window, or ``""`` when the session will not say.

    Wayland deliberately has no API for this: a background process may not
    enumerate other clients' surfaces. Rather than guess, this returns an empty
    string and callers rely on the vision result, which does see the window.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return ""
    if shutil.which("xdotool") is None:
        return ""
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ── understanding ────────────────────────────────────────────────────────────

# The floor between two genuinely fresh captures, enforced at the one place
# every capture goes through. A second look a moment later reuses the frame that
# already exists instead of photographing the same pixels again.
MIN_FRESH_GAP_SECONDS = 1.25

_DESCRIBE_PROMPT = """You are the eyes of a desktop assistant. Read this screenshot.

Reply with JSON only, no commentary:
{
  "app": "the application or website in focus, e.g. WhatsApp Web / Firefox / VS Code",
  "window_title": "visible title bar text, or empty",
  "activity": "one short phrase: what the user appears to be doing",
  "summary": "1-2 sentences a human would say about this screen",
  "notable": ["anything that looks like an error, warning or unfinished task"],
  "visible_text": ["up to 12 short exact strings of text you can actually read"],
  "confidence": 0.0
}

Rules: only report what is genuinely legible. Never invent text. If the screen
is mostly unreadable or blank, say so in "summary" and set confidence low."""

_READ_TEXT_PROMPT = """Transcribe all readable text in this screenshot.

Reply with JSON only:
{"lines": ["exact line", "..."], "confidence": 0.0}

Keep lines in reading order. Skip pixels you cannot read with confidence
rather than guessing. Preserve the original language and spelling."""


def describe(frame: Optional[Capture] = None,
             reuse_seconds: float = 2.0,
             prompt: str = "") -> tuple[dict, vision_client.VisionResult, str]:
    """Understand the screen. Returns ``(analysis, result, capture_path)``.

    The returned dict carries the model's reading; ``result`` carries the
    transport truth (which model, how long, what went wrong). Keeping them
    separate is what lets an answer say "I could not see the screen" instead of
    presenting a guess as an observation.
    """
    if frame is None:
        frame, reason = capture(reuse_seconds=reuse_seconds)
        if frame is None:
            return {}, vision_client.VisionResult(ok=False, error=reason), ""

    analysis, result = vision_client.analyze_json(
        frame.path,
        prompt or _DESCRIBE_PROMPT,
        max_output_tokens=900,
    )
    if analysis is None:
        return {}, result, frame.path

    allowed, why = privacy_verdict(
        app=str(analysis.get("app") or ""),
        title=str(analysis.get("window_title") or ""),
        visible_text=" ".join(str(x) for x in (analysis.get("visible_text") or [])[:6]),
    )
    if not allowed:
        return {}, vision_client.VisionResult(ok=False, error=why, model=result.model,
                                              seconds=result.seconds), frame.path
    analysis["_capture"] = frame.as_dict()
    return analysis, result, frame.path


def read_text(frame: Optional[Capture] = None,
              reuse_seconds: float = 2.0) -> tuple[list[str], vision_client.VisionResult]:
    """Transcribe the screen, honestly omitting what it cannot read."""
    if frame is None:
        frame, reason = capture(reuse_seconds=reuse_seconds)
        if frame is None:
            return [], vision_client.VisionResult(ok=False, error=reason)
    data, result = vision_client.analyze_json(frame.path, _READ_TEXT_PROMPT, max_output_tokens=1200)
    if data is None:
        return [], result
    lines = data.get("lines")
    if not isinstance(lines, list):
        return [], result
    return [str(line) for line in lines if str(line).strip()], result


_LOCATE_PROMPT = """Find where this text appears on screen.

Target text: {target!r}

Reply with JSON only:
{{"found": true|false, "x": 0-1000, "y": 0-1000, "context": "what is next to it", "confidence": 0.0}}

Coordinates are normalised: x=0 is the left edge, x=1000 the right edge, same
for y top to bottom. Give the centre of the target. If it is not on screen, set
"found": false."""


def locate_text(target: str,
                frame: Optional[Capture] = None,
                reuse_seconds: float = 2.0) -> tuple[Optional[tuple[int, int]], dict, str]:
    """Find on-screen text and return pixel coordinates for clicking.

    This is what makes "click the Send button" possible without a screen
    automation library: the model reads the frame, says *where* the thing is,
    and the input bridge clicks there.
    """
    if not str(target or "").strip():
        return None, {}, "no target text supplied"
    if frame is None:
        frame, reason = capture(reuse_seconds=reuse_seconds)
        if frame is None:
            return None, {}, reason
    data, result = vision_client.analyze_json(
        frame.path, _LOCATE_PROMPT.format(target=target), max_output_tokens=250,
    )
    if data is None:
        return None, {}, result.error or "model could not read the screen"
    if not data.get("found"):
        return None, data, f"'{target}' is not visible on screen"
    try:
        nx = max(0, min(1000, float(data.get("x", 0))))
        ny = max(0, min(1000, float(data.get("y", 0))))
    except (TypeError, ValueError):
        return None, data, "model returned unusable coordinates"
    width, height = frame.width or screen_size()[0], frame.height or screen_size()[1]
    if not width or not height:
        return None, data, "screen size unknown, so coordinates cannot be resolved"
    return (int(nx / 1000 * width), int(ny / 1000 * height)), data, ""


# ── retention ────────────────────────────────────────────────────────────────

def recent_frames(limit: int = 6) -> list[dict]:
    """Newest frames first, as metadata (no image data)."""
    try:
        files = sorted(FRAME_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out: list[dict] = []
    for path in files[:max(1, int(limit))]:
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({
            "path": str(path),
            "kb": round(stat.st_size / 1024, 1),
            "age_seconds": round(time.time() - stat.st_mtime, 1),
        })
    return out


def cleanup(keep: Optional[int] = None) -> dict:
    """Delete old frames. Called by the watching loop; safe to call anytime."""
    keep = int(keep or _frame_limit())
    removed = 0
    freed = 0
    try:
        files = sorted(FRAME_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {"removed": 0, "kept": 0, "freed_kb": 0}
    for path in files[keep:]:
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            continue
    return {"removed": removed, "kept": min(len(files), keep), "freed_kb": round(freed / 1024, 1)}


# ── self-test ────────────────────────────────────────────────────────────────

def self_test(live: bool = False) -> dict:
    """Offline image/geometry checks; ``live=True`` also captures and reads."""
    checks: dict[str, Any] = {}

    checks["hamming_zero"] = hamming(0b1011, 0b1011) == 0
    checks["hamming_distance"] = hamming(0b0000, 0b1111) == 4

    try:
        from PIL import Image  # noqa: PLC0415
        flat = Image.new("L", (64, 64), color=10)
        brighter = Image.new("L", (64, 64), color=250)
        half = Image.new("L", (64, 64), color=10)
        for x in range(32):
            for y in range(32):
                half.putpixel((x, y), 250)
        checks["ahash_rejects_uniform_pairs"] = (
            hamming(ahash(flat), ahash(brighter)) == 0 and not same_screen(
                Capture(path="", ahash=ahash(flat), checksum="bytes-a"),
                Capture(path="", ahash=ahash(brighter), checksum="bytes-b"),
            )
        )
        checks["ahash_flat_stable"] = ahash(flat) == ahash(Image.new("L", (64, 64), color=10))
        checks["ahash_detects_change"] = hamming(ahash(flat), ahash(half)) > 0
        checks["same_screen_identical"] = same_screen(
            Capture(path="", ahash=ahash(flat), checksum="a"),
            Capture(path="", ahash=ahash(flat), checksum="a"),
        )
        # NOTE: a uniform image hashes to all-ones at *any* brightness (every
        # pixel equals the average), so the differing pair here must be a real
        # structural change -- `half` -- not merely a brighter frame.
        checks["ahash_uniform_blind"] = ahash(flat) == ahash(brighter)
        checks["same_screen_differs"] = not same_screen(
            Capture(path="", ahash=ahash(flat), checksum="a"),
            Capture(path="", ahash=ahash(half), checksum="b"),
        )
    except Exception:
        checks["ahash_flat_stable"] = False
        checks["ahash_detects_change"] = False
        checks["same_screen_identical"] = False
        checks["same_screen_differs"] = False

    allowed, _ = privacy_verdict(app="KeePassXC", title="Vault")
    checks["blocks_password_manager"] = not allowed
    allowed, _ = privacy_verdict(app="Firefox", title="Private Browsing")
    checks["blocks_private_browsing"] = not allowed
    allowed, _ = privacy_verdict(app="WhatsApp Web", title="Mimi")
    checks["allows_messaging"] = allowed

    try:
        import tempfile
        from PIL import Image as _Image  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.png")
            _Image.new("RGB", (80, 40), color=(200, 30, 30)).save(path)
            frame = Capture(path=path)
            before = _Image.open(path).getpixel((10, 10))
            # Temporarily drive redaction through a frame + patched config.
            original = _privacy_config
            globals()["_privacy_config"] = lambda: {
                "redact_regions": [{"left": 0, "top": 0, "width": 40, "height": 40}]
            }
            try:
                count = redact_regions(frame)
                after = _Image.open(path).getpixel((10, 10))
            finally:
                globals()["_privacy_config"] = original
            checks["redact_applies"] = count == 1 and after == (0, 0, 0) and before != after
            checks["redact_leaves_rest"] = _Image.open(path).getpixel((70, 30)) == (200, 30, 30)
    except Exception:
        checks["redact_applies"] = False
        checks["redact_leaves_rest"] = False

    checks["privacy_default"] = privacy_verdict(app="Nonexistent App 123")[0]

    if live:
        frame, reason = capture()
        checks["live_capture"] = frame is not None
        checks["live_capture_reason"] = reason
        if frame is not None:
            checks["live_capture_seconds"] = round(frame.duration, 2)
            checks["live_capture_size"] = f"{frame.width}x{frame.height}"
            analysis, result, _ = describe(frame)
            checks["live_describe"] = bool(analysis)
            checks["live_app"] = str(analysis.get("app", ""))[:60]
            checks["live_model"] = result.model
            checks["live_seconds"] = round(result.seconds, 2)
            checks["live_error"] = result.error

    checks["ok"] = all(bool(value) for key, value in checks.items()
                       if not key.startswith("live_"))
    return checks


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys
    print(json.dumps(self_test(live="--live" in sys.argv), indent=2))
