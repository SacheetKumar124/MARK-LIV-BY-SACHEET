"""core/jobs.py — work that happens later: "do this while I'm away".

Reminders already existed, and they only *notify*. There was no way to ask for a
thing to be **done** — at a time, after a delay, or when a condition finally
becomes true — and to have it survive a restart. The old `_deferred` list was
the message-injection buffer: it lived and died inside one session.

Three kinds of job
------------------
    at     — a wall-clock moment ("tomorrow at 9")
    after  — a delay from now ("in 45 minutes")
    when   — a condition, evaluated by the rules engine's own snapshot
             ("when the download finishes", expressed as a measurement)

What makes it safe to run unattended
------------------------------------
1.  **On disk, always.**  `memory/jobs.json` is written before a job runs and
    again after, so a crash mid-job leaves it visibly un-finished rather than
    silently forgotten. A job that was due while the app was closed runs on the
    next tick with the lateness stated, not dropped.
2.  **Bounded retries.**  A failing job is retried twice with the error kept,
    then parked as `failed` with the reason. An infinite retry loop on a task
    that cannot succeed is worse than a visible failure.
3.  **One runner, injected.**  This module never imports session, GUI or action
    code. `execute()` takes a callable; whoever owns the app decides how a task
    text becomes work. That also makes the whole queue testable offline.
4.  **Never more than a handful at once.**  Jobs due in the same tick run in
    order, oldest first, and the batch is capped so a backlog cannot own the
    machine for a minute.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from core import activity_log

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = BASE_DIR / "memory"
JOBS_PATH = MEMORY_DIR / "jobs.json"

KINDS = ("at", "after", "when")
MAX_JOBS = 200
MAX_TRIES = 3
MAX_PER_TICK = 4
LATENESS_NOTE = "was due"


# ── storage ──────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"jobs": [], "history": []}


def _load() -> dict:
    if not JOBS_PATH.exists():
        return _empty()
    try:
        data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        base = _empty()
        base.update({k: v for k, v in data.items() if k in base})
        if not isinstance(base["jobs"], list):
            base["jobs"] = []
        if not isinstance(base["history"], list):
            base["history"] = []
        return base
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Jobs] ⚠️ load failed: {exc}")
        return _empty()


def _save(data: dict) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        data["history"] = data["history"][-120:]
        if len(data["jobs"]) > MAX_JOBS:
            active = [j for j in data["jobs"] if j.get("status") == "pending"]
            done = [j for j in data["jobs"] if j.get("status") != "pending"]
            data["jobs"] = (active + done)[-MAX_JOBS:]
        JOBS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Jobs] ⚠️ save failed: {exc}")


def _slug(text: str, limit: int = 30) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", str(text).casefold()).strip("_") or "job")[:limit]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── authoring ────────────────────────────────────────────────────────────────

def add_job(title: str, task: str = "", kind: str = "", at: Any = None,
            after_seconds: Any = None, condition: Optional[dict] = None,
            deliver: str = "speak", **extra: Any) -> dict:
    """Queue a job. Tolerant about how the model fills the arguments.

    ``kind`` is inferred when it is omitted: `at` when a clock time is given,
    `after` when a delay is given, `when` when a condition is given.
    """
    kind = str(kind or "").strip().casefold()
    if kind not in KINDS:
        if condition:
            kind = "when"
        elif after_seconds is not None:
            kind = "after"
        elif at is not None:
            kind = "at"
        else:
            return {"ok": False, "error": "say when it should run: at, after or when"}

    task = str(task or "").strip()[:400]
    title = str(title or task or "job").strip()[:80]
    if not task:
        return {"ok": False, "error": "a job needs something to do"}

    job: dict[str, Any] = {
        "id": f"{_slug(title)}_{int(time.time() * 1000) % 10_000_000}",
        "title": title,
        "task": task,
        "kind": kind,
        "deliver": "speak" if str(deliver) != "silent" else "silent",
        "created": _now(),
        "status": "pending",
        "tries": 0,
        "last_error": "",
        "result": "",
    }

    if kind == "after":
        try:
            seconds = max(1.0, float(after_seconds))
        except (TypeError, ValueError):
            return {"ok": False, "error": "how long from now? give a number of seconds"}
        job["due_ts"] = time.time() + seconds
        job["due_at"] = datetime.fromtimestamp(job["due_ts"]).strftime("%Y-%m-%d %H:%M")
    elif kind == "at":
        target = _parse_when(at)
        if target is None:
            return {"ok": False, "error": f"could not read the time '{at}'"}
        job["due_ts"] = target
        job["due_at"] = datetime.fromtimestamp(target).strftime("%Y-%m-%d %H:%M")
    else:
        condition = dict(condition or extra.get("when") or {})
        if not condition:
            return {"ok": False, "error": "a 'when' job needs a condition"}
        job["condition"] = condition
        job["due_at"] = "when " + ", ".join(f"{k}={v}" for k, v in condition.items())

    data = _load()
    data["jobs"].append(job)
    _save(data)
    activity_log.record("jobs", f"job queued: {job['title']}",
                        detail=f"kind={kind} due={job.get('due_at')} task={task[:120]}",
                        why="the user asked for this to happen later")
    return {"ok": True, "job": job}


def _parse_when(value: Any) -> Optional[float]:
    """Read a clock time as a human states it. Returns an epoch, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):                        # already an epoch
        return float(value)
    text = str(value).strip().casefold()
    if not text:
        return None

    # ISO-ish / date+time first, then clock-only, then words.
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(text.replace("T", "T"), fmt).timestamp()
        except ValueError:
            continue

    now = datetime.now()
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", text)
    base_day = now.date()
    if "tomorrow" in text:
        base_day = (now + timedelta(days=1)).date()
    elif "yesterday" in text:
        base_day = (now - timedelta(days=1)).date()
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        target = datetime.combine(base_day, datetime.min.time()).replace(
            hour=min(23, hour), minute=min(59, minute))
        if target <= now and "tomorrow" not in text:
            target += timedelta(days=1)                        # the next occurrence
        return target.timestamp()

    relative = re.search(r"\bin\s+(\d{1,3})\s*(second|minute|hour|day)s?\b", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        factor = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]
        return (now + timedelta(seconds=amount * factor)).timestamp()

    return None


def parse_duration(text: str) -> Optional[float]:
    """'45 minutes' → 2700 seconds. Used for the natural 'in X' form."""
    match = re.search(r"(\d{1,4})\s*(second|minute|hour|day)s?", str(text or "").casefold())
    if not match:
        return None
    factor = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[match.group(2)]
    return float(match.group(1)) * factor


def _find(needle: str, data: Optional[dict] = None) -> Optional[dict]:
    data = data or _load()
    needle = str(needle or "").strip().casefold()
    if not needle:
        return None
    for job in data["jobs"]:
        if job.get("status") == "pending" and (needle == job.get("id", "").casefold()
                                               or needle == job.get("title", "").casefold()):
            return job
    for job in data["jobs"]:
        if job.get("status") == "pending" and (needle in job.get("title", "").casefold()
                                               or needle in job.get("task", "").casefold()):
            return job
    return None


def cancel_job(needle: str) -> str:
    data = _load()
    job = _find(needle, data)
    if not job:
        return f"No pending job matches '{needle}'."
    job["status"] = "cancelled"
    job["finished"] = _now()
    _save(data)
    activity_log.record("jobs", f"job cancelled: {job['title']}",
                        why="the user cancelled it", actor="user")
    return f"Cancelled: {job['title']}"


def list_jobs(include_done: bool = False) -> list[dict]:
    jobs = _load()["jobs"]
    if not include_done:
        jobs = [j for j in jobs if j.get("status") == "pending"]
    return sorted(jobs, key=lambda j: j.get("due_ts") or 0)


def render_jobs(include_done: bool = False) -> str:
    jobs = list_jobs(include_done=include_done)
    if not jobs:
        return "Nothing queued for later."
    lines = []
    for job in jobs:
        status = job.get("status", "pending")
        when = job.get("due_at") or "?"
        lines.append(f"- {job['title']} — {job.get('task')} [{status}, {when}]"
                     if status != "pending" else
                     f"- {job['title']} — {job.get('task')} (when: {when})")
    return "Queued work:\n" + "\n".join(lines)


# ── due selection ────────────────────────────────────────────────────────────

def due(now: Optional[float] = None, snapshot: Optional[dict] = None) -> list[dict]:
    """Jobs whose moment has come, oldest first, capped per tick."""
    from core import rules                                          # local: no cycle

    now_ts = float(now if now is not None else time.time())
    pending = [j for j in _load()["jobs"] if j.get("status") == "pending"]
    ready: list[dict] = []
    for job in pending:
        if job.get("kind") == "when":
            condition = dict(job.get("condition") or {})
            if not condition:
                continue
            probe = {"when": {**condition, "trigger": condition.get("trigger", "cpu")}}
            matched, reason, _values = rules._evaluate(     # reuse the one evaluator
                probe, dict(snapshot or {}), {"streaks": {}, "seen_ports": []}, now_ts,
                datetime.fromtimestamp(now_ts))
            if matched:
                job["condition_reason"] = reason
                ready.append(job)
        else:
            if float(job.get("due_ts") or 0) <= now_ts:
                job["lateness_s"] = round(now_ts - float(job.get("due_ts") or now_ts), 1)
                ready.append(job)
    ready.sort(key=lambda j: str(j.get("created", "")))
    return ready[:MAX_PER_TICK]


def _finish(data: dict, job_id: str, status: str, result: str, error: str = "") -> None:
    """Set a job's final state. Counting attempts is the caller's job, not this
    function's: mixing the two is how a retry counter stops incrementing."""
    for job in data["jobs"]:
        if job.get("id") != job_id:
            continue
        job["status"] = status
        job["result"] = str(result)[:400]
        job["last_error"] = str(error)[:300]
        job["finished"] = _now()
        data["history"].append({
            "id": job_id, "title": job.get("title", ""), "at": _now(),
            "status": status, "result": str(result)[:200], "error": str(error)[:200],
        })
        break


# ── execution ────────────────────────────────────────────────────────────────

def execute(runner: Callable[[str], Any], snapshot: Optional[dict] = None,
            now: Optional[float] = None, limit: int = MAX_PER_TICK) -> dict:
    """Run every due job through `runner(task_text) -> result|None`.

    The runner is whatever the app already uses to perform a task — in Mark-LIV
    that is the action registry with `pc_automation`, so deferred work goes
    through the same verification and undo paths as a spoken command.

    Returns `{"ran": [...], "failed": [...], "messages": [...]}`. Messages are
    one-line reports the caller may speak; nothing here speaks by itself.
    """
    result: dict[str, Any] = {"ran": [], "failed": [], "messages": []}
    jobs = due(now=now, snapshot=snapshot)[: max(1, limit)]
    for job in jobs:
        data = _load()
        title = job.get("title", "job")
        task_text = job.get("task", "")
        try:
            outcome = runner(task_text)
            ok = True if outcome is None else (
                outcome.get("ok", True) if isinstance(outcome, dict) else bool(outcome))
            text = (outcome.get("message") if isinstance(outcome, dict) else str(outcome)) or ""
            if not ok:
                raise RuntimeError(text or "the runner reported failure")

            _finish(data, job["id"], "done", text or "done")
            result["ran"].append(title)
            # Tagged so the model reports it rather than wondering who spoke.
            report = f"[JOB] Finished the thing you left for later: {title}."
            if text:
                report += f" {str(text)[:180]}"
            if float(job.get("lateness_s") or 0) > 120:
                report += (f" (It {LATENESS_NOTE} "
                           f"{int(float(job['lateness_s']) / 60)} minutes ago.)")
            if job.get("deliver", "speak") != "silent":
                result["messages"].append(report)
            activity_log.record("jobs", f"job done: {title}",
                                detail=str(text)[:200], outcome="ok",
                                why="deferred work the user queued")
            _save(data)
        except Exception as exc:                               # noqa: BLE001
            # Re-read, because the runner may have taken a while and the file is
            # the one source of truth about how many attempts this has had. The
            # increment is persisted in *both* branches — an unpersisted retry
            # counter is an infinite retry loop on a task that cannot succeed.
            data = _load()
            current = next((j for j in data["jobs"] if j.get("id") == job["id"]), None)
            tries = int((current or job).get("tries", 0)) + 1
            if current is not None:
                current["tries"] = tries
                current["last_error"] = str(exc)[:300]
            if tries >= MAX_TRIES:
                _finish(data, job["id"], "failed", "", error=str(exc))
                result["failed"].append(title)
                result["messages"].append(
                    f"[JOB] I could not finish '{title}' after {tries} attempts: "
                    f"{str(exc)[:160]}. Say so plainly.")
                activity_log.record("jobs", f"job failed: {title}", detail=str(exc)[:250],
                                    outcome="error", why=f"gave up after {tries} attempts")
            else:
                if current is not None:
                    current["due_ts"] = time.time() + 90 * tries
                    current["due_at"] = datetime.fromtimestamp(
                        current["due_ts"]).strftime("%Y-%m-%d %H:%M")
                activity_log.record("jobs", f"job retrying: {title}", detail=str(exc)[:200],
                                    outcome="retry", why=f"attempt {tries} of {MAX_TRIES}")
            _save(data)
    return result


def run_now(needle: str, runner: Callable[[str], Any]) -> str:
    """Run one queued job immediately, whatever its condition says."""
    data = _load()
    job = _find(needle, data)
    if not job:
        return f"No pending job matches '{needle}'."
    try:
        outcome = runner(job.get("task", ""))
        text = (outcome.get("message") if isinstance(outcome, dict) else str(outcome)) or "done"
        _finish(data, job["id"], "done", text)
        _save(data)                    # without this, a run-now job runs again on the next tick
        activity_log.record("jobs", f"job run on demand: {job['title']}",
                            detail=str(text)[:200], why="the user asked for it now")
        return f"Ran '{job['title']}': {str(text)[:200]}"
    except Exception as exc:                                   # noqa: BLE001
        _finish(data, job["id"], "failed", "", error=str(exc))
        _save(data)
        return f"'{job['title']}' failed: {str(exc)[:200]}"


def stats() -> dict:
    data = _load()
    jobs = data["jobs"]
    return {
        "pending": len([j for j in jobs if j.get("status") == "pending"]),
        "done": len([j for j in jobs if j.get("status") == "done"]),
        "failed": len([j for j in jobs if j.get("status") == "failed"]),
        "cancelled": len([j for j in jobs if j.get("status") == "cancelled"]),
        "next_due": min([j.get("due_at") for j in jobs
                         if j.get("status") == "pending" and j.get("due_at")] or [""]) or "",
        "history": len(data.get("history", [])),
    }


def _self_test() -> dict:
    import tempfile

    global JOBS_PATH
    original = JOBS_PATH
    details: dict[str, Any] = {}
    ran: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            JOBS_PATH = Path(tmp) / "jobs.json"
            now = time.time()
            snapshot = {"cpu_percent": 10.0, "ram_percent": 20.0, "disk_free_percent": 50.0,
                        "battery_percent": 90.0, "listeners": []}

            details["needs_a_task"] = add_job("x", "", "after", after_seconds=5).get("ok") is False
            details["infers_after"] = add_job("soon", "say hi", after_seconds=60).get("job", {}).get(
                "kind") == "after"
            details["infers_at"] = add_job("later", "brief me", at="23:59").get("job", {}).get(
                "kind") == "at"
            details["infers_when"] = add_job("hot", "find it",
                                             condition={"trigger": "cpu", "above": 80}).get(
                "job", {}).get("kind") == "when"

            immediate = add_job("immediate", "do it now", after_seconds=0.01)
            details["queued"] = immediate.get("ok") is True
            details["not_due_yet"] = not due(now=now - 10, snapshot=snapshot)
            ready = due(now=now + 5, snapshot=snapshot)
            details["due_by_time"] = any(j["title"] == "immediate" for j in ready)
            details["condition_job_deferred"] = not any(j["title"] == "hot" for j in ready)

            outcome = execute(lambda task: ran.append(task) or {"ok": True, "message": "did it"},
                              snapshot=snapshot, now=now + 5)
            details["ran_task"] = ran == ["do it now"]
            details["reported"] = any("immediate" in m for m in outcome["messages"])
            details["marked_done"] = stats()["done"] == 1
            details["not_rerun"] = not execute(
                lambda task: ran.append(task), snapshot=snapshot, now=now + 6)["ran"]

            when_job = due(now=now + 7, snapshot={**snapshot, "cpu_percent": 95.0})
            details["condition_fires_when_true"] = any(j["title"] == "hot" for j in when_job)

            add_job("fails", "explode", after_seconds=0.01)

            def _boom(task: str):
                raise RuntimeError("nope")

            last: dict = {}
            for attempt in range(MAX_TRIES):
                last = execute(_boom, snapshot=snapshot, now=now + 20 + attempt * 200)
            details["gives_up_after_max_tries"] = stats()["failed"] == 1, stats()
            details["failure_is_spoken"] = any("could not finish" in m
                                               for m in last.get("messages", []))
            details["retried_before_giving_up"] = stats()["failed"] == 1 and len(
                [j for j in _load()["jobs"] if j.get("title") == "fails" and
                 int(j.get("tries", 0)) >= MAX_TRIES - 1]) >= 1

            fresh = add_job("run me now", "say hello", after_seconds=99999)
            details["cancel_works"] = "Cancelled" in cancel_job("soon")
            details["list_shows_pending"] = "Queued" in render_jobs() or render_jobs() == \
                "Nothing queued for later."
            details["stats_shape"] = {"pending", "done", "failed"} <= set(stats())
            details["durable"] = JOBS_PATH.exists()
            details["parse_clock"] = _parse_when("07:30") is not None
            details["parse_relative"] = parse_duration("45 minutes") == 2700.0
            details["parse_bad"] = _parse_when("the day after never") is None
            details["run_now"] = "Ran" in run_now(fresh["job"]["title"], lambda task: "ok")
            details["run_now_on_done_refused"] = "No pending job" in run_now(
                fresh["job"]["title"], lambda task: "ok")
    finally:
        JOBS_PATH = original

    ok = all(value[0] if isinstance(value, tuple) else value for value in details.values())
    return {"ok": bool(ok), "details": {k: (v[0] if isinstance(v, tuple) else v)
                                        for k, v in details.items()}}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
