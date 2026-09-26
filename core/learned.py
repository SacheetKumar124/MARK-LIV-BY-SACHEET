"""core/learned.py — the part of the memory that grows on its own.

Three different problems live in one module because they share a store and a
vocabulary: what the assistant was *wrong* about, what it *noticed* worth
remembering, and what *happened* recently.

Why each exists
---------------
1.  **Corrections.**  ``recall_memory`` searches long_term.json, session
    summaries are capped at ten, and nothing anywhere recorded "no, that is not
    what I meant". A correction therefore evaporated the moment it was spoken,
    and the same mistake came back a week later. A correction is stored in a
    category of its own which is rendered FIRST and IN FULL in the system
    prompt — ahead of ordinary notes — because a rule that overrides behaviour
    is only useful where it cannot be budgeted away.

2.  **Proposals.**  The assistant hears "my sister's name is Ayşe" once, in a
    live voice transcript, and a live transcript mishears things. Writing that
    straight into permanent memory is how a store fills with quietly wrong
    facts. So the session log is mined for facts that were *unambiguously*
    stated, and everything else waits in a review queue: JARVIS can read them
    back to you and you approve or reject them in one sentence. Only explicit
    imperatives ("remember that…", "note that…") are stored straight away —
    there the user is literally asking for storage.

3.  **History.**  The audit trail already holds hundreds of entries and the
    session list holds the last ten conversations, and nothing ever read either
    one back. "When did I last message Rayan?" and "what did you change
    yesterday?" are answerable from data that is already on disk.

Deterministic on purpose
-----------------------
All three use regexes over the user's own words — no second model call, no
network, no embeddings. Extraction runs in microseconds at session end, so it
costs nothing that a Live session would notice.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core import activity_log

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = BASE_DIR / "memory"
LEARNED_PATH = MEMORY_DIR / "learned.json"

# The category a correction is filed under. memory_manager renders it before
# every other category and without a budget, which is the whole point.
CORRECTIONS_CAT = "corrections"

# A proposal is a small thing; keep it small so the queue stays readable.
MAX_FACT_CHARS = 240
MAX_PROPOSALS = 400
_REVIEWABLE = ("pending",)


# ── storage ──────────────────────────────────────────────────────────────────

def _empty_queue() -> dict:
    return {"proposals": [], "rejected_keys": []}


def _load_queue() -> dict:
    if not LEARNED_PATH.exists():
        return _empty_queue()
    try:
        data = json.loads(LEARNED_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_queue()
        base = _empty_queue()
        base.update({k: v for k, v in data.items() if k in base})
        if not isinstance(base["proposals"], list):
            base["proposals"] = []
        if not isinstance(base["rejected_keys"], list):
            base["rejected_keys"] = []
        return base
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Learned] ⚠️ queue load failed: {exc}")
        return _empty_queue()


def _save_queue(data: dict) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        proposals = data.get("proposals", [])
        if len(proposals) > MAX_PROPOSALS:
            # Keep pending items even if the queue is enormous; drop the oldest
            # decided ones first, since they are only kept for history.
            pending = [p for p in proposals if p.get("status") in _REVIEWABLE]
            decided = [p for p in proposals if p.get("status") not in _REVIEWABLE]
            data["proposals"] = (pending + decided)[-MAX_PROPOSALS:]
        LEARNED_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Learned] ⚠️ queue save failed: {exc}")


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).casefold()).strip("_")
    return (slug or "fact")[:limit]


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── 1. corrections ───────────────────────────────────────────────────────────

def add_correction(wrong: str, right: str, context: str = "") -> dict:
    """Store "you said X; the truth is Y" where the prompt cannot drop it.

    Written into long_term.json rather than this module's own file so that one
    store answers "what do you know", and so the existing memory panel, forget
    flow and trim guards all apply without a second implementation.
    """
    from memory.memory_manager import update_memory      # local: avoid import cycle

    wrong = str(wrong or "").strip()[:160]
    right = str(right or "").strip()[:MAX_FACT_CHARS]
    if not right:
        return {"ok": False, "error": "nothing to correct to"}

    key = _slug(right if len(right) < 40 else wrong or right, 40)
    value = right
    if wrong:
        value = f"NOT: {wrong}. INSTEAD: {right}"
    if context:
        value = f"{value} (context: {str(context).strip()[:80]})"

    update_memory({CORRECTIONS_CAT: {key: {"value": value}}})
    activity_log.record(
        "memory", "correction stored", detail=value[:300],
        why="the assistant was told it was wrong — this outranks ordinary notes",
    )
    return {"ok": True, "key": key, "value": value}


def list_corrections() -> list[dict]:
    from memory.memory_manager import load_memory

    items = (load_memory().get(CORRECTIONS_CAT) or {})
    rows = []
    for key, entry in items.items():
        value = entry.get("value") if isinstance(entry, dict) else str(entry)
        rows.append({
            "key": key,
            "value": str(value or ""),
            "updated": entry.get("updated", "") if isinstance(entry, dict) else "",
        })
    rows.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return rows


def forget_correction(key: str) -> str:
    from memory.memory_manager import forget

    return forget(str(key).strip(), CORRECTIONS_CAT)


def render_corrections(limit: int = 8) -> str:
    rows = list_corrections()[:limit]
    if not rows:
        return "No corrections stored."
    lines = [f"- {r['key']}: {r['value']}" for r in rows]
    return "Corrections I must follow:\n" + "\n".join(lines)


# ── 2. proposals ─────────────────────────────────────────────────────────────

# A fact from a live voice transcript is a guess until a human agrees. These
# patterns are the ones where the *user themselves* stated the thing plainly;
# even then most are only proposed, not stored.
_IMPERATIVE = re.compile(
    r"\b(remember|note|keep in mind|don'?t forget|make a note)\s*(that|:)?\s*(?P<fact>.{4,200})",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(r"\bi (?:prefer|like|always want|hate|dislike)\s+(?P<fact>.{3,160})", re.IGNORECASE)
_WORKING_ON = re.compile(r"\bi(?:'m| am)\s+(?:working on|building|writing|making)\s+(?P<fact>.{3,160})", re.IGNORECASE)
_PLAN = re.compile(r"\bi (?:plan to|want to|wish i could)\s+(?P<fact>.{3,160})", re.IGNORECASE)
_CALL_ME = re.compile(r"\bcall me\s+(?P<fact>[A-Za-z0-9 _\-\.]{2,40})", re.IGNORECASE)
_MY_X_IS_Y = re.compile(
    r"\bmy\s+(?P<who>mom|mother|dad|father|brother|sister|wife|husband|girlfriend|"
    r"boyfriend|partner|son|daughter|friend|boss|manager|teacher|mentor|doctor|"
    r"colleague|coworker|roommate)\b[^.\n]{0,20}?\bis\s+(?:named\s+|called\s+)?"
    r"(?P<fact>[A-Za-z0-9][A-Za-z0-9 _\-\.]{1,60})",
    re.IGNORECASE,
)

_RELATION_CATEGORY = {"relationships": ("mom", "mother", "dad", "father", "brother", "sister",
                                        "wife", "husband", "girlfriend", "boyfriend", "partner",
                                        "son", "daughter", "friend", "boss", "manager", "teacher",
                                        "mentor", "doctor", "colleague", "coworker", "roommate")}


def _known_keys() -> set[str]:
    from memory.memory_manager import load_memory

    keys: set[str] = set()
    for cat, items in load_memory().items():
        if isinstance(items, dict):
            for entry in items.values():
                value = entry.get("value") if isinstance(entry, dict) else entry
                text = str(value or "").casefold()
                if text:
                    keys.add(text)
    return keys


def propose_fact(key: str, value: str, category: str = "notes",
                 evidence: str = "", status: str = "pending") -> dict:
    """Queue a fact for review (or store it, when the user asked for storage)."""
    value = str(value or "").strip().strip("\"'. ")[:MAX_FACT_CHARS]
    if len(value) < 3:
        return {"ok": False, "error": "too short to be a fact"}
    key = _slug(key or value, 48)

    queue = _load_queue()
    if key in set(queue.get("rejected_keys", [])):
        return {"ok": False, "error": "this was rejected before — not proposing it again"}
    for existing in queue["proposals"]:
        if existing.get("key") == key and existing.get("value") == value:
            return {"ok": False, "error": "already seen", "duplicate": True}
    if status != "pending" and value.casefold() in _known_keys():
        return {"ok": False, "error": "already known", "duplicate": True}

    record = {
        "key": key,
        "value": value,
        "category": category if category in _VALID_CATEGORIES else "notes",
        "evidence": str(evidence or "")[:200],
        "created": _now_iso(),
        "status": status,
        "source": "session" if evidence else "assistant",
    }
    queue["proposals"].append(record)
    _save_queue(queue)

    if status == "pending":
        activity_log.record("memory", f"learned a fact, awaiting confirmation: {value[:120]}",
                            why="proposed rather than stored: voice transcripts mishear",
                            outcome="pending")
        return {"ok": True, "proposal": record, "stored": False}

    return _write_fact(record)


_VALID_CATEGORIES = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}


def _write_fact(record: dict) -> dict:
    from memory.memory_manager import remember

    note = remember(record["key"], record["value"], record.get("category", "notes"))
    activity_log.record("memory", f"learned: {record['value'][:140]}",
                        why=f"confirmed fact ({record.get('category', 'notes')})",
                        outcome="stored")
    return {"ok": True, "proposal": record, "stored": True, "message": note}


def _find_proposal(needle: str, queue: Optional[dict] = None) -> Optional[dict]:
    """Find a pending proposal. `queue` MUST be passed by any caller that
    intends to modify what it finds: loading a second copy here and mutating
    *that* leaves the saved file untouched, which is a bug this code had —
    rejections reported success and the fact stayed in the queue."""
    needle = str(needle or "").strip().casefold()
    if not needle:
        return None
    queue = queue if queue is not None else _load_queue()
    pending = [p for p in queue["proposals"] if p.get("status") in _REVIEWABLE]
    for item in reversed(pending):
        if needle in (item.get("key", "") + " " + item.get("value", "")).casefold():
            return item
    return None


def approve_fact(needle: str) -> str:
    queue = _load_queue()
    target = _find_proposal(needle, queue)
    if not target:
        return f"No pending fact matches '{needle}'."
    target["status"] = "accepted"
    target["decided"] = _now_iso()
    _save_queue(queue)
    _write_fact(target)
    return f"Stored: {target['value']}"


def reject_fact(needle: str, reason: str = "") -> str:
    queue = _load_queue()
    target = _find_proposal(needle, queue)
    if not target:
        return f"No pending fact matches '{needle}'."
    target["status"] = "rejected"
    target["decided"] = _now_iso()
    if reason:
        target["reason"] = str(reason)[:120]
    rejected = set(queue.get("rejected_keys", []))
    rejected.add(target.get("key", ""))
    queue["rejected_keys"] = sorted(k for k in rejected if k)
    _save_queue(queue)
    activity_log.record("memory", f"rejected: {target['value'][:120]}",
                        detail=str(reason or "")[:120],
                        why="the user said this fact was wrong", outcome="rejected")
    return f"Rejected and never proposing again: {target['value']}"


def pending(limit: int = 10) -> list[dict]:
    queue = _load_queue()
    rows = [p for p in queue["proposals"] if p.get("status") in _REVIEWABLE]
    rows.sort(key=lambda p: p.get("created", ""), reverse=True)
    return rows[: max(1, limit)]


def render_pending(limit: int = 10) -> str:
    rows = pending(limit)
    if not rows:
        return "Nothing is waiting for confirmation."
    lines = [f"{i}. {r['value']}  [{r.get('category', 'notes')}, key {r['key']}]"
             for i, r in enumerate(rows, 1)]
    return ("Facts I picked up but have not stored — the user must confirm each:\n"
            + "\n".join(lines)
            + "\nRead them out one short line each and ask which to keep.")


def pending_hint() -> str:
    """One prompt line when facts are waiting, empty otherwise.

    Kept tiny on purpose: it rides in the system prompt of every session, so it
    must cost nothing when the queue is empty (the normal case).
    """
    count = len(pending(limit=99))
    if not count:
        return ""
    return (f"\n[LEARNING] {count} fact(s) I noticed are awaiting confirmation. "
            f"Do not read them out unasked — mention them only if the user asks "
            f"what you noticed, what you learned, or asks you to remember "
            f"something (call review_learned then).\n")


# ── 3. learning from a finished session ──────────────────────────────────────

def _strip_speaker(line: str) -> tuple[str, str]:
    """Split "You: …" / "J.A.R.V.I.S: …" into (speaker, text)."""
    text = str(line or "").strip()
    if ":" not in text:
        return "", text
    head, rest = text.split(":", 1)
    if len(head) > 24:
        return "", text
    return head.strip().casefold(), rest.strip()


def learn_from_session(lines: list[str], summary: str = "") -> dict:
    """Mine one conversation for facts worth keeping. Never raises.

    Only the user's own lines are read — the assistant's sentences are
    paraphrases, and storing a paraphrase as a fact is how a memory fills with
    things nobody said. Explicit imperatives are stored immediately; everything
    else becomes a pending proposal.
    """
    result = {"stored": [], "proposed": [], "skipped": []}
    try:
        for raw in list(lines or [])[-120:]:
            speaker, text = _strip_speaker(raw)
            if speaker and speaker not in ("you", "user", "human", "sacheet"):
                continue
            if not text or len(text) < 6:
                continue

            match = _IMPERATIVE.search(text)
            if match:
                fact = match.group("fact").strip()
                outcome = propose_fact(_slug(fact), fact, "notes",
                                       evidence=text[:120], status="accepted")
                (result["stored"] if outcome.get("stored") else result["proposed"]).append(fact)
                continue

            match = _CALL_ME.search(text)
            if match:
                name = match.group("fact").strip()
                outcome = propose_fact("preferred_name", name, "identity",
                                       evidence=text[:120])
                (result["stored"] if outcome.get("stored") else result["proposed"]).append(name)
                continue

            match = _MY_X_IS_Y.search(text)
            if match:
                who = match.group("who").casefold()
                who = {"mom": "mother", "dad": "father"}.get(who, who)
                fact = f"{who.title()}'s name is {match.group('fact').strip()}"
                # Proposed, not stored: a name heard once through a microphone is
                # exactly the kind of fact that is worth a confirmation.
                outcome = propose_fact(f"{who}_name", fact, "relationships",
                                       evidence=text[:120])
                (result["stored"] if outcome.get("stored") else result["proposed"]).append(fact)
                continue

            for pattern, category, key_prefix in (
                (_PREFERENCE, "preferences", "preference"),
                (_WORKING_ON, "projects", "project"),
                (_PLAN, "wishes", "plan"),
            ):
                match = pattern.search(text)
                if not match:
                    continue
                fact = match.group("fact").strip()
                outcome = propose_fact(f"{key_prefix}_{_slug(fact, 32)}", fact, category,
                                       evidence=text[:120])
                (result["stored"] if outcome.get("stored") else result["proposed"]).append(fact)
                break

        if result["stored"] or result["proposed"]:
            activity_log.record(
                "memory", "session learned from",
                detail=(f"{len(result['stored'])} stored, {len(result['proposed'])} awaiting "
                        f"confirmation: " + "; ".join((result["stored"] + result["proposed"])[:4]))[:400],
                why="the user stated these in their own words during the session",
                meta={"summary": str(summary or "")[:200]},
            )
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Learned] ⚠️ session mining failed: {exc}")
        result["error"] = str(exc)
    return result


# ── 4. history recall ────────────────────────────────────────────────────────

def _score(query_words: list[str], text: str) -> int:
    hay = str(text or "").casefold()
    return sum(3 if word in hay else 0 for word in query_words)


def recall_history(query: str = "", limit: int = 8, days: int = 30) -> str:
    """Search what *happened*: sessions, the audit trail, corrections, proposals.

    This is the half of memory the recall_memory tool cannot reach — it reads
    long_term.json only, by design, and it is why "when did I last message
    Rayan?" used to be unanswerable while the answer sat in the activity log.
    """
    from memory.memory_manager import load_memory

    words = [w for w in re.split(r"[^\w]+", (query or "").casefold()) if len(w) > 1]
    cutoff = time.time() - max(1, days) * 86_400
    rows: list[tuple[int, str, str]] = []                       # (score, when, text)

    memory = load_memory()
    for entry in (memory.get("sessions") or []):
        if not isinstance(entry, dict):
            continue
        text = f"{entry.get('date', '')}: {entry.get('summary', '')}"
        score = _score(words, text) if words else 1
        if score:
            rows.append((score + 1, entry.get("date", ""), text))

    try:
        for entry in activity_log.recent(limit=400):
            if float(entry.get("ts", 0)) < cutoff:
                continue
            text = (f"{entry.get('time', '')} — {entry.get('action', '')}"
                    + (f": {entry.get('detail', '')}" if entry.get("detail") else ""))
            score = _score(words, text) if words else 0
            if score:
                rows.append((score, entry.get("time", ""), text))
    except Exception:                                           # noqa: BLE001
        pass

    for row in list_corrections():
        text = f"correction: {row['value']}"
        score = _score(words, text) if words else 1
        if score:
            rows.append((score + 2, row.get("updated", ""), text))

    for item in _load_queue().get("proposals", []):
        text = f"{item.get('created', '')} — {item.get('status', '')}: {item.get('value', '')}"
        score = _score(words, text) if words else 0
        if score:
            rows.append((score, item.get("created", ""), text))

    if not rows:
        return (f"Nothing in the last {days} days matches '{query}'."
                if query else "No history recorded yet.")

    rows.sort(key=lambda r: (-r[0], str(r[1])), reverse=False)
    rows.sort(key=lambda r: -r[0])
    lines: list[str] = []
    for _score_, when, text in rows[: max(1, limit)]:
        lines.append(f"- [{when}] {text}"[:300])
    head = (f"History matching '{query}' (most relevant first):" if query
            else "Recent history:")
    return head + "\n" + "\n".join(lines)


def stats() -> dict:
    queue = _load_queue()
    proposals = queue.get("proposals", [])
    by_status: dict[str, int] = {}
    for item in proposals:
        by_status[item.get("status", "?")] = by_status.get(item.get("status", "?"), 0) + 1
    return {
        "corrections": len(list_corrections()),
        "pending": len([p for p in proposals if p.get("status") in _REVIEWABLE]),
        "proposals": len(proposals),
        "by_status": by_status,
        "rejected_keys": len(queue.get("rejected_keys", [])),
    }


def _self_test() -> dict:
    import tempfile

    from memory import memory_manager

    global LEARNED_PATH
    original, original_store = LEARNED_PATH, memory_manager.MEMORY_PATH
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            LEARNED_PATH = Path(tmp) / "learned.json"
            # Both stores are redirected. A self-test that writes into the real
            # long_term.json leaves invented facts in the user's memory — which
            # it did, once, before this line existed.
            memory_manager.MEMORY_PATH = Path(tmp) / "long_term.json"

            mined = learn_from_session([
                "You: remember that my flight is on the 14th of October",
                "You: I prefer dark mode everywhere",
                "You: my sister is named Ayesha",
                "J.A.R.V.I.S: Understood, sir.",
                "You: so anyway, what is the weather",
            ])
            # An explicit "remember that" is stored at once; first-person
            # statements are proposed, because a live transcript mishears.
            details["stores_what_was_asked_for"] = any(
                "flight" in item for item in mined["stored"]), mined
            details["proposes_what_was_merely_said"] = (
                len(mined["proposed"]) == 2
                and any("dark mode" in item for item in mined["proposed"])
                and any("Ayesha" in item for item in mined["proposed"])
            ), mined
            details["ignores_assistant_lines"] = not any(
                "Understood" in item for item in mined["stored"] + mined["proposed"]
            )

            proposal = propose_fact("preference_coffee", "prefers black coffee", "preferences",
                                    evidence="You: I always take it black")
            details["proposes_without_storing"] = (
                proposal.get("ok") and not proposal.get("stored")
            )
            details["queue_lists_it"] = any(p["key"] == "preference_coffee" for p in pending())
            details["duplicate_refused"] = propose_fact(
                "preference_coffee", "prefers black coffee", "preferences").get("duplicate") is True

            details["approve_writes_to_store"] = "Stored" in approve_fact("coffee")
            from memory.memory_manager import load_memory as _load_mem

            stored = json.dumps(_load_mem(), ensure_ascii=False)
            details["approved_fact_is_in_the_store"] = "black coffee" in stored

            propose_fact("preference_tea", "prefers tea in the morning", "preferences")
            details["reject_learns"] = "never proposing" in reject_fact("tea")
            details["rejected_not_reproposed"] = not propose_fact(
                "preference_tea", "prefers tea in the morning", "preferences").get("ok")

            details["history_finds_sessions"] = "flight" in recall_history("flight")
            details["history_empty_query_ok"] = isinstance(recall_history(""), str)
            details["stats_shape"] = {"corrections", "pending", "proposals"} <= set(stats())

            # The prompt hint must exist while anything waits, and vanish once
            # the queue is empty — a permanent "0 facts waiting" line would ride
            # in every session for no reason.
            details["hint_while_pending"] = pending_hint().startswith("\n[LEARNING]")
            for item in pending(limit=99):
                reject_fact(item["key"])
            details["hint_gone_when_empty"] = pending_hint() == ""
    finally:
        LEARNED_PATH = original
        memory_manager.MEMORY_PATH = original_store

    ok = all(value[0] if isinstance(value, tuple) else value for value in details.values())
    return {"ok": bool(ok), "details": {k: (v[0] if isinstance(v, tuple) else v)
                                        for k, v in details.items()}}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
