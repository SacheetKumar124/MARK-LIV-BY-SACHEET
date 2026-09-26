"""core/task_runner.py — one way for background work to reach the real tools.

Rules and jobs both need to *do* something, and neither should know how a task
becomes work. This module is the seam: the app binds its own runner at startup
(so background work goes through the same registry, verification and audit
paths as a spoken command) and falls back to its own private registry when
nothing is bound — which is what makes the queue usable from `tools/selftest.py`
and from a plain `python3 -c` without a live session.

Two rules that matter
---------------------
1.  **Never raise.**  A background task that throws must come back as a report,
    not as an exception nobody sees. The runners here catch everything and
    return `{"ok": False, "message": "..."}`.
2.  **Never run two heavy things at once.**  Background work is serialised with
    a lock: a scan and a chat automation in the same second would fight over
    the desktop and the network, and "one at a time" is the honest behaviour.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

_BOUND_RUNNER: Optional[Callable[[str], Any]] = None
_PRIVATE_REGISTRY: Any = None
_LOCK = threading.RLock()          # serialises background work, not re-entrant deadlocks
_LAST: dict[str, Any] = {}


def bind(fn: Callable[[str], Any]) -> None:
    """Register the app's own runner: task text in, result (or None) out."""
    global _BOUND_RUNNER
    _BOUND_RUNNER = fn


def _registry() -> Any:
    """A private action registry, built once, for when the app has not bound one."""
    global _PRIVATE_REGISTRY
    if _PRIVATE_REGISTRY is None:
        from core.action_loader import discover_actions    # local: heavy import

        _PRIVATE_REGISTRY = discover_actions()[0]
    return _PRIVATE_REGISTRY


def run_task(text: str, timeout_note: bool = True) -> dict:
    """Carry out one natural-language task. Always returns a dict, never raises."""
    task = str(text or "").strip()
    started = time.monotonic()
    if not task:
        return {"ok": False, "message": "There was nothing to do — the task was empty."}

    runner = _BOUND_RUNNER
    with _LOCK:
        try:
            outcome = runner(task) if runner else _registry().run(
                "pc_automation", {"task": task}, {})
        except Exception as exc:                               # noqa: BLE001
            message = f"Background task failed: {exc}"
            _record(task, False, message, started)
            return {"ok": False, "message": message}

    if isinstance(outcome, dict):
        ok = bool(outcome.get("ok", True))
        message = str(outcome.get("message") or outcome.get("result") or "").strip()
    else:
        ok = not str(outcome or "").lower().startswith(("tool '", "i could not"))
        message = str(outcome or "").strip()

    _record(task, ok, message, started)
    return {"ok": ok, "message": message[:400], "seconds": round(time.monotonic() - started, 2)}


def _record(task: str, ok: bool, message: str, started: float) -> None:
    global _LAST
    _LAST = {"task": task, "ok": ok, "message": message[:300],
             "seconds": round(time.monotonic() - started, 2)}
    try:
        from core import activity_log

        activity_log.record(
            "jobs", f"background task {'done' if ok else 'failed'}: {task[:80]}",
            detail=message[:250], outcome="ok" if ok else "error",
            why="deferred work (rule or queued job) ran without the user watching",
        )
    except Exception:                                          # noqa: BLE001
        pass


def status() -> dict:
    return {"bound": _BOUND_RUNNER is not None, "last": dict(_LAST)}


def _self_test() -> dict:
    import tempfile

    details: dict[str, Any] = {}
    bind(lambda task: {"ok": True, "message": f"did: {task}"})
    good = run_task("check the download")
    details["bound_runner_used"] = good["ok"] and good["message"] == "did: check the download"

    def _boom(task: str):
        raise RuntimeError("no display")

    bind(_boom)
    bad = run_task("type hello")
    details["never_raises"] = bad["ok"] is False and "no display" in bad["message"]

    bind(lambda task: "")
    empty = run_task("")
    details["empty_task_refused"] = empty["ok"] is False

    bind(lambda task: "Tool 'pc_automation' failed: nope")
    failed_text = run_task("x")
    details["failure_text_detected"] = failed_text["ok"] is False

    details["status_shape"] = {"bound", "last"} <= set(status())
    _LAST.clear()
    return {"ok": all(details.values()), "details": details}


if __name__ == "__main__":
    import json

    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
