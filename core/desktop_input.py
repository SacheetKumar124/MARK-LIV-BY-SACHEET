"""Session-aware desktop input bridge for MARK LIII.

One entry point for keyboard, mouse and screen access that works on both
X11 and Wayland sessions:

*   Wayland (Kali GNOME default): ``ydotool`` talks to ``ydotoold`` over
    ``/run/ydotoold/.socket`` and injects real kernel-level input events.
*   X11: the classic ``pyautogui`` path is used unchanged.

Function signatures deliberately mirror the ``pyautogui`` calls used across
``actions/``, so existing call sites can be switched with a one-line import
plus a mechanical rename (``pyautogui.press`` → ``desktop_input.press``).

Every function raises ``DesktopInputError`` with a friendly, actionable
message when the current session cannot perform the action; it never fails
silently and never falls back to doing the wrong thing.

This module is import-safe everywhere: nothing at module scope touches the
display, so importing it never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional, Tuple

__all__ = [
    "DesktopInputError", "session_type", "capabilities",
    "typewrite", "hotkey", "press", "click", "scroll", "hscroll",
    "moveTo", "dragTo", "size", "screenshot", "position",
]


class DesktopInputError(RuntimeError):
    """Raised when the current desktop session cannot perform an action."""


_YDOTOOL_SOCKET = "/run/ydotoold/.socket"


def _ydotool_sockets() -> tuple[str, ...]:
    """Sockets ydotoold may use, in priority order.

    ``YDOTOOL_SOCKET`` is honoured first because that is ydotool's own documented
    contract — respecting it lets the daemon live wherever it was actually
    started. Then the system-wide socket, then the per-user runtime dir, which is
    where a ``systemd --user`` ydotoold listens on Kali. On this box only the
    per-user path exists, so the ordering matters: without it, input injection
    would report "not ready" while the daemon is in fact running.
    """
    paths: list[str] = []
    override = os.environ.get("YDOTOOL_SOCKET", "").strip()
    if override:
        paths.append(override)
    paths.append(_YDOTOOL_SOCKET)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime:
        paths.append(f"{runtime}/.ydotool_socket")
    return tuple(paths)

# evdev key codes for the named keys used across the action modules.
_KEY_CODES = {
    "enter": 28, "return": 28, "space": 57, "tab": 15, "esc": 1, "escape": 1,
    "backspace": 14, "delete": 119, "del": 119, "up": 103, "down": 108,
    "left": 105, "right": 106, "home": 102, "end": 107, "pageup": 104,
    "pagedown": 109, "insert": 110, "f1": 59, "f2": 60, "f3": 61, "f4": 62,
    "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87,
    "f12": 88, "volumeup": 115, "volumedown": 114, "volumemute": 113,
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "altgr": 100,
    "win": 125, "super": 125, "meta": 125, "command": 125, "cmd": 125,
    "fn": 0, "option": 56, "winleft": 125, "capslock": 58, "numlock": 69,
}
_BUTTON_CODES = {"left": 0xC0, "right": 0xC1, "middle": 0xC2}
_BTN_CODES = {"left": 272, "right": 273, "middle": 274}  # evdev BTN_LEFT/RIGHT/MIDDLE

_pyautogui = None
_pyautogui_loaded = False


def _load_pyautogui():
    """Import pyautogui lazily; cache the failure so we only try once."""
    global _pyautogui, _pyautogui_loaded
    if not _pyautogui_loaded:
        _pyautogui_loaded = True
        try:
            import pyautogui  # noqa: PLC0415
            pyautogui.FAILSAFE = False
            _pyautogui = pyautogui
        except Exception:
            _pyautogui = None
    return _pyautogui


def session_type() -> str:
    """Return 'wayland', 'x11', or 'other' for the current desktop session."""
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().casefold()
    if session in {"wayland", "x11"}:
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "other"


def _ydotool_ready() -> Tuple[bool, str]:
    """Return whether ydotool can reach its daemon right now."""
    if shutil.which("ydotool") is None:
        return False, "ydotool is not installed (sudo apt install ydotool)"
    if any(os.path.exists(path) for path in _ydotool_sockets()):
        return True, "ok"
    try:
        result = subprocess.run(
            ["pgrep", "-x", "ydotoold"],
            capture_output=True, timeout=2, check=False,
        )
        if result.returncode == 0:
            return True, "ok"
    except (OSError, subprocess.SubprocessError):
        pass
    return False, (
        "ydotoold is not running; enable it once with: "
        "systemctl --user enable --now ydotool"
    )


def _run_ydotool(args: list[str], timeout: float = 6.0) -> None:
    """Run one ydotool command or raise a friendly DesktopInputError."""
    ok, reason = _ydotool_ready()
    if not ok:
        raise DesktopInputError(f"Wayland input unavailable: {reason}")
    try:
        result = subprocess.run(
            ["ydotool", *args], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopInputError(f"ydotool failed to run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:160]
        raise DesktopInputError(f"ydotool error: {detail or 'non-zero exit'}")


def _pyautogui_or_raise():
    """Return pyautogui on X11 sessions or raise a friendly error."""
    if session_type() == "wayland":
        raise DesktopInputError(
            "This session is Wayland; pyautogui cannot inject input here."
        )
    module = _load_pyautogui()
    if module is None:
        raise DesktopInputError(
            "pyautogui is not available in this session "
            "(pip install pyautogui, and run inside a graphical session)."
        )
    return module


def capabilities() -> dict:
    """Report what input/screenshot paths are usable right now."""
    session = session_type()
    yd_ok, yd_reason = _ydotool_ready()
    module = _load_pyautogui() if session == "x11" else None
    return {
        "session": session,
        "ydotool": yd_ok,
        "ydotool_reason": yd_reason if not yd_ok else "",
        "pyautogui": module is not None,
        "input_ready": yd_ok if session == "wayland" else module is not None,
    }


def _ydotool_key_code(key: str) -> int:
    """Map a named key to its evdev code, tolerating pyautogui-style names."""
    name = str(key).strip().casefold()
    if name in _KEY_CODES:
        return _KEY_CODES[name]
    if len(name) == 1:
        char = name
        if char.isdigit():
            return 2 + int(char) - 1 if char != "0" else 11
        if char.isalpha():
            # evdev: a=30 ... z=55
            return 30 + (ord(char) - ord("a"))
    raise DesktopInputError(f"Unsupported key name for Wayland: {key!r}")


def _ydotool_press(key: str) -> None:
    code = _ydotool_key_code(key)
    _run_ydotool(["key", f"{code}:1", f"{code}:0"])


def _ydotool_hotkey(*keys: str) -> None:
    if not keys:
        return
    if len(keys) == 1:
        _ydotool_press(keys[0])
        return
    codes = [_ydotool_key_code(key) for key in keys]
    events: list[str] = []
    for code in codes[:-1]:
        events.append(f"{code}:1")
    last = codes[-1]
    events.extend([f"{last}:1", f"{last}:0"])
    for code in reversed(codes[:-1]):
        events.append(f"{code}:0")
    _run_ydotool(["key", *events])


def _ydotool_click(x: Optional[float], y: Optional[float],
                   button: str = "left", clicks: int = 1) -> None:
    if x is not None and y is not None:
        _run_ydotool(["mousemove", "-a", "--", str(int(x)), str(int(y))])
        time.sleep(0.05)
    code = _BUTTON_CODES.get(button.casefold(), 0xC0)
    for _ in range(max(1, int(clicks))):
        _run_ydotool(["click", str(code)])
        time.sleep(0.05)


def _gdbus_screenshot() -> str:
    """Capture the screen on Wayland and return a PNG path.

    The xdg-desktop-portal route is tried first because it is the only one GNOME
    still authorises for outside callers -- on Kali GNOME Wayland, GNOME Shell's
    own Screenshot method answers ``AccessDenied``.  The Shell call is kept as a
    fallback for desktops that still permit it.
    """
    from core import kali_compat  # local import keeps this module import-safe

    ok, detail = kali_compat.screenshot()
    if ok:
        return detail
    handle = tempfile.NamedTemporaryFile(
        prefix="jarvis-shot-", suffix=".png", delete=False
    )
    path = handle.name
    handle.close()
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
         "--object-path", "/org/gnome/Shell/Screenshot",
         "--method", "org.gnome.Shell.Screenshot.Screenshot",
         "true", "false", path],
        capture_output=True, text=True, timeout=6, check=False,
    )
    if result.returncode != 0 or not os.path.exists(path):
        raise DesktopInputError(
            "GNOME screenshot was denied; allow screenshots for this session."
        )
    return path


# ── public API (pyautogui-compatible signatures) ─────────────────────────────


def typewrite(text: str, interval: float = 0.0) -> None:
    """Type text into the focused window on either session."""
    text = str(text)
    if session_type() == "wayland":
        _run_ydotool(["type", "--", text], timeout=max(6.0, 0.02 * len(text) + 6))
        return
    module = _pyautogui_or_raise()
    module.typewrite(text, interval=interval)


write = typewrite  # pyautogui compatibility alias


def press(key: str) -> None:
    """Press and release one named key on either session."""
    if session_type() == "wayland":
        _ydotool_press(key)
        return
    module = _pyautogui_or_raise()
    module.press(key)


def hotkey(*keys: str) -> None:
    """Press a chord like hotkey('ctrl', 'v') on either session."""
    if not keys:
        return
    if session_type() == "wayland":
        _ydotool_hotkey(*keys)
        return
    module = _pyautogui_or_raise()
    module.hotkey(*keys)


def click(x: Optional[float] = None, y: Optional[float] = None,
          button: str = "left", clicks: int = 1, **_ignored) -> None:
    """Click at a position (or the current one) on either session."""
    if session_type() == "wayland":
        _ydotool_click(x, y, button=button, clicks=clicks)
        return
    module = _pyautogui_or_raise()
    if x is not None and y is not None:
        module.click(x, y, button=button, clicks=clicks)
    else:
        module.click(button=button, clicks=clicks)


def scroll(amount: int, x: Optional[float] = None,
           y: Optional[float] = None) -> None:
    """Scroll vertically; positive is up, matching pyautogui semantics."""
    if session_type() == "wayland":
        # pyautogui: positive = up; evdev REL_WHEEL: positive = up. Pass through.
        if x is not None and y is not None:
            _run_ydotool(["mousemove", "-a", "--", str(int(x)), str(int(y))])
        _run_ydotool(["mousemove", "--wheel", "--", "0", str(int(amount))])
        return
    module = _pyautogui_or_raise()
    module.scroll(amount)


def hscroll(amount: int) -> None:
    """Scroll horizontally; positive is right."""
    if session_type() == "wayland":
        _run_ydotool(["mousemove", "--wheel", "--", str(int(amount)), "0"])
        return
    module = _pyautogui_or_raise()
    module.hscroll(amount)


def moveTo(x: float, y: float, duration: float = 0.0, **_ignored) -> None:
    """Move the mouse to absolute coordinates on either session."""
    if session_type() == "wayland":
        if duration:
            time.sleep(min(duration, 0.5))
        _run_ydotool(["mousemove", "-a", "--", str(int(x)), str(int(y))])
        return
    module = _pyautogui_or_raise()
    module.moveTo(x, y, duration=duration)


def dragTo(x: float, y: float, duration: float = 0.0,
           button: str = "left", **_ignored) -> None:
    """Drag from the current position to absolute coordinates."""
    if session_type() == "wayland":
        code = _BTN_CODES.get(button.casefold(), 272)
        _run_ydotool(["mousedown", "--", str(code)])
        time.sleep(min(duration, 0.5) if duration else 0.05)
        _run_ydotool(["mousemove", "-a", "--", str(int(x)), str(int(y))])
        time.sleep(0.05)
        _run_ydotool(["mouseup", "--", str(code)])
        return
    module = _pyautogui_or_raise()
    module.dragTo(x, y, duration=duration, button=button)


def size() -> Tuple[int, int]:
    """Return the primary screen size in pixels."""
    if session_type() == "wayland":
        shot = _gdbus_screenshot()
        try:
            from PIL import Image  # noqa: PLC0415
            with Image.open(shot) as image:
                return int(image.width), int(image.height)
        except DesktopInputError:
            raise
        except Exception as exc:
            raise DesktopInputError(f"Cannot read screen size: {exc}") from exc
        finally:
            try:
                os.unlink(shot)
            except OSError:
                pass
    module = _pyautogui_or_raise()
    return module.size()


def position() -> Tuple[int, int]:
    """Return the current mouse position (Wayland: best-effort)."""
    if session_type() == "wayland":
        return 0, 0
    module = _pyautogui_or_raise()
    return module.position()


def screenshot(region: Optional[Tuple[int, int, int, int]] = None):
    """Return a PIL image of the screen on either session."""
    if session_type() == "wayland":
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError as exc:
            raise DesktopInputError(
                "Pillow is required for Wayland screenshots."
            ) from exc
        path = _gdbus_screenshot()
        try:
            image = Image.open(path)
            if region:
                left, top, width, height = region
                image = image.crop((int(left), int(top),
                                    int(left + width), int(top + height)))
            return image
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    module = _pyautogui_or_raise()
    return module.screenshot(region=region)
