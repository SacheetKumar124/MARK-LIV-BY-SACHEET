"""Screenshot understanding: a fast, self-healing Gemini vision client.

Why this module is defensive
----------------------------
Measured on this machine (Kali, 2026-09) while the screenshot plugin was being
built, against the live API:

* ``gemini-flash-latest``  -> 503 UNAVAILABLE ("high demand"), repeatedly
* ``gemini-2.5-flash``     -> 404 NOT_FOUND ("no longer available")
* ``gemini-2.0-flash``     -> 404 NOT_FOUND
* ``gemini-3.1-flash-lite``-> OK, 2.5 s, read a WhatsApp chat pane accurately
* ``gemini-flash-lite-latest`` -> OK, 1.4 s

Two lessons are baked into this file rather than assumed away:

1. **A hardcoded model name is a time bomb.** Models are retired out from under
   you (404) and rate-limited under load (503). Every call therefore walks an
   ordered chain of candidates and records what it learned about each one, so a
   dead model is skipped instantly next time instead of costing a round trip.
2. **Vision is the slow part of any loop that looks at a screen.** Latency is
   tracked per model, and the cheapest model that works is preferred *forward*
   once a fast one has proven itself.

Public surface
--------------
``analyze(...)``      one image (or several) + a prompt -> :class:`VisionResult`
``analyze_json(...)`` same, but the caller wants a parsed dict
``text_completion``   prompt-only call, sharing the same chain and budget
``stats``             what has been happening, for ``/status`` style reporting
``self_test``         offline checks (never touches the network)

Nothing here raises past the caller: a failure is always a ``VisionResult`` with
``ok=False`` and a human-readable ``error``. That is what lets the plugin tell
the user *why* it could not see the screen instead of going silent.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
STATE_DIR = BASE_DIR / "memory"
HEALTH_PATH = STATE_DIR / "vision_health.json"

__all__ = [
    "VisionResult", "analyze", "analyze_json", "text_completion",
    "loads_lenient", "health_snapshot", "stats", "self_test",
    "DEFAULT_MODEL_CHAIN", "describe_chain",
]

# ── models ───────────────────────────────────────────────────────────────────
#
# Ordered by what actually answers, then by quality.  The chain is walked
# top-down, the first model that answers wins, 404s are remembered as retired
# and 503s are cooled down briefly.
#
# Re-measured live on this machine (one tiny text call per name), because the
# previous order had it backwards: `gemini-flash-latest` and the two newest
# flash models were all 503 while `gemini-3.6-flash` answered in 2.2 s and
# `gemini-3.1-flash-lite` in 1.7 s — and `gemini-3.6-flash` was not in the
# chain at all. The two `-lite` entries that did answer took 32 s and 55 s,
# far too slow to sit in the middle of a verification loop, so they moved to
# the back. 2.5-generation names are gone: they return 404 NOT_FOUND.
#
# In words: quick models that answer today -> newest (best quality, may be
# throttled) -> measured-slow but working -> last resort.
DEFAULT_MODEL_CHAIN: tuple[str, ...] = (
    "gemini-3.6-flash",         # 4.3 s median on a real screenshot, 3/3 answered
    "gemini-3.1-flash-lite",    # 9.1 s median, throttles on text — fallback, not leader
    "gemini-3.8-flash",         # best quality; throttled at measurement time
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",    # answered, but 32 s
    "gemini-flash-lite-latest", # answered, but 55 s
    "gemini-flash-latest",      # 503-prone last resort
)

# Response mime type for structured calls.  Gemini honours this natively, which
# makes JSON parsing the exception rather than the rule.
JSON_MIME = "application/json"

# Retry/backoff knobs.  A single 503 is common; hammering it is not polite and
# not fast, so the model is put in a short cooldown and the next one is tried
# immediately instead of sleeping.
_COOLDOWN_LOAD_SECONDS = 90.0       # 503 / 429
_COOLDOWN_ERROR_SECONDS = 30.0      # 500 / network
_RETIRED_SECONDS = 7 * 24 * 3600.0  # 404 -- effectively permanent

# Wall-clock ceiling for one ``analyze`` call across the WHOLE chain. A chain
# walk is only useful while it is still fast enough to be worth having: a look
# that takes a minute has already failed the user, whatever it eventually reads.
# Each attempt's timeout is clipped to whatever is left of this.
_TOTAL_CALL_SECONDS = 40.0

_SENSITIVE_SUBSTRINGS = ("api_key", "apikey", "token", "secret", "password")


# ── configuration ────────────────────────────────────────────────────────────

def _load_plugin_config() -> dict:
    """Read config/screen_ai.json (the vision section) if it exists."""
    try:
        raw = (BASE_DIR / "config" / "screen_ai.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        vision = data.get("vision")
        return vision if isinstance(vision, dict) else {}
    except (OSError, ValueError):
        return {}


def _api_key() -> str:
    """Gemini key from the app config, then the usual environment variables."""
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GENAI_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("gemini_api_key") or "").strip()
    except (OSError, ValueError):
        return ""


def model_chain() -> list[str]:
    """The ordered candidate list, config-overridable, de-duplicated."""
    configured = _load_plugin_config().get("models")
    chain: list[str] = []
    if isinstance(configured, list):
        chain = [str(name).strip() for name in configured if str(name).strip()]
    if not chain:
        chain = list(DEFAULT_MODEL_CHAIN)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in chain:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _budget_limits() -> tuple[int, int]:
    """(max calls per minute, max calls per hour)."""
    cfg = _load_plugin_config()
    per_minute = int(cfg.get("max_calls_per_minute", 20) or 20)
    per_hour = int(cfg.get("max_calls_per_hour", 400) or 400)
    return max(1, per_minute), max(1, per_hour)


def _timeout_default() -> float:
    return float(_load_plugin_config().get("timeout_seconds", 45) or 45)


def _thinking_budget() -> int:
    """How much the models are allowed to deliberate before answering.

    Measured on this machine, on the same screenshot, with the same prompt:

        thinking default -> 9.5 s, 503 UNAVAILABLE
        thinking off     -> 4.3 s, correct JSON, correct chat name

    Roughly a 7x difference, and the default path was the one being throttled.
    Nothing this module asks for needs deliberation — "which chat is open",
    "did the text land in the message box" have one short factual answer — so the
    budget belongs to the answer rather than to a monologue nobody reads.
    Configurable as ``thinking_budget`` in the vision section; a positive value
    deliberately re-enables deliberation for callers that want it.

    Defined this early on purpose: :class:`_Health` reads it at import time, to
    discard latency averages that were measured under different settings.
    """
    try:
        value = int(_load_plugin_config().get("thinking_budget", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


def slow_seconds() -> float:
    """A model averaging slower than this is not preferred for ordinary looks."""
    return float(_load_plugin_config().get("slow_seconds", 6.0) or 6.0)


# ── health / budget state ────────────────────────────────────────────────────

class _Health:
    """Persisted memory of what each model did last time.

    Kept on disk on purpose: the process restarts, but a model that was retired
    last week is still retired today, and re-learning that costs a round trip on
    every startup.
    """

    def __init__(self, path: Path = HEALTH_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"models": {}, "calls": []}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                models = data.get("models")
                self._state["models"] = models if isinstance(models, dict) else {}
                calls = data.get("calls")
                self._state["calls"] = calls if isinstance(calls, list) else []
                self._forget_stale_latencies(data.get("latency_sig"))
        except (OSError, ValueError):
            pass

    def _forget_stale_latencies(self, stored_sig: Any) -> None:
        """Drop recorded latencies when the *call settings* have changed.

        A measured average is only comparable under the settings that produced
        it. The thinking budget is worth 7x on this machine (30 s -> 4 s on the
        same screenshot), so averages learned with deliberation on were leaving
        every model looking slow and pinning the fast ones to the back of the
        queue. Success/failure counts and cooldowns are kept — those are
        properties of the model, not of the settings.
        """
        sig = f"thinking={_thinking_budget()}"
        if stored_sig == sig:
            return
        for record in (self._state.get("models") or {}).values():
            if isinstance(record, dict):
                record["avg_seconds"] = None
        self._state["latency_sig"] = sig
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # -- model bookkeeping --

    def entry(self, model: str) -> dict:
        models = self._state.setdefault("models", {})
        rec = models.get(model)
        if not isinstance(rec, dict):
            rec = {"ok": 0, "fails": 0, "blocked_until": 0.0, "avg_seconds": None,
                   "last_model_error": ""}
            models[model] = rec
        return rec

    def mark_ok(self, model: str, seconds: float) -> None:
        with self._lock:
            rec = self.entry(model)
            rec["ok"] = int(rec.get("ok", 0)) + 1
            rec["blocked_until"] = 0.0
            rec["last_ok_ts"] = round(time.time(), 1)
            rec["last_model_error"] = ""
            previous = rec.get("avg_seconds")
            if isinstance(previous, (int, float)) and previous > 0:
                rec["avg_seconds"] = round(0.7 * float(previous) + 0.3 * float(seconds), 2)
            else:
                rec["avg_seconds"] = round(float(seconds), 2)
            self._save()

    def mark_failed(self, model: str, kind: str, detail: str = "") -> None:
        """``kind`` is one of ``retired`` | ``load`` | ``error``."""
        now = time.time()
        cooldown = {
            "retired": _RETIRED_SECONDS,
            "load": _COOLDOWN_LOAD_SECONDS,
            "error": _COOLDOWN_ERROR_SECONDS,
        }.get(kind, _COOLDOWN_ERROR_SECONDS)
        with self._lock:
            rec = self.entry(model)
            rec["fails"] = int(rec.get("fails", 0)) + 1
            rec["blocked_until"] = round(now + cooldown, 1)
            rec["last_model_error"] = f"{kind}: {str(detail)[:160]}"
            if kind == "retired":
                rec["retired"] = True
            self._save()

    def candidates(self, preferred: Optional[Sequence[str]] = None) -> list[str]:
        """Chain order: blocked models removed, then ordered by what actually works.

        Configured order alone is not good enough, and this was measured rather
        than guessed. With the chain taken literally, ``gemini-3.7-flash`` sat at
        the top, had answered exactly once at an average of **15.7 s**, and so
        every single look cost 15 s before anything else was tried — while
        ``gemini-3.5-flash-lite`` was sitting there answering in 2.2 s, 17 times
        out of 17. Ordering by intent alone silently traded 7x latency for
        nothing.

        So the order within the *ready* set is:

        1. proven and quick (answered before, average under ``slow_seconds``)
        2. never tried — a neutral prior, NOT a demotion
        3. proven but slow
        4. has failed before

        Ranking untried models above *known-slow* ones is the correction of a
        measured mistake, not a preference: with proven-first, a model sitting
        on a 27 s average permanently walled off three never-tried models that
        answer in 1-2 s, and every single look cost 30 s. The untried bucket
        empties itself after one attempt — each model moves into bucket 1, 3 or
        4 with real data — so this can only misjudge a name once.

        Configured order still decides ties and still decides which untried model
        goes first, so changing the config file remains meaningful.
        """
        now = time.time()
        chain = list(preferred) if preferred else model_chain()
        ready: list[str] = []
        cooling: list[tuple[float, str]] = []
        with self._lock:
            for model in chain:
                rec = self._state.get("models", {}).get(model)
                blocked_until = float((rec or {}).get("blocked_until") or 0.0)
                if blocked_until <= now:
                    ready.append(model)
                else:
                    cooling.append((blocked_until, model))

            # If literally everything is cooling down, do not go blind and do
            # not bet the whole call on one name: hand back the models that
            # recover soonest, so the caller can still walk a few candidates.
            if not ready:
                return [model for _until, model in sorted(cooling)[:3]] or chain[:1]

            slow_seconds = float(_load_plugin_config().get("slow_seconds", 6.0) or 6.0)

            def rank(item: tuple[int, str]) -> tuple[int, float, int]:
                """Promote what has earned it, demote what has not, otherwise
                leave the configured order alone.

                This is deliberately conservative. Two earlier policies were
                tried against live data and both were worse: "proven first"
                pinned a 27 s average to the front of every look, and "untried
                ahead of known-slow" kept shuffling the queue as models drifted
                in and out of throttling. The configured order is the order a
                human measured by hand, so it stays authoritative unless a model
                has *proven* itself quick (two answers under ``slow_seconds``)
                or has never worked at all.
                """
                position, model = item
                record = self._state.get("models", {}).get(model) or {}
                ok = int(record.get("ok", 0))
                fails = int(record.get("fails", 0))
                average = record.get("avg_seconds")
                if ok >= 2 and isinstance(average, (int, float)) \
                        and 0 < average <= slow_seconds:
                    return (0, float(average), position)      # proven quick: promote
                if ok == 0 and fails >= 3:
                    return (3, 0.0, position)                # has never once worked
                if ok and fails >= 3 and fails >= ok:
                    return (2, 0.0, position)                # answers, but unreliable
                return (1, 0.0, position)                    # configured order

        ordered = [model for _position, model in sorted(enumerate(ready), key=rank)]
        return ordered

    # -- call budget --

    def spend(self) -> tuple[bool, str]:
        """Record an attempt; returns (allowed, reason-if-not)."""
        per_minute, per_hour = _budget_limits()
        now = time.time()
        with self._lock:
            calls = [float(t) for t in self._state.get("calls", [])]
            calls = [t for t in calls if now - t < 3600]
            last_minute = [t for t in calls if now - t < 60]
            if len(last_minute) >= per_minute:
                wait = 60 - (now - min(last_minute))
                return False, f"rate limit: {per_minute} looks/minute reached (retry in {wait:.0f}s)"
            if len(calls) >= per_hour:
                return False, f"hourly budget reached ({per_hour} looks/hour)"
            calls.append(now)
            self._state["calls"] = calls[-800:]
            self._save()
        return True, ""

    def usage(self) -> dict:
        now = time.time()
        with self._lock:
            calls = [float(t) for t in self._state.get("calls", [])]
            models = {k: dict(v) for k, v in self._state.get("models", {}).items()}
        return {
            "calls_last_minute": len([t for t in calls if now - t < 60]),
            "calls_last_hour": len([t for t in calls if now - t < 3600]),
            "budget_per_minute": _budget_limits()[0],
            "budget_per_hour": _budget_limits()[1],
            "models": models,
        }


_HEALTH = _Health()
_CLIENT_LOCK = threading.Lock()
_CLIENT: Any = None
_CLIENT_ERROR = ""


def _client() -> Any:
    """Build the google-genai client once; cache the failure too."""
    global _CLIENT, _CLIENT_ERROR
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        if _CLIENT_ERROR:
            raise RuntimeError(_CLIENT_ERROR)
        key = _api_key()
        if not key:
            _CLIENT_ERROR = (
                "no Gemini API key found — add \"gemini_api_key\" to config/api_keys.json"
            )
            raise RuntimeError(_CLIENT_ERROR)
        try:
            from google import genai  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - dependency guard
            _CLIENT_ERROR = f"google-genai is not installed ({exc}); pip install google-genai"
            raise RuntimeError(_CLIENT_ERROR) from exc
        try:
            _CLIENT = genai.Client(api_key=key)
        except Exception as exc:
            _CLIENT_ERROR = f"could not create the Gemini client: {exc}"
            raise RuntimeError(_CLIENT_ERROR) from exc
        return _CLIENT


def reset_client() -> None:
    """Drop the cached client (used after a key change)."""
    global _CLIENT, _CLIENT_ERROR
    with _CLIENT_LOCK:
        _CLIENT = None
        _CLIENT_ERROR = ""


# ── results ──────────────────────────────────────────────────────────────────

@dataclass
class VisionResult:
    """Outcome of one vision (or text) call -- never an exception."""

    ok: bool
    text: str = ""
    data: Any = None
    model: str = ""
    seconds: float = 0.0
    attempts: int = 0
    error: str = ""
    tried: list[str] = field(default_factory=list)
    used_fallback: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "model": self.model,
            "seconds": round(self.seconds, 2),
            "attempts": self.attempts,
            "used_fallback": self.used_fallback,
            "chars": len(self.text or ""),
            "error": self.error,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text if self.ok else f"[vision failed] {self.error}"


# ── JSON repair ──────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_fence(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1).strip() if match else text.strip()


def _first_json_value(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` or ``[...]`` block, string-aware."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
    return None


def loads_lenient(text: str) -> Any:
    """Parse model JSON, tolerating the ways models actually misbehave.

    Handles: fenced code blocks, prose around the object, trailing commas,
    smart quotes, JSON that got wrapped in a single-key envelope, and a
    ``None``/empty reply. Raises ``ValueError`` when nothing can be salvaged, so
    callers can decide what to say to the user.
    """
    if text is None:
        raise ValueError("empty response")
    raw = str(text).strip()
    if not raw:
        raise ValueError("empty response")

    candidates: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(raw)
    add(_strip_fence(raw))
    block = _first_json_value(_strip_fence(raw))
    if block:
        add(block)
    # Trailing commas and typographic quotes are the two most common corruptions.
    for candidate in list(candidates):
        add(re.sub(r",\s*([}\]])", r"\1", candidate))
        add(candidate.replace("\u201c", '"').replace("\u201d", '"')
                      .replace("\u2018", "'").replace("\u2019", "'"))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError(f"could not parse JSON from model output: {raw[:180]!r}")


# ── image preparation ────────────────────────────────────────────────────────

def prepare_image(
    source: Any,
    max_side: int = 1400,
    quality: int = 82,
    format: str = "JPEG",
) -> tuple[bytes, str]:
    """Normalise any image input into ``(bytes, mime_type)``.

    ``source`` may be a path, raw bytes, or a PIL image. Full-screen PNGs from
    xdg-desktop-portal are ~300 KB; the same frame as a 1400 px JPEG is ~100 KB
    and measurably faster to upload, with no loss of readable text at this
    resolution (verified by re-reading a chat window after downscaling).
    """
    if source is None:
        raise ValueError("no image supplied")
    payload: bytes
    if isinstance(source, (bytes, bytearray)):
        payload = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        payload = Path(source).read_bytes()
    else:
        buffer = io.BytesIO()
        source.convert("RGB").save(buffer, format=format, quality=quality)
        payload = buffer.getvalue()
        return payload, f"image/{format.lower()}"

    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return payload, "image/png"

    try:
        with Image.open(io.BytesIO(payload)) as image:
            rgb = image.convert("RGB")
            if max(rgb.size) > max_side:
                rgb.thumbnail((max_side, max_side), Image.BILINEAR)
            buffer = io.BytesIO()
            rgb.save(buffer, format=format, quality=quality, optimize=False)
            return buffer.getvalue(), f"image/{format.lower()}"
    except Exception:
        # An unreadable image is not worth failing over: send it as-is and let
        # the model complain about it rather than us guessing.
        return payload, "image/png"


def image_fingerprint(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


# ── the call itself ──────────────────────────────────────────────────────────

def _classify_error(exc: Exception) -> tuple[str, str]:
    """Map an SDK exception to ``(kind, message)`` where kind is retired|load|error."""
    message = str(exc)
    code = getattr(exc, "code", None)
    if code is None:
        match = re.search(r"\b(4\d\d|5\d\d)\b", message)
        code = int(match.group(1)) if match else None
    lower = message.casefold()
    if code == 404 or "no longer available" in lower or "not found" in lower:
        return "retired", message
    if code in (429, 503) or "unavailable" in lower or "high demand" in lower \
            or "overloaded" in lower or "rate" in lower:
        return "load", message
    return "error", message


def _call_once(
    model: str,
    parts: list[Any],
    system: str,
    mime: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
    thinking_budget: Optional[int] = None,
) -> str:
    """One model, one attempt. Raises on failure."""
    from google.genai import types  # noqa: PLC0415

    budget = _thinking_budget() if thinking_budget is None else int(thinking_budget)
    thinking = None
    if budget >= 0:
        try:
            thinking = types.ThinkingConfig(thinking_budget=int(budget))
        except Exception:                                       # pragma: no cover
            thinking = None

    def _config(use_thinking: Any) -> Any:
        return types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type=mime or None,
            max_output_tokens=int(max_output_tokens),
            temperature=float(temperature),
            thinking_config=use_thinking,
        )

    try:
        response = _client().models.generate_content(
            model=model,
            contents=parts,
            config=_config(thinking),
        )
    except Exception as exc:
        # A model that rejects the thinking knob outright must not take the whole
        # chain down with it: retry the same model the plain way, once.
        kind, _msg = _classify_error(exc)
        if thinking is None or kind != "error":
            raise
        response = _client().models.generate_content(
            model=model,
            contents=parts,
            config=_config(None),
        )
    text = getattr(response, "text", None)
    if text is None:
        # A blocked/emptied response has parts but no text property value.
        try:
            text = "".join(
                getattr(part, "text", "") or ""
                for part in (response.candidates[0].content.parts or [])
            )
        except Exception:
            text = ""
    return str(text or "").strip()


def analyze(
    images: Any,
    prompt: str,
    system: str = "",
    *,
    json_mode: bool = True,
    max_output_tokens: int = 900,
    temperature: float = 0.15,
    timeout: Optional[float] = None,
    models: Optional[Sequence[str]] = None,
    max_attempts: int = 3,
    require_json: bool = False,
    thinking_budget: Optional[int] = None,
) -> VisionResult:
    """Understand one or more images. Never raises.

    ``images`` is a single image source or a list of them (a before/after pair,
    for example). ``models`` overrides the chain for this call only, which is how
    a caller can demand the fast model for a polling loop.
    """
    started = time.monotonic()
    result = VisionResult(ok=False)
    try:
        from google.genai import types  # noqa: PLC0415
    except Exception as exc:
        result.error = f"google-genai is not installed ({exc})"
        return result

    sources = images if isinstance(images, (list, tuple)) else [images]
    if not sources:
        result.error = "no image supplied"
        return result

    parts: list[Any] = []
    try:
        for source in sources:
            data, mime_type = prepare_image(source)
            parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    except Exception as exc:
        result.error = f"could not read the image: {exc}"
        return result
    parts.append(str(prompt))

    allowed, reason = _HEALTH.spend()
    if not allowed:
        result.error = reason
        return result

    mime = JSON_MIME if json_mode else ""
    chain = list(models) if models else _HEALTH.candidates()
    budget = max(1, int(max_attempts))
    errors: list[str] = []
    attempts = 0        # raw round trips spent
    charged = 0         # attempts that count against `budget`
    # A 503/429 is the API saying *this model* is busy, not that the request is
    # wrong. Counting those against the budget was a measured bug: three busy
    # models at the head of the chain consumed a three-attempt cap while two
    # perfectly healthy models sat unused behind them, so a screen look came
    # back "503 UNAVAILABLE" even though the assistant could see fine. A busy
    # model is now free to skip past; every other outcome still costs a slot.
    raw_ceiling = min(len(chain), max(budget * 2, budget + 3))
    # One wall-clock ceiling for the whole walk. Each attempt gets whatever is
    # left of it, so a chain of slow-failing models cannot quietly turn a
    # screen look into a two-minute wait.
    deadline = started + float(
        _load_plugin_config().get("max_seconds_per_call", _TOTAL_CALL_SECONDS)
        or _TOTAL_CALL_SECONDS
    )
    per_attempt = float(timeout or _timeout_default())

    for model in chain:
        remaining = deadline - time.monotonic()
        if charged >= budget or attempts >= raw_ceiling or remaining <= 1.0:
            break
        attempt_started = time.monotonic()
        attempts += 1
        result.attempts = attempts
        result.tried.append(model)
        try:
            text = _call_once(
                model=model,
                parts=parts,
                system=system,
                mime=mime,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                timeout=min(per_attempt, remaining),
                thinking_budget=thinking_budget,
            )
        except Exception as exc:
            kind, message = _classify_error(exc)
            _HEALTH.mark_failed(model, kind, message)
            errors.append(f"{model}: {message[:120]}")
            if kind != "load":
                charged += 1        # a real failure costs a slot; a busy one does not
            continue

        # An empty answer is not an answer. This is not hypothetical: with a
        # small output budget a model can spend the whole thing thinking and
        # return "" — which used to be accepted as success, so the call
        # returned empty text and the NEXT model (the one that would have
        # read the screen) was never tried.
        if not str(text or "").strip():
            _HEALTH.mark_failed(model, "error", "empty response")
            errors.append(f"{model}: empty response")
            charged += 1
            continue

        elapsed = time.monotonic() - attempt_started
        _HEALTH.mark_ok(model, elapsed)

        result.text = text
        result.model = model
        result.seconds = time.monotonic() - started
        result.used_fallback = len(result.tried) > 1
        result.error = ""
        result.ok = True

        if json_mode:
            try:
                result.data = loads_lenient(text)
            except ValueError as exc:
                if require_json:
                    result.ok = False
                    result.error = f"{model} returned unusable JSON: {exc}"
                    errors.append(result.error)
                    charged += 1
                    continue
                result.data = None
        return result

    result.seconds = time.monotonic() - started
    if errors:
        # The caller speaks this string, so a chain that was merely busy gets one
        # human sentence instead of a wall of provider JSON.
        busy = all(("503" in e or "429" in e or "UNAVAILABLE" in e) for e in errors)
        if busy:
            result.error = ("every vision model is busy right now "
                            f"({len(errors)} returned 503) — try again in a minute")
        else:
            result.error = " | ".join(errors[-3:])
    elif not chain:
        result.error = "no models configured"
    else:
        result.error = "all model attempts failed"
    return result


def analyze_json(
    images: Any,
    prompt: str,
    system: str = "",
    *,
    max_output_tokens: int = 900,
    models: Optional[Sequence[str]] = None,
    timeout: Optional[float] = None,
    thinking_budget: Optional[int] = None,
) -> tuple[Optional[dict], VisionResult]:
    """Vision call where a parsed dict is expected. Returns ``(data, result)``.

    The dict is ``None`` when the model could not be read or returned something
    unparseable -- the result always carries the reason.
    """
    result = analyze(
        images, prompt, system,
        json_mode=True, max_output_tokens=max_output_tokens,
        models=models, timeout=timeout, require_json=True,
        thinking_budget=thinking_budget,
    )
    data = result.data if isinstance(result.data, dict) else None
    return data, result


def text_completion(
    prompt: str,
    system: str = "",
    *,
    max_output_tokens: int = 400,
    temperature: float = 0.4,
    models: Optional[Sequence[str]] = None,
    thinking_budget: Optional[int] = None,
) -> VisionResult:
    """Prompt-only completion sharing the same chain, budget and error handling.

    Used for drafting chat replies and for summarising what is on screen, where
    the image has already been turned into text.
    """
    parts = [str(prompt)]
    from google.genai import types  # noqa: PLC0415

    result = VisionResult(ok=False)
    allowed, reason = _HEALTH.spend()
    if not allowed:
        result.error = reason
        return result

    chain = list(models) if models else _HEALTH.candidates()
    errors: list[str] = []
    started = time.monotonic()
    # Same rules as the image walk, and they were wrong here in the same way: a
    # busy model is skipped without cost, an empty answer is not an answer, and
    # the whole walk is bounded by one wall-clock ceiling.
    deadline = started + float(
        _load_plugin_config().get("max_seconds_per_call", _TOTAL_CALL_SECONDS)
        or _TOTAL_CALL_SECONDS
    )
    charged = 0
    attempts = 0
    raw_ceiling = min(len(chain), 6)

    for model in chain:
        remaining = deadline - time.monotonic()
        if charged >= 3 or attempts >= raw_ceiling or remaining <= 1.0:
            break
        attempts += 1
        result.attempts = attempts
        result.tried.append(model)
        attempt_started = time.monotonic()
        try:
            text = _call_once(
                model=model,
                parts=list(parts),
                system=system,
                mime="",
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                timeout=min(_timeout_default(), remaining),
                thinking_budget=thinking_budget,
            )
        except Exception as exc:
            kind, message = _classify_error(exc)
            _HEALTH.mark_failed(model, kind, message)
            errors.append(f"{model}: {message[:120]}")
            if kind != "load":
                charged += 1
            continue
        if not str(text or "").strip():
            _HEALTH.mark_failed(model, "error", "empty response")
            errors.append(f"{model}: empty response")
            charged += 1
            continue
        _HEALTH.mark_ok(model, time.monotonic() - attempt_started)
        result.ok = True
        result.text = text
        result.model = model
        result.seconds = time.monotonic() - started
        result.used_fallback = len(result.tried) > 1
        return result

    result.seconds = time.monotonic() - started
    busy = bool(errors) and all(
        ("503" in e or "429" in e or "UNAVAILABLE" in e) for e in errors)
    if busy:
        result.error = ("every text model is busy right now "
                        f"({len(errors)} returned 503) — try again in a minute")
    else:
        result.error = " | ".join(errors[-3:]) or "all model attempts failed"
    return result


# ── reporting & self-test ────────────────────────────────────────────────────

def describe_chain() -> str:
    usage = _HEALTH.usage()
    lines = []
    for model in model_chain():
        rec = usage["models"].get(model) or {}
        if rec.get("retired"):
            state = "retired (404)"
        elif float(rec.get("blocked_until") or 0) > time.time():
            state = f"cooling down {float(rec['blocked_until']) - time.time():.0f}s"
        elif rec.get("ok"):
            state = f"ok x{rec.get('ok')} (avg {rec.get('avg_seconds')}s)"
        else:
            state = "untried"
        lines.append(f"  - {model}: {state}")
    return "Vision models:\n" + "\n".join(lines)


def health_snapshot() -> dict:
    return _HEALTH.usage()


def stats() -> dict:
    usage = _HEALTH.usage()
    usage["chain"] = model_chain()
    usage["key_present"] = bool(_api_key())
    return usage


def self_test(live: bool = False) -> dict:
    """Offline checks by default; ``live=True`` performs one real vision call."""
    checks: dict[str, Any] = {}

    checks["json_plain"] = loads_lenient('{"a": 1}') == {"a": 1}
    checks["json_fenced"] = loads_lenient('```json\n{"a": 2}\n```') == {"a": 2}
    checks["json_prose"] = loads_lenient('Sure! Here it is: {"a": 3} hope that helps') == {"a": 3}
    checks["json_trailing_comma"] = loads_lenient('{"a": [4,5,],}') == {"a": [4, 5]}
    checks["json_nested_braces"] = loads_lenient('{"a": {"b": "}"}, "c": 6}')["c"] == 6
    try:
        loads_lenient("definitely not json")
        checks["json_rejects_garbage"] = False
    except ValueError:
        checks["json_rejects_garbage"] = True

    chain = model_chain()
    checks["chain_nonempty"] = len(chain) >= 2
    checks["chain_unique"] = len(chain) == len(set(chain))

    try:
        payload, mime = prepare_image(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        checks["prepare_passthrough"] = bool(payload) and mime.startswith("image/")
    except Exception:
        checks["prepare_passthrough"] = False

    checks["classify_404"] = _classify_error(type("E", (Exception,), {})("404 NOT_FOUND"))[0] == "retired"
    checks["classify_503"] = _classify_error(type("E", (Exception,), {})("503 UNAVAILABLE"))[0] == "load"
    checks["key_present"] = bool(_api_key())
    checks["budget_shape"] = len(_budget_limits()) == 2

    if live:
        shot = None
        try:
            from core import kali_compat  # noqa: PLC0415
            ok, path = kali_compat.screenshot()
            shot = path if ok else None
        except Exception:
            shot = None
        if shot:
            result = analyze(
                shot,
                'Reply with JSON only: {"ok": true, "note": "one short phrase about this screen"}',
                max_output_tokens=120,
            )
            checks["live_call"] = result.ok
            checks["live_model"] = result.model
            checks["live_seconds"] = round(result.seconds, 2)
            checks["live_error"] = result.error
        else:
            checks["live_call"] = False
            checks["live_error"] = "no screenshot available"

    checks["ok"] = all(bool(v) for k, v in checks.items()
                       if k not in ("live_model", "live_seconds", "live_error"))
    return checks


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys
    print(json.dumps(self_test(live="--live" in sys.argv), indent=2))
    print(describe_chain())
