"""
pc_automation.py — JARVIS's hands on a Kali GNOME 50.2 Wayland desktop.

Why this file exists
--------------------
Mark-LIV already has pieces of desktop control scattered across actions:
``computer_control`` types and clicks, ``computer_settings`` moves volume and
brightness, ``open_app`` launches programs. What it never had is *task-level*
automation: one tool call that carries a goal like "reply to Ravi on WhatsApp
saying I will be late" and does the whole thing — open the app, find the chat,
type the message, send it, and PROVE on screen that it happened.

This file is that layer. One action, one natural-language ``task`` parameter,
and a set of deterministic skills behind it. The model stays in its fast lane
(one tool call, one spoken line); the loops here do the slow, careful part.

The session it targets
----------------------
Kali GNU/Linux Rolling 2026.x, GNOME Shell 50.2, Wayland (wayland-0), Python
3.14. Everything routes through the session-aware layers that already live in
core/:

*   keystrokes and clicks  -> core.desktop_input  (ydotool on Wayland,
                               real pyautogui on X11)
*   volume / notify / focus -> core.kali_compat    (wpctl, D-Bus, portal)
*   screenshots             -> core.screen_watch   (xdg-desktop-portal first)
*   screen understanding    -> core.vision_client  (Gemini, model ladder)
*   audit trail             -> core.activity_log   ("why did you do that?")

The core idea: every risky step is verified
-------------------------------------------
Blind automation on a GUI fails silently — a chat that never opened, a message
typed into the wrong window, a search box that never had focus. So every skill
here follows the same loop:

    ACT  ->  SETTLE  ->  SCREENSHOT  ->  ASK VISION "did it happen?"  ->  proceed

A skill that cannot verify reports that honestly (VERIFIED / UNVERIFIED) rather
than claiming success. A skill asked to SEND something verifies the text is in
the right place BEFORE pressing Enter, and again AFTER.

WhatsApp Web, specifically
--------------------------
Two routes, chosen by what the user gave:

*   a phone number  -> the ``https://wa.me/<number>?text=...`` deep link, which
    makes WhatsApp Web open the exact chat with the text already in the compose
    box. Deterministic, no clicking at all. Enter sends.
*   a contact name  -> focus the tab, open WhatsApp's own chat-list search
    (Ctrl+Alt+/ — NOT Ctrl+K, which is Firefox's search bar; a verified click
    on the box in the left sidebar is the fallback), type the name, press Enter,
    then verify WHICH conversation actually opened by reading its name back,
    then type and send with the verify-before-send guard.

If WhatsApp Web is not logged in, the skill says exactly that ("QR code on
screen — scan it once") instead of pretending.

The context brain — why it looks before it clicks
-------------------------------------------------
Searching for a contact who is already on screen is how GUI automation goes
wrong: the chat list opens, the search box steals the caret, and the message
lands in the search field, in another window, or nowhere at all. So this file
keeps a small, honest memory of the desktop in ``memory/pc_context.json`` —
what is focused, which conversation is open, whether the message box has the
caret — written after every verified step and read before every ambiguous one.

That memory changes the *order* of the WhatsApp skill. The already-open chat
is checked FIRST; the chat-list search is the fallback, not the default. If the
conversation you want is open, nothing is clicked and nothing is searched: the
caret is aimed at the compose box, verified, and the text goes in there.

Three entry points read that memory:

*   ``action="context"``     — "where am I?" : focused app, open chat, unread.
*   ``action="current_chat"`` — reads the conversation that is already open.
*   ``action="reply"``        — types into the open chat. No searching at all.

Self-test: ``python3 actions/pc_automation.py`` runs the read-only checks.
"""

from __future__ import annotations

# Self-test bootstrap: running ``python3 actions/pc_automation.py`` directly
# needs the project root importable; the normal path (imported as an action by
# the loader) never takes this branch.
if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

from core import activity_log
from core import desktop_input as di
from core import input_guard
from core import kali_compat as kc
from core import vision_budget

# ── Timing knobs ─────────────────────────────────────────────────────────────
# Wayland compositors animate. Every UI transition needs a settle delay before
# a screenshot means anything; these are tuned for GNOME 50 on this machine
# (a ~1366x768 laptop panel, PipeWire audio, ydotoold input).

_SETTLE_LAUNCH   = 6.0   # cold app/web-app launch (browser tab open, paint, JIT)
_SETTLE_NAV      = 3.0   # in-app navigation: search results, new chat open
_SETTLE_KEY      = 0.8   # after a shortcut: focus ring, popup, selection
_SETTLE_TYPE     = 0.5   # after typing: React re-render, contact list filter
_SETTLE_VISION   = 0.4   # small breath before grabbing a verification frame

_VISION_TIMEOUT  = 25.0  # fast-model budget for one verification question
_VISION_MODELS   = None  # None = chain default (fast first); verify loops want speed

_ENTER           = "enter"

# WhatsApp Web routes and anchors ------------------------------------------------
_WA_URL          = "https://web.whatsapp.com"
_WA_TITLE_KEYS   = ("whatsapp",)             # window/tab title match, lowercase
# WhatsApp Web's own shortcut for the chat-list search is Ctrl+Alt+/. Do NOT
# use Ctrl+K here: that is FIREFOX's "focus the search bar" shortcut, not
# WhatsApp's, so on a machine where the web app does not swallow it the contact
# name is typed into the browser's search box and Enter fires a web search
# instead of opening a chat. The geometric click below is the fallback.
_SEARCH_SHORTCUT = ("ctrl", "alt", "/")

# Coordinates for the fallback click on WA Web's search box, as fractions of
# the screen. The search box sits at the top of the left chat-list column on
# every layout I measured from 1280x720 up to 1920x1080; fractions travel
# across resolutions better than pixels.
_SEARCH_BOX_XY   = (0.085, 0.062)

# Where the MESSAGE compose box sits: bottom of the right-hand conversation
# pane. The chat list owns roughly the left third of the window, so the middle
# of the message pane is around x=0.65; the message box is the bottom strip of
# it. Two candidates, tried in order, both verified by vision after the click.
_COMPOSE_BOX_XY  = ((0.66, 0.94), (0.58, 0.90))

# The desktop-context memory ---------------------------------------------------
# One small file, rewritten after every verified step. It answers the two
# questions every automation decision depends on: what is focused, and is a
# chat conversation open (and with whom). Cheap to read, so the model can ask
# for it as often as it likes.
_CONTEXT_PATH    = Path(__file__).resolve().parent.parent / "memory" / "pc_context.json"
_CONTEXT_FRESH   = 25.0      # seconds a remembered context stays trustworthy

# Words a vision model likes to hand back when it is asked to read "the open
# conversation" but none is open: the app names, the product names, and the
# browser. They are not people, and treating one as a contact name would mean
# confidently claiming a conversation that does not exist.
_NOT_A_CONTACT = {
    "whatsapp", "whatsapp web", "whats app", "web whatsapp", "telegram",
    "telegram web", "signal", "discord", "instagram", "instagram web",
    "messenger", "google chrome", "chrome", "chromium", "firefox", "browser",
    "messages", "chats", "chat", "none", "n a", "unknown", "whatsapp desktop",
}

_CONTEXT_QUESTION = (
    "Describe the state of this desktop, for an assistant deciding what to do "
    "next. Read the name at the top of any open conversation in WhatsApp Web, "
    "Telegram, Signal, Discord or Instagram. "
    'Answer with STRICT JSON only: {"match": true, '
    '"app": "<the application with focus>", '
    '"chat_app": "<whatsapp|telegram|signal|discord|instagram|browser|none>", '
    '"chat": "<exact name of the OPEN conversation, or empty>", '
    '"compose": true/false, '
    '"unread": <integer count of unread badges you can see> '
    '"detail": "<one short sentence about what the user is doing>"}'
    " Never wrap the JSON in prose or code fences."
)


# ══════════════════════════════════════════════════════════════════════════════
# Small shared machinery
# ══════════════════════════════════════════════════════════════════════════════

def _settle(seconds: float) -> None:
    """Wait long enough for the compositor to finish what we just started."""
    time.sleep(seconds)


def _input_ready() -> tuple[bool, str]:
    """Typing/clicking needs a live input path. Report, never crash."""
    try:
        caps = di.capabilities()
    except Exception as exc:                                   # pragma: no cover
        return False, f"desktop_input unavailable: {exc}"
    if caps.get("input_ready"):
        return True, caps.get("backend", "ready")
    reason = caps.get("ydotool_reason") or "no input backend is reachable"
    if str(caps.get("session", "")).lower() == "wayland":
        reason += (" — start the ydotoold daemon: "
                   "systemctl --user enable --now ydotoold")
    return False, reason


def _type(text: str, interval: float = 0.02) -> None:
    """Type text — but only into a window this skill has just verified.

    Every typing site in this file is preceded by a look at the screen that
    confirmed the field (the compose box, the chat-list search box). That look is
    what mints the permit; without one, this refuses rather than typing into
    whatever happens to have focus. The refusal is raised so it surfaces as one
    clean sentence instead of half a message sent to the wrong place.
    """
    allowed, why = input_guard.check_keystroke("type")
    if not allowed:
        raise PermissionError(why)
    di.typewrite(text, interval=interval)


def _press(key: str) -> None:
    di.press(key)


def _hotkey(*keys: str) -> None:
    di.hotkey(*keys)


def _grab(reuse_seconds: float = 0.0) -> tuple[Optional[bytes], str]:
    """One screenshot as JPEG-ready bytes. Returns (bytes, note)."""
    try:
        data, _cap, note = __import__(
            "core.screen_watch", fromlist=["capture_bytes"]
        ).capture_bytes(reuse_seconds=reuse_seconds)
        return data, note
    except Exception as exc:                                   # pragma: no cover
        return None, f"capture failed: {exc}"


def _vision_json(frame: bytes, question: str, *,
                 timeout: float = _VISION_TIMEOUT,
                 max_tokens: int = 220) -> tuple[bool, dict, str]:
    """Ask the vision layer one question about a screenshot.

    Returns ``(ok, payload, why)``. ``ok`` is whether the vision call itself
    succeeded — NOT whether the answer was yes. The WHOLE parsed object comes
    back, not just its ``detail`` field, because a single look often answers
    several questions at once (which chat is open *and* whether the compose box
    has the caret), and re-asking for each field would double the latency.
    """
    try:
        from core import vision_client
    except Exception as exc:                                   # pragma: no cover
        return False, {}, f"vision_client unavailable: {exc}"

    result = vision_client.analyze(
        frame,
        question,
        system=("You verify GUI automation steps from screenshots. "
                "Answer with STRICT JSON only: "
                '{"match": true/false, "app": "<dominant application>", '
                '"detail": "<one short sentence of evidence>"}. '
                "Never wrap the JSON in prose or code fences."),
        json_mode=True,
        timeout=timeout,
        models=_VISION_MODELS,
        max_output_tokens=max_tokens,
    )
    if not result.ok:
        return False, {}, f"vision call failed: {result.error}"

    payload = result.data
    if not isinstance(payload, dict):
        try:
            payload = vision_client.loads_lenient(result.text or "{}")
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return False, {}, f"vision answer was not JSON: {str(result.text)[:120]}"
    return True, payload, str(payload.get("detail", ""))[:300]


def _vision_ask(frame: bytes, question: str, *,
                timeout: float = _VISION_TIMEOUT) -> tuple[bool, str]:
    """The two-field form used by the verify loops: (match, detail)."""
    ok, payload, why = _vision_json(frame, question, timeout=timeout)
    if not ok:
        return False, why
    return bool(payload.get("match")), "" if payload.get("detail") is None \
        else str(payload.get("detail", ""))[:200]


def _verify_screen(question: str, *,
                   settle: float = _SETTLE_VISION,
                   reuse_seconds: float = 0.0) -> tuple[bool, str]:
    """The one-liner every skill uses: settle, grab, ask."""
    _settle(settle)
    frame, note = _grab(reuse_seconds=reuse_seconds)
    if frame is None:
        return False, note
    ok, detail = _vision_ask(frame, question)
    return ok, detail


def _frame_and_verify(question: str,
                      settle: float = _SETTLE_VISION) -> tuple[Optional[bytes], bool, str]:
    """``_verify_screen`` that also keeps the frame it judged.

    A caller that has to locate something on the same screen — the click
    fallbacks ask where the compose box actually is — would otherwise pay for a
    second photograph of a screen it is already holding. One frame, several
    questions: that is the whole reason this helper exists.
    """
    _settle(settle)
    frame, note = _grab()
    if frame is None:
        return None, False, note
    ok, detail = _vision_ask(frame, question)
    return frame, ok, detail


def _vision_coords(frame: bytes, what: str,
                   expect: str = "WhatsApp Web") -> tuple[Optional[tuple[float, float]], bool]:
    """One frame -> where an element actually is, plus whether the expected app
    owns the screen. Returns ``((x, y) or None, expected_app_visible)``.

    This is the user's rule as code: **analyse the frame once, parse the exact
    position of the target, and commit to that point** — rather than clicking a
    remembered fraction and hoping the layout has not moved. The app question is
    answered from the same frame on purpose: a coordinate click lands in
    whatever window is actually in front, so that fact has to come from the
    picture, not from a separate look.
    """
    ok, payload, _why = _vision_json(
        frame,
        f"Look at this screenshot. (a) Is {expect} the dominant window on "
        f"screen? (b) If it is, where is {what}? Give its CENTRE as fractions "
        "of the image width and height — 0.0 is the left/top edge and 1.0 is "
        "the right/bottom edge. "
        'Answer with STRICT JSON only: {"app": true/false, '
        '"found": true/false, "x": <0.0-1.0>, "y": <0.0-1.0>, '
        '"detail": "..."}.',
    )
    if not ok:
        return None, False
    app_ok = bool(payload.get("app"))
    if not (app_ok and payload.get("found")):
        return None, app_ok
    try:
        x, y = float(payload.get("x")), float(payload.get("y"))
    except (TypeError, ValueError):
        return None, app_ok
    if not (0.02 <= x <= 0.995 and 0.02 <= y <= 0.995):
        return None, app_ok
    return (x, y), app_ok


# ── JSON field extraction from a vision answer ───────────────────────────────

def _json_field(detail: str, key: str) -> str:
    """Best-effort read of one field out of a JSON-ish vision answer."""
    match = re.search(rf'"{key}"\s*:\s*"([^"]*)"', detail or "")
    return match.group(1) if match else ""


def _name_match(a: str, b: str) -> bool:
    """Is the contact the user named the one that is actually open?

    Contact names are typed, remembered and said aloud, so they arrive
    inexactly: 'Rayan Ali' vs 'rayan', 'mom' vs 'Mom 💛', 'Dr. Ehsan' vs
    'Ehsan'. Normalising away case, punctuation, emoji and honorifics and then
    accepting either containment or a token-prefix match gets all of those
    right without ever merging two different people: 'Ali' does not match
    'Rayan Ali' by containment... it does, so containment requires a minimum
    length, and single-token matches must be the full token.
    """
    def norm(value: str) -> list[str]:
        cleaned = re.sub(r"[^\w\s]+", " ", (value or "").casefold())
        drop = {"dr", "mr", "mrs", "ms", "the", "bin", "and"}
        return [t for t in cleaned.split() if t and t not in drop]

    left, right = norm(a), norm(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if " ".join(left) == " ".join(right):
        return True
    if len(left) == 1 and len(right) > 1:
        return left[0] == right[0] or left[0] == right[-1]
    if len(right) == 1 and len(left) > 1:
        return right[0] == left[0] or right[0] == left[-1]
    return set(left).issubset(set(right)) or set(right).issubset(set(left))


# ── The context brain: memory of what is on the desktop ───────────────────────

def _context_read() -> dict:
    """The remembered desktop context, or an empty dict if there is none."""
    try:
        data = json.loads(_CONTEXT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _context_write(**fields: Any) -> None:
    """Merge new knowledge into the context file. Never raises."""
    data = _context_read()
    data.update(fields)
    data["updated"] = time.time()
    try:
        _CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONTEXT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except OSError:                                            # pragma: no cover
        pass


def _context_age() -> float:
    """Seconds since the context was last confirmed. A large number if never."""
    stamp = float(_context_read().get("updated") or 0.0)
    return time.time() - stamp if stamp else 1e9


def _desktop_context(*, force: bool = False) -> dict:
    """What is happening on the desktop right now.

    Remembered for ``_CONTEXT_FRESH`` seconds, so asking "where am I?" twice in
    a row costs one vision call rather than two. ``force=True`` always looks.
    """
    remembered = _context_read()
    if not force and remembered.get("app") and _context_age() <= _CONTEXT_FRESH:
        return remembered

    frame, note = _grab()
    if frame is None:
        return dict(remembered, error=note)
    ok, payload, why = _vision_json(frame, _CONTEXT_QUESTION)
    if not ok:
        return dict(remembered, error=why)

    raw_unread = str(payload.get("unread") or "0").strip()
    seen_chat = str(payload.get("chat") or "").strip()
    context = {
        "app": str(payload.get("app") or "")[:60],
        "chat_app": str(payload.get("chat_app") or "none").lower()[:20],
        "chat": (seen_chat[:60] if _looks_like_a_contact(seen_chat) else ""),
        "compose_ready": bool(payload.get("compose")),
        "unread": int(raw_unread) if raw_unread.isdigit() else 0,
        "note": str(payload.get("detail") or "")[:200],
        "seen_at": time.time(),
        "error": "",
    }
    _context_write(**context)
    return context


# ── Audit helper ─────────────────────────────────────────────────────────────

def _audit(skill: str, detail: str) -> None:
    try:
        activity_log.record("pc_automation", skill, detail=detail[:300],
                            why="user asked for it")
    except Exception:                                          # pragma: no cover
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Application lifecycle
# ══════════════════════════════════════════════════════════════════════════════

_APP_HINTS: dict[str, tuple[str, ...]] = {
    # launch-token -> desktop names worth trying before a generic xdg-open
    "firefox":     ("firefox", "firefox-esr"),
    "chrome":      ("google-chrome", "chromium"),
    "chromium":    ("chromium",),
    "code":        ("code",),
    "files":       ("nautilus",),
    "terminal":    ("kgx", "gnome-terminal", "konsole", "xterm"),
    "calculator":  ("gnome-calculator",),
    "text":        ("org.gnome.TextEditor", "gedit"),
    "settings":    ("gnome-control-center",),
    "spotify":     ("spotify",),
    "discord":     ("discord",),
    "whatsapp":    (),   # WhatsApp lives in the browser here — see _skill_whatsapp
}


def _launch(token: str) -> tuple[bool, str]:
    """Launch a desktop app by its common name. GTK apps via gtk-launch when
    a .desktop exists, otherwise gio open / xdg-open."""
    if not token.strip():
        return False, "no application named"

    for candidate in _APP_HINTS.get(token.lower(), ()):        # exact binaries
        if subprocess.run(["sh", "-c", f"command -v {candidate}"],
                          capture_output=True).returncode == 0:
            subprocess.Popen([candidate], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, candidate

    # gtk-launch uses the desktop file id, which is the token with .desktop
    desktop_id = f"{token.lower()}.desktop"
    probes = (
        ["gtk-launch", token.lower()],
        ["gio", "launch", f"/usr/share/applications/{desktop_id}"],
        ["xdg-open", token.lower()],          # last resort: let the session decide
    )
    for cmd in probes:
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return True, " ".join(cmd)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False, f"could not launch '{token}'"


def _dominant_app() -> str:
    """What application owns the screen right now? Used by every verify loop."""
    ok, detail = _verify_screen(
        "Name the dominant application visible in this screenshot "
        "(the window with focus / filling most of the screen).",
        settle=0.0,
    )
    if not ok:
        return ""
    app = _json_field(detail, "app") or detail
    return app.strip().lower()[:60]


def _app_is(app_guesses: tuple[str, ...]) -> tuple[bool, str]:
    ok, detail = _verify_screen(
        "Is one of these applications the dominant window on screen right now: "
        + ", ".join(app_guesses) + "? "
        'Answer {"match": true/false, "app": "<name>", "detail": "..."}.',
        settle=_SETTLE_VISION,
    )
    return ok, detail


# ══════════════════════════════════════════════════════════════════════════════
# Skill: WhatsApp Web
# ══════════════════════════════════════════════════════════════════════════════

_WA_NUMBER_RE = re.compile(r"(?:\+?\d[\d\s\-()]{6,}\d)")
_WA_CC_RE     = re.compile(r"^\+?")


def _wa_digits(text: str) -> str:
    """Extract a dialable number from free text: strip everything but digits,
    keep a leading + if the user wrote one."""
    match = _WA_NUMBER_RE.search(text or "")
    if not match:
        return ""
    raw = match.group(0)
    plus = "+" if raw.lstrip().startswith("+") else ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:                     # too short to be a real number
        return ""
    return plus + digits


def _wa_digits_missing_cc(digits: str) -> bool:
    """A number without country code that isn't local-length. wa.me requires
    the full international form; we flag it instead of messaging a stranger."""
    bare = _WA_CC_RE.sub("", digits)
    return not digits.startswith("+") and len(bare) <= 10


def _wa_open() -> tuple[bool, str]:
    """Get WhatsApp Web on screen — focused tab, logged in."""
    # A WhatsApp window/tab may already exist; focussing beats re-launching.
    for key in _WA_TITLE_KEYS:
        ok, note = kc.focus_window(key)
        if ok:
            _settle(_SETTLE_KEY)
            break

    if _app_is(_WA_TITLE_KEYS)[0]:
        return True, "WhatsApp Web already focused"

    try:
        subprocess.Popen(["xdg-open", _WA_URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, "xdg-open is missing — cannot open a browser"

    _settle(_SETTLE_LAUNCH)
    ok, detail = _app_is(_WA_TITLE_KEYS)
    if not ok:
        return False, f"WhatsApp Web did not appear on screen ({detail or 'no confirmation'})"

    # Logged in, or staring at the QR code?
    logged, log_detail = _verify_screen(
        "Is WhatsApp Web showing a logged-in chat interface (chat list and "
        "conversations visible, NOT a QR code pairing screen)? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.',
        settle=2.5,          # WhatsApp Web takes a moment after first paint
    )
    if not logged:
        return False, ("WhatsApp Web is showing a login/QR screen — "
                       "scan the QR code once with the phone, then ask again")
    return True, log_detail


def _looks_like_a_contact(name: str) -> bool:
    """Is this a conversation name, or just the name of the app?"""
    cleaned = re.sub(r"[^\w\s]+", " ", (name or "").casefold()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return bool(cleaned) and cleaned not in _NOT_A_CONTACT


# The one question asked of a single frame. It is deliberately multi-part:
# which conversation is open (read from the header of the message pane, or the
# highlighted row in the LEFT sidebar if the pane is empty), is the caret in the
# message box, is anything covering the interface, and which browser is this.
# One frame answering four things is what keeps the whole WhatsApp flow at a
# handful of captures instead of a screenshot per question.
_WA_STATE_QUESTION = (
    "Look at WhatsApp Web in Firefox and answer all of these from this ONE "
    "screenshot. (a) Is a conversation OPEN (message history visible in the "
    "right-hand pane)? Read the EXACT name from the header at the top of that "
    "conversation; if the right pane is empty, read the names in the LEFT "
    "sidebar and give the one that is highlighted/active. (b) Does the message "
    "compose box at the bottom of the open conversation show a text caret, "
    "ready for typing? (c) Is any overlay, modal, popup, banner or notification "
    "card covering the interface — anything that would swallow a click? "
    "(d) Which browser is this (firefox, chrome, chromium, other)? "
    'Answer with STRICT JSON only: {"match": true/false, '
    '"name": "<exact contact or group name, or empty>", '
    '"compose": true/false, "blocked": true/false, '
    '"browser": "<browser>", "detail": "<one short sentence>"}. '
    "Never wrap the JSON in prose."
)


def _wa_read_state(settle: float) -> dict:
    """One frame, one question: who is open, is the caret ready, is the view
    blocked, which browser. Never more than one capture per call."""
    _settle(settle)
    frame, note = _grab()
    if frame is None:
        return {"open": False, "name": "", "compose": False, "blocked": False,
                "browser": "", "detail": note}
    ok, payload, why = _vision_json(frame, _WA_STATE_QUESTION)
    if not ok:
        return {"open": False, "name": "", "compose": False, "blocked": False,
                "browser": "", "detail": why}
    name = str(payload.get("name") or "").strip()
    # "WhatsApp" is not a person. The model returns the app name when no
    # conversation is actually open, and that must read as "nothing open" rather
    # than as a chat with a contact called WhatsApp.
    if not _looks_like_a_contact(name):
        return {"open": False, "name": "", "compose": bool(payload.get("compose")),
                "blocked": bool(payload.get("blocked")),
                "browser": str(payload.get("browser") or "")[:20], "detail": why}
    return {
        "open": bool(payload.get("match", True)),
        "name": name[:60],
        "compose": bool(payload.get("compose")),
        "blocked": bool(payload.get("blocked")),
        "browser": str(payload.get("browser") or "")[:20],
        "detail": why,
    }


def _wa_open_chat() -> dict:
    """Which conversation is open in WhatsApp Web right now, and can we type
    into it? One look answers both questions.

    Returns ``{"open": bool, "name": str, "compose": bool, "blocked": bool,
    "browser": str, "detail": str}``. ``open`` means a conversation name was
    legibly read; an unattended window or the QR screen returns False rather
    than a guess, because guessing a name is how a message gets typed to a
    stranger.

    An overlay — a cookie banner, a notification card, a modal — can hide both
    the conversation and the caret. It is dismissed ONCE with Escape and the
    frame is taken again; if it is still there, the caller proceeds with what
    it can see rather than fighting the screen. One retry, never a loop: that
    is the user's rule, and a dismissal loop is how automation starts clicking
    at things nobody asked for.
    """
    state = _wa_read_state(_SETTLE_VISION)
    if state.get("blocked"):
        _press("esc")
        _settle(_SETTLE_KEY)
        state = _wa_read_state(_SETTLE_VISION)
        state["dismissed_overlay"] = True
    return state


def _wa_compose_focus() -> tuple[bool, str]:
    """Make sure the typing caret is in the MESSAGE compose box — the bottom field
    of the open conversation, not the search bar and not the chat list.

    Order matters here, and it is the user's rule written as code: **look first,
    and in the ordinary case that look is the only thing that happens.** If the
    caret is already in the message box, nothing is pressed and nothing is
    clicked — the caller simply types and sends. Extra keystrokes are a fallback
    for when the field is genuinely not aimed, never a routine step.

    The fallbacks themselves are modest and each is verified: Escape closes a
    leftover search panel or emoji popup that would otherwise swallow the text,
    then a click on the box itself. If none of them lands, the caller is told
    plainly and refuses to type — a blind keystroke is how a message ends up in
    the wrong window.
    """
    ready, why = _input_ready()
    if not ready:
        return False, why

    question = (
        "In WhatsApp Web, is the MESSAGE compose box at the bottom of the open "
        "conversation focused — a blinking text caret in that field, not in the "
        "chat-list search bar? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.'
    )

    # 1. Already aimed at the message box: do nothing at all.
    ok, detail = _verify_screen(question, settle=_SETTLE_VISION)
    if ok:
        return True, detail or "message box already had the caret"

    # 2. Something is in the way (a search panel, an emoji picker). Keep the
    # frame this look judged — the click fallback below parses the box's
    # position out of it rather than photographing the same screen again.
    _press("esc")
    _settle(_SETTLE_KEY)
    frame, ok, detail = _frame_and_verify(question, _SETTLE_VISION)
    if ok:
        return True, detail or "message box focused after clearing the panel"

    # 3. Last resort: click the box itself, then verify.
    #
    # The frame already in hand answers two things at once: is WhatsApp Web the
    # window actually in front (a coordinate click lands in whatever is in
    # front, so this cannot be assumed), and where IS the compose box? The
    # parsed point is tried first and the remembered fractions sit behind it as
    # the last resort for a frame the model could not read.
    point, whatsapp_up = ((None, False) if frame is None
                          else _vision_coords(
                              frame, "the message compose box at the bottom of "
                                     "the open conversation"))
    if frame is None:
        whatsapp_up = _app_is(_WA_TITLE_KEYS)[0]
    if not whatsapp_up:
        return False, ("WhatsApp Web is not the window in front, and I will not "
                       "click at screen coordinates in an application I cannot "
                       "see — bring the chat up first, then ask me again")

    targets: list[tuple[float, float]] = ([point] if point else []) + list(_COMPOSE_BOX_XY)
    for fx, fy in targets:
        try:
            w, h = di.size()
            di.click(int(w * fx), int(h * fy))
        except Exception as exc:                               # pragma: no cover
            return False, f"compose-box click failed: {exc}"
        _settle(_SETTLE_KEY)
        ok, detail = _verify_screen(question, settle=_SETTLE_VISION)
        if ok:
            return True, detail or "message box focused"

    return False, ("the message box could not be focused — I will not type into "
                   "a field I could not verify")


def _wa_focus_search() -> tuple[bool, str]:
    """Put the caret in WhatsApp Web's chat-list search box.

    The shortcut is WhatsApp's own (Ctrl+Alt+/), never Ctrl+K — see the note on
    ``_SEARCH_SHORTCUT``. The search box lives at the top of the LEFT sidebar,
    and the verify question says so, so an address bar or a browser search field
    can never be mistaken for it.
    """
    _hotkey(*_SEARCH_SHORTCUT)
    _settle(_SETTLE_KEY)
    focused, detail = _verify_screen(
        "In WhatsApp Web, is the chat-list SEARCH input at the top of the LEFT "
        "sidebar now focused (a search field with a text cursor, ready to "
        "type)? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.',
        settle=_SETTLE_KEY,
    )
    if focused:
        return True, detail

    # Fallback: parse where the box is from one frame, then click. The
    # remembered fraction of the screen stays behind the parsed point as the
    # last resort.
    try:
        w, h = di.size()
        targets: list[tuple[float, float]] = []
        frame, _note = _grab()
        if frame is not None:
            point, _wa_up = _vision_coords(
                frame, "the chat-list SEARCH box at the top of the LEFT sidebar")
            if point:
                targets.append(point)
        targets.append(_SEARCH_BOX_XY)
        focused, detail = False, "no click attempted"
        for fx, fy in targets:
            di.click(int(w * fx), int(h * fy))
            _settle(_SETTLE_KEY)
            focused, detail = _verify_screen(
                "Is the WhatsApp Web search input at the top of the LEFT sidebar "
                "focused now (cursor in that search field, not in the browser)? "
                'Answer {"match": true/false, "app": "...", "detail": "..."}.',
                settle=_SETTLE_KEY,
            )
            if focused:
                return True, detail
        return focused, detail
    except Exception as exc:                                   # pragma: no cover
        return False, f"search-box click failed: {exc}"


def _wa_pick_contact(name: str) -> tuple[bool, str, bool]:
    """Search a contact by name and open their chat. Vision reads the result
    list so a fuzzy match still lands on the right human.

    Returns ``(ok, detail, compose)``. ``compose`` is read from the same look
    that confirms the chat opened, so the caller never has to spend a second
    screenshot asking whether the caret landed in the message box.
    """
    ok, why = _wa_focus_search()
    if not ok:
        return False, f"could not focus the search box ({why})", False

    # The search box is focused and was just verified on screen, so typing the
    # name is authorised — for the next few seconds, in this process only.
    input_guard.authorize("WhatsApp Web", "chat-list search box focused and verified")
    _type(name)
    _settle(_SETTLE_TYPE + 1.2)            # contact list filtering takes a beat

    _press(_ENTER)                    # open the highlighted search result
    _settle(_SETTLE_NAV)

    # ONE look, and it is the look that decides: WHICH chat actually opened,
    # and is the caret already in its message box? Verifying the open
    # conversation by name is strictly stronger than verifying that some
    # search result existed — and asking both questions of the same frame is
    # what keeps this route at two captures instead of three.
    frame, note = _grab()
    if frame is None:
        return False, f"could not confirm the chat opened ({note})", False
    ok, payload, why = _vision_json(
        frame,
        "In WhatsApp Web right now: is a chat conversation open (message "
        "history visible in the right-hand pane), and if so, what is the "
        "EXACT name shown at the top of that conversation? Also say whether "
        "the message compose box at the bottom of it shows a text caret. "
        'Answer with STRICT JSON only: {"match": true/false, '
        '"name": "<exact name at the top of the open conversation, or empty>", '
        '"compose": true/false, "detail": "..."}.',
    )
    if not ok:
        return False, f"could not confirm the chat opened ({why})", False

    opened_name = str(payload.get("name") or "").strip()
    if not bool(payload.get("match")) or not _looks_like_a_contact(opened_name):
        _press("esc")
        return False, (f"no chat matching '{name}' opened "
                       f"({opened_name or 'nothing legible on screen'})"), False
    if not _name_match(opened_name, name):
        # Enter opened somebody else's chat. Nothing has been typed, so this is
        # a clean refusal rather than a wrong message: close the search and say
        # exactly who was in the way.
        _press("esc")
        return False, (f"the chat that opened is '{opened_name}', not '{name}' — "
                       "I searched for the right name but did not type anything"), False
    return True, why, bool(payload.get("compose"))


def _wa_send_text(text: str, *, prefilled: bool = False,
                  caret_verified: bool = False) -> tuple[bool, str]:
    """Send ``text`` from the open conversation's compose box.

    Three modes, and which one is used is decided by what has already been
    SEEN, not by a preference:

    * ``prefilled=True`` — a ``wa.me`` deep link already typed the message;
      verify that and press Enter, never typing it a second time.
    * ``caret_verified=True`` — a look in this same request already showed the
      caret sitting in the message box (the chat-open look, or the focus
      verify), and nothing has been pressed since. This is the user's own rule
      as code: type, Enter, and one look at the end to prove the send. The
      in-box re-check survives only as the recovery path, because a frame that
      shows the text still in the box is the only safe reason to press Enter a
      second time — and the only way to know a failed send did not actually
      send.
    * neither — the belt-and-braces path: type, prove the text is in the box,
      then Enter. Used when nothing is known about the caret.
    """
    _settle(_SETTLE_KEY)

    # The caller only reaches this function once the open conversation has been
    # read from a frame (that is what `caret_verified` and `prefilled` mean), so
    # the permit is issued here and expires with the typing that follows.
    if prefilled or caret_verified:
        input_guard.authorize("WhatsApp Web",
                              "open conversation and its message box verified")

    def _in_box() -> tuple[bool, str]:
        return _verify_screen(
            f"Is the exact text '{text[:80]}' now sitting in WhatsApp Web's "
            "message compose box (present, NOT yet sent)? "
            'Answer {"match": true/false, "app": "...", "detail": "..."}.',
            settle=_SETTLE_VISION,
        )

    def _sent() -> tuple[bool, str]:
        return _verify_screen(
            f"Did the message '{text[:80]}' actually SEND (it now appears as a "
            "sent message bubble in the conversation, message box empty)? "
            'Answer {"match": true/false, "app": "...", "detail": "..."}.',
            settle=_SETTLE_VISION,
        )

    if prefilled:
        landed, detail = _in_box()
        if not landed:
            # The deep link's prefill did not take — type it ourselves, once.
            _type(text, interval=0.015)
            _settle(_SETTLE_TYPE)
            landed, detail = _in_box()
        if not landed:
            return False, ("the text was not in the message box — refusing to "
                           f"press Enter ({detail or 'no confirmation'})")
        _press(_ENTER)
        _settle(1.2)
        sent, detail = _sent()
        return (sent, "message sent and VERIFIED on screen") if sent else \
               (False, f"Enter was pressed but the sent bubble was not confirmed ({detail})")

    if caret_verified:
        _type(text, interval=0.015)
        _settle(_SETTLE_TYPE)
        _press(_ENTER)
        _settle(1.2)
        sent, detail = _sent()
        if sent:
            return True, ("typed straight into the verified message box, Enter "
                          "pressed, and the sent bubble is VERIFIED on screen")
        # Not confirmed. The one safe retry: a frame that still shows the text
        # in the compose box proves the message did not go anywhere.
        still, box_detail = _in_box()
        if still:
            _press(_ENTER)
            _settle(1.2)
            sent2, detail2 = _sent()
            if sent2:
                return True, ("typed, Enter pressed (twice — the first send was "
                              "not visible), and the sent bubble is VERIFIED on screen")
            return False, ("the text is in the box but the send could not be "
                           f"confirmed after a second Enter ({detail2})")
        return False, (f"Enter was pressed but no sent bubble could be confirmed "
                       f"({detail})")

    # Belt-and-braces path: nothing is known about the caret yet, so the compose
    # box is confirmed from one frame BEFORE a single character is typed. This is
    # the look-before-you-type rule as code, and it is also what mints the permit
    # the keystroke guard requires.
    ready, ready_detail = _verify_screen(
        "Is WhatsApp Web's message compose box visible at the bottom of the open "
        "conversation (empty, or holding only its placeholder text)? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.',
        settle=_SETTLE_VISION,
    )
    if not ready:
        return False, ("the message box was not confirmed on screen, so I did not "
                       f"type anything ({ready_detail or 'no confirmation'})")
    input_guard.authorize("WhatsApp Web", f"compose box confirmed: {ready_detail}")

    _type(text, interval=0.015)
    _settle(_SETTLE_TYPE)
    landed, detail = _in_box()
    if not landed:
        return False, ("the text was not in the message box — refusing to press "
                       f"Enter ({detail or 'no confirmation'})")

    _press(_ENTER)
    _settle(1.2)
    sent, detail = _sent()
    return (sent, "message sent and VERIFIED on screen") if sent else \
           (False, f"Enter was pressed but the sent bubble was not confirmed ({detail})")


def _wa_try_direct(name: str, text: str, state: Optional[dict] = None) -> Optional[str]:
    """If the conversation with ``name`` is ALREADY open, reply in it and return
    the spoken result. Returns ``None`` when the open chat is somebody else, so
    the caller can fall through to searching.

    This is the whole behavioural point: a chat that is already on screen needs
    no search, no menu, and no clicking around the chat list — the message goes
    into the box that is already there, aimed and verified first.

    ``state`` lets a caller hand over a look it just took, so the same screen is
    not photographed and re-read twice.
    """
    state = state or _wa_open_chat()
    if not state.get("open"):
        _context_write(app="whatsapp", chat_app="whatsapp", chat="",
                       compose_ready=bool(state.get("compose")),
                       note="no conversation open in WhatsApp Web")
        return None

    open_name = str(state.get("name") or "")
    if not _name_match(open_name, name):
        # Somebody else's chat is open. Remember who, so the model can say
        # "you are in the chat with X, not Y" instead of guessing.
        _context_write(app="whatsapp", chat_app="whatsapp", chat=open_name,
                       compose_ready=bool(state.get("compose")),
                       note=f"open chat is {open_name}, asked for {name}")
        return None

    # The look that found this chat also answered the second question — is the
    # caret already in the message box? If it is, nothing is pressed and nothing
    # is clicked: the text goes in and Enter follows. Asking again would mean a
    # second screenshot for a question the same frame already answered.
    caret = bool(state.get("compose"))
    if caret:
        focused, why = True, "the same look showed the caret already in the message box"
    else:
        focused, why = _wa_compose_focus()
    if not focused:
        return (f"The chat with {open_name} is already open, sir, but {why}. "
                "I would rather not type blind — tell me to try again.")

    ok, why = _wa_send_text(text, caret_verified=True)
    _context_write(app="whatsapp", chat_app="whatsapp", chat=open_name,
                   compose_ready=True, last_action="whatsapp:direct",
                   last_message=text[:120],
                   note=f"replied in the chat that was already open ({open_name})")
    if ok:
        return (f"You were already chatting with {open_name}, sir — I typed "
                "straight into that conversation, no searching, and the send "
                "is VERIFIED on screen.")
    return (f"I typed into the open chat with {open_name}, but could not confirm "
            f"the send: {why}")


def _skill_whatsapp(task: str, contact: str = "", message: str = "") -> str:
    """Whole-task WhatsApp automation: open, find, type, send, verify.

    Order is the point. The already-open conversation is checked FIRST — if the
    chat you want is on screen, nothing is clicked and nothing is searched and
    the message goes straight into the compose box that is already there. Only
    after that does the chat-list search run, and even then the compose box is
    aimed at and verified before a single character is typed.
    """
    _audit("whatsapp", task)

    ready, why = _input_ready()
    if not ready:
        return f"Sir, I cannot drive the keyboard yet — {why}"

    digits = _wa_digits(task)
    if message.strip():
        text_to_send = message.strip()
    else:
        m = re.search(r"(?:say|text|send|message)\s+[\"“']?(.+?)[\"”']?\s*(?:to\s+\S+)?\s*$",
                      task, re.IGNORECASE)
        text_to_send = (m.group(1) if m else "").strip() or "Hello!"

    if digits:
        # ── Route 1: wa.me deep link — deterministic, zero clicking ────────
        if _wa_digits_missing_cc(digits):
            return (f"The number {digits} has no country code, sir. Give it in "
                    f"international form (for example +91...) and I will send it.")
        from urllib.parse import quote
        deep = f"https://wa.me/{_WA_CC_RE.sub('', digits)}?text={quote(text_to_send)}"
        try:
            subprocess.Popen(["xdg-open", deep],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            return "xdg-open is missing — cannot open the browser."
        _settle(_SETTLE_LAUNCH + 2.0)
        # The deep link already typed the message into the compose box; verify
        # that rather than typing it a second time.
        ok, detail = _wa_send_text(text_to_send, prefilled=True)
        _context_write(app="whatsapp", chat_app="whatsapp", chat=digits,
                       compose_ready=True, last_action="whatsapp:number",
                       last_message=text_to_send[:120],
                       note=f"messaged the number {digits}")
        return (f"Message to {digits}: {detail}" if ok
                else f"Message to {digits} NOT confirmed — {detail}")

    # ── Who is the target? ────────────────────────────────────────────────
    name = (contact or "").strip()
    if not name:
        name_m = re.search(r"(?:to|chat with|open)\s+([A-Za-z][\w .'-]{1,40})", task)
        name = name_m.group(1).strip() if name_m else ""

    if not name:
        # No name given. If a conversation is already open, that IS the target —
        # the user is mid-conversation and simply wants words in that chat.
        ctx = _desktop_context()
        open_now = str(ctx.get("chat") or "")
        if open_now:
            direct = _wa_try_direct(open_now, text_to_send)
            if direct:
                return direct
        return ("Whom should I message, sir? Give me a contact name or a phone "
                "number (with country code).")

    # ── The brain: look before you click ──────────────────────────────────
    # One look, taken here and handed to the direct-reply attempt, so the same
    # screen is not photographed and re-read twice in the same decision.
    state = _wa_open_chat()
    direct = _wa_try_direct(name, text_to_send, state)
    if direct:
        return direct

    # ── Chat-list route ───────────────────────────────────────────────────
    ok, why = _wa_open()
    if not ok:
        return why

    # Focusing may have revealed the right chat (it was open behind another
    # window), so look once more before spending a search on it.
    direct = _wa_try_direct(name, text_to_send)
    if direct:
        return direct

    ok, why, compose = _wa_pick_contact(name)
    if not ok:
        return f"Could not open the chat with {name}: {why}"

    # Enter in the search box opens the chat but does NOT always hand the caret
    # to the message box; the look that confirmed the chat opened already asked
    # about the caret, so only aim at the box when that look did not see one.
    if not compose:
        focused, why = _wa_compose_focus()
        if not focused:
            return (f"The chat with {name} is open, but {why}. Say 'retry' and I "
                    "will aim at the message box again.")

    ok, why = _wa_send_text(text_to_send, caret_verified=True)
    _context_write(app="whatsapp", chat_app="whatsapp", chat=name,
                   compose_ready=True, last_action="whatsapp:search",
                   last_message=text_to_send[:120],
                   note=f"searched the chat list for {name} and messaged them")
    return (f"Message sent to {name} — {why}." if ok
            else f"Message to {name} NOT sent: {why}.")


# ══════════════════════════════════════════════════════════════════════════════
# Context brain — "where am I?", "what does this chat say?", "reply here"
# ══════════════════════════════════════════════════════════════════════════════

def _skill_context(task: str = "") -> str:
    """A short, cheap brief of the desktop right now.

    Remembered context first; a fresh look only when the memory is stale or the
    user explicitly asks for one. This is what stops the assistant from walking
    back to a chat list it does not need to visit.
    """
    _audit("context", task or "where am i")
    force = bool(re.search(r"\b(fresh|now|look|check|refresh)\b", task or "", re.IGNORECASE))
    ctx = _desktop_context(force=force)

    if ctx.get("error") and not ctx.get("app"):
        return f"I cannot see the screen right now — {ctx['error']}"

    bits: list[str] = []
    if ctx.get("app"):
        bits.append(f"focused app: {ctx['app']}")
    if ctx.get("chat"):
        bits.append(f"{ctx.get('chat_app') or 'chat'} conversation open: {ctx['chat']}")
        if ctx.get("compose_ready"):
            bits.append("message box ready")
    if int(ctx.get("unread") or 0) > 0:
        bits.append(f"about {ctx['unread']} unread")
    if ctx.get("note"):
        bits.append(str(ctx["note"]))

    age = _context_age()
    when = "just now" if age <= _CONTEXT_FRESH else f"{age:.0f} seconds ago"
    if not bits:
        return f"I cannot tell what is focused on the desktop ({when})."
    spoken = f"Right now, {when}: " + "; ".join(bits[:6]) + "."
    if ctx.get("chat"):
        spoken += (" Say the word and I will type your reply straight into that "
                   "open chat — no searching.")
    return spoken


def _skill_current_chat(task: str = "") -> str:
    """What the conversation already on screen actually says."""
    _audit("current_chat", task or "read the open chat")
    ctx = _desktop_context(force=True)
    if not ctx.get("chat"):
        return ("No conversation is open on screen, sir. Open the chat, or tell "
                "me who to message and I will find them.")

    frame, note = _grab()
    if frame is None:
        return f"Could not capture the screen: {note}"
    ok, payload, why = _vision_json(
        frame,
        f"Read the open conversation with '{ctx['chat']}'. List the last few "
        "messages oldest-first as 'SENDER: text', then one sentence on what they "
        "seem to want. "
        'Answer with STRICT JSON only: {"match": true, "chat": "<name>", '
        '"detail": "<the reading, at most 3 sentences>"}.',
        timeout=30.0,
        max_tokens=700,
    )
    if not ok:
        return f"I could not read the open chat: {why}"
    summary = str(payload.get("detail") or "").strip()
    read_name = str(payload.get("chat") or "").strip()
    _context_write(app="whatsapp", chat_app="whatsapp",
                   chat=(read_name[:60] if _looks_like_a_contact(read_name)
                         else str(ctx["chat"])[:60]),
                   compose_ready=bool(ctx.get("compose_ready")),
                   note=f"read the open chat with {ctx['chat']}")
    if not summary:
        return f"The chat with {ctx['chat']} is open, but I could not make out its content."
    return f"The open chat with {ctx['chat']}: {summary[:600]}"


def _skill_reply_current(text: str, contact: str = "") -> str:
    """Type into the conversation that is ALREADY open — no searching, no
    clicking through the chat list. Falls back to the normal WhatsApp path when
    nobody is open, and says so when the open chat is not the person named.
    """
    _audit("reply_current", contact or text)
    message = (text or "").strip()
    if not message:
        return "What should I say, sir?"

    ready, why = _input_ready()
    if not ready:
        return f"Keyboard control unavailable — {why}"

    state = _wa_open_chat()
    if not state.get("open"):
        if contact:
            return _skill_whatsapp(message, contact=contact, message=message)
        # A conversation may be open somewhere that is NOT a chat app we can
        # safely type into (a web page, an AI chat, a document). "Reply here"
        # must never be read as "type on whatever happens to be focused" — that
        # is exactly how a message ends up somewhere nobody asked for.
        ctx = _desktop_context()
        other = str(ctx.get("chat_app") or "")
        if ctx.get("chat") and other not in ("whatsapp", "none", ""):
            return (f"The open conversation is with {ctx['chat']} in {other}, sir — "
                    "not WhatsApp — so I will not type into it blind. Tell me who "
                    "to message on WhatsApp and I will do it there.")
        return ("No conversation is open on screen, sir — tell me who to message "
                "and I will open that chat first.")

    open_name = str(state.get("name") or "")
    caret = bool(state.get("compose"))
    if caret:
        focused, why = True, "the same look showed the caret already in the message box"
    else:
        focused, why = _wa_compose_focus()
    if not focused:
        return f"The chat with {open_name} is open, but {why}"

    ok, why = _wa_send_text(message, caret_verified=True)
    _context_write(app="whatsapp", chat_app="whatsapp", chat=open_name,
                   compose_ready=True, last_action="reply_current",
                   last_message=message[:120],
                   note=f"replied in the open chat with {open_name}")
    if not ok:
        return f"Could not confirm the message in the chat with {open_name}: {why}"
    if contact and not _name_match(open_name, contact):
        return (f"Sent in the open chat, sir — but heads up: that chat is with "
                f"{open_name}, not {contact}. Say the word if I should take it "
                "to the right person instead.")
    return f"Typed and sent in the open chat with {open_name} — {why}."


# ══════════════════════════════════════════════════════════════════════════════
# Skill: YouTube  (search + play first result, fullscreen on request)
# ══════════════════════════════════════════════════════════════════════════════

def _skill_youtube(task: str) -> str:
    _audit("youtube", task)
    q = re.search(r"(?:play|search|open|find)\s+(?:youtube\s+)?(?:for\s+)?(.+)",
                  task, re.IGNORECASE)
    query = (q.group(1) if q else "").strip()
    query = re.sub(r"\s+on\s+youtube\s*$", "", query, flags=re.IGNORECASE)
    if not query:
        return "What should I search on YouTube, sir?"
    if query.lower().startswith("youtube"):
        query = query[7:].strip() or query

    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return "xdg-open is missing — cannot open the browser."
    _settle(_SETTLE_LAUNCH)

    results, detail = _verify_screen(
        "Are YouTube search results visible on screen (a list of video "
        "thumbnails and titles)? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.',
        settle=1.5,
    )
    if not results:
        return f"YouTube results did not load ({detail or 'no confirmation'})."

    wants_play = any(w in task.lower() for w in ("play", "watch", "fullscreen", "full screen"))
    fullscreen = "full screen" in task.lower() or "fullscreen" in task.lower()
    if wants_play:
        # Click the first result — it reliably sits in the upper-left area of
        # the results grid on every layout checked.
        try:
            w, h = di.size()
            di.click(int(w * 0.30), int(h * 0.30))
            _settle(_SETTLE_NAV + 1.0)
        except Exception as exc:
            return f"Results are open but I could not click the first video ({exc})."
        playing, pdetail = _verify_screen(
            "Is a YouTube VIDEO PLAYER now on screen (video content visible, "
            "not the search list)? "
            'Answer {"match": true/false, "app": "...", "detail": "..."}.',
            settle=1.0,
        )
        if not playing:
            return f"Video did not start ({pdetail or 'no player on screen'})."
        if fullscreen:
            _press("f")                  # YouTube: f toggles fullscreen on the player
            _settle(_SETTLE_KEY)
        return f"Playing '{query}' on YouTube{', fullscreen' if fullscreen else ''} — verified on screen."
    return f"YouTube is open with results for '{query}'."


# ══════════════════════════════════════════════════════════════════════════════
# Skill: web search in the default browser
# ══════════════════════════════════════════════════════════════════════════════

def _skill_web_search(task: str) -> str:
    _audit("web_search", task)
    if _looks_like_messaging(task):
        # The user's own rule: a message is typed into the chat, never searched
        # for and never opened in a browser. Refusing here keeps a wandering
        # model from turning "text Rayan" into a Google search.
        return ("That is a message to send, not a web search — I do not search the "
                "web for conversations. Use pc_automation action='reply' if the chat "
                "is open, or action='whatsapp' with the contact and message.")
    q = re.search(r"(?:search(?:\s+the)?\s+(?:web|internet)\s+for\s*|google\s+)(.+)",
                  task, re.IGNORECASE)
    query = (q.group(1) if q else "").strip(" ?.")
    if not query:
        return "What should I search for, sir?"
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return "xdg-open is missing — cannot open the browser."
    _settle(_SETTLE_LAUNCH)
    ok, detail = _verify_screen(
        f"Do Google search results for '{query[:60]}' appear on screen? "
        'Answer {"match": true/false, "app": "...", "detail": "..."}.',
        settle=1.0,
    )
    return (f"Search results for '{query}' are on screen — {detail}."
            if ok else f"Opened the search; results not confirmed ({detail}).")


# ══════════════════════════════════════════════════════════════════════════════
# Skill: open / close applications
# ══════════════════════════════════════════════════════════════════════════════

def _skill_open_app(task: str) -> str:
    _audit("open_app", task)
    if _looks_like_messaging(task) and not re.search(
            r"\b(open|launch|start)\s+whatsapp", task, re.IGNORECASE):
        return ("That is a message, not an application to launch — opening a "
                "browser for it would lose the conversation already on screen. "
                "Use pc_automation action='reply' (chat open) or action='whatsapp' "
                "(named contact) and I will type it directly.")
    m = re.search(r"(?:open|launch|start|run)\s+(?:the\s+)?(?:app\s+|application\s+)?"
                  r"([A-Za-z0-9 .+#-]{2,40})", task, re.IGNORECASE)
    if not m:
        return "Which application should I open, sir?"
    token = m.group(1).strip().rstrip(".!")
    ok, how = _launch(token)
    if not ok:
        return f"Could not launch '{token}' ({how})."
    _settle(_SETTLE_LAUNCH)
    guesses = tuple({token.lower(), *()})
    onscreen, detail = _app_is(guesses)
    if onscreen:
        return f"{token} is open and on screen — {detail}."
    return f"{token} launched ({how}); window not confirmed on screen yet."


def _skill_close_app(task: str) -> str:
    _audit("close_app", task)
    m = re.search(r"(?:close|kill|quit|exit)\s+(?:the\s+)?([A-Za-z0-9 .+#-]{2,40})",
                  task, re.IGNORECASE)
    token = (m.group(1) if m else "").strip().rstrip(".!")
    if not token:
        # no target: close the focused window
        ok, note = kc.window_action("close")
        return f"Focused window close: {'done' if ok else note}."
    # pkill by name is honest but blunt — try graceful window close first
    if kc.focus_window(token)[0]:
        _settle(_SETTLE_KEY)
        ok, note = kc.window_action("close")
        return f"{token} closed gracefully." if ok else f"Close not confirmed: {note}."
    r = subprocess.run(["pkill", "-f", token], capture_output=True)
    if r.returncode == 0:
        return f"{token} processes signalled to terminate."
    return f"No running process matched '{token}'."


# ══════════════════════════════════════════════════════════════════════════════
# Skill: window management (GNOME Shell / Mutter via keyboard is Wayland-legal)
# ══════════════════════════════════════════════════════════════════════════════

_WINDOW_MOVES: dict[str, tuple[tuple[str, ...], ...]] = {
    "fullscreen":  (("super", "up"),),          # maximize; YouTube 'f' handled there
    "maximize":    (("super", "up"),),
    "minimize":    (("super", "h"),),
    "snap_left":   (("super", "left"),),
    "snap_right":  (("super", "right"),),
    "switch":      (("alt", "tab"),),
    "close":       (("alt", "f4"),),
}


def _skill_window(task: str) -> str:
    _audit("window", task)
    t = task.lower()
    ready, why = _input_ready()
    if not ready:
        return f"Keyboard control unavailable — {why}"

    if "switch" in t or "next window" in t or "alt tab" in t:
        _hotkey("alt", "tab")
        _settle(_SETTLE_KEY)
        return "Switched window (Alt-Tab)."
    if "snap" in t and ("left" in t or "half" in t):
        _hotkey("super", "left"); _settle(_SETTLE_KEY)
        return "Window snapped left."
    if "snap" in t and ("right" in t or "half" in t):
        _hotkey("super", "right"); _settle(_SETTLE_KEY)
        return "Window snapped right."
    if "full" in t:
        _hotkey("super", "up"); _settle(_SETTLE_KEY)
        return "Window maximized."
    if "minimize" in t or "minimise" in t:
        _hotkey("super", "h"); _settle(_SETTLE_KEY)
        return "Window minimized."
    if "close" in t:
        _hotkey("alt", "f4"); _settle(_SETTLE_KEY)
        return "Window closed."
    if "focus" in t:
        m = re.search(r"focus\s+(?:the\s+)?(.+)", task, re.IGNORECASE)
        target = (m.group(1) if m else "").strip()
        if target:
            ok, note = kc.focus_window(target)
            return f"Focused '{target}'." if ok else f"Focus failed: {note}"
    return "Which window action, sir: snap left/right, maximize, minimize, switch, close, focus <app>?"


# ══════════════════════════════════════════════════════════════════════════════
# Skill: media keys (playerctl first — it works without focus on PipeWire/MPRIS)
# ══════════════════════════════════════════════════════════════════════════════

def _media_cmd(action: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["playerctl", action], capture_output=True, timeout=5)
        if r.returncode == 0:
            return True, "playerctl"
    except FileNotFoundError:
        pass
    return False, "playerctl not available"


def _skill_media(task: str) -> str:
    _audit("media", task)
    t = task.lower()
    table = (
        (("pause",), "pause"),
        (("play music", "play song", "resume", "unpause"), "play"),
        (("play/pause", "play pause", "toggle"), "play-pause"),
        (("next", "skip"), "next"),
        (("previous", "back song", "last song"), "previous"),
    )
    for keys, action in table:
        if any(k in t for k in keys):
            ok, how = _media_cmd(action)
            if ok:
                return f"Media {action} done via {how}."
            # fallback: XF86 keys reach GNOME regardless of playerctl
            ready, why = _input_ready()
            if not ready:
                return f"Cannot send media keys — {why}"
            keymap = {"pause": "xf86audiopause", "play": "xf86audioplay",
                      "play-pause": "xf86audioplay", "next": "xf86audionext",
                      "previous": "xf86audioprev"}
            _press(keymap[action])
            _settle(_SETTLE_KEY)
            return f"Media {action} sent via media key."
    if "volume" in t:
        m = re.search(r"(\d{1,3})\s*(?:%|percent)", t)
        if m and ("set" in t):
            return str(kc.volume_set(min(100, max(0, int(m.group(1))))))
        if "up" in t:
            return str(kc.volume_step(10))
        if "down" in t:
            return str(kc.volume_step(-10))
        if "mute" in t:
            return str(kc.mute_toggle())
        return f"Volume is {kc.volume_get()}%."
    return "Media: pause / play / next / previous / volume up|down|set N%|mute."


# ══════════════════════════════════════════════════════════════════════════════
# Skill: clipboard
# ══════════════════════════════════════════════════════════════════════════════

def _skill_clipboard(task: str) -> str:
    _audit("clipboard", task)
    t = task.lower()
    m = re.search(r"copy\s+[\"“'](.+?)[\"”']\s*(?:to clipboard)?\s*$", task,
                  re.IGNORECASE)
    if "paste" in t:
        ok, note = kc.clipboard_paste()
        # clipboard_paste returns clipboard text; to paste INTO the focused
        # window we drive ctrl+v
        ready, why = _input_ready()
        if ready:
            _hotkey("ctrl", "v")
            _settle(_SETTLE_KEY)
            return "Pasted clipboard into the focused window."
        return f"Clipboard read: {note}; keyboard unavailable for pasting ({why})."
    if m:
        ok, note = kc.clipboard_copy(m.group(1))
        return "Copied to clipboard." if ok else f"Copy failed: {note}"
    if "read" in t or "show" in t or "what" in t:
        ok, note = kc.clipboard_paste()
        return f"Clipboard holds: {note[:200]}" if ok else f"Clipboard read failed: {note}"
    return "Clipboard: copy '<text>', paste, or read."


# ══════════════════════════════════════════════════════════════════════════════
# Skill: desktop notifications
# ══════════════════════════════════════════════════════════════════════════════

def _skill_notify(task: str) -> str:
    _audit("notify", task)
    m = re.search(r"notify(?:\s+me)?(?:\s+(?:that|about|with))?\s+['\"]?(.+?)['\"]?$",
                  task, re.IGNORECASE)
    body = (m.group(1) if m else task).strip()
    ok, note = kc.notify("JARVIS", body[:180])
    return "Notification posted." if ok else f"Notification failed: {note}"


# ══════════════════════════════════════════════════════════════════════════════
# Skill: screenshot + read screen text
# ══════════════════════════════════════════════════════════════════════════════

def _skill_read_screen(task: str) -> str:
    _audit("read_screen", task)
    allowed, why = vision_budget.claim("pc_automation.read_screen")
    if not allowed:
        return why
    frame, note = _grab()
    if frame is None:
        return f"Could not capture the screen: {note}"
    try:
        from core import vision_client
    except Exception as exc:                                   # pragma: no cover
        return f"vision unavailable: {exc}"
    result = vision_client.analyze(
        frame,
        "Transcribe or summarise the important text content on this screen. "
        "If it is a conversation, list each message with its sender. Keep it short.",
        json_mode=False,
        timeout=30.0,
        max_output_tokens=600,
    )
    if not result.ok:
        return f"Screen read failed: {result.error}"
    return f"On screen: {result.text[:600]}"


def _skill_screenshot(task: str) -> str:
    _audit("screenshot", task)
    allowed, why = vision_budget.claim("pc_automation.screenshot")
    if not allowed:
        return why
    ok, where = kc.screenshot()
    return f"Screenshot saved to {where}." if ok else f"Screenshot failed: {where}"


# ══════════════════════════════════════════════════════════════════════════════
# Skill: type text anywhere
# ══════════════════════════════════════════════════════════════════════════════

def _skill_type_text(task: str) -> str:
    _audit("type_text", task)
    ready, why = _input_ready()
    if not ready:
        return f"Keyboard control unavailable — {why}"
    # "type this: hello" and "type in WhatsApp: hello" both mean the text after the
    # colon, and "… and press enter" is an instruction, not part of the message.
    # Both were being typed into the field verbatim, which is exactly the kind of
    # small wrongness a person only notices after it has been sent.
    m = re.search(r"type\s+(?:this\s*:?\s*|in\s+\S+\s*:?\s*)?['\"]?(.+?)['\"]?\s*$",
                  task, re.IGNORECASE)
    text = (m.group(1) if m else "").strip()
    text = re.sub(r"[\s,]*(?:and\s+|then\s+)?(?:press\s+)?enter\b.*$", "", text,
                  flags=re.IGNORECASE).strip(" \t,.")
    if not text:
        return "What should I type, sir?"
    press_enter = bool(re.search(r"(then\s+)?(press\s+)?enter\b", task, re.IGNORECASE))

    # Look before you type. This skill used to type into whatever window had
    # focus, which is one of the ways "who r u" reached a page nobody meant to
    # touch. One frame answers both questions the guard needs: is there a text
    # field, and in which application. No field, no typing.
    frame, note = _grab()
    if frame is None:
        return f"I could not look at the screen to find a text field ({note})."
    ok, payload, why = _vision_json(
        frame,
        "Look at this screenshot. Is there a visible text field, input box or "
        "message box with the keyboard caret — somewhere a person could be "
        'typing right now? Answer {"match": true/false, "app": "<the '
        'application>", "detail": "<where it is>"}.',
    )
    if not (ok and payload.get("match")):
        detail = str(payload.get("detail") or why or "no text field in view")[:140]
        return (f"I could not find a text field on screen to type into ({detail}), "
                "so I typed nothing. Open the window or the chat you want and ask "
                "me again.")
    input_guard.authorize(str(payload.get("app") or "verified text field"),
                          f"text field seen: {payload.get('detail', '')}")

    _type(text, interval=0.015)
    _settle(_SETTLE_TYPE)
    if press_enter:
        _press(_ENTER)
        _settle(_SETTLE_KEY)
        return f"Typed and sent: {text[:80]}"
    return f"Typed: {text[:80]}"


# ══════════════════════════════════════════════════════════════════════════════
# Skill: files  (Downloads, Documents, open a path, recent files)
# ══════════════════════════════════════════════════════════════════════════════

def _skill_files(task: str) -> str:
    _audit("files", task)
    t = task.lower()
    home = Path.home()
    if "download" in t:
        d = home / "Downloads"
        items = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        listing = ", ".join(p.name for p in items) or "(empty)"
        return f"Newest in Downloads: {listing}"
    if "document" in t:
        d = home / "Documents"
        items = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        listing = ", ".join(p.name for p in items) or "(empty)"
        return f"Newest in Documents: {listing}"
    m = re.search(r"open\s+(?:the\s+)?(?:file|folder|path)\s+(['\"]?)(.+\S)\1",
                  task, re.IGNORECASE)
    if m:
        target = Path(m.group(2)).expanduser()
        if not target.exists():
            return f"'{target}' does not exist."
        try:
            subprocess.Popen(["xdg-open", str(target)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened {target.name}."
        except FileNotFoundError:
            return "xdg-open missing."
    return "Files: what's in Downloads/Documents, or open <path>."


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ══════════════════════════════════════════════════════════════════════════════

_SKILLS: list[tuple[tuple[str, ...], Callable[[str], str]]] = [
    (("whatsapp", "wa.me", "message my", "text my", "reply to my"), _skill_whatsapp),
    (("youtube",),                                                 _skill_youtube),
    (("search the web", "search web", "google "),                  _skill_web_search),
    (("close the app", "close app", "kill ", "quit the"),          _skill_close_app),
    (("open the app", "open app", "launch the", "start the"),      _skill_open_app),
    (("snap", "switch window", "minimize", "maximize", "full screen window",
      "close the window", "focus "),                               _skill_window),
    (("play music", "pause", "next song", "previous song", "volume",
      "skip song", "media"),                                       _skill_media),
    (("clipboard", "copy this", "paste"),                          _skill_clipboard),
    (("notify me", "send a notification", "notification"),         _skill_notify),
    (("what's on my screen", "read my screen", "read the screen",
      "what is on my screen", "screenshot"),                       _skill_read_screen),
    (("take a screenshot", "screenshot the screen"),               _skill_screenshot),
    (("type this", "type in", "type and send"),                    _skill_type_text),
    (("downloads", "documents", "open the file", "open the folder"), _skill_files),
]


# A request that means "say words to a person". It is checked BEFORE every other
# skill, because the failure it prevents is specific and embarrassing: on
# 27 September the user asked JARVIS to text someone, the phrasing missed the
# narrow WhatsApp keys below, no skill matched, and the model — left to
# improvise — opened a website instead of typing into the chat that was already
# on screen. Messaging never means "navigate somewhere".
_MESSAGING_RE = re.compile(
    r"\b(whatsapp|message|messages|text|texts|dm|dms|reply|respond|send|tell|say)\b",
    re.IGNORECASE,
)
# Verbs that mean "move the desktop somewhere", which must never win over a
# messaging request.
_NAVIGATION_RE = re.compile(
    r"\b(open|launch|start|visit|browse|website|web ?site|browser|google|search)\b",
    re.IGNORECASE,
)
_PERSON_RE = re.compile(
    r"\b(to|my|for|with)\s+([A-Za-z][A-Za-z'\-]{1,24})", re.IGNORECASE)


def _looks_like_messaging(task: str) -> bool:
    """True when the goal is to say something to a person.

    Narrow on purpose: a sentence that merely contains "tell" inside a search
    request must not be captured, so a contact-shaped phrase ("to Rayan",
    "my sister") or the word WhatsApp is required as well.
    """
    text = str(task or "")
    if "whatsapp" in text.casefold():
        return True
    if not _MESSAGING_RE.search(text):
        return False
    return bool(_PERSON_RE.search(text))


def _detect_skill(task: str) -> Optional[Callable[[str], str]]:
    t = f" {task.lower().strip()} "
    if _looks_like_messaging(task):
        return _skill_whatsapp
    for keys, fn in _SKILLS:
        if any(k in t for k in keys):
            return fn
    return None


_HELP = ("Tell me what to do on the computer, sir — for example: "
         "'WhatsApp Ravi: running 10 minutes late', "
         "'reply in the chat that is open: on my way', "
         "'play lofi beats on youtube fullscreen', 'what's on my screen'.")


def _run_task(task: str, contact: str = "", message: str = "") -> str:
    """Route a natural-language goal to one of the skills behind it."""
    if contact or (message and not task):
        return _skill_whatsapp(task, contact=contact, message=message)

    skill = _detect_skill(task)
    if skill is None:
        # Nothing matched. A bare "reply here" still works, because the context
        # brain can see which conversation is open — that is the most useful
        # safe default when the phrasing is not one of the known shapes.
        if re.search(r"\b(reply|respond|answer|say|tell|type)\b", task, re.IGNORECASE):
            return _skill_reply_current(task)
        return (_skill_read_screen(task) +
                " (No direct automation matched that; I read the screen instead. "
                "Try naming the app: WhatsApp, YouTube, open/close <app>, window, "
                "media, clipboard, notify, files.)")
    if skill is _skill_whatsapp:
        return _skill_whatsapp(task, contact=contact, message=message)
    return skill(task)


_CONTEXT_ACTIONS = ("context", "where_am_i", "whereami", "status", "what_is_open")
_CHAT_ACTIONS    = ("current_chat", "read_current", "read_chat", "what_did_they_say")
_REPLY_ACTIONS   = ("reply", "reply_current", "type_current", "send_current",
                    "chat_back", "reply_here")
_WA_ACTIONS      = ("whatsapp", "whatsapp_send", "message_contact", "send_whatsapp")
_TASK_ACTIONS    = ("", "auto", "do", "task", "run")


def _route(params: dict) -> tuple[str, Callable[[], str]]:
    """Map the parameters to ``(label, thunk)``.

    Deliberately pure: no screenshots, no keystrokes, no vision. Keeping the
    decision separate from the doing is what makes the routing testable without
    driving the desktop, which matters when the wrong route means a message to
    the wrong person.
    """
    action = str(params.get("action") or "").strip().lower()
    task = str(params.get("task") or "").strip()
    contact = str(params.get("contact") or "").strip()
    message = str(params.get("message") or "").strip()

    if action in _CONTEXT_ACTIONS:
        return "context", (lambda: _skill_context(task))
    if action in _CHAT_ACTIONS:
        return "current_chat", (lambda: _skill_current_chat(task))
    if action in _REPLY_ACTIONS:
        return "reply", (lambda: _skill_reply_current(message or task, contact))
    if action in _WA_ACTIONS:
        phrase = task or f"message {contact} say {message}"
        return "whatsapp", (lambda: _skill_whatsapp(phrase, contact=contact, message=message))
    if action in _TASK_ACTIONS:
        if not task and not (contact or message):
            return "help", (lambda: _HELP)
        return "task", (lambda: _run_task(task, contact, message))
    return "unknown", (lambda: (
        f"I do not know the pc_automation action '{action}', sir. Use 'context', "
        "'current_chat', 'reply', 'whatsapp', or leave `action` out and give me "
        "a `task`."))


def run(parameters: dict, ctx: dict | None = None) -> str:
    """Entry point.

    Two ways in, and the first one is the one that keeps the assistant from
    blundering around the chat list:

    *   ``action`` — a named verb. ``context`` says what is on the desktop,
        ``current_chat`` reads the open conversation, ``reply`` types into the
        open conversation without searching, ``whatsapp`` messages a named
        contact, ``task``/omitted routes a sentence through the skill detector.
    *   ``task`` — the goal in the user's own words (the original interface).

    Everything returns one speakable sentence and never raises.
    """
    try:
        _label, thunk = _route(parameters or {})
        return thunk()
    except PermissionError as refusal:
        # The keystroke guard declined: the screen was never verified for typing.
        # This is a decision, not a crash, so it says so in one sentence and does
        # not touch the keyboard at all.
        _audit("refused", f"keystroke guard: {refusal}")
        return str(refusal)
    except Exception as exc:                                   # never crash Jarvis
        _audit("error", f"{type(exc).__name__}: {exc}")
        return f"The automation hit an error: {type(exc).__name__}: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# TOOL declaration — this is what makes the file an action
# ══════════════════════════════════════════════════════════════════════════════

TOOL = {
    "name": "pc_automation",
    "description": (
        "Your hands on this Kali GNOME Wayland machine. Use it for WHOLE TASKS: "
        "WhatsApp Web, YouTube, Google searches, opening/closing apps, windows, "
        "media, clipboard, notifications, reading the screen, typing, files. "
        "\n\n"
        "LOOK BEFORE YOU CLICK. Call action='context' first when you are unsure "
        "what is on the desktop; it reports the focused app and, crucially, "
        "WHICH CONVERSATION IS ALREADY OPEN in WhatsApp Web. "
        "If a chat is already open, DO NOT search, DO NOT go back to the chat "
        "list, DO NOT click around the main menu: call action='reply' with "
        "message=<what to say> and it types straight into that open chat's "
        "message box and verifies the send. If the user names somebody else, "
        "use action='whatsapp' with contact=<name> and message=<text>. Use "
        "action='current_chat' to read what the open conversation says. "
        "\n\n"
        "For everything else pass the user's goal as one sentence in `task` "
        "(for example 'play lofi on youtube fullscreen', 'snap the window "
        "left', 'what is in my downloads'). Prefer this over computer_control "
        "for anything app-level; computer_control is for a single raw "
        "keystroke the user explicitly describes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "context | current_chat | reply | whatsapp | task. "
                    "'context' = what is focused and which chat is open. "
                    "'current_chat' = read the conversation already on screen. "
                    "'reply' = type into the open chat (message=...). "
                    "'whatsapp' = message a named contact (contact=..., message=...). "
                    "'task' or omitted = route the `task` sentence to a skill."
                ),
            },
            "task": {
                "type": "STRING",
                "description": (
                    "The automation goal in natural language, e.g. "
                    "'WhatsApp +919876543210: I will be late', "
                    "'reply to Ravi on whatsapp saying done', "
                    "'play lofi on youtube fullscreen', 'what's on my screen'"
                ),
            },
            "contact": {
                "type": "STRING",
                "description": (
                    "Contact or group name to message, e.g. 'Ravi', 'Rayan Ali'. "
                    "Not needed for action='reply' (that uses the chat already open)."
                ),
            },
            "message": {
                "type": "STRING",
                "description": "The exact words to send or type, verbatim.",
            },
        },
        "required": [],
    },
    "handler": run,
    "behavior": "BLOCKING",
}


# ══════════════════════════════════════════════════════════════════════════════
# Self-test — read-only checks, safe to run on the desktop
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("pc_automation self-test")
    print(f"  session:            {kc.session_type()}")
    ready, why = _input_ready()
    print(f"  input ready:        {ready} ({why})")
    print(f"  wa number parse:    {_wa_digits('message +91 98765 43210 hi') or 'FAIL'}")
    print(f"  wa name parse:      {(_skill_whatsapp and True) and 'handler present'}")
    skill = _detect_skill("reply to ravi on whatsapp saying done")
    print(f"  dispatcher whatsapp:{skill is _skill_whatsapp}")
    skill = _detect_skill("play lofi on youtube fullscreen")
    print(f"  dispatcher youtube: {skill is _skill_youtube}")
    skill = _detect_skill("what is on my screen")
    print(f"  dispatcher read:    {skill is _skill_read_screen}")

    # ── the context brain ───────────────────────────────────────────────
    print(f"  context file:       {_CONTEXT_PATH.name} "
          f"({_context_age():.1f}s old, {len(_context_read())} keys)")

    # Name matching is what decides "already chatting with them" — it has to be
    # generous with how people type names and strict about not merging people.
    cases = [
        ("Rayan Ali", "rayan", True),
        ("Mom💛", "mom", True),
        ("Dr. Ehsan", "Ehsan", True),
        ("Ehsan", "Rayan Ali", False),
        ("Ali", "Rayan Ali", True),
        ("", "Ravi", False),
        ("Work Group", "work group", True),
    ]
    bad = [f"{a!r}/{b!r}" for a, b, want in cases if _name_match(a, b) is not want]
    print(f"  name match:         {'ok (7 cases)' if not bad else 'FAIL ' + ', '.join(bad)}")

    # Action routing must reach the right skill without touching the GUI.
    routes = [
        ({"action": "context"}, "context"),
        ({"action": "current_chat"}, "current_chat"),
        ({"action": "reply", "message": "hi"}, "reply"),
        ({"action": "whatsapp", "contact": "Ravi", "message": "hi"}, "whatsapp"),
        ({"task": "play lofi on youtube"}, "task"),
        ({"action": "nonsense"}, "unknown"),
        ({}, "help"),
    ]
    for params, expected in routes:
        got, _thunk = _route(params)
        label = params.get("action") or params.get("task") or "(empty)"
        print(f"  route {str(label):<29} {'ok' if got == expected else 'FAIL'}")

    frame, note = _grab()
    print(f"  screenshot:         {'ok' if frame else note} "
          f"({len(frame or b'')} bytes)")
    ok, where = kc.screenshot()
    print(f"  portal screenshot:  {'ok -> ' + where if ok else where}")
    print("done.")
