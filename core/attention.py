"""core/attention.py — decide what is worth interrupting the user for.

This is the difference between an assistant that feels intelligent and one
that feels like a spam bot. "More automatic" must not mean "more
interruptions": every observation gets scored for urgency, relevance, novelty
and confidence, and only the top few earn the right to speak. Everything else
queues silently into a digest the user can ask for.

Scoring is deliberately simple and deterministic — it runs every tick, it must
cost nothing, and its decisions must be explainable after the fact (the reason
string is written to the activity log).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core import activity_log

BASE_DIR = Path(__file__).resolve().parent.parent
POLICY_PATH = BASE_DIR / "config" / "assistant_policy.json"
STATE_PATH = BASE_DIR / "memory" / "attention_state.json"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "tick_seconds": 45,
    "min_interrupt_score": 0.62,
    "weights": {"urgency": 0.42, "relevance": 0.30, "novelty": 0.18, "confidence": 0.10},
    "budget": {"interrupts_per_hour": 4, "interrupts_per_day": 24, "min_gap_seconds": 180},
    "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:30",
                    "allow_urgent": True, "urgent_floor": 0.90},
    "digest": {"max_items": 40, "ttl_hours": 48},
    "novelty_ttl_hours": 6,
    "rescore_after_seconds": 900,
}

URGENT_FLOOR = 0.90
QUEUE_FLOOR = 0.30
_LOCK = threading.RLock()


# ── observations ────────────────────────────────────────────────────────────


@dataclass
class Observation:
    """One thing a sense noticed, with its own honest confidence."""

    source: str                      # telemetry | presence | security | listeners | phone | user
    title: str
    detail: str = ""
    urgency: float = 0.0
    relevance: float = 0.0
    novelty: float = 1.0
    confidence: float = 1.0
    kind: str = "info"
    tags: tuple[str, ...] = field(default_factory=tuple)
    meta: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable identity for dedup, so the same news never repeats.

        Digits are kept deliberately: "port 4444 opened" and "port 5555
        opened" are different news, and collapsing them (an earlier version
        normalised numbers away) silently hid real changes.
        """
        raw = f"{self.source}|{self.kind}|{self.title}".casefold()
        raw = re.sub(r"\s+", " ", raw).strip()
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]

    def clamp(self) -> "Observation":
        for name in ("urgency", "relevance", "novelty", "confidence"):
            value = float(getattr(self, name) or 0.0)
            setattr(self, name, max(0.0, min(1.0, value)))
        return self


@dataclass
class Decision:
    action: str                      # interrupt | queue | drop
    score: float
    reason: str
    observation: Optional[Observation] = None

    def as_dict(self) -> dict:
        obs = self.observation
        return {
            "action": self.action,
            "score": round(self.score, 3),
            "reason": self.reason,
            "title": obs.title if obs else "",
            "source": obs.source if obs else "",
        }


# ── policy loading ──────────────────────────────────────────────────────────


# Sections other modules read through this same loader.  They are carried
# through verbatim so `core/senses.py`, `core/brain.py` and the actions all see
# one consistent view of the file instead of each re-reading it differently.
PASSTHROUGH_SECTIONS = ("senses", "trust", "phone", "safety_boundaries")


def load_policy() -> dict:
    """Read the policy file, falling back to defaults section by section."""
    policy = json.loads(json.dumps(DEFAULTS))     # deep copy
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return policy
    if not isinstance(raw, dict):
        return policy
    attention = raw.get("attention")
    if isinstance(attention, dict):
        for key, value in attention.items():
            if isinstance(value, dict) and isinstance(policy.get(key), dict):
                policy[key].update(value)
            else:
                policy[key] = value
    for section in PASSTHROUGH_SECTIONS:
        if isinstance(raw.get(section), dict):
            policy[section] = raw[section]
    return policy


def _parse_hhmm(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return fallback


# ── the engine ──────────────────────────────────────────────────────────────


class AttentionEngine:
    """Score observations and spend a limited interruption budget."""

    def __init__(self, policy: Optional[dict] = None, persist: bool = True) -> None:
        self._lock = threading.RLock()
        self.policy = policy or load_policy()
        self._persist = persist
        self._state: dict[str, Any] = {
            "seen": {},              # fingerprint -> last ts
            "spend": [],             # interrupt timestamps
            "digest": [],            # queued observations
            "muted_until": 0.0,
            "quiet_override": {},
        }
        if persist:
            self._load()

    # ── state ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in self._state:
                    if key in data:
                        self._state[key] = data[key]
        except (OSError, ValueError):
            pass
        self._state["spend"] = [float(t) for t in self._state.get("spend", [])][-400:]
        self._prune_state()

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_PATH)
        except OSError:
            pass

    def _prune_state(self) -> None:
        now = time.time()
        ttl = float(self.policy.get("novelty_ttl_hours", 6)) * 3600
        seen = {fp: ts for fp, ts in dict(self._state.get("seen", {})).items()
                if now - float(ts) < ttl}
        self._state["seen"] = seen
        digest_ttl = float(self.policy.get("digest", {}).get("ttl_hours", 48)) * 3600
        digest = [item for item in self._state.get("digest", [])
                  if now - float(item.get("ts") or 0) < digest_ttl]
        self._state["digest"] = digest[-int(self.policy.get("digest", {}).get("max_items", 40)):]

    # ── scoring ─────────────────────────────────────────────────────────

    def score(self, obs: Observation) -> float:
        obs.clamp()
        weights = self.policy.get("weights", DEFAULTS["weights"])
        return (
            float(weights.get("urgency", 0.42)) * obs.urgency
            + float(weights.get("relevance", 0.30)) * obs.relevance
            + float(weights.get("novelty", 0.18)) * obs.novelty
            + float(weights.get("confidence", 0.10)) * obs.confidence
        )

    def _quiet_hours_active(self, now: datetime) -> bool:
        config = self.policy.get("quiet_hours", {})
        if not config.get("enabled", True):
            return False
        if self._state.get("quiet_override", {}).get("disabled"):
            return False
        override = self._state.get("quiet_override", {}).get("window")
        start_text = override[0] if isinstance(override, (list, tuple)) and override else config.get("start", "23:00")
        end_text = override[1] if isinstance(override, (list, tuple)) and len(override) > 1 else config.get("end", "07:30")
        start = _parse_hhmm(start_text, (23, 0))
        end = _parse_hhmm(end_text, (7, 30))
        minutes = now.hour * 60 + now.minute
        start_m, end_m = start[0] * 60 + start[1], end[0] * 60 + end[1]
        if start_m == end_m:
            return False
        if start_m < end_m:
            return start_m <= minutes < end_m
        return minutes >= start_m or minutes < end_m        # crosses midnight

    def budget_state(self, now_ts: Optional[float] = None) -> dict:
        now = float(now_ts or time.time())
        budget = self.policy.get("budget", DEFAULTS["budget"])
        spend = [t for t in self._state.get("spend", []) if now - float(t) < 86400]
        hour = [t for t in spend if now - float(t) < 3600]
        last = max(spend) if spend else 0.0
        gap = now - last if last else 1e9
        return {
            "hour_used": len(hour),
            "hour_limit": int(budget.get("interrupts_per_hour", 4)),
            "day_used": len(spend),
            "day_limit": int(budget.get("interrupts_per_day", 24)),
            "seconds_since_last": None if last == 0 else round(gap, 1),
            "min_gap_seconds": int(budget.get("min_gap_seconds", 180)),
            "muted_for_s": max(0, round(float(self._state.get("muted_until", 0)) - now)),
        }

    # ── the decision ────────────────────────────────────────────────────

    def observe(self, obs: Observation) -> Decision:
        """Score one observation and decide: interrupt, queue, or drop."""
        obs.clamp()
        with self._lock:
            now_ts = time.time()
            self._prune_state()
            fingerprint = obs.fingerprint
            seen_at = float(self._state.get("seen", {}).get(fingerprint, 0) or 0)
            repeat = bool(seen_at)
            if repeat:
                # Already told the user this. It stays interesting, but it no
                # longer deserves a second interruption on novelty grounds.
                obs.novelty = 0.05
            self._state.setdefault("seen", {})[fingerprint] = now_ts

            score = self.score(obs)
            urgent = obs.urgency >= float(
                self.policy.get("quiet_hours", {}).get("urgent_floor", URGENT_FLOOR)
            )

            if not self.policy.get("enabled", True):
                decision = Decision("drop", score, "attention engine disabled", obs)
                self._save()
                return decision

            if repeat:
                # Urgency bypasses quiet hours and the budget, but never this:
                # if the user has already been told, repeating it every tick is
                # spam no matter how urgent the underlying condition is.  A
                # changed condition produces a different fingerprint anyway.
                decision = Decision("drop", score, "already reported this exact observation", obs)
                self._state["seen"][fingerprint] = now_ts
                self._save()
                return decision

            muted_for = float(self._state.get("muted_until", 0)) - now_ts
            quiet = self._quiet_hours_active(datetime.now())
            budget = self.budget_state(now_ts)
            floor = float(self.policy.get("min_interrupt_score", 0.62))

            reason_block = ""
            if muted_for > 0 and not urgent:
                reason_block = f"muted for another {int(muted_for)}s"
            elif quiet and not urgent:
                reason_block = "quiet hours"
            elif budget["hour_used"] >= budget["hour_limit"] and not urgent:
                reason_block = f"hourly interruption budget spent ({budget['hour_used']}/{budget['hour_limit']})"
            elif budget["day_used"] >= budget["day_limit"] and not urgent:
                reason_block = f"daily interruption budget spent ({budget['day_used']}/{budget['day_limit']})"
            elif budget["seconds_since_last"] is not None and \
                    budget["seconds_since_last"] < budget["min_gap_seconds"] and not urgent:
                reason_block = f"only {int(budget['seconds_since_last'])}s since the last interruption"

            if not reason_block and score >= floor:
                self._state.setdefault("spend", []).append(now_ts)
                reason = (f"score {score:.2f} ≥ {floor:.2f} "
                          f"(urgency {obs.urgency:.2f}, relevance {obs.relevance:.2f}, "
                          f"novelty {obs.novelty:.2f}, confidence {obs.confidence:.2f})")
                activity_log.record(
                    "interrupt", f"spoke up: {obs.title}", detail=obs.detail, why=reason,
                    meta={"source": obs.source, "score": round(score, 3)},
                )
                self._save()
                return Decision("interrupt", score, reason, obs)

            if not reason_block:
                reason_block = f"score {score:.2f} below the {floor:.2f} interrupt floor"

            # A digest slot has to be earned by urgency or relevance.  Novelty
            # alone (simply never having seen it before) is not worth keeping.
            interesting = obs.urgency >= 0.40 or obs.relevance >= 0.50
            if interesting and (score >= QUEUE_FLOOR or urgent):
                self._queue(obs, score, reason_block, now_ts)
                self._save()
                return Decision("queue", score, reason_block, obs)

            self._save()
            return Decision("drop", score, reason_block, obs)

    def _queue(self, obs: Observation, score: float, reason: str, now_ts: float) -> None:
        digest = self._state.setdefault("digest", [])
        digest.append({
            "ts": now_ts,
            "time": time.strftime("%H:%M", time.localtime(now_ts)),
            "source": obs.source,
            "kind": obs.kind,
            "title": obs.title,
            "detail": obs.detail,
            "score": round(score, 3),
            "reason": reason,
            "fingerprint": obs.fingerprint,
            "meta": obs.meta,
        })
        limit = int(self.policy.get("digest", {}).get("max_items", 40))
        self._state["digest"] = digest[-limit:]
        activity_log.record(
            "observation", f"queued: {obs.title}", detail=obs.detail, why=reason,
            meta={"source": obs.source, "score": round(score, 3)},
        )

    # ── digest and controls ─────────────────────────────────────────────

    def digest(self, limit: int = 25, mark_seen: bool = True) -> list[dict]:
        with self._lock:
            items = list(self._state.get("digest", []))[-max(1, int(limit or 25)):]
            if mark_seen and items:
                self._state["digest"] = []
                self._save()
            return list(reversed(items))

    def peek_digest(self, limit: int = 25) -> list[dict]:
        return self.digest(limit=limit, mark_seen=False)

    def digest_text(self, limit: int = 12) -> str:
        items = self.digest(limit=limit)
        if not items:
            return "Nothing queued — you are all caught up."
        lines = [f"{len(items)} thing(s) I noticed but did not interrupt you for:"]
        for item in items:
            lines.append(f"- [{item.get('time')}] {item.get('title')} ({item.get('source')})")
            if item.get("detail"):
                lines.append(f"    {item['detail']}")
        return "\n".join(lines)

    def mute(self, minutes: int) -> str:
        minutes = max(1, min(int(minutes or 0), 1440))
        with self._lock:
            self._state["muted_until"] = time.time() + minutes * 60
            self._save()
        activity_log.record("attention", f"muted for {minutes}m", why="user request", actor="user")
        return f"Holding interruptions for {minutes} minute(s). Urgent alerts still get through."

    def unmute(self) -> str:
        with self._lock:
            self._state["muted_until"] = 0.0
            self._save()
        return "Interruptions resumed."

    def set_quiet_hours(self, start: Optional[str] = None, end: Optional[str] = None,
                        enabled: Optional[bool] = None) -> str:
        with self._lock:
            override = dict(self._state.get("quiet_override", {}))
            if start and end:
                override["window"] = [start, end]
            if enabled is not None:
                override["disabled"] = not enabled
            self._state["quiet_override"] = override
            self._save()
        return self.describe_quiet_hours()

    def describe_quiet_hours(self) -> str:
        config = self.policy.get("quiet_hours", {})
        override = self._state.get("quiet_override", {})
        window = override.get("window") or [config.get("start", "23:00"), config.get("end", "07:30")]
        enabled = config.get("enabled", True) and not override.get("disabled")
        state = "on" if enabled else "off"
        return f"Quiet hours are {state}: {window[0]}–{window[1]} (urgent alerts always break through)."

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": bool(self.policy.get("enabled", True)),
                "interrupt_floor": float(self.policy.get("min_interrupt_score", 0.62)),
                "budget": self.budget_state(),
                "digest_queued": len(self._state.get("digest", [])),
                "quiet_hours": self.describe_quiet_hours(),
                "tracked_observations": len(self._state.get("seen", {})),
                "recent_interrupts": [
                    e["action"] for e in activity_log.recent(limit=5, category="interrupt")
                ],
            }

    def snapshot(self) -> list[dict]:
        """Freeze observations so later ticks do not resurface old news."""
        with self._lock:
            return list(self._state.get("digest", []))


def _self_test() -> dict:
    """Non-destructive exercise of the scoring and budget logic."""
    engine = AttentionEngine(persist=False)
    details: dict[str, Any] = {}

    calm = Observation("telemetry", "Disk is 91% full", urgency=0.35, relevance=0.6, confidence=1.0)
    hot = Observation("listeners", "New port 4444 is listening", urgency=0.95, relevance=0.95, confidence=0.9)
    boring = Observation("presence", "User is idle", urgency=0.1, relevance=0.2, confidence=1.0)

    calm_decision = engine.observe(calm)
    hot_decision = engine.observe(hot)
    boring_decision = engine.observe(boring)
    repeat_decision = engine.observe(hot)

    details["calm_decision"] = calm_decision.action
    details["hot_decision"] = hot_decision.action
    details["hot_interrupts"] = hot_decision.action == "interrupt"
    details["boring_dropped"] = boring_decision.action == "drop"
    details["repeat_suppressed"] = repeat_decision.action != "interrupt"
    details["score_ordering"] = engine.score(hot) > engine.score(calm) > engine.score(boring)
    details["budget_enforced"] = engine.budget_state()["hour_used"] >= 1
    details["digest_has_items"] = len(engine.digest(limit=5, mark_seen=False)) >= 1

    ok = (
        details["hot_interrupts"]
        and details["boring_dropped"]
        and details["repeat_suppressed"]
        and details["score_ordering"]
        and details["digest_has_items"]
    )
    return {"ok": ok, "details": details}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(_self_test(), indent=2, ensure_ascii=False))
