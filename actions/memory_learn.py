"""actions/memory_learn.py — corrections, review and history, by voice.

Three things the assistant could not do before this file existed:

*   **Be corrected.**  "No, that is not what I meant" now has somewhere to go.
    A correction outranks ordinary notes in the prompt, so it changes the next
    answer and not just this one.
*   **Be told what it picked up.**  Facts mined from a session wait for a human
    "yes"; this is how the user hears and decides them, one sentence each.
*   **Be asked what happened.**  "When did I last message Rayan?" is answered
    from the audit trail and the session list instead of a guess.

The action names are deliberately worded for speech: the model reads the
descriptions, the user says the words. Nothing here talks to the GUI, so it is
also the layer `tools/selftest.py` exercises.
"""

from __future__ import annotations

import time
from typing import Any

from core import activity_log, learned

TOOL = {
    "name": "memory_learn",
    "description": (
        "Manage what I learn and remember long-term. "
        "action='correct' when the user says I got something wrong or tells me a "
        "rule to follow from now on — pass what I said wrong and what is correct. "
        "action='review' to hear facts I noticed but never stored, then "
        "action='approve' or action='reject' with the key. "
        "action='history' to search what actually happened (past sessions, my audit "
        "trail, past corrections) — use it for 'when did I last…', 'what did you do "
        "yesterday', 'did we talk about…'. "
        "action='remember' to store a fact the user states outright. "
        "action='corrections' to list the standing corrections."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("One of: correct, review, approve, reject, history, "
                                "remember, corrections, forget_correction, stats"),
            },
            "wrong": {"type": "STRING", "description": "What I said or did that was wrong"},
            "right": {"type": "STRING", "description": "What is actually correct / the rule to follow"},
            "context": {"type": "STRING", "description": "When this applies, if it matters"},
            "key": {"type": "STRING", "description": "Key of the fact (for approve/reject/forget)"},
            "query": {"type": "STRING", "description": "What to search for in history"},
            "value": {"type": "STRING", "description": "The fact to store (action=remember)"},
            "category": {
                "type": "STRING",
                "description": "identity, preferences, projects, relationships, wishes or notes",
            },
            "reason": {"type": "STRING", "description": "Why a fact is being rejected"},
            "days": {"type": "INTEGER", "description": "How far back to search history (default 30)"},
        },
        "required": ["action"],
    },
    "handler": None,           # bound at the bottom, once run() exists
}


def _clip(text: Any, limit: int = 300) -> str:
    return str(text or "").strip()[:limit]


def run(parameters: dict[str, Any], **ctx: Any) -> str:
    args = parameters or {}
    action = _clip(args.get("action"), 40).casefold().replace(" ", "_")
    started = time.monotonic()

    if action in ("correct", "correction", "add_correction", "learn_correction"):
        wrong = _clip(args.get("wrong"))
        right = _clip(args.get("right") or args.get("value") or args.get("correct"))
        if not right:
            return ("I need to know what is right — say it as a rule, and I will "
                    "follow it from now on.")
        result = learned.add_correction(wrong, right, _clip(args.get("context"), 120))
        if not result.get("ok"):
            return f"I could not store that correction: {result.get('error')}"
        return (f"Understood — corrected and stored: {result['value']}. "
                f"It now outranks my ordinary notes, so I will follow it from now on.")

    if action in ("review", "pending", "review_learned", "learned"):
        return learned.render_pending(limit=int(args.get("limit") or 10))

    if action in ("approve", "keep", "accept"):
        key = _clip(args.get("key") or args.get("value"))
        if not key:
            return "Which one? Give me the key or a few words from the fact."
        return learned.approve_fact(key)

    if action in ("reject", "forget_fact", "discard"):
        key = _clip(args.get("key") or args.get("value"))
        if not key:
            return "Which fact should I drop?"
        return learned.reject_fact(key, _clip(args.get("reason"), 120))

    if action in ("history", "recall_history", "what_happened", "recall"):
        query = _clip(args.get("query"), 120)
        days = int(args.get("days") or 30)
        text = learned.recall_history(query, limit=int(args.get("limit") or 8), days=days)
        activity_log.record("memory", "history recalled", detail=query or "(everything)",
                            why="the user asked what happened", actor="user")
        return text

    if action in ("remember", "store", "save"):
        value = _clip(args.get("value") or args.get("right"))
        if not value:
            return "What should I remember?"
        fact = learned.propose_fact(learned._slug(value), value,
                                    _clip(args.get("category"), 30) or "notes",
                                    status="accepted")
        if fact.get("stored"):
            return f"Stored: {value}"
        return f"I did not store that ({fact.get('error')})."

    if action in ("corrections", "list_corrections"):
        return learned.render_corrections()

    if action in ("forget_correction", "remove_correction"):
        key = _clip(args.get("key"))
        if not key:
            return "Which correction should I drop?"
        return learned.forget_correction(key)

    if action in ("stats", "status"):
        info = learned.stats()
        return (f"Learning: {info['corrections']} standing correction(s), "
                f"{info['pending']} fact(s) awaiting confirmation, "
                f"{info['proposals']} proposal(s) seen in total.")

    elapsed = round((time.monotonic() - started) * 1000, 1)
    return (f"I do not know the memory action '{action}' ({elapsed} ms). I can do: "
            f"correct, review, approve, reject, history, remember, corrections, "
            f"forget_correction, stats.")


TOOL["handler"] = run


TOOL["handler"] = run


def _self_test() -> dict:
    out = run({"action": "history", "query": "nothing-here-xyz"})
    return {
        "ok": isinstance(out, str) and bool(out),
        "details": {"history_answers": isinstance(out, str),
                    "unknown_named": "do not know" in run({"action": "wat"}).casefold(),
                    "review_renderable": isinstance(run({"action": "review"}), str),
                    "corrections_renderable": isinstance(run({"action": "corrections"}), str),
                    "stats_renderable": "correction" in run({"action": "stats"}).casefold()},
    }


if __name__ == "__main__":
    import json

    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
