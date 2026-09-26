#computer_control.py
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
import threading
from collections import OrderedDict
from pathlib import Path

from core import desktop_input as di
from core import input_guard
from core import kali_compat as kc

try:
    import pyautogui  # X11 only; Wayland sessions route through desktop_input
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except Exception:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "jarvis_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    caps = di.capabilities()
    if not caps.get("input_ready"):
        raise RuntimeError(
            f"Desktop input unavailable: {caps.get('ydotool_reason') or 'install pyautogui for X11 sessions'}"
        )

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


# ── Idempotency guard ────────────────────────────────────────────────────────
# A model that cannot tell whether an action landed will simply try again. That
# is not a hypothetical: one real session pasted the same sentence into a chat
# five times and pressed enter after each one, because nothing ever told it the
# first attempt had worked. Every repeat looked like a fresh, legitimate request
# from our side, so nothing stopped it.
#
# This turns a repeat into a deterministic answer. An identical
# (action, text, key/target) inside the window is refused with an explicit
# "already done", which is exactly the signal the model was missing. It cannot
# swallow a legitimate repeat of different content, and pointer actions are
# exempt on purpose: repeated clicks at the same spot are a normal, deliberate
# act (re-focusing, spamming a button the user asked for), not a runaway loop.
_DUP_WINDOW_SECONDS = 12.0

# Only actions that place *content* somewhere are guarded. That distinction is
# the whole design: the damage in the observed loop came from the same sentence
# being typed into a chat repeatedly, not from the Enter presses after it.
#
# Keypresses and pointer actions are deliberately NOT guarded. Pressing Enter
# twice in a row is an ordinary thing to ask for (submitting two messages), as
# is clicking the same spot again. An earlier version of this guard covered every
# action, and its own test caught it refusing a second, perfectly legitimate
# "press enter" — which is exactly the kind of false positive that makes an
# assistant feel broken.
_DUP_GUARDED = frozenset({"type", "smart_type", "paste"})

# How many times the same content may run before it is refused. Two, not one:
# "send that again" is a normal request, and refusing it would be wrong. The loop
# that motivated this ran the same text five times, so a ceiling of two still
# ends it — it just ends it at the third attempt instead of the second.
_DUP_MAX_REPEATS = 2

# fingerprint -> (times executed inside the window, monotonic time of the last
# real execution). The timestamp is only written when content actually runs,
# never when a repeat is refused. That gives a predictable cooldown: two allowed,
# then blocked, then the window expires 12s after the last genuine send and the
# slate is clean again. Updating it on refusal instead would let a retrying model
# hold content blocked forever.
_recent_actions: "OrderedDict[tuple, tuple]" = OrderedDict()
_recent_lock = threading.Lock()


# ── Action aliases ───────────────────────────────────────────────────────────
# Every name the model has been observed to use, mapped onto the one action that
# actually implements it. Keep this table generous: an unmapped spelling costs
# the user a failed action they can see and an error the model then retries,
# while a mapping costs one dictionary entry.
_ACTION_ALIASES = {
    # single keypress
    "key": "press", "keypress": "press", "press_key": "press", "tap": "press",
    "key_press": "press", "hit": "press",
    # submit
    "enter": "press", "return": "press", "submit": "press", "send": "press",
    # pointer
    "doubleclick": "double_click", "dblclick": "double_click",
    "double_click_at": "double_click",
    "rightclick": "right_click", "right_click_at": "right_click",
    "leftclick": "click", "left_click_at": "click", "mouse_click": "click",
    "mousemove": "move", "mouse_move": "move", "move_to": "move",
    "mouse_scroll": "scroll", "wheel": "scroll",
    # text entry
    "typewrite": "type", "write": "type", "typing": "type", "input": "type",
    "type_text": "type", "keyboard_type": "type",
    "smarttype": "smart_type", "type_replace": "smart_type",
    "copy_to_clipboard": "copy", "copy_paste": "paste", "clipboard_paste": "paste",
    # windows and screen
    "focus": "focus_window", "switch_window": "focus_window", "activate_window": "focus_window",
    "capture": "screenshot", "screengrab": "screenshot", "capture_screen": "screenshot",
    "find_on_screen": "screen_find", "locate": "screen_find",
    "click_on_screen": "screen_click",
    "clear": "clear_field", "select_all_delete": "clear_field",
    "sleep": "wait", "delay": "wait",
}


# Everything run() can dispatch, for the error message below. Derived from the
# alias table so a new alias can never make this list misleading.
_VALID_ACTIONS = (
    "type", "smart_type", "click", "double_click", "right_click", "move",
    "drag", "hotkey", "press", "scroll", "copy", "paste", "screenshot",
    "screen_find", "screen_click", "wait", "clear_field", "focus_window",
    "random_data", "user_data",
)


def _previous_runs(action: str, params: dict) -> int:
    """How many times this exact content already ran inside the window.

    Records the attempt when it is allowed through, so the caller's first two
    invocations always execute. Returns the count *before* this one, meaning a
    return of ``_DUP_MAX_REPEATS`` or more is the signal to refuse."""
    if action not in _DUP_GUARDED:
        return 0

    fingerprint = (
        action,
        str(params.get("text", ""))[:400],
        str(params.get("path", "")),
        str(params.get("value", "")),
    )
    # No content beyond the action name — a bare 'paste' that pastes whatever is
    # already on the clipboard. Nothing identifiable, so refusing would be a guess.
    if not any(part for part in fingerprint[1:]):
        return 0

    now = time.monotonic()
    with _recent_lock:
        for stale in [k for k, v in _recent_actions.items() if now - v[1] > _DUP_WINDOW_SECONDS]:
            del _recent_actions[stale]

        entry = _recent_actions.get(fingerprint)
        if entry is None:
            _recent_actions[fingerprint] = (1, now)
            return 0

        count, _last = entry
        if count >= _DUP_MAX_REPEATS:
            # Deliberately does not touch the stored timestamp: the cooldown runs
            # from the last real execution, not from the last refusal.
            return count

        _recent_actions[fingerprint] = (count + 1, now)
        return count


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _type(text: str, interval: float = 0.03) -> str:
    time.sleep(0.3)
    di.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        di.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    di.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    if x is not None and y is not None:
        di.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    di.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    di.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    di.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    di.scroll(clicks) if vertical else di.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    di.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    di.moveTo(x1, y1, duration=0.2)
    di.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        di.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = di.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    di.hotkey(select_key, "a")
    time.sleep(0.1)
    di.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    os_name = _get_os()

    if os_name == "windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=5, **_WIN_HIDE,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name contains "{title}") to true'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        # The old code returned success unconditionally after xdotool, even when
        # it exited non-zero. Report what actually happened instead, and be honest
        # that Wayland gives an unprivileged app no way to raise another window.
        ok, detail = kc.focus_window(title)
        if ok:
            time.sleep(0.3)
            return f"Focused window: {title} ({detail})"
        return f"focus_window (Linux): {detail}"

    return f"focus_window: unknown OS '{os_name}'"


def _screen_find(description: str) -> tuple[int, int] | None:
    """Find a UI element by its description and return its centre.

    Routed through core/vision_client rather than calling the SDK here, for four
    reasons that were each a real fault when this built its own request: it named
    one hardcoded model (`gemini-flash-lite-latest` — measured at 55s and the
    most 503-prone name of the set), it had no fallback when that name was busy,
    it shared none of the call budget, and it never disabled deliberation. One
    look is now one look, on the fastest model that is actually answering.
    """
    try:
        from core import screen_watch, vision_client
    except Exception as exc:                                   # pragma: no cover
        print(f"[ComputerControl] ⚠️ screen_find unavailable: {exc}")
        return None

    try:
        frame, _path, note = screen_watch.capture_bytes()
        if frame is None:
            print(f"[ComputerControl] ⚠️ screen_find could not capture: {note}")
            return None
        try:
            w, h = di.size()
        except Exception:
            w, h = 0, 0

        prompt = (
            f"This is a screenshot of a {w}×{h} pixel screen. "
            f"Locate the UI element described as: '{description}'. "
            "Reply with ONLY the centre coordinates, in pixels, in the order "
            "x,y — nothing else. If the element is not visible, reply NOT_FOUND."
        )
        result = vision_client.analyze(
            frame, prompt,
            system=("You locate UI elements in screenshots and answer with either "
                    "two integers separated by a comma, or the word NOT_FOUND. "
                    "Never explain."),
            json_mode=False,
            max_output_tokens=64,
            timeout=20.0,
        )
        if not result.ok:
            print(f"[ComputerControl] ⚠️ screen_find vision failed: {result.error}")
            return None

        text = (result.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None

def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    raw_action = params.get("action", "").lower().strip()

    if not raw_action:
        return "No action specified for computer_control."

    # ── Action aliases ───────────────────────────────────────────────────────
    # The model reads the tool declaration, not this file, and then says what it
    # means in its own words. Real logs caught it asking for "key" (a single
    # keypress) and "enter" (to submit) — and the first returned
    # "Unknown action: 'key'" while a chat message sat unsent in front of the
    # user. Normalising spellings here is far more reliable than hoping the model
    # memorises one blessed name.
    action = _ACTION_ALIASES.get(raw_action, raw_action)

    # "enter"/"submit"/"send" mean one keypress of Enter with no key given.
    if raw_action in ("enter", "return", "submit", "send", "key_enter"):
        params = {**params, "key": "enter"}

    # ── Keystroke guard ───────────────────────────────────────────────────
    # This is the single choke point every keyboard action passes through, and it
    # is the fix for the day JARVIS typed "who r u" into a window nobody asked him
    # to touch and pressed Enter. Typing, pasting and blind clicks need a permit —
    # an unexpired authorisation issued by code that has just read a screenshot
    # and confirmed the text field. A tool call cannot mint one, so "type this
    # somewhere" is no longer reachable from a model's decision alone. The real
    # typing path is pc_automation, which looks first and then types.
    if action in ("type", "smart_type", "paste", "press", "hotkey") or (
            action in ("click", "left_click", "double_click", "right_click")
            and params.get("x") is None):
        if action in ("press", "hotkey"):
            keys = params.get("key") or params.get("keys") or ""
            kind = ("enter" if str(keys).strip().casefold() in ("enter", "return", "")
                    else ("hotkey" if action == "hotkey" else "press"))
        else:
            keys, kind = "", ("click" if "click" in action else action)
        _allowed, _why = input_guard.check_keystroke(kind, keys=keys)
        if not _allowed:
            print(f"[ComputerControl] ⛔ refused {action}: {_why[:100]}")
            if player:
                player.write_log("[Computer] keystroke refused — window not verified")
            return _why

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    # Content that keeps being re-sent gets an explicit refusal rather than
    # another execution. See _previous_runs for why this exists and why it only
    # covers text-bearing actions.
    _prev_runs = _previous_runs(action, params)
    if _prev_runs >= _DUP_MAX_REPEATS:
        print(f"[ComputerControl] ⏭️  Suppressed repeat {action} (already ran {_prev_runs}×)")
        if player:
            player.write_log(f"[Computer] repeat {action} suppressed")
        return (
            f"Already done: this exact {action} has run {_prev_runs} times in the "
            f"last few seconds and succeeded each time. It is NOT being repeated, "
            f"and repeating it will not change the result. Consider the action "
            f"complete and tell the user what happened. If you genuinely meant "
            f"different content, send different text."
        )

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            # Accept keys as a list, or as "ctrl+shift+t" / "ctrl, shift, t". The
            # model has been seen using both separators and both field names, and
            # a combination that silently became one key is worse than an error.
            raw = params.get("keys") or params.get("key") or ""
            if isinstance(raw, (list, tuple)):
                keys = [str(k).strip() for k in raw]
            else:
                keys = [k.strip() for k in str(raw).replace(",", "+").split("+")]
            keys = [k for k in keys if k]
            if not keys:
                return ("hotkey needs a combination, e.g. "
                        "{'action':'hotkey','keys':'ctrl+shift+t'}")
            return _hotkey(*keys)

        if action == "press":
            # 'key' is the documented field; 'keys' is accepted because the model
            # conflates the two constantly — and for a single keypress they mean
            # the same thing.
            return _press(params.get("key") or params.get("keys") or "enter")

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords = _screen_find(desc)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        # Nothing matched. Name every valid action so the model can correct itself
        # in one step instead of guessing again — 'Unknown action' with no options
        # was what produced five turn-around retries in a real session.
        return (
            f"Unknown action: '{action}'. Valid actions are: "
            + ", ".join(_VALID_ACTIONS)
            + ". To press one key use action='press', key='enter'; to press a "
            "combination use action='hotkey', keys='ctrl+c'."
        )

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
