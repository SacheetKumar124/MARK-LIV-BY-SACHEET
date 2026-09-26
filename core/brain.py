"""core/brain.py — the loop that makes Jarvis proactive instead of reactive.

One tick is: gather what the senses noticed → let the attention engine decide
what is worth saying → hand the winners to Jarvis as a tagged message he
phrases himself → keep everything else in a digest.

Two deliberate choices
----------------------
1.  **Jarvis does the phrasing, not this module.** The tick emits a
    ``[ATTENTION]`` message into the live session, exactly like the existing
    ``[SYSTEM_ALERT]`` and ``[PROACTIVE_CHECK]`` paths. That is why an alert
    arrives in the user's own language, with personality, instead of as a
    robot string assembled in Python.
2.  **The decision is deterministic and logged.** Whether to interrupt is
    scored arithmetic with a written reason, not a model call. Model calls cost
    seconds and tokens on every tick; a rule you can read is also a rule you
    can argue with.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from core import activity_log, jobs, rules
from core.attention import AttentionEngine, Decision, Observation, load_policy
from core.senses import SensesHub

_LOCK = threading.RLock()


class Brain:
    """Tie senses, attention and delivery into one proactive loop."""

    def __init__(
        self,
        engine: Optional[AttentionEngine] = None,
        senses: Optional[SensesHub] = None,
        deliver: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.policy = load_policy()
        self.engine = engine or AttentionEngine(self.policy)
        self.senses = senses or SensesHub(self.policy)
        self._deliver = deliver
        # Deferred work (a fired rule, a due job) needs a way to *do* things.
        # The brain owns the schedule; the app owns the tools — this is the seam.
        self._task_runner: Optional[Callable[[str], Any]] = None
        self._ticks = 0
        self._last_tick = 0.0
        self._last_result: dict = {}
        self._lock = threading.RLock()

    # ── wiring ──────────────────────────────────────────────────────────

    def bind_deliver(self, deliver: Callable[[str], Any]) -> None:
        """Register how a spoken line reaches the user (session or TTS)."""
        self._deliver = deliver

    def bind_task_runner(self, runner: Callable[[str], Any]) -> None:
        """Register how deferred work is carried out (rule tasks, queued jobs)."""
        self._task_runner = runner

    @property
    def tick_seconds(self) -> int:
        return int(self.policy.get("tick_seconds", 45))

    # ── the tick ────────────────────────────────────────────────────────

    def tick(self, force: bool = False) -> dict:
        """Run one proactive cycle. Never raises; returns a summary dict."""
        started = time.monotonic()
        result: dict[str, Any] = {
            "observations": 0, "interrupts": [], "decisions": [],
            "queued": 0, "dropped": 0, "elapsed_s": 0.0, "errors": [],
            # Lines the caller should speak, alongside `interrupts`. Watch rules
            # and finished jobs both land here, so one delivery path handles all
            # of the assistant's unprompted speech.
            "watch": [],
            "rules": {"fired": [], "skipped": [], "evaluated": 0},
            "jobs": {"ran": [], "failed": []},
        }
        try:
            observations = self.senses.gather(force=force)
        except Exception as exc:                            # noqa: BLE001
            result["errors"].append(f"senses: {exc}")
            observations = []
        result["observations"] = len(observations)

        for observation in observations:
            try:
                decision = self.engine.observe(observation)
            except Exception as exc:                        # noqa: BLE001
                result["errors"].append(f"attention: {exc}")
                continue
            result["decisions"].append(decision.as_dict())
            if decision.action == "interrupt":
                result["interrupts"].append(self.interrupt_message(decision))
            elif decision.action == "queue":
                result["queued"] += 1
            else:
                result["dropped"] += 1

        # ── standing watch rules ────────────────────────────────────────
        # Evaluated after attention, so a rule can never be rate-limited away
        # by the attention budget: what the user explicitly asked to be told
        # about always wins against what the engine merely finds interesting.
        snapshot: dict[str, Any] = {}
        try:
            snapshot = self.senses.snapshot()
            watched = rules.check(snapshot)
            result["rules"] = {"fired": watched["fired"],
                               "skipped": watched["skipped"],
                               "evaluated": watched["evaluated"]}
            result["watch"].extend(self.watch_message(text) for text in watched["notify"])
            for item in watched["work"]:
                report = self._do_work(item.get("task", ""), origin=f"rule '{item.get('name')}'")
                if report:
                    result["watch"].append(report)
        except Exception as exc:                            # noqa: BLE001
            result["errors"].append(f"rules: {exc}")

        # ── deferred work the user queued ───────────────────────────────
        try:
            if self._task_runner is not None:
                outcome = jobs.execute(self._task_runner, snapshot=snapshot)
                result["jobs"] = {"ran": outcome["ran"], "failed": outcome["failed"]}
                result["watch"].extend(outcome["messages"])
            else:
                due_now = jobs.due(snapshot=snapshot)
                if due_now:
                    result["errors"].append(
                        f"{len(due_now)} job(s) due but no task runner is bound")
        except Exception as exc:                            # noqa: BLE001
            result["errors"].append(f"jobs: {exc}")

        spoken = result["interrupts"] + result["watch"]
        if spoken and self._deliver is not None:
            for text in spoken:
                try:
                    self._deliver(text)
                    result.setdefault("delivered", []).append(text)
                except Exception as exc:                    # noqa: BLE001
                    result["errors"].append(f"delivery: {exc}")

        result["elapsed_s"] = round(time.monotonic() - started, 3)
        with self._lock:
            self._ticks += 1
            self._last_tick = time.time()
            self._last_result = result
        if result["observations"]:
            activity_log.record(
                "attention",
                f"brain tick: {result['observations']} observation(s), "
                f"{len(result['interrupts'])} interrupt(s)",
                detail="; ".join(item.get("title", "") for item in result["decisions"][:6])[:400],
                why="proactive cycle",
                meta={"queued": result["queued"], "dropped": result["dropped"]},
            )
        return result

    # ── deferred work ───────────────────────────────────────────────────

    def _do_work(self, task: str, origin: str = "a queued job") -> str:
        """Carry out background work and return the line to speak about it.

        Runs under the tick, which the app calls on a worker thread, so a slow
        task delays the next tick rather than the conversation. The result is
        always reported — silence after real work is indistinguishable from
        work that never ran, which is the failure this whole module exists to
        avoid.
        """
        task = str(task or "").strip()
        if not task:
            return ""
        if self._task_runner is None:
            return (f"[WATCH] {origin} wanted to run '{task}', but nothing is wired up "
                    f"to carry it out. Say so plainly in one sentence.")
        try:
            outcome = self._task_runner(task)
        except Exception as exc:                            # noqa: BLE001
            activity_log.record("jobs", f"background task failed: {task[:80]}",
                                detail=str(exc)[:250], outcome="error",
                                why=f"{origin} asked for it")
            return (f"[WATCH] {origin} asked me to '{task}' and it failed: {exc}. "
                    f"Say what went wrong in one short sentence.")
        message = ""
        if isinstance(outcome, dict):
            message = str(outcome.get("message") or "")
        elif outcome:
            message = str(outcome)
        return (f"[WATCH] {origin} asked me to '{task}'. Result: {message[:240] or 'done'}. "
                f"Report it in one short sentence in the user's language.")

    @staticmethod
    def watch_message(text: str) -> str:
        """Wrap a fired watch rule so the model speaks it, not this module."""
        return (f"[WATCH] A rule the user set up has fired: {text}. "
                f"Say it in one short, natural sentence in the user's language, and "
                f"make clear it was the rule they asked for. Do not ask permission — "
                f"it has already happened.")

    # ── message shaping ─────────────────────────────────────────────────

    @staticmethod
    def interrupt_message(decision: Decision) -> str:
        """Wrap an observation so Jarvis phrases it in the user's language."""
        obs = decision.observation
        if obs is None:
            return "[ATTENTION] Something worth mentioning came up."
        parts = [
            "[ATTENTION] Something I noticed on my own — not a user request.",
            f"Fact: {obs.title}.",
        ]
        if obs.detail:
            parts.append(f"Detail: {obs.detail}")
        parts.append(f"Signal: {obs.source} sense, confidence {obs.confidence:.0%}.")
        if obs.tags:
            parts.append(f"Tags: {', '.join(obs.tags)}.")
        parts.append(
            "Say it in one short, natural sentence in the user's language. "
            "No alarmism, no lists, no reading this message aloud. "
            "If it is actionable, offer the single next step in a few words."
        )
        return " ".join(parts)

    def digest_text(self, limit: int = 12) -> str:
        return self.engine.digest_text(limit=limit)

    def digest_message(self, limit: int = 12) -> str:
        """A digest item ready to drop into the session."""
        items = self.engine.digest(limit=limit)
        if not items:
            return ""
        lines = [f"- [{item.get('time')}] {item.get('title')} ({item.get('source')})"
                 for item in items]
        return (
            "[DIGEST] These are things I noticed but chose NOT to interrupt you for. "
            "Summarise them in a few short sentences in the user's language, most "
            "important first, and say nothing if a line is trivia:\n" + "\n".join(lines)
        )

    # ── introspection ───────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            ticks, last_tick, last_result = self._ticks, self._last_tick, self._last_result
        return {
            "ticks": ticks,
            "last_tick_seconds_ago": None if not last_tick else round(time.time() - last_tick, 1),
            "tick_seconds": self.tick_seconds,
            "last_result": {
                "observations": last_result.get("observations", 0),
                "interrupts": len(last_result.get("interrupts", [])),
                "queued": last_result.get("queued", 0),
                "dropped": last_result.get("dropped", 0),
            },
            "attention": self.engine.stats(),
            "rules": {"total": rules.stats()["rules"], "enabled": rules.stats()["enabled"]},
            "jobs": jobs.stats(),
            "senses": self.senses.snapshot(),
            "activity": activity_log.stats(),
        }

    def run_forever(self, stop_event: Optional[threading.Event] = None,
                    interval: Optional[int] = None) -> None:
        """Blocking loop, for standalone use or a dedicated thread."""
        period = int(interval or self.tick_seconds)
        while not (stop_event and stop_event.is_set()):
            self.tick()
            slept = 0.0
            while slept < period and not (stop_event and stop_event.is_set()):
                time.sleep(1)
                slept += 1


_SINGLETON: Optional[Brain] = None


def get_brain() -> Brain:
    """Process-wide brain, so actions and the loop share one attention state."""
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = Brain()
        return _SINGLETON


def _self_test() -> dict:
    """Prove the loop decides sanely without ever delivering speech."""
    brain = Brain(engine=AttentionEngine(persist=False), senses=SensesHub(persist=False))
    delivered: list[str] = []
    brain.bind_deliver(delivered.append)
    result = brain.tick(force=True)
    details: dict[str, Any] = {
        "tick_ran": isinstance(result, dict),
        "no_errors": not result.get("errors"),
        "counts_add_up": (len(result["interrupts"]) + result["queued"] + result["dropped"])
        == result["observations"],
        "elapsed_under_5s": result["elapsed_s"] < 5,
    }
    hot = Observation("listeners", "New port 4444 is listening", detail="self test",
                      urgency=0.95, relevance=0.9, confidence=0.9)
    decision = brain.engine.observe(hot)
    message = Brain.interrupt_message(decision)
    details["message_tagged"] = message.startswith("[ATTENTION]")
    details["message_names_fact"] = "4444" in message
    details["status_shape"] = {"ticks", "attention", "senses"} <= set(brain.status().keys())
    details["digest_renderable"] = isinstance(brain.digest_text(limit=3), str)

    ran: list[str] = []
    brain.bind_task_runner(lambda task: ran.append(task) or {"ok": True, "message": "done"})
    report = brain._do_work("find the busy process", origin="rule 'cpu hot'")
    details["work_runs"] = ran == ["find the busy process"]
    details["work_reported"] = report.startswith("[WATCH]") and "done" in report
    brain._task_runner = None
    details["work_without_runner_says_so"] = "nothing is wired up" in brain._do_work("x")
    details["watch_message_tagged"] = Brain.watch_message("disk is low").startswith("[WATCH]")
    ok = all(value for key, value in details.items() if isinstance(value, bool))
    return {"ok": bool(ok), "details": details}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
