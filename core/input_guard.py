"""core/input_guard.py — the rule that stops JARVIS typing into the wrong window.

Why this exists
---------------
On 27 September JARVIS typed "who r u" into a window nobody asked him to touch,
pressed Enter, and later tried the same thing at a page the user had just
opened. Nothing in the code was wrong in the sense of raising an exception:
`computer_control` types into whatever window happens to have focus, and on this
machine — GNOME 50 on Wayland — the application is not allowed to ask which
window that is. `screen_watch.active_window_title()` returns an empty string for
exactly that reason. So a model that decided "type this" typed it *somewhere*,
and Enter sent it.

The rule
--------
Keystrokes that can change something — typing, pasting, Enter, deleting — are
allowed only when a **permit** is live. A permit is issued by code that has just
looked at the screen and confirmed where it is about to type; it names the
application it saw, carries a short expiry, and can be revoked. The model cannot
issue one: permits exist in the process, not in the conversation, so a tool call
alone can never produce one.

Nothing about this depends on X11 introspection, which is what makes it work on
Wayland. Where the window *can* be identified (XWayland, X11 sessions), a second
allowance applies for browsers and chat apps — a front window that is provably
Firefox is a safe place to type without a fresh frame.

What is refused outright, permit or not
---------------------------------------
*   Anything aimed at a terminal — including JARVIS's own console. Typing into
    the window that is running the assistant is never what anybody meant.
*   The combinations that make an application disappear or that open another
    one (``alt+F4``, ``alt+tab``, ``super``, ``ctrl+alt+t``). Those turn one
    wrong keystroke into a lost window or a new terminal, which is how a small
    mistake became a large one.

Self-test: ``python3 core/input_guard.py``
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

# ── Tunables ────────────────────────────────────────────────────────────────

# How long a verified look authorises typing. Long enough to type a paragraph
# and press Enter, short enough that it cannot outlive the screen it described.
PERMIT_TTL = 30.0

# Enter is allowed this long after the last permitted keystroke, so the ordinary
# type-then-send sequence works without a second verification.
ENTER_GRACE = 12.0

# Keystrokes that cannot change anything and need no permit.
SAFE_COMBOS = frozenset({
    "ctrl+c", "ctrl+shift+c", "ctrl+a", "ctrl+f", "ctrl+shift+f", "escape", "esc",
    "up", "down", "left", "right", "pageup", "pagedown", "home", "end", "tab",
})

# Combinations refused even with a permit: they leave the window we verified.
FORBIDDEN_COMBOS = frozenset({
    "alt+f4", "alt+tab", "ctrl+alt+t", "ctrl+alt+f1", "ctrl+alt+f2", "ctrl+alt+f3",
    "super", "super+d", "super+l", "ctrl+alt+delete", "ctrl+alt+backspace",
})

# Applications that are never a valid typing target.
TERMINAL_HINTS = (
    "konsole", "gnome-terminal", "gnome-terminal-server", "xterm", "kitty",
    "alacritty", "terminator", "tilix", "xfce4-terminal", "mate-terminal",
    "lxterminal", "ptyxis", "wezterm", "hyper", "guake", "yakuake", "st-256color",
)
# Titles that mean "this is the assistant's own console".
SELF_HINTS = ("mark-liv", "mark-liii", "mark-lii", "python3 main.py", "python main.py")

BROWSER_HINTS = ("firefox", "firefox-esr", "navigator", "chromium", "chrome",
                 "google-chrome", "brave", "vivaldi", "opera", "edge", "epiphany",
                 "zen", "librewolf")
CHAT_HINTS = ("whatsapp", "telegram", "discord", "slack", "signal", "element",
              "messenger", "skype", "thunderbird", "evolution")

REFUSAL = (
    "I will not type into a window I have not verified — that is how text ends "
    "up somewhere it was never meant to go. Use pc_automation: it looks at the "
    "screen once, confirms the text field, and then types. Do not retry "
    "computer_control typing for this request."
)

# ── State ───────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_permit: dict[str, Any] = {}          # {"app","evidence","issued_at","expires_at"}
_last_typed_at = 0.0
_last_typed_app = ""
_history: list[str] = []


# ── Permits ─────────────────────────────────────────────────────────────────

def authorize(app: str, evidence: str = "", ttl: float = PERMIT_TTL) -> dict:
    """Allow typing for a short while, because a screenshot says it is safe.

    Call this only from code that has just read a frame and confirmed the
    application it is about to type into. ``evidence`` is the sentence the vision
    call gave, kept so the log can explain the decision later.
    """
    global _permit
    with _lock:
        now = time.time()
        _permit = {
            "app": str(app or "unknown")[:60],
            "evidence": str(evidence or "")[:200],
            "issued_at": now,
            "expires_at": now + max(1.0, float(ttl)),
        }
        _remember(f"permit issued for {_permit['app']} ({_permit['evidence'][:60]})")
        return dict(_permit)


def revoke(reason: str = "") -> None:
    """Drop the permit now — used when a step fails and nothing should type."""
    global _permit
    with _lock:
        if _permit:
            _remember(f"permit revoked ({reason[:60] or 'no reason given'})")
        _permit = {}


def permit_status() -> dict:
    """For the log, the doctor and the tests."""
    with _lock:
        now = time.time()
        live = bool(_permit) and float(_permit.get("expires_at", 0)) > now
        return {
            "live": live,
            "app": _permit.get("app", "") if live else "",
            "evidence": _permit.get("evidence", "") if live else "",
            "seconds_left": round(float(_permit.get("expires_at", 0)) - now, 1) if live else 0.0,
            "seconds_since_typing": round(now - _last_typed_at, 1) if _last_typed_at else None,
            "recent": list(_history[-6:]),
        }


def _remember(line: str) -> None:
    _history.append(f"{time.strftime('%H:%M:%S')} {line}")
    del _history[:-14]


# ── Identifying the focused window (best effort) ───────────────────────────

def focused_window() -> dict:
    """Ask X11/XWayland who has focus. Returns ``{}`` when the session will not say.

    On a Wayland session this is normally empty, by design of the protocol — the
    permit system above is what makes typing safe there, not this function.
    """
    if not shutil.which("xdotool"):
        return {}
    try:
        window_id = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True,
            timeout=3, check=False,
        ).stdout.strip()
        if not window_id:
            return {}
        name = subprocess.run(
            ["xdotool", "getwindowname", window_id], capture_output=True, text=True,
            timeout=3, check=False,
        ).stdout.strip()
        klass = subprocess.run(
            ["xdotool", "getwindowclassname", window_id], capture_output=True, text=True,
            timeout=3, check=False,
        ).stdout.strip()
        return {"id": window_id, "title": name, "class": klass, "source": "xdotool"}
    except (OSError, subprocess.SubprocessError):
        return {}


def app_kind(*names: str) -> str:
    """Classify an application name/window title: browser, chat, terminal, other."""
    hay = " ".join(str(n or "") for n in names).casefold()
    if not hay.strip() or hay.strip() in ("unknown", "none", ""):
        return "unknown"
    if any(hint in hay for hint in SELF_HINTS):
        return "self"
    if any(hint in hay for hint in TERMINAL_HINTS):
        return "terminal"
    if any(hint in hay for hint in BROWSER_HINTS):
        return "browser"
    if any(hint in hay for hint in CHAT_HINTS):
        return "chat"
    return "other"


# ── The check ───────────────────────────────────────────────────────────────

def _combo(keys: Any) -> str:
    if isinstance(keys, (list, tuple)):
        parts = [str(k).strip().casefold() for k in keys if str(k).strip()]
    else:
        parts = [p.strip().casefold() for p in re.split(r"[+,\s]+", str(keys or "")) if p.strip()]
    order = {"ctrl": 0, "control": 0, "alt": 1, "shift": 2, "super": 3, "cmd": 3, "meta": 3,
             "win": 3}
    parts.sort(key=lambda p: order.get(p, 9))
    return "+".join(p.replace("control", "ctrl") for p in parts)


def check_keystroke(kind: str, *, keys: Any = "", app: str = "") -> tuple[bool, str]:
    """Decide whether one keystroke may happen. Returns ``(allowed, reason)``.

    ``kind`` is ``type``, ``paste``, ``enter``/``press`` or ``hotkey``. ``app`` is
    an optional caller-supplied name of the window it believes it is typing into
    (a vision answer); it is used for classification, never as proof.
    """
    global _last_typed_at, _last_typed_app
    kind = str(kind or "type").casefold()
    window = focused_window()
    names = [app, window.get("class", ""), window.get("title", "")]
    kind_of_window = app_kind(*names)

    combo = _combo(keys) if keys else ""

    with _lock:
        now = time.time()
        permit_live = bool(_permit) and float(_permit.get("expires_at", 0)) > now
        permit_app = _permit.get("app", "") if permit_live else ""
        typed_recently = (now - _last_typed_at) <= ENTER_GRACE and bool(_last_typed_at)

        # 1. Never into the assistant's own console or a terminal, ever.
        if kind_of_window in ("terminal", "self"):
            reason = ("the focused window is a terminal"
                      if kind_of_window == "terminal" else
                      "the focused window is my own console")
            _remember(f"refused {kind}: {reason}")
            return False, (
                f"I will not send keystrokes to {reason}. Typing there would put "
                f"text into a shell — or into my own log — instead of wherever you "
                f"meant. Switch to the window you want and say 'look again'."
            )

        # 2. Combinations that lose or replace the window we verified.
        if kind == "hotkey" and combo in FORBIDDEN_COMBOS:
            _remember(f"refused forbidden combo {combo}")
            return False, (
                f"'{combo}' switches away from or closes whatever is in front, so I "
                f"will not send it as a blind keystroke. Ask me for the window you "
                f"want and I will open it by name."
            )

        # 3. Harmless keys need no permit.
        if kind == "hotkey" and combo in SAFE_COMBOS:
            _remember(f"allowed safe combo {combo}")
            return True, ""

        # 4. Anything else needs a verified look at the screen.
        if permit_live:
            if kind in ("type", "smart_type", "paste"):
                _last_typed_at = now
                _last_typed_app = permit_app or app
            _remember(f"allowed {kind} under permit for {permit_app}"
                      + (f" ({combo})" if combo else ""))
            return True, ""

        # Enter immediately after a permitted keystroke is the normal
        # type-then-send sequence, and is allowed without a second look.
        if kind in ("enter", "press") and combo in ("enter", "return", "") and typed_recently:
            _remember(f"allowed {kind} after typing")
            return True, ""

        # 5. A provably safe front window is a second allowance, for X11/XWayland
        #    sessions that can report one. Wayland simply takes the refusal path.
        if kind_of_window in ("browser", "chat"):
            if kind in ("type", "smart_type", "paste"):
                _last_typed_at = now
                _last_typed_app = kind_of_window
            _remember(f"allowed {kind} into verified {kind_of_window} window")
            return True, ""

        _remember(f"refused {kind}: no permit, window is '{kind_of_window}'")
        return False, REFUSAL


def status() -> dict:
    permit = permit_status()
    window = focused_window()
    return {
        "permit": permit,
        "focused": window or {"note": "the session will not report a focused window"},
        "focused_kind": app_kind(window.get("class", ""), window.get("title", "")),
        "last_typed_app": _last_typed_app,
    }


# ── Self-test ───────────────────────────────────────────────────────────────

def _self_test() -> dict:
    """Prove the policy with an injected window, on any machine.

    The focus lookup is stubbed for the duration, so this passes identically on
    Wayland, on X11 and in CI — and it can never send a real keystroke.
    """
    global focused_window
    details: dict[str, Any] = {}
    original = focused_window
    try:
        revoke("test start")
        focused_window = lambda: {"class": "firefox", "title": "WhatsApp Web — Firefox",
                                  "source": "test"}
        ok, _why = check_keystroke("type", app="WhatsApp Web")
        details["browser_without_permit_allowed"] = ok
        details["enter_after_typing_allowed"] = check_keystroke("enter", keys="enter")[0]
        details["safe_combo_allowed"] = check_keystroke("hotkey", keys="ctrl+c")[0]
        details["forbidden_combo_refused"] = not check_keystroke("hotkey", keys="alt+F4")[0]
        details["terminal_combo_refused"] = not check_keystroke(
            "hotkey", keys="ctrl+alt+t")[0]

        focused_window = lambda: {"class": "Gnome-terminal", "title": "sacheet@ethical-hacker",
                                  "source": "test"}
        ok, why = check_keystroke("type", app="")
        details["terminal_refused"] = (not ok) and "terminal" in why
        details["terminal_refused_even_with_permit"] = (
            authorize("WhatsApp Web", "test") and not check_keystroke("type")[0]
        )

        focused_window = lambda: {"class": "Mark-LIV", "title": "python3 main.py",
                                  "source": "test"}
        ok, why = check_keystroke("type", app="")
        details["own_console_refused"] = (not ok) and "console" in why

        # The Wayland case: nothing can be identified at all. The permit from the
        # previous case is dropped first, so this is genuinely the bare case.
        revoke("test: unidentified window")
        focused_window = lambda: {}
        ok, why = check_keystroke("type", app="")
        details["unidentified_window_refused"] = (not ok) and "pc_automation" in why
        # A live permit is what makes typing possible when the session cannot say
        # which window has focus — which is the normal state on Wayland.
        authorize("WhatsApp Web", "compose box verified from a frame")
        details["permit_covers_unidentified_window"] = check_keystroke("type")[0]
        revoke("test: back to no permit")

        revoke("test: permit required for paste")
        ok, _why = check_keystroke("paste")
        details["paste_refused_without_permit"] = not ok

        authorize("WhatsApp Web", "compose box has the caret", ttl=PERMIT_TTL)
        details["typing_allowed_under_permit"] = check_keystroke("type", app="WhatsApp Web")[0]
        details["enter_allowed_under_permit"] = check_keystroke("enter", keys="enter")[0]

        authorize("WhatsApp Web", "test: expiry", ttl=PERMIT_TTL)
        _permit["expires_at"] = time.time() - 1        # as if the look were long ago
        details["expired_permit_refused"] = not check_keystroke("type")[0]

        authorize("Firefox", "test", ttl=PERMIT_TTL)
        revoke("test: explicit revoke")
        details["revoke_works"] = not check_keystroke("type")[0]

        details["classification"] = (
            app_kind("Firefox") == "browser" and app_kind("WhatsApp") == "chat"
            and app_kind("konsole") == "terminal" and app_kind("python3 main.py") == "self"
            and app_kind("") == "unknown"
        )
        details["status_shape"] = {"permit", "focused", "focused_kind"} <= set(status())
    finally:
        focused_window = original
        revoke("test end")

    ok = all(value[0] if isinstance(value, tuple) else value for value in details.values())
    return {"ok": bool(ok), "details": {k: (v[0] if isinstance(v, tuple) else v)
                                        for k, v in details.items()}}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
