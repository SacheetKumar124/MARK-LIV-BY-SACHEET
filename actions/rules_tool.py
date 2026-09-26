"""actions/rules_tool.py — "when X, do Y", said out loud.

The engine lives in core/rules.py; this file is the part the model reads and
the user speaks to. It exists as its own action rather than more parameters on
an existing tool because the descriptions are what teach the model the
difference between a rule and a reminder:

    "warn me when the disk is nearly full"     → a rule (a standing watcher)
    "remind me at six to call my mother"       → a reminder (one moment, once)

Getting those two confused is the difference between a machine that watches
your disk forever and a machine that tells you the disk is full once at 18:00.
"""

from __future__ import annotations

import time
from typing import Any

from core import activity_log, rules

TOOL = {
    "name": "rules",
    "description": (
        "Standing watch rules: 'when X happens, do Y', where X is something the "
        "machine can measure. Use it for anything phrased 'whenever', 'from now "
        "on when', 'if ... then tell me', 'every day at'. "
        "Available triggers: port (a new listening port opens; optional port= and "
        "process=), cpu/ram (above=<percent>, optional for_ticks=), disk "
        "(free_below=<percent>), battery (below=<percent>), time "
        "(at='HH:MM', optional days=['mon','fri']). "
        "Actions: notify (say a message; {port} {process} {value} {free} "
        "{battery} {time} are filled in) or task (do real work, phrased "
        "as an instruction like 'find what is using the CPU'). "
        "For a one-off moment use reminder instead — a rule is a standing "
        "watcher that keeps running until stopped."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: add, list, remove, pause, resume, test, stats",
            },
            "name": {"type": "STRING", "description": "Short name for the rule"},
            "trigger": {
                "type": "STRING",
                "description": "port, cpu, ram, disk, battery or time",
            },
            "message": {"type": "STRING", "description": "What to say when it fires (notify)"},
            "task": {
                "type": "STRING",
                "description": "What to do when it fires, as an instruction (task)",
            },
            "port": {"type": "INTEGER", "description": "Watch one specific port"},
            "process": {"type": "STRING", "description": "Only when the process name matches"},
            "above": {"type": "INTEGER", "description": "CPU/RAM threshold, percent"},
            "below": {"type": "INTEGER", "description": "Battery threshold, percent"},
            "free_below": {"type": "INTEGER", "description": "Disk free space threshold, percent"},
            "over_hours": {"type": "INTEGER", "description": "How stale the last scan may be, hours"},
            "at": {"type": "STRING", "description": "Clock time for a daily rule, HH:MM"},
            "days": {"type": "STRING", "description": "Comma-separated weekdays, e.g. 'mon,tue'"},
            "for_ticks": {"type": "INTEGER", "description": "Must stay true this many checks"},
            "cooldown_s": {"type": "INTEGER", "description": "Minimum seconds between firings"},
            "max_per_hour": {"type": "INTEGER", "description": "Firings allowed per hour"},
            "key": {"type": "STRING", "description": "Which rule (name or id) for remove/pause/resume/test"},
        },
        "required": ["action"],
    },
    "handler": None,
}

_TRIGGER_KEYS = ("port", "process", "above", "below", "free_below", "over_hours",
                 "at", "days", "for_ticks")


def run(parameters: dict[str, Any], **ctx: Any) -> str:
    args = parameters or {}
    action = str(args.get("action") or "list").strip().casefold().replace(" ", "_")
    started = time.monotonic()

    if action in ("add", "create", "watch"):
        when = {"trigger": args.get("trigger")}
        for key in _TRIGGER_KEYS:
            if args.get(key) not in (None, ""):
                when[key] = args[key]
        task = str(args.get("task") or "").strip()
        then = {"action": "task", "task": task} if task else {
            "action": "notify",
            "message": str(args.get("message") or args.get("name") or "").strip(),
        }
        name = str(args.get("name") or task or args.get("message")
                   or args.get("trigger") or "rule").strip()
        result = rules.add_rule(
            name, when=when, then=then,
            cooldown_s=args.get("cooldown_s") or rules.DEFAULT_COOLDOWN,
            max_per_hour=args.get("max_per_hour") or rules.DEFAULT_MAX_PER_HOUR,
        )
        if not result.get("ok"):
            return (f"I could not set that watch rule: {result.get('error')}. "
                    f"Say it as: when <what the machine can measure>, <what I should do>.")
        rule = result["rule"]
        doing = ("I will do the work: " + rule["then"]["task"]) if task else \
                ("I will say: " + rule["then"]["message"])
        return (f"Watching. Rule set: {rule['name']} — when {rule['when']['trigger']} "
                f"matches, {doing}. It stays active until you ask me to stop it, "
                f"and will not repeat more than {rule['max_per_hour']} times an hour.")

    if action in ("list", "show", "status"):
        return rules.render_rules()

    if action in ("remove", "delete", "forget", "stop"):
        key = str(args.get("key") or args.get("name") or args.get("message") or "").strip()
        if not key:
            return "Which rule should I stop watching?"
        return rules.remove_rule(key)

    if action in ("pause", "disable", "mute"):
        key = str(args.get("key") or args.get("name") or "").strip()
        return rules.set_enabled(key, False) if key else "Which rule should I pause?"

    if action in ("resume", "enable", "unmute"):
        key = str(args.get("key") or args.get("name") or "").strip()
        return rules.set_enabled(key, True) if key else "Which rule should I resume?"

    if action in ("test", "dry_run", "check"):
        key = str(args.get("key") or args.get("name") or "").strip()
        if not key:
            return "Which rule should I test?"
        snapshot = _snapshot()
        verdict = rules.test_rule(key, snapshot)
        activity_log.record("rules", "rule tested", detail=verdict[:300],
                            why="the user asked whether it would fire")
        return verdict + " (This was a check only — nothing was said or done.)"

    if action in ("stats", "how_many"):
        info = rules.stats()
        return (f"{info['enabled']} of {info['rules']} watch rule(s) active, "
                f"{info['fired_last_24h']} firing(s) in the last day.")

    return (f"I do not know the rules action '{action}' "
            f"({round((time.monotonic() - started) * 1000, 1)} ms). "
            f"I can do: add, list, remove, pause, resume, test, stats.")


def _snapshot() -> dict:
    """Live machine readings, via the senses, so `test` answers about now."""
    try:
        from core.senses import SensesHub

        return SensesHub(persist=False).snapshot()
    except Exception:                                          # noqa: BLE001
        return {}


TOOL["handler"] = run


def _self_test() -> dict:
    import tempfile
    from pathlib import Path

    # Same rule as the queue's self-test: a scratch store, never the user's real
    # rules.json — a check must not be able to change what JARVIS watches.
    original = rules.RULES_PATH
    details: dict = {}
    tmp = tempfile.TemporaryDirectory()
    rules.RULES_PATH = Path(tmp.name) / "rules.json"
    details["isolated_from_real_rules"] = True
    added = run({"action": "add", "name": "self test", "trigger": "port",
                 "port": 45_999, "message": "port {port} opened by {process}"})
    details["add_answers"] = "Watching" in added
    details["list_answers"] = "self test" in run({"action": "list"})
    details["test_answers"] = "would not fire" in run({"action": "test", "key": "self test"}
                                                      ).casefold() or \
                              "would fire" in run({"action": "test", "key": "self test"}).casefold()
    details["pause_answers"] = "Paused" in run({"action": "pause", "key": "self test"})
    details["resume_answers"] = "Enabled" in run({"action": "resume", "key": "self test"})
    details["remove_answers"] = "Removed" in run({"action": "remove", "key": "self test"})
    details["bad_trigger_explained"] = "could not" in run(
        {"action": "add", "name": "bad", "trigger": "moon", "message": "x"}).casefold()
    details["unknown_named"] = "do not know" in run({"action": "wat"}).casefold()
    details["task_rule_ok"] = "Watching" in run(
        {"action": "add", "name": "selftest task", "trigger": "cpu",
         "above": 99, "task": "find the process"})
    run({"action": "remove", "key": "selftest task"})
    rules.RULES_PATH = original
    tmp.cleanup()
    return {"ok": all(details.values()), "details": details}


if __name__ == "__main__":
    import json

    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
