"""core/phone_bridge.py — the seam between the laptop and the phone.

The point of this module is continuity, not gadgetry: a task started here
should be able to finish there, and something Jarvis decided while the user was
away should be waiting rather than lost.

How the pieces actually connect
-------------------------------
*   The phone reaches Jarvis through the existing dashboard server (`/api/command`
    already feeds the live session), so no new transport is invented here.
*   Anything Jarvis wants to *send* to the phone lands in an outbox; the phone
    page pulls it and speaks it with the browser's own voice. Nothing requires
    an Android app or a cloud account.
*   Handoffs are small records: title, detail, status. They survive restarts
    because they live in one JSON file, and they expire so the list stays
    honest instead of growing forever.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core import activity_log

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "memory" / "phone_session.json"
_LOCK = threading.RLock()
_MAX_NOTES = 40


def _now() -> float:
    return time.time()


def _config() -> dict:
    try:
        policy = json.loads((BASE_DIR / "config" / "assistant_policy.json").read_text(encoding="utf-8"))
        phone = policy.get("phone") or {}
        return phone if isinstance(phone, dict) else {}
    except (OSError, ValueError):
        return {}


def _load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("handoffs", [])
            data.setdefault("outbox", [])
            data.setdefault("inbox", [])
            data.setdefault("notes", [])
            return data
    except (OSError, ValueError):
        pass
    return {"handoffs": [], "outbox": [], "inbox": [], "notes": []}


def _save(state: dict) -> None:
    try:
        with _LOCK:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(STATE_PATH)
    except OSError:
        pass


def _prune(state: dict) -> dict:
    config = _config()
    ttl = float(config.get("handoff_ttl_hours", 72)) * 3600
    outbox_max = int(config.get("outbox_max", 200))
    state["handoffs"] = [
        item for item in state.get("handoffs", [])
        if _now() - float(item.get("ts") or 0) < ttl or item.get("status") == "pending"
    ][-200:]
    state["outbox"] = state.get("outbox", [])[-outbox_max:]
    state["inbox"] = state.get("inbox", [])[-200:]
    state["notes"] = state.get("notes", [])[-_MAX_NOTES:]
    return state


# ── handoffs ────────────────────────────────────────────────────────────────


def handoff(title: str, detail: str = "", tag: str = "") -> dict:
    """Queue something for the phone to pick up."""
    item = {
        "id": uuid.uuid4().hex[:10],
        "ts": _now(),
        "time": time.strftime("%H:%M"),
        "title": str(title or "Task")[:160],
        "detail": str(detail or "")[:600],
        "tag": str(tag or "")[:40],
        "status": "pending",
    }
    with _LOCK:
        state = _prune(_load())
        state["handoffs"].append(item)
        _save(state)
    activity_log.record("phone", f"handed off: {item['title']}", detail=item["detail"],
                        why="task continues on the phone", meta={"id": item["id"]})
    return item


def pending() -> list[dict]:
    with _LOCK:
        state = _prune(_load())
    return [item for item in state.get("handoffs", []) if item.get("status") == "pending"]


def complete(identifier: str, result: str = "") -> str:
    """Mark a handoff done (called by the phone, or by Jarvis on request)."""
    with _LOCK:
        state = _prune(_load())
        for item in state.get("handoffs", []):
            if item.get("id") == identifier or item.get("title") == identifier:
                item["status"] = "done"
                item["result"] = str(result or "")[:600]
                item["done_ts"] = _now()
                _save(state)
                activity_log.record("phone", f"handoff finished: {item.get('title')}",
                                    detail=item.get("result", ""), why="completed on the phone")
                return f"Marked '{item.get('title')}' as done."
    return f"No pending handoff matching {identifier!r}."


def pull_for_phone(limit: int = 10, mark: bool = False) -> list[dict]:
    """Everything the phone should now show, newest first.

    Reads do not consume by default: a phone that drops its connection before
    rendering must not lose the message.  The device acks explicitly with
    :func:`ack`, which is why delivery is honest rather than optimistic.
    """
    with _LOCK:
        state = _prune(_load())
        items = [item for item in state.get("handoffs", []) if item.get("status") == "pending"]
        outbox = [item for item in state.get("outbox", []) if item.get("status") == "queued"]
        if mark:
            for item in state.get("outbox", []):
                item["status"] = "pulled"
            _save(state)
    return (list(reversed(items)) + list(reversed(outbox)))[: max(1, int(limit or 10))]


def ack(ids: list[str]) -> str:
    """Mark handoffs and outbox messages as delivered by the phone."""
    wanted = {str(i) for i in (ids or [])}
    if not wanted:
        return "Nothing to acknowledge."
    count = 0
    with _LOCK:
        state = _prune(_load())
        for item in state.get("handoffs", []):
            if item.get("id") in wanted and item.get("status") == "pending":
                item["status"] = "delivered"
                count += 1
        for item in state.get("outbox", []):
            if item.get("id") in wanted and item.get("status") == "queued":
                item["status"] = "delivered"
                count += 1
        _save(state)
    return f"Acknowledged {count} item(s)."


# ── outbox (Jarvis → phone) ─────────────────────────────────────────────────


def push(text: str, channel: str = "phone", urgent: bool = False) -> dict:
    """Queue a message for the phone page to show and speak."""
    item = {
        "id": uuid.uuid4().hex[:10],
        "ts": _now(),
        "time": time.strftime("%H:%M"),
        "text": str(text or "")[:800],
        "channel": str(channel or "phone")[:40],
        "urgent": bool(urgent),
        "status": "queued",
    }
    with _LOCK:
        state = _prune(_load())
        state["outbox"].append(item)
        _save(state)
    return item


def outbox(limit: int = 20) -> list[dict]:
    with _LOCK:
        state = _prune(_load())
    return list(reversed(state.get("outbox", [])))[:limit]


# ── inbox (phone → Jarvis) ──────────────────────────────────────────────────


def inbox_append(text: str, source: str = "phone") -> dict:
    item = {"ts": _now(), "time": time.strftime("%H:%M"), "text": str(text or "")[:800],
            "source": source, "status": "new"}
    with _LOCK:
        state = _prune(_load())
        state["inbox"].append(item)
        _save(state)
    return item


def inbox_drain(limit: int = 10) -> list[dict]:
    with _LOCK:
        state = _prune(_load())
        items = [item for item in state.get("inbox", []) if item.get("status") == "new"][:limit]
        for item in items:
            item["status"] = "handled"
        _save(state)
    return items


# ── continuity notes ────────────────────────────────────────────────────────


def note(role: str, text: str) -> None:
    """Keep a short running transcript so a task can continue on either device."""
    text = str(text or "").strip()
    if not text:
        return
    with _LOCK:
        state = _prune(_load())
        state["notes"].append({"ts": _now(), "time": time.strftime("%H:%M"),
                               "role": str(role)[:20], "text": text[:400]})
        state["notes"] = state["notes"][-_MAX_NOTES:]
        _save(state)


def recent_notes(limit: int = 12) -> list[dict]:
    with _LOCK:
        state = _load()
    return list(reversed(state.get("notes", [])))[:limit]


# ── pairing / status ────────────────────────────────────────────────────────


def pairing_info(port: int = 8000) -> dict:
    """Where the phone page lives on the LAN."""
    try:
        from core import senses
        ip = senses.lan_address()
    except Exception:                                      # noqa: BLE001
        ip = "127.0.0.1"
    return {
        "lan_ip": ip,
        "port": port,
        "phone_page": f"http://{ip}:{port}/phone",
        "login_page": f"http://{ip}:{port}/login",
        "note": "Open the phone page on the same Wi-Fi, sign in once, and it keeps a token.",
    }


def state() -> dict:
    config = _config()
    with _LOCK:
        data = _prune(_load())
    return {
        "enabled": bool(config.get("enabled", True)),
        "pending_handoffs": len([i for i in data.get("handoffs", []) if i.get("status") == "pending"]),
        "queued_messages": len([i for i in data.get("outbox", []) if i.get("status") == "queued"]),
        "new_inbox": len([i for i in data.get("inbox", []) if i.get("status") == "new"]),
        "notes_kept": len(data.get("notes", [])),
        "pairing": pairing_info(),
    }


def clear(kind: str = "all") -> str:
    with _LOCK:
        state_data = _load()
        kinds = ["handoffs", "outbox", "inbox", "notes"] if kind in {"all", ""} else [kind]
        for key in kinds:
            if key in state_data:
                state_data[key] = []
        _save(state_data)
    activity_log.record("phone", f"cleared {kind}", why="user request", actor="user")
    return f"Cleared {', '.join(kinds)}."


def _self_test() -> dict:
    details: dict[str, Any] = {}
    item = handoff("Self-test handoff", "created by the phone bridge self-test", tag="test")
    details["handoff_created"] = bool(item.get("id"))
    details["appears_pending"] = any(i["id"] == item["id"] for i in pending())
    details["push_queued"] = bool(push("Self-test message").get("id"))
    details["inbox_roundtrip"] = bool(inbox_append("Self-test note").get("ts"))
    note("user", "self-test continuity line")
    details["notes_kept"] = len(recent_notes(limit=3)) >= 1
    details["completed"] = "done" in complete(item["id"], "self-test").casefold()
    pairing = pairing_info()
    details["pairing_has_url"] = pairing["phone_page"].startswith("http://")
    # Leave no test residue behind.
    with _LOCK:
        data = _load()
        data["handoffs"] = [i for i in data.get("handoffs", []) if i.get("tag") != "test"
                            and i.get("title") != "Self-test handoff"]
        data["outbox"] = [i for i in data.get("outbox", []) if i.get("text") != "Self-test message"]
        data["inbox"] = [i for i in data.get("inbox", []) if i.get("text") != "Self-test note"]
        data["notes"] = [i for i in data.get("notes", []) if "self-test" not in str(i.get("text", ""))]
        _save(data)
    ok = all(value for key, value in details.items() if isinstance(value, bool))
    return {"ok": ok, "details": details}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
