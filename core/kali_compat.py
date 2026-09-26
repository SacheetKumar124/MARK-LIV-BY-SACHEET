"""Session-aware system integration for Kali Linux (and any Linux desktop).

Why this module exists
----------------------
Kali Rolling ships a different desktop stack from the one the OS actions were
originally written against.  Verified on this machine (Kali 2026.2, GNOME,
Wayland):

* audio is **PipeWire** -- ``wpctl`` answers, ``pactl`` is not installed
* ``brightnessctl`` is not installed and ``/sys/class/backlight/*/brightness``
  is root-only, but logind's polkit-authorized ``SetBrightness`` works and needs
  no root and no extra package
* the session is **Wayland**, so ``scrot``, ``import`` and ``xdotool`` cannot
  see the screen, and GNOME Shell's own ``org.gnome.Shell.Screenshot`` method
  answers ``AccessDenied``
* the supported capture route on GNOME Wayland is the **xdg-desktop-portal**
  Screenshot API, which returns a real file URI (confirmed working here)
* clipboard tools are ``wl-copy`` / ``wl-paste`` rather than ``xclip``

Every public function here walks an ordered backend cascade and returns
``(ok, detail)`` instead of raising, so a caller can report *what actually
happened* rather than what it hoped would happen.  ``detail`` names the backend
that answered, which is what makes Jarvis's answers honest on an unfamiliar box.

Nothing in this module requires root, and nothing writes outside the user's own
home directory or the session bus.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "session_type", "backend_report",
    "volume_get", "volume_set", "volume_step", "mute_toggle",
    "brightness_get", "brightness_set", "brightness_step",
    "screenshot", "clipboard_copy", "clipboard_paste",
    "notify", "focus_window", "window_action", "lock_screen", "backlight_device",
]

_TIMEOUT = 6


# ── process helpers ──────────────────────────────────────────────────────────

def _has(binary: str) -> bool:
    """True when *binary* is on PATH."""
    return shutil.which(binary) is not None


def _run(argv: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    """Run one argument list without a shell; never raises."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, -1, "", str(exc))


def _ok(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == 0


def session_type() -> str:
    """Return ``"wayland"``, ``"x11"`` or ``"none"`` for this session."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    if os.environ.get("XDG_SESSION_TYPE", "").casefold() == "x11":
        return "x11"
    return "none"


def _clamp_percent(value: Any) -> Optional[int]:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


# ── volume: wpctl (PipeWire) -> pactl -> pamixer ─────────────────────────────

_SINK = "@DEFAULT_AUDIO_SINK@"
_PACTL_SINK = "@DEFAULT_SINK@"


def _wpctl_volume() -> Optional[int]:
    if not _has("wpctl"):
        return None
    match = re.search(r"([0-9.]+)", _run(["wpctl", "get-volume", _SINK]).stdout)
    return _clamp_percent(float(match.group(1)) * 100) if match else None


def _pactl_volume() -> Optional[int]:
    if not _has("pactl"):
        return None
    match = re.search(r"(\d+)%", _run(["pactl", "get-sink-volume", _PACTL_SINK]).stdout)
    return _clamp_percent(match.group(1)) if match else None


def _pamixer_volume() -> Optional[int]:
    if not _has("pamixer"):
        return None
    return _clamp_percent(_run(["pamixer", "--get-volume"]).stdout.strip())


def volume_get() -> Optional[int]:
    """Master output volume 0-100, or ``None`` when no backend can read it."""
    for probe in (_wpctl_volume, _pactl_volume, _pamixer_volume):
        value = probe()
        if value is not None:
            return value
    return None


def volume_set(value: Any) -> tuple[bool, str]:
    """Set the master output volume to an absolute percentage."""
    percent = _clamp_percent(value)
    if percent is None:
        return False, f"Not a usable volume value: {value!r}"

    if _has("wpctl"):
        # wpctl takes a linear factor, e.g. 0.55 for 55%.
        result = _run(["wpctl", "set-volume", _SINK, f"{percent / 100:.2f}"])
        if _ok(result):
            return True, f"wpctl (PipeWire) -> {percent}%"
    if _has("pactl"):
        result = _run(["pactl", "set-sink-volume", _PACTL_SINK, f"{percent}%"])
        if _ok(result):
            return True, f"pactl -> {percent}%"
    if _has("pamixer"):
        result = _run(["pamixer", "--set-volume", str(percent)])
        if _ok(result):
            return True, f"pamixer -> {percent}%"
    return False, (
        "No working audio backend. On Kali GNOME install pipewire-tools "
        "(wpctl) or pulseaudio-utils (pactl)."
    )


def volume_step(delta: int) -> tuple[bool, str]:
    """Raise or lower the master volume by *delta* percentage points."""
    delta = int(delta)
    if _has("wpctl"):
        # wpctl honours a trailing %+ / %- for relative changes.
        arg = f"{abs(delta)}%{'+' if delta >= 0 else '-'}"
        result = _run(["wpctl", "set-volume", _SINK, arg])
        if _ok(result):
            return True, f"wpctl (PipeWire) {arg}"
    if _has("pactl"):
        arg = f"{'+' if delta >= 0 else '-'}{abs(delta)}%"
        result = _run(["pactl", "set-sink-volume", _PACTL_SINK, arg])
        if _ok(result):
            return True, f"pactl {arg}"
    if _has("pamixer"):
        flag = "--increase" if delta >= 0 else "--decrease"
        result = _run(["pamixer", flag, str(abs(delta))])
        if _ok(result):
            return True, f"pamixer {flag} {abs(delta)}"
    return False, "No working audio backend for a relative volume change."


def mute_toggle() -> tuple[bool, str]:
    """Toggle output mute on whichever audio backend is available."""
    if _has("wpctl"):
        result = _run(["wpctl", "set-mute", _SINK, "toggle"])
        if _ok(result):
            return True, "wpctl (PipeWire) toggled mute"
    if _has("pactl"):
        result = _run(["pactl", "set-sink-mute", _PACTL_SINK, "toggle"])
        if _ok(result):
            return True, "pactl toggled mute"
    if _has("pamixer"):
        result = _run(["pamixer", "--toggle-mute"])
        if _ok(result):
            return True, "pamixer toggled mute"
    return False, "No working audio backend for mute."


# ── brightness: logind (polkit, no root) -> brightnessctl -> sysfs ───────────

def backlight_device() -> Optional[str]:
    """Name of the first backlight device, e.g. ``intel_backlight``."""
    for path in sorted(glob.glob("/sys/class/backlight/*/brightness")):
        return Path(path).parent.name
    return None


def _sysfs_read(device: str) -> Optional[tuple[int, int]]:
    base = Path("/sys/class/backlight") / device
    try:
        current = int((base / "brightness").read_text(encoding="ascii").strip())
        maximum = int((base / "max_brightness").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return (current, maximum) if maximum > 0 else None


def _logind_set_brightness(device: str, raw_value: int) -> bool:
    """Set brightness through logind -- polkit-authorized, no root needed."""
    argv = [
        "busctl", "call", "org.freedesktop.login1",
        "/org/freedesktop/login1/session/auto",
        "org.freedesktop.login1.Session", "SetBrightness",
        "ssu", "backlight", device, str(int(raw_value)),
    ]
    if _has("busctl") and _ok(_run(argv)):
        return True
    if _has("gdbus"):
        return _ok(_run([
            "gdbus", "call", "--system", "--dest", "org.freedesktop.login1",
            "--object-path", "/org/freedesktop/login1/session/auto",
            "--method", "org.freedesktop.login1.Session.SetBrightness",
            "backlight", device, str(int(raw_value)),
        ]))
    return False


def brightness_get() -> Optional[int]:
    """Screen brightness 0-100, or ``None`` where it cannot be read."""
    device = backlight_device()
    if device:
        reading = _sysfs_read(device)
        if reading:
            current, maximum = reading
            return _clamp_percent(current * 100 / maximum)
    if _has("brightnessctl"):
        value = _run(["brightnessctl", "get"]).stdout.strip()
        maximum = _run(["brightnessctl", "max"]).stdout.strip()
        try:
            return _clamp_percent(int(value) * 100 / int(maximum))
        except (ValueError, ZeroDivisionError):
            return None
    if _has("light"):
        return _clamp_percent(_run(["light", "-G"]).stdout.strip())
    return None


def brightness_set(value: Any) -> tuple[bool, str]:
    """Set screen brightness to an absolute percentage."""
    percent = _clamp_percent(value)
    if percent is None:
        return False, f"Not a usable brightness value: {value!r}"

    device = backlight_device()
    if device:
        reading = _sysfs_read(device)
        if reading:
            _current, maximum = reading
            raw = max(1, int(round(maximum * percent / 100)))
            if _logind_set_brightness(device, raw):
                return True, f"logind SetBrightness ({device}) -> {percent}%"

    if _has("brightnessctl"):
        result = _run(["brightnessctl", "set", f"{percent}%"])
        if _ok(result):
            return True, f"brightnessctl -> {percent}%"
    if _has("light"):
        result = _run(["light", "-S", str(percent)])
        if _ok(result):
            return True, f"light -> {percent}%"

    if device:
        return False, (
            f"Brightness is not writable for {device}. Install brightnessctl "
            "(apt install brightnessctl) or use a polkit rule for logind."
        )
    return False, "No backlight device found, so brightness cannot be changed."


def brightness_step(delta: int) -> tuple[bool, str]:
    """Raise or lower screen brightness by *delta* percentage points."""
    current = brightness_get()
    if current is None:
        return False, "Brightness is not readable on this machine."
    return brightness_set(current + int(delta))


# ── screenshots: portal -> GNOME Shell -> grim -> legacy X11 tools ───────────

def _portal_screenshot(timeout: int = 20) -> tuple[Optional[str], str]:
    """Capture via xdg-desktop-portal; the supported GNOME Wayland route."""
    try:
        import dbus                      # noqa: PLC0415
        import dbus.mainloop.glib        # noqa: PLC0415
        from gi.repository import GLib   # noqa: PLC0415
    except Exception as exc:             # pragma: no cover - depends on host
        return None, f"portal backend unavailable ({exc.__class__.__name__})"

    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        sender = bus.get_unique_name().lstrip(":").replace(".", "_")
        token = f"jarvis{os.getpid()}"
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        loop = GLib.MainLoop()
        state: dict[str, Any] = {}

        def _on_response(response, results):
            state["response"] = int(response)
            state["results"] = {str(k): str(v) for k, v in results.items()}
            loop.quit()

        bus.add_signal_receiver(
            _on_response, signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request", path=handle,
        )
        portal = bus.get_object(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        )
        options = dbus.Dictionary(
            {
                "handle_token": dbus.String(token),
                "interactive": dbus.Boolean(False),
                "modal": dbus.Boolean(False),
            },
            signature="sv",
        )
        dbus.Interface(portal, "org.freedesktop.portal.Screenshot").Screenshot(
            "", options,
        )
        GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
        loop.run()
    except Exception as exc:
        return None, f"portal call failed: {exc}"

    if state.get("response") != 0:
        return None, (
            "the screenshot request was refused "
            f"(portal response {state.get('response')}); a desktop prompt may be waiting"
        )
    uri = (state.get("results") or {}).get("uri", "")
    if not uri.startswith("file://"):
        return None, f"portal returned no usable file: {uri!r}"

    from urllib.parse import unquote, urlparse  # noqa: PLC0415
    return unquote(urlparse(uri).path), "xdg-desktop-portal"


def _gnome_shell_screenshot(path: str) -> tuple[Optional[str], str]:
    if not _has("gdbus"):
        return None, "gdbus not installed"
    result = _run([
        "gdbus", "call", "--session", "--dest", "org.gnome.Shell",
        "--object-path", "/org/gnome/Shell/Screenshot",
        "--method", "org.gnome.Shell.Screenshot.Screenshot",
        "true", "false", path,
    ], timeout=8)
    if _ok(result) and os.path.exists(path):
        return path, "gnome-shell dbus"
    return None, "GNOME Shell refused the capture (it only trusts the portal now)"


def _grim_screenshot(path: str) -> tuple[Optional[str], str]:
    if not _has("grim"):
        return None, "grim not installed"
    if _ok(_run(["grim", path], timeout=10)) and os.path.exists(path):
        return path, "grim"
    return None, "grim failed (it needs a wlroots compositor, not GNOME)"


def _legacy_screenshot(path: str) -> tuple[Optional[str], str]:
    for argv, label in (
        (["gnome-screenshot", "-f", path], "gnome-screenshot"),
        (["scrot", path], "scrot"),
        (["import", "-window", "root", path], "ImageMagick import (X11)"),
    ):
        if not _has(argv[0]):
            continue
        if _ok(_run(argv, timeout=10)) and os.path.exists(path):
            return path, label
    return None, "no legacy capture tool worked"


def screenshot(destination: Optional[str] = None) -> tuple[bool, str]:
    """Capture the screen and return ``(ok, path-or-reason)``.

    The cascade is ordered by what actually works on a modern Kali GNOME
    Wayland session, not by what is oldest.
    """
    if destination is None:
        destination = os.path.join(
            os.path.expanduser("~"), "Pictures",
            f"jarvis-{int(__import__('time').time())}.png",
        )
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.stem + "-try.png")

    problems: list[str] = []
    for backend in (_portal_screenshot,):
        path, note = backend()
        if path:
            try:
                if str(Path(path)) != str(target):
                    shutil.move(path, target)
            except OSError as exc:
                return False, f"captured but could not move the file: {exc}"
            return True, str(target)
        problems.append(note)

    for backend in (_gnome_shell_screenshot, _grim_screenshot, _legacy_screenshot):
        path, note = backend(str(scratch))  # type: ignore[arg-type]
        if path:
            try:
                shutil.move(str(scratch), target)
            except OSError as exc:
                return False, f"captured but could not move the file: {exc}"
            return True, str(target)
        problems.append(note)

    return False, "Screenshot failed. " + "; ".join(problems)


# ── clipboard ────────────────────────────────────────────────────────────────

def clipboard_copy(text: str) -> tuple[bool, str]:
    """Put *text* on the clipboard using whatever this session provides."""
    payload = "" if text is None else str(text)
    if session_type() == "wayland" and _has("wl-copy"):
        try:
            # A Wayland selection is served by the process that set it, so
            # wl-copy deliberately stays resident after returning.  Its stdio is
            # detached here so it can never hold this process's stdout/stderr
            # open, which would hang any caller that pipes our output.
            process = subprocess.run(
                ["wl-copy"], input=payload, text=True, timeout=_TIMEOUT, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if process.returncode == 0:
                return True, "wl-copy"
        except (OSError, subprocess.SubprocessError):
            pass
    if _has("xclip"):
        try:
            process = subprocess.run(
                ["xclip", "-selection", "clipboard"], input=payload, text=True,
                timeout=_TIMEOUT, check=False,
            )
            if process.returncode == 0:
                return True, "xclip"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import pyperclip  # noqa: PLC0415
        pyperclip.copy(payload)
        return True, "pyperclip"
    except Exception as exc:
        return False, f"no clipboard backend worked: {exc}"


def clipboard_paste() -> tuple[bool, str]:
    """Read the clipboard back; returns ``(ok, text-or-reason)``."""
    if session_type() == "wayland" and _has("wl-paste"):
        result = _run(["wl-paste", "-n"], timeout=_TIMEOUT)
        if _ok(result):
            return True, result.stdout
    if _has("xclip"):
        result = _run(["xclip", "-selection", "clipboard", "-o"], timeout=_TIMEOUT)
        if _ok(result):
            return True, result.stdout
    try:
        import pyperclip  # noqa: PLC0415
        return True, pyperclip.paste()
    except Exception as exc:
        return False, f"no clipboard backend worked: {exc}"


# ── desktop notification ─────────────────────────────────────────────────────

def notify(title: str, body: str = "", urgency: str = "normal") -> tuple[bool, str]:
    """Send a desktop notification.

    ``notify-send`` is tried first, but it is genuinely broken on some Kali
    installs (a libnotify/libnotify-bin version skew raises
    ``symbol lookup error: undefined symbol
    notify_notification_get_activation_app_launch_context``), so the notification
    daemon's own D-Bus API is used as the fallback.
    """
    if _has("notify-send"):
        result = _run(
            ["notify-send", "-a", "MARK LIII", "-u", urgency, str(title), str(body or "")],
        )
        if _ok(result):
            return True, "notify-send"
        note = (result.stderr or "").strip().splitlines()
        first = note[0] if note else "notify-send failed"
    else:
        first = "notify-send is not installed (apt install libnotify-bin)"

    if _has("gdbus"):
        result = _run([
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            "MARK LIII", "0", "", str(title), str(body or ""), "[]", "{}", "8000",
        ])
        if _ok(result):
            return True, "notifications dbus"

    return False, first[:160]


# ── window focus (honest about Wayland's limits) ─────────────────────────────

def focus_window(title: str) -> tuple[bool, str]:
    """Try to focus a window by title.

    X11: ``wmctrl`` then ``xdotool``.  Wayland: there is no supported way for an
    unprivileged client to raise another application's window, so this reports
    that plainly instead of pretending to have worked.
    """
    if not title:
        return False, "focus_window needs a window title"

    if session_type() == "wayland":
        if _has("wmctrl"):
            result = _run(["wmctrl", "-a", title])
            if _ok(result):
                return True, "wmctrl (XWayland window)"
        return False, (
            "Wayland does not let a background app raise another window; focus it "
            "with the compositor (Super+Tab) or run the app under XWayland."
        )

    for argv, label in (
        (["wmctrl", "-a", title], "wmctrl"),
        (["xdotool", "search", "--name", title, "windowactivate"], "xdotool"),
    ):
        if _has(argv[0]) and _ok(_run(argv)):
            return True, label
    return False, "No X11 window manager tool succeeded (install wmctrl or xdotool)."


# GNOME's own window keybindings, which are the supported way to move windows on
# a Wayland session where no client may reposition another client's window.
_WINDOW_KEYS = {
    "maximize": ("win", "up"),
    "restore": ("win", "down"),
    "unmaximize": ("win", "down"),
    "left": ("win", "left"),
    "right": ("win", "right"),
}

_X11_WINDOW_OPS = {
    "maximize": ["wmctrl", "-r", ":ACTIVE:", "-b", "add,maximized_vert,maximized_horz"],
    "restore": ["wmctrl", "-r", ":ACTIVE:", "-b", "remove,maximized_vert,maximized_horz"],
    "unmaximize": ["wmctrl", "-r", ":ACTIVE:", "-b", "remove,maximized_vert,maximized_horz"],
}


def window_action(action: str) -> tuple[bool, str]:
    """Maximize, restore or tile the focused window.

    On Wayland this drives GNOME's own keybindings through the ydotool bridge,
    because ``wmctrl`` cannot manage a Wayland surface at all -- the previous
    implementation called it and swallowed the failure, so "maximize" quietly did
    nothing on this desktop.
    """
    key = str(action or "").strip().casefold()
    if key not in _WINDOW_KEYS:
        return False, f"Unknown window action: {action!r}"

    if session_type() == "wayland":
        try:
            from core import desktop_input  # lazy: keeps this module import-safe
            desktop_input.hotkey(*_WINDOW_KEYS[key])
        except Exception as exc:
            return False, (
                "Wayland window control needs the ydotool bridge "
                f"({exc.__class__.__name__}: {exc})"
            )
        return True, f"ydotool -> GNOME keybinding {'+'.join(_WINDOW_KEYS[key])}"

    argv = _X11_WINDOW_OPS.get(key)
    if argv and _has(argv[0]) and _ok(_run(argv)):
        return True, "wmctrl"
    return False, "No working window-management tool on X11 (install wmctrl)."


def lock_screen() -> tuple[bool, str]:
    """Lock the session on either session type."""
    if _has("loginctl"):
        result = _run(["loginctl", "lock-session"])
        if _ok(result):
            return True, "loginctl"
    if _has("xdg-screensaver"):
        result = _run(["xdg-screensaver", "lock"])
        if _ok(result):
            return True, "xdg-screensaver"
    if session_type() == "wayland" and _has("gdbus"):
        result = _run([
            "gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
            "--object-path", "/org/gnome/ScreenSaver",
            "--method", "org.gnome.ScreenSaver.Lock",
        ])
        if _ok(result):
            return True, "gnome-screensaver dbus"
    return False, "No working screen-lock method on this session."


# ── reporting ────────────────────────────────────────────────────────────────

def backend_report() -> dict[str, Any]:
    """Describe which backend each capability would use right now.

    This is what lets Jarvis say "I used PipeWire" or "brightness needs one
    package" instead of guessing, and it is safe to call on every startup.
    """
    return {
        "session": session_type(),
        "compositor": os.environ.get("XDG_CURRENT_DESKTOP", "") or "unknown",
        "audio": {
            "wpctl": _has("wpctl"),
            "pactl": _has("pactl"),
            "pamixer": _has("pamixer"),
            "volume": volume_get(),
        },
        "brightness": {
            "device": backlight_device(),
            "logind": _has("busctl") or _has("gdbus"),
            "brightnessctl": _has("brightnessctl"),
            "percent": brightness_get(),
        },
        "screenshot": {
            "portal": _has("gdbus") or _has("busctl"),
            "gnome_shell": _has("gdbus"),
            "grim": _has("grim"),
            "x11": _has("scrot") or _has("gnome-screenshot") or _has("import"),
        },
        "clipboard": {
            "wl_copy": _has("wl-copy"),
            "xclip": _has("xclip"),
        },
        "notify": _has("notify-send"),
        "lock": _has("loginctl"),
        "focus_window": "x11-only" if session_type() == "wayland" else "wmctrl/xdotool",
    }
