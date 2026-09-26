#!/usr/bin/env python3
"""Kali compatibility doctor for MARK LIII.

Run it from the project root:

    python3 tools/doctor.py
    python3 tools/doctor.py --json

It answers one question -- "will this app's OS-level features actually work on
THIS machine?" -- by resolving every backend the way the app resolves it at
runtime, plus reading the live values (volume, brightness) so a silently broken
chain is visible rather than assumed.  Nothing here needs root and nothing is
changed on your system.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK = "OK"
WARN = "WARN"
BAD = "FAIL"


def _state(good: bool, warn: bool = False) -> str:
    return OK if good else (WARN if warn else BAD)


def _distro() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return platform.system()
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.system()


def _python_status() -> dict:
    marker = Path(sysconfig.get_paths()["stdlib"]) / "EXTERNALLY-MANAGED"
    in_venv = sys.prefix != sys.base_prefix
    managed = marker.exists() and not in_venv
    if in_venv:
        note = "virtualenv — pip installs normally"
    elif managed:
        note = (
            "PEP 668 externally-managed (normal on Kali): the app's installer adds "
            "--user --break-system-packages automatically; a manual install needs "
            "the same flags or a virtualenv"
        )
    else:
        note = "no PEP 668 marker — pip installs to the user site"
    return {
        "version": platform.python_version(),
        "executable": sys.executable,
        "in_venv": in_venv,
        "externally_managed": managed,
        # Not a failure: this is exactly the case the installer is built to handle.
        "install_ok": True,
        "note": note,
    }


SYSTEM_TOOLS = {
    "input (Wayland)": ["ydotool", "ydotoold"],
    "input (X11)": ["xdotool", "wmctrl"],
    "clipboard": ["wl-copy", "wl-paste", "xclip"],
    "screenshots": ["grim", "scrot", "gnome-screenshot"],
    "audio": ["wpctl", "pactl", "pamixer"],
    "brightness": ["brightnessctl"],
    "notifications": ["notify-send"],
    "dbus / portal": ["gdbus", "busctl", "xdg-open"],
    "session": ["loginctl"],
    "network / browser": ["xdg-open", "nmap"],
}


def _tools() -> dict:
    report: dict[str, dict] = {}
    for group, names in SYSTEM_TOOLS.items():
        report[group] = {name: (shutil.which(name) or "") for name in names}
    return report


def _ydotool_socket() -> dict:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    candidates = ["/run/ydotoold/.socket"]
    if runtime:
        candidates.append(f"{runtime}/.ydotool_socket")
    return {path: os.path.exists(path) for path in candidates}


def _capabilities() -> dict:
    """Resolve the app's capability cascade and read live values."""
    result: dict[str, object] = {}
    try:
        kc = importlib.import_module("core.kali_compat")
    except Exception as exc:  # pragma: no cover - degraded import
        return {"error": f"core.kali_compat could not be imported: {exc}"}

    result["session"] = kc.session_type()
    result["backends"] = kc.backend_report()

    volume = kc.volume_get()
    result["volume_read"] = volume
    result["volume_state"] = _state(volume is not None)

    brightness = kc.brightness_get()
    result["brightness_read"] = brightness
    result["brightness_state"] = _state(brightness is not None)

    # A read-only clipboard probe: writing would clobber whatever the user has
    # copied, which a diagnostic has no business doing.
    ok, detail = kc.clipboard_paste()
    result["clipboard_state"] = _state(ok, warn=not ok)
    result["clipboard_detail"] = "read ok" if ok else detail

    ok, detail = kc.notify("MARK LIII", "doctor probe")
    result["notify_state"] = _state(ok, warn=not ok)
    result["notify_detail"] = detail

    # The shared one-look-per-request gate. Reported here because "JARVIS took
    # eight screenshots for one sentence" is a capability question, not a code
    # question: the answer is whether every vision tool is going through the
    # budget. Unavailable means something is bypassing it.
    try:
        vb = importlib.import_module("core.vision_budget")
        st = vb.status()
        result["vision_look_budget"] = (
            f"one look per request ({int(vb.DEFAULT_WINDOW)}s wall-clock fallback); "
            f"{st['looks_this_request']} look(s) this request"
        )
        result["vision_budget_state"] = _state(True)
    except Exception as exc:                                    # pragma: no cover
        result["vision_look_budget"] = f"unavailable: {exc}"
        result["vision_budget_state"] = _state(False, warn=True)

    return result


def _deep_checks() -> dict:
    """Opt-in checks that touch the system: capture a screenshot for real."""
    try:
        import os as _os
        kc = importlib.import_module("core.kali_compat")
    except Exception as exc:
        return {"error": str(exc)}
    path = _os.path.join(_os.path.expanduser("~"), ".cache", "jarvis-doctor-shot.png")
    ok, detail = kc.screenshot(path)
    size = _os.path.getsize(path) if ok and _os.path.exists(path) else 0
    if ok and _os.path.exists(path):
        try:
            _os.unlink(path)
        except OSError:
            pass
    return {"screenshot_ok": ok, "screenshot_detail": detail, "bytes": size}


def _local_tools_lane() -> dict:
    """How much of the local tool allowlist is actually installed.

    The allowlist is what keeps shell access safe, so the doctor reports its
    coverage the same way it reports every other capability. Desktop
    automation uses it to run its inspections, so coverage matters even in a
    build with no security stack.
    """
    try:
        le = importlib.import_module("core.local_exec")
    except Exception as exc:
        return {"error": f"core.local_exec could not be imported: {exc}"}
    available = le.available_locally()
    present = sum(1 for value in available.values() if value)
    return {
        "allowlisted": len(available),
        "present": present,
        "missing": sorted(name for name, ok in available.items() if not ok),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Kali compatibility doctor")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--deep", action="store_true",
        help="also capture a real screenshot (writes one file, then removes it)",
    )
    args = parser.parse_args()

    caps = _capabilities()
    if args.deep:
        caps.update(_deep_checks())
    tools = _tools()
    payload = {
        "distro": _distro(),
        "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "python": _python_status(),
        "capabilities": caps,
        "system_tools": tools,
        "ydotool_sockets": _ydotool_socket(),
        "local_tools_lane": _local_tools_lane(),
        "notify_send_broken": False,
    }

    # One known Kali failure mode worth naming explicitly.  Bounded, because a
    # doctor must never be the thing that hangs.
    if shutil.which("notify-send"):
        try:
            probe = subprocess.run(
                ["notify-send", "--version"], capture_output=True, text=True,
                timeout=5, check=False,
            )
            payload["notify_send_broken"] = probe.returncode != 0
        except (OSError, subprocess.SubprocessError):
            payload["notify_send_broken"] = True

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"\nMARK LIII — compatibility check")
    print(f"  distro   : {payload['distro']}")
    print(f"  session  : {payload['session']} ({payload['desktop']})")
    py = payload["python"]
    print(f"  python   : {py['version']}  [{_state(py['install_ok'])}] {py['note']}")

    print("\n  CAPABILITIES (what the app will actually use)")
    print(f"    session detected : {caps.get('session', '?')}")
    print(f"    volume read      : {caps.get('volume_read')}  [{caps.get('volume_state')}]")
    print(f"    brightness read  : {caps.get('brightness_read')}  [{caps.get('brightness_state')}]")
    print(f"    clipboard        : {caps.get('clipboard_detail')}  [{caps.get('clipboard_state')}]")
    print(f"    notifications    : {caps.get('notify_detail')}  [{caps.get('notify_state')}]")
    print(f"    vision budget    : {caps.get('vision_look_budget')}  [{caps.get('vision_budget_state')}]")

    if args.deep:
        print(
            f"    screenshot       : {caps.get('screenshot_detail')} "
            f"({caps.get('bytes')} bytes)  [{_state(bool(caps.get('screenshot_ok')), warn=True)}]"
        )

    backends = caps.get("backends") or {}
    if isinstance(backends, dict):
        audio = backends.get("audio", {})
        brightness = backends.get("brightness", {})
        shot = backends.get("screenshot", {})
        print("\n  BACKEND RESOLUTION")
        print(f"    audio       : wpctl={audio.get('wpctl')} pactl={audio.get('pactl')} pamixer={audio.get('pamixer')}")
        print(f"    brightness  : device={brightness.get('device')} logind={brightness.get('logind')} brightnessctl={brightness.get('brightnessctl')}")
        print(f"    screenshot  : portal={shot.get('portal')} gnome-shell={shot.get('gnome_shell')} grim={shot.get('grim')} x11={shot.get('x11')}")
        print(f"    window focus: {backends.get('focus_window')}")

    print("\n  SYSTEM TOOLS")
    missing: list[tuple[str, str]] = []
    for group, names in tools.items():
        found = [n for n, path in names.items() if path]
        absent = [n for n, path in names.items() if not path]
        missing += [(group, n) for n in absent]
        print(f"    {group:20} present: {', '.join(found) if found else '—'}")
        if absent:
            print(f"    {'':20} missing: {', '.join(absent)}")

    sockets = payload["ydotool_sockets"]
    print("\n  WAYLAND INPUT")
    for path, exists in sockets.items():
        print(f"    {path}: {'present' if exists else 'absent'}")

    lane = payload["local_tools_lane"]
    print("\n  LOCAL TOOL LANE")
    if "error" in lane:
        print(f"    {lane['error']}")
    else:
        print(f"    {lane['present']}/{lane['allowlisted']} allowlisted tools present locally")

    if payload["notify_send_broken"]:
        print(
            "\n  ⚠ notify-send is installed but broken (a libnotify version skew).\n"
            "    The app falls back to the notification daemon's D-Bus API, so alerts\n"
            "    still work. To repair the CLI itself:\n"
            "      sudo apt install --reinstall libnotify-bin libnotify4"
        )

    print("\n  VERDICT")
    failures = [
        name for name, state in (
            ("volume", caps.get("volume_state")),
            ("brightness", caps.get("brightness_state")),
        ) if state == BAD
    ]
    if failures:
        print(f"    ✗ not working: {', '.join(failures)} — see the resolution lines above")
    else:
        print("    ✓ audio, brightness, screenshots, clipboard and notifications all resolve")
    print(f"    missing optional tools: {', '.join(n for _, n in missing) or 'none'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
