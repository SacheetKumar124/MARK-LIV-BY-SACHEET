"""core/activity_log.py — append-only audit trail for what Jarvis decides.

Jarvis increasingly acts without being asked: it interrupts, it queues an
observation, it locks the session, it suggests a sweep. Autonomy without a
record is untrustworthy, so every one of those writes a line here with a
``why`` field. The question that always follows "the assistant did something
on its own" — *why did you do that?* — then has a file-backed answer instead
of a guess.

Design notes
------------
* Best-effort: logging must never break the assistant, so every function
  swallows filesystem errors and returns a safe value.
* JSON Lines, not a JSON array: appending is atomic enough to be safe when the
  brain loop and an action handler write at the same moment, and a truncated
  final line cannot corrupt earlier history.
* Trimmed by line count, because this file is read by humans and by Jarvis.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "memory"
LOG_PATH = LOG_DIR / "activity_log.jsonl"

MAX_LINES = 4000
TRIM_TO = 3000

_LOCK = threading.RLock()

CATEGORIES = (
    "speech", "attention", "interrupt", "observation", "digest",
    "security", "watch", "phone", "trust", "error", "action",
)

_SENSITIVE_KEYS = ("password", "token", "secret", "api_key", "apikey", "key")


def _now() -> float:
    return time.time()


def _scrub(value: Any, limit: int = 600) -> Any:
    """Keep secrets out of the audit trail, and bound every stored string."""
    if isinstance(value, dict):
        return {
            key: ("«redacted»" if any(s in str(key).casefold() for s in _SENSITIVE_KEYS)
                  else _scrub(item, limit))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, limit) for item in value][:20]
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


def record(
    category: str,
    action: str,
    detail: str = "",
    why: str = "",
    outcome: str = "ok",
    actor: str = "jarvis",
    meta: Optional[dict] = None,
) -> dict:
    """Append one auditable event. Returns the stored entry (never raises)."""
    entry = {
        "ts": round(_now(), 3),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "category": str(category or "action")[:40],
        "action": str(action or "")[:120],
        "detail": _scrub(str(detail or "")),
        "why": _scrub(str(why or "")),
        "outcome": str(outcome or "ok")[:40],
        "actor": str(actor or "jarvis")[:40],
    }
    if meta:
        entry["meta"] = _scrub(meta)
    try:
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _maybe_trim()
    except OSError:
        pass
    return entry


def _read_all() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    entries: list[dict] = []
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue          # a torn final line is not a crisis
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except OSError:
        return []
    return entries


def recent(
    limit: int = 40,
    category: Optional[str] = None,
    actor: Optional[str] = None,
    since_seconds: Optional[float] = None,
) -> list[dict]:
    """Most recent entries first, optionally filtered."""
    limit = max(1, min(int(limit or 40), 500))
    entries = _read_all()
    if category:
        entries = [e for e in entries if e.get("category") == category]
    if actor:
        entries = [e for e in entries if e.get("actor") == actor]
    if since_seconds:
        floor = _now() - float(since_seconds)
        entries = [e for e in entries if float(e.get("ts") or 0) >= floor]
    return list(reversed(entries[-limit:]))


def explain(query: Optional[str] = None, limit: int = 18) -> str:
    """Plain-language audit trail: why Jarvis did what it did.

    ``query`` filters on any substring of the action, detail, why or
    category, which makes "explain lock" or "explain scan" work.
    """
    entries = recent(limit=max(limit, 40))
    needle = str(query or "").strip().casefold()
    if needle:
        entries = [
            e for e in entries
            if needle in " ".join(
                str(e.get(key, "")) for key in ("category", "action", "detail", "why", "outcome")
            ).casefold()
        ]
    if not entries:
        return "Nothing recorded for that yet." if needle else "The activity log is empty."

    lines: list[str] = []
    for entry in entries[:limit]:
        head = f"{entry.get('time', '?')} — {entry.get('action') or entry.get('category')}"
        if entry.get("outcome") and entry["outcome"] != "ok":
            head += f" [{entry['outcome']}]"
        lines.append(head)
        if entry.get("detail"):
            lines.append(f"    {entry['detail']}")
        if entry.get("why"):
            lines.append(f"    why: {entry['why']}")
    return "\n".join(lines)


def stats() -> dict:
    entries = _read_all()
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("category") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    size = 0
    try:
        size = LOG_PATH.stat().st_size
    except OSError:
        pass
    return {
        "entries": len(entries),
        "by_category": counts,
        "bytes": size,
        "path": str(LOG_PATH),
        "last": entries[-1]["time"] if entries else "",
    }


def _maybe_trim() -> None:
    """Cap the file so an assistant running for months stays readable."""
    try:
        with _LOCK:
            if not LOG_PATH.exists() or LOG_PATH.stat().st_size < 2_000_000:
                return
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= MAX_LINES:
                return
            kept = lines[-TRIM_TO:]
            tmp = LOG_PATH.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, LOG_PATH)
    except OSError:
        pass


def clear() -> str:
    """Wipe the trail (explicit user request only)."""
    try:
        with _LOCK:
            if LOG_PATH.exists():
                LOG_PATH.unlink()
        return "Activity log cleared."
    except OSError as exc:
        return f"Could not clear the activity log: {exc}"
