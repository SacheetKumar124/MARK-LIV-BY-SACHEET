"""One look per request — the gate that stops a screenshot storm.

The problem this solves
-----------------------
Screen understanding is one of the few abilities JARVIS has *twice*: the live
session owns ``screen_process``, the ``screen_ai`` plugin owns ``see_screen`` /
``read_text`` / ``what_am_i_doing``, and ``pc_automation`` owns ``read_screen``.
Each is reasonable on its own, so a model deciding "I should check what is on
screen" can call two or three of them — and one user sentence ("take a
screenshot and analyse it") turns into eight captures, eight vision calls and
eight frames written to disk. That is the behaviour this module ends: nobody
was wrong, there was simply no shared rule for *how many times a request may
look*.

The rule
--------
A request starts when the user speaks. The first capture in that request is
allowed. Every later look in the same request is refused with one sentence
that names what to do instead — "answer from the picture you already have".
Speaking again is a new request, so a genuine "now look again" costs nothing,
while a repeat look the user never asked for is impossible to reach.

If no user speech has been seen — an armed watch loop, an unattended flow, a
script calling a skill directly — the wall-clock fallback applies: one look per
``DEFAULT_WINDOW`` seconds. Those flows keep working exactly as before.

Why a hard gate and not a prompt instruction
--------------------------------------------
Instructions are advisory; a model under time pressure re-asks. The gate is
two floats, an epoch counter and a lock: it never blocks, never sleeps, and
returns the refusal in microseconds, so it costs nothing on the happy path.

Self-test: ``python3 core/vision_budget.py``
"""

from __future__ import annotations

import threading
import time

# ── Tunables ────────────────────────────────────────────────────────────────

# How long one look keeps the request "already seen" when no user speech has
# arrived to start a new request. Long enough that a model cannot re-ask its
# way through it, short enough that an unattended flow still gets fresh eyes.
DEFAULT_WINDOW = 45.0

# The one sentence every tool returns when it refuses to look again. It says
# what already exists, why a second look is unnecessary, and what to do
# instead — a bare "no" reads to a model as a puzzle to solve another way,
# which is exactly the loop this module exists to break.
REFUSAL = (
    "One look per request — the screen was already captured a moment ago and "
    "that picture is in your context. Answer from it. Do not call any "
    "screenshot tool again for this request (screen_process, screen_ai, "
    "pc_automation read_screen). If a fresh look is genuinely needed, ask the "
    "user to say \"look again\"."
)

# The same idea, phrased for a human reading the log rather than a model.
REFUSAL_LOG = "⏹ duplicate look refused (one look per request)"

# ── State ───────────────────────────────────────────────────────────────────

_lock = threading.RLock()

_epoch = 0                 # bumped by every user utterance
_last_claim_at = 0.0       # monotonic time of the allowed look
_last_claim_epoch = -1     # which request that look belonged to
_last_requester = ""       # which tool took it (for the log / doctor)
_looks_this_request = 0
_speech_at = 0.0           # monotonic time of the last user utterance
_history: list[str] = []   # last few decisions, for status/debugging


# ── The API ─────────────────────────────────────────────────────────────────

def note_user_speech(*, at: float | None = None) -> None:
    """The user said something — a new request starts, so a look is allowed.

    Called from the live receive loop on every user transcription. It does not
    capture anything; it only re-opens the gate.
    """
    global _epoch, _last_claim_epoch, _looks_this_request, _speech_at
    with _lock:
        _epoch += 1
        _last_claim_epoch = -1
        _looks_this_request = 0
        _speech_at = time.monotonic() if at is None else float(at)


# A clearer name for non-speech entry points (a typed request, a dashboard
# command, a test). Same effect: the next look belongs to a fresh request.
begin_request = note_user_speech


def claim(requester: str, *, window: float = DEFAULT_WINDOW,
          force: bool = False) -> tuple[bool, str]:
    """Ask permission to look at the screen.

    Returns ``(True, "")`` when this request has not looked yet — the caller
    captures. Returns ``(False, REFUSAL)`` when a look already happened in the
    same request; the caller must not capture and should hand the refusal back
    to the model as its tool result.

    ``force=True`` is for a genuinely authoritative capture (a test harness, an
    explicit path that has already been gated elsewhere).
    """
    global _last_claim_at, _last_claim_epoch, _last_requester, _looks_this_request
    with _lock:
        now = time.monotonic()
        if not force and _last_claim_epoch == _epoch and \
                (now - _last_claim_at) <= float(window):
            _remember(f"refused {requester} (look taken by {_last_requester} "
                      f"{now - _last_claim_at:.1f}s ago)")
            return False, REFUSAL
        _last_claim_at = now
        _last_claim_epoch = _epoch
        _last_requester = requester
        _looks_this_request += 1
        _remember(f"allowed {requester}")
        return True, ""


def peek(*, window: float = DEFAULT_WINDOW) -> bool:
    """Would a look be allowed right now? Checked without consuming the look."""
    with _lock:
        now = time.monotonic()
        return not (_last_claim_epoch == _epoch and
                    (now - _last_claim_at) <= float(window))


def status() -> dict:
    """Everything the doctor, logs and tests need to know, in one dict."""
    with _lock:
        now = time.monotonic()
        return {
            "epoch": _epoch,
            "looks_this_request": _looks_this_request,
            "last_requester": _last_requester,
            "seconds_since_look": (now - _last_claim_at) if _last_claim_at else None,
            "seconds_since_speech": (now - _speech_at) if _speech_at else None,
            "available": peek(),
            "recent": list(_history[-6:]),
        }


def reset() -> None:
    """Forget everything. Tests and the doctor only — never a normal path."""
    global _epoch, _last_claim_at, _last_claim_epoch, _last_requester
    global _looks_this_request, _speech_at
    with _lock:
        _epoch = 0
        _last_claim_at = 0.0
        _last_claim_epoch = -1
        _last_requester = ""
        _looks_this_request = 0
        _speech_at = 0.0
        _history.clear()


def _remember(line: str) -> None:
    _history.append(f"{time.strftime('%H:%M:%S')} {line}")
    del _history[:-12]


# ── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("vision_budget self-test")
    ok = True

    def check(label: str, got: bool) -> None:
        global ok
        print(f"  {'✅' if got else '❌'} {label}")
        ok = ok and got

    reset()
    allowed, why = claim("screen_process")
    check("first look in a request is allowed", allowed and why == "")

    allowed, why = claim("screen_ai")
    check("second tool in the same request is refused", not allowed)
    check("refusal names the tools and the fix",
          "screen_process" in why and "look again" in why)

    allowed, why = claim("screen_process")
    check("a third attempt is refused too", not allowed)

    note_user_speech()
    allowed, why = claim("pc_automation")
    check("a new user sentence re-opens the gate", allowed)

    allowed, why = claim("screen_process")
    check("and only once per request", not allowed)

    reset()
    allowed, _ = claim("generator", window=0.0)
    allowed2, _ = claim("generator", window=0.0)
    check("window=0.0 allows back-to-back looks (unattended flows)",
          allowed and allowed2)

    reset()
    check("peek agrees with claim", peek() is True)
    claim("screen_ai")
    check("peek is false after the one look", peek() is False)
    check("status reports the look",
          status()["looks_this_request"] == 1 and
          status()["last_requester"] == "screen_ai")

    print("ALL PASS" if ok else "FAILURES PRESENT")
    raise SystemExit(0 if ok else 1)
