"""actions/task_queue.py — "do this later", as real work rather than a nudge.

A reminder tells you something at a time; a queued job *does* something at a
time (or after a delay, or when a condition finally holds) and reports back
afterwards. "When the build finishes, run the tests" and "in twenty minutes,
check whether the download stopped" are jobs, not reminders.

The queue is on disk (`memory/jobs.json`) and survives a restart, and the work
itself runs through the same action registry as a spoken command — so a queued
task gets the same verification, the same audit trail and the same undo path as
anything asked for out loud.
"""

from __future__ import annotations

import time
from typing import Any

from core import activity_log, jobs

TOOL = {
    "name": "task_queue",
    "description": (
        "Queue real work for later, or list/cancel what is queued. Use it when the "
        "user says 'in 20 minutes do X', 'at 3pm run X', 'tomorrow morning X', or "
        "'when the CPU drops below 50%, X'. The task string is an instruction I "
        "will carry out later with my normal tools. For something that only needs "
        "*saying* at a time, use reminder instead. Jobs survive a restart, and I "
        "report the result after they run."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: add, list, cancel, run_now, stats",
            },
            "title": {"type": "STRING", "description": "Short label for the job"},
            "task": {
                "type": "STRING",
                "description": "The instruction to carry out later, e.g. 'check the download and tell me if it stopped'",
            },
            "kind": {"type": "STRING", "description": "at, after or when"},
            "at": {"type": "STRING", "description": "Clock or date+time, e.g. '15:30' or '2026-10-01 09:00'"},
            "after_seconds": {"type": "INTEGER", "description": "Delay from now, in seconds"},
            "condition": {
                "type": "STRING",
                "description": ("For kind=when: the measurement, e.g. 'cpu below 50' or "
                                "'battery below 20' or 'disk free_below 5'"),
            },
            "key": {"type": "STRING", "description": "Which job, for cancel or run_now"},
        },
        "required": ["action"],
    },
    "handler": None,
}

# "cpu below 50", "disk free_below 5", "port 4444", "battery below 20" —
# parsed here because the model writes conditions as words far more
# reliably than as nested JSON.
_CONDITION = (
    ("port", "port\\s*(\\d{2,5})", lambda m: {"trigger": "port", "port": int(m.group(1))}),
    ("cpu", "\\bcpu\\b[^\\d]{0,12}(\\d{1,3})", lambda m: {"trigger": "cpu", "above": int(m.group(1))}),
    ("ram", "\\bram\\b[^\\d]{0,12}(\\d{1,3})", lambda m: {"trigger": "ram", "above": int(m.group(1))}),
    ("disk", "disk[^\\d]{0,16}(\\d{1,3})", lambda m: {"trigger": "disk", "free_below": int(m.group(1))}),
    ("battery", "batter[^\\d]{0,16}(\\d{1,3})", lambda m: {"trigger": "battery", "below": int(m.group(1))}),
)


def parse_condition(text: str) -> dict:
    import re

    hay = str(text or "").casefold()
    for _name, pattern, build in _CONDITION:
        match = re.search(pattern, hay)
        if match:
            condition = build(match)
            # "when the CPU drops below 50" and "when the CPU is over 90" are the
            # same trigger pointed in opposite directions. Flipping the number
            # would silently turn one into the other, so the direction is kept.
            if "below" in hay or "under" in hay or "drops" in hay or "less than" in hay:
                if condition.get("above") is not None:
                    condition["below"] = condition.pop("above")
            return condition
    return {}


def run(parameters: dict[str, Any], **ctx: Any) -> str:
    args = parameters or {}
    action = str(args.get("action") or "list").strip().casefold().replace(" ", "_")
    started = time.monotonic()

    if action in ("add", "queue", "later", "schedule"):
        task = str(args.get("task") or args.get("title") or "").strip()
        if not task:
            return "What should I do later? Tell me the thing to do and when."
        condition = parse_condition(str(args.get("condition") or "")) or None
        kind = str(args.get("kind") or "").strip()
        after = args.get("after_seconds")
        if after in (None, "") and str(args.get("at") or ""):
            parsed = jobs.parse_duration(str(args.get("at")))
            if parsed:                                          # "in 20 minutes"
                after, args["at"] = parsed, None
        result = jobs.add_job(
            title=str(args.get("title") or task)[:80],
            task=task,
            kind=kind,
            at=args.get("at"),
            after_seconds=after,
            condition={"trigger": (condition or {}).get("trigger", "cpu"),
                       **{k: v for k, v in (condition or {}).items() if k != "trigger"}}
            if condition else None,
        )
        if not result.get("ok"):
            return (f"I could not queue that: {result.get('error')}. Give me the task "
                    f"and a time — 'in 20 minutes', 'at 15:30' or 'when the CPU drops'.")
        job = result["job"]
        return (f"Queued: {job['title']} — {job['task']}. It runs {job.get('due_at')} "
                f"and I will report back when it is done.")

    if action in ("list", "show", "whats_queued", "status"):
        return jobs.render_jobs(include_done=bool(args.get("include_done")))

    if action in ("cancel", "remove", "delete", "forget"):
        key = str(args.get("key") or args.get("title") or args.get("task") or "").strip()
        if not key:
            return "Which queued job should I drop?"
        return jobs.cancel_job(key)

    if action in ("run_now", "now", "start"):
        key = str(args.get("key") or args.get("title") or args.get("task") or "").strip()
        if not key:
            return "Which queued job should I run now?"
        runner = ctx.get("task_runner") or ctx.get("runner")
        if runner is None:
            from core.task_runner import run_task          # late import: optional
            runner = run_task
        return jobs.run_now(key, runner)

    if action in ("stats", "how_many"):
        info = jobs.stats()
        return (f"{info['pending']} job(s) waiting, {info['done']} done, "
                f"{info['failed']} failed. Next due: {info['next_due'] or 'nothing scheduled'}.")

    return (f"I do not know the queue action '{action}' "
            f"({round((time.monotonic() - started) * 1000, 1)} ms). "
            f"I can do: add, list, cancel, run_now, stats.")


TOOL["handler"] = run


def _self_test() -> dict:
    import tempfile
    from pathlib import Path

    # Run against a scratch file, never the user's real queue. A self-test that
    # writes into memory/jobs.json leaves cancelled test jobs in somebody's
    # "what is queued?" answer, which is exactly the kind of clutter that makes
    # a feature look broken.
    original = jobs.JOBS_PATH
    details: dict = {}
    tmp = tempfile.TemporaryDirectory()
    jobs.JOBS_PATH = Path(tmp.name) / "jobs.json"
    details["isolated_from_real_queue"] = True
    queued = run({"action": "add", "title": "selftest job", "task": "say hello",
                  "after_seconds": 3600})
    details["queue_answers"] = "Queued" in queued
    details["list_shows_it"] = "selftest job" in run({"action": "list"})
    details["condition_parsed"] = parse_condition("cpu below 50") == {"trigger": "cpu", "below": 50}
    details["condition_above_kept"] = parse_condition("cpu over 90") == {"trigger": "cpu", "above": 90}
    details["condition_battery"] = parse_condition("when battery is below 20") == {
        "trigger": "battery", "below": 20}
    details["condition_disk"] = parse_condition("disk free under 5 percent") == {
        "trigger": "disk", "free_below": 5}
    details["when_job_queued"] = "Queued" in run(
        {"action": "add", "title": "selftest when", "task": "tell me",
         "condition": "cpu below 5"})
    details["cancel_answers"] = "Cancelled" in run({"action": "cancel", "key": "selftest when"})
    run({"action": "cancel", "key": "selftest job"})
    details["stats_answers"] = "job(s) waiting" in run({"action": "stats"})
    details["unknown_named"] = "do not know" in run({"action": "wat"}).casefold()
    jobs.JOBS_PATH = original
    tmp.cleanup()
    return {"ok": all(details.values()), "details": details}


if __name__ == "__main__":
    import json

    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
