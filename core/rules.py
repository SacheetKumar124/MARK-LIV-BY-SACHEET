"""core/rules.py — "when X happens, do Y", written by the user, run by the brain.

The senses already produce observations and the attention engine already decides
whether one is worth *saying*. What was missing is anything that let the user
attach an **action** to a condition. Every trigger here is something the machine
can already measure, and every action comes from machinery that already runs:

    when a new port opens       -> notify me, and tell me the process
    when the CPU stays over 85% -> find out what is eating it
    when the disk drops below 5% -> warn me
    when battery drops below 20% -> warn me
    when my last scan is 7 days old -> remind me to scan
    every day at 08:30          -> brief me

Design rules that are deliberate, not incidental
------------------------------------------------
1.  **Never a model call to decide.**  A rule fires on arithmetic against a
    snapshot dict. This runs on every brain tick, so a model call here would be
    a token bill every 45 seconds.
2.  **Rate-limited by construction.**  Each rule carries a cooldown and an
    hourly cap, because "the CPU is hot" is true for minutes at a time and a
    rule that speaks every tick is a rule the user turns off.
3.  **Port rules are edge-triggered, not level-triggered.**  A new port is the
    event; "the port is still open" is not. The set of matching listeners is
    kept in state, so a rule fires once when a port appears, not once a minute
    until it closes.
4.  **Nothing runs invisibly.**  Every firing is written to the activity log
    with the rule's name and the measured values that satisfied it.
5.  **`task` actions go through the same executor as everything else.**  This
    module never imports GUI or session code — it hands the caller a task string
    and the caller decides how work is performed.
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
RULES_PATH = MEMORY_DIR / "rules.json"

TRIGGERS = ("port", "cpu", "ram", "disk", "battery", "time")
ACTIONS = ("notify", "task")
DEFAULT_COOLDOWN = 600
DEFAULT_MAX_PER_HOUR = 6
FIRE_LOG_MAX = 200


# ── storage ──────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"rules": [], "state": {"seen_ports": [], "streaks": {}, "fired": []}}


def _load() -> dict:
    if not RULES_PATH.exists():
        return _empty()
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        base = _empty()
        base.update({k: v for k, v in data.items() if k in base})
        if not isinstance(base["rules"], list):
            base["rules"] = []
        state = base.get("state") if isinstance(base.get("state"), dict) else {}
        state.setdefault("seen_ports", [])
        state.setdefault("streaks", {})
        state.setdefault("fired", [])
        base["state"] = state
        return base
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Rules] ⚠️ load failed: {exc}")
        return _empty()


def _save(data: dict) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        state = data.get("state", {})
        if isinstance(state.get("fired"), list) and len(state["fired"]) > FIRE_LOG_MAX:
            state["fired"] = state["fired"][-FIRE_LOG_MAX:]
        RULES_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception as exc:                                   # noqa: BLE001
        print(f"[Rules] ⚠️ save failed: {exc}")


def _slug(text: str, limit: int = 40) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", str(text).casefold()).strip("_") or "rule")[:limit]


# ── authoring ────────────────────────────────────────────────────────────────

_MISSING = object()


def add_rule(name: str, when: Optional[dict] = None, then: Optional[dict] = None,
             cooldown_s: Any = DEFAULT_COOLDOWN, max_per_hour: Any = DEFAULT_MAX_PER_HOUR,
             enabled: bool = True, **trigger_fields: Any) -> dict:
    """Create a rule. ``when`` may be passed as a dict, or as trigger fields.

    Tolerant on purpose: the model fills these arguments from speech, so
    ``add_rule("disk", trigger="disk", free_below=5)`` and
    ``add_rule("disk", when={"trigger": "disk", "free_below": 5})`` must both
    work.
    """
    when = dict(when or {})
    if trigger_fields:
        when.update({k: v for k, v in trigger_fields.items() if v is not None})
    if "trigger" not in when:
        for key in TRIGGERS:
            if key in when:
                when["trigger"] = key
                break
    trigger = str(when.get("trigger", "")).strip().casefold()
    if trigger not in TRIGGERS:
        return {"ok": False, "error": f"trigger must be one of {', '.join(TRIGGERS)}"}

    then = dict(then or {})
    action = str(then.get("action", "")).strip().casefold() or "notify"
    if action not in ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(ACTIONS)}"}
    if action == "notify" and not str(then.get("message", "")).strip():
        return {"ok": False, "error": "a notify rule needs a message"}
    if action == "task" and not str(then.get("task", "")).strip():
        return {"ok": False, "error": "a task rule needs a task"}

    try:
        cooldown = max(30, int(float(cooldown_s or DEFAULT_COOLDOWN)))
    except (TypeError, ValueError):
        cooldown = DEFAULT_COOLDOWN
    try:
        cap = max(1, int(float(max_per_hour or DEFAULT_MAX_PER_HOUR)))
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_PER_HOUR

    condition: dict[str, Any] = {"trigger": trigger}
    for key, value in when.items():
        if key == "trigger" or value is None:
            continue
        condition[key] = value

    rule = {
        "id": f"{_slug(name, 28)}_{int(time.time()) % 100000}",
        "name": str(name or trigger).strip()[:80],
        "enabled": bool(enabled),
        "when": condition,
        "then": {"action": action,
                 **({"message": str(then.get("message"))[:300]} if then.get("message") else {}),
                 **({"task": str(then.get("task"))[:300]} if then.get("task") else {})},
        "cooldown_s": cooldown,
        "max_per_hour": cap,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_fired": 0.0,
    }

    data = _load()
    data["rules"] = [r for r in data["rules"] if r.get("id") != rule["id"]]
    data["rules"].append(rule)
    _save(data)
    activity_log.record("rules", f"rule added: {rule['name']}",
                        detail=json.dumps({"when": condition, "then": rule["then"]},
                                          ensure_ascii=False)[:300],
                        why="the user asked the assistant to watch for this")
    return {"ok": True, "rule": rule}


def _find(needle: str, data: Optional[dict] = None) -> Optional[dict]:
    data = data or _load()
    needle = str(needle or "").strip().casefold()
    if not needle:
        return None
    for rule in data["rules"]:
        if needle == rule.get("id", "").casefold() or needle == rule.get("name", "").casefold():
            return rule
    for rule in data["rules"]:
        if needle in rule.get("name", "").casefold() or needle in rule.get("id", "").casefold():
            return rule
    return None


def remove_rule(needle: str) -> str:
    data = _load()
    rule = _find(needle, data)
    if not rule:
        return f"No rule matches '{needle}'."
    data["rules"] = [r for r in data["rules"] if r.get("id") != rule["id"]]
    _save(data)
    activity_log.record("rules", f"rule removed: {rule['name']}",
                        why="the user asked for it to stop", actor="user")
    return f"Removed rule: {rule['name']}"


def set_enabled(needle: str, enabled: bool) -> str:
    data = _load()
    rule = _find(needle, data)
    if not rule:
        return f"No rule matches '{needle}'."
    rule["enabled"] = bool(enabled)
    _save(data)
    return f"{'Enabled' if enabled else 'Paused'}: {rule['name']}"


def list_rules() -> list[dict]:
    return list(_load()["rules"])


def render_rules() -> str:
    rules = list_rules()
    if not rules:
        return ("No watch rules yet. Examples of what can be added: "
                "'when a new port opens, tell me'; 'when the CPU stays above 85%, "
                "find the process'; 'every day at 8:30, brief me'.")
    lines = []
    for rule in rules:
        when = rule.get("when", {})
        then = rule.get("then", {})
        cond = ", ".join(f"{k}={v}" for k, v in when.items() if k != "trigger")
        doing = (f"say: {then.get('message')}" if then.get("action") == "notify"
                 else f"do: {then.get('task')}")
        state = "on" if rule.get("enabled") else "PAUSED"
        lines.append(f"- {rule['name']} [{state}] when {when.get('trigger')}"
                     f"{' (' + cond + ')' if cond else ''} → {doing}")
    return "My watch rules:\n" + "\n".join(lines)


# ── evaluation ───────────────────────────────────────────────────────────────

def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _listener_rows(snapshot: dict) -> list[dict]:
    """Accept listeners as ints, dicts, or the senses' own string form."""
    rows = []
    for item in (snapshot.get("listeners") or []):
        if isinstance(item, dict):
            rows.append({"port": int(item.get("port") or 0),
                         "process": str(item.get("process") or item.get("name") or ""),
                         "address": str(item.get("address") or "")})
        else:
            text = str(item)
            port = 0
            match = re.search(r"\b(\d{2,5})\b", text)
            if match:
                port = int(match.group(1))
            rows.append({"port": port, "process": text, "address": ""})
    return rows


def _port_matches(condition: dict, row: dict) -> bool:
    want = _as_float(condition.get("port"))
    if want is not None and int(want) != row["port"]:
        return False
    process = str(condition.get("process") or "").strip().casefold()
    if process and process not in row["process"].casefold():
        return False
    return True


def _time_matches(condition: dict, now: datetime) -> bool:
    at = condition.get("at") or condition.get("time") or condition.get("every")
    if not at:
        return False
    targets = at if isinstance(at, (list, tuple)) else [at]
    days = condition.get("days")
    if days:
        names = ([str(d).strip().casefold()[:3] for d in days]
                 if isinstance(days, (list, tuple)) else
                 [d.strip().casefold()[:3] for d in str(days).split(",")])
        if now.strftime("%a").casefold()[:3] not in names:
            return False
    tolerance = int(_as_float(condition.get("within_minutes")) or 2)
    for target in targets:
        try:
            hour, minute = (int(part) for part in str(target).strip().split(":")[:2])
        except (TypeError, ValueError):
            continue
        delta = abs((now.hour * 60 + now.minute) - (hour * 60 + minute))
        if min(delta, 1440 - delta) <= tolerance:
            return True
    return False


def _fired_this_hour(rule: dict, state: dict, now: float) -> int:
    hour_ago = now - 3600
    return len([f for f in state.get("fired", [])
                if f.get("id") == rule.get("id") and float(f.get("ts", 0)) >= hour_ago])


def _evaluate(rule: dict, snapshot: dict, state: dict,
              now_ts: float, now_dt: datetime) -> tuple[bool, str, dict]:
    """Return (matches, human reason, values used). Pure arithmetic — no I/O."""
    condition = rule.get("when", {})
    trigger = condition.get("trigger")
    values: dict[str, Any] = {}

    if trigger == "port":
        seen = {int(p) for p in state.get("seen_ports", []) if str(p).isdigit()}
        rows = [row for row in _listener_rows(snapshot) if _port_matches(condition, row)]
        new_rows = [row for row in rows if row["port"] and row["port"] not in seen]
        if not new_rows:
            return False, "no new matching listener", values
        row = new_rows[0]
        values = {"port": row["port"], "process": row["process"] or "unknown process"}
        return True, f"new listener on port {row['port']} ({values['process']})", values

    if trigger in ("cpu", "ram"):
        value = _as_float(snapshot.get("cpu_percent" if trigger == "cpu" else "ram_percent"))
        above = _as_float(condition.get("above", condition.get("over")))
        below = _as_float(condition.get("below", condition.get("under")))
        direction = "above" if above is not None else ("below" if below is not None else None)
        if value is None or direction is None:
            return False, "no reading (needs above= or below=)", values
        threshold = above if direction == "above" else below
        values = {"value": round(value, 1), "threshold": threshold}
        satisfied = value >= threshold if direction == "above" else value <= threshold
        key = f"{rule.get('id')}:{trigger}"
        if not satisfied:
            state.get("streaks", {}).pop(key, None)
            return False, f"{trigger} {value:.0f}% not {direction} {threshold:.0f}%", values
        need = int(_as_float(condition.get("for_ticks")) or 1)
        streak = int(state.setdefault("streaks", {}).get(key, 0)) + 1
        state["streaks"][key] = streak
        if streak < need:
            return False, f"{trigger} {direction} {threshold:.0f}% for {streak}/{need} checks", values
        return True, f"{trigger} at {value:.0f}% ({direction} {threshold:.0f}%)", values

    if trigger == "disk":
        free = _as_float(snapshot.get("disk_free_percent"))
        below = _as_float(condition.get("free_below", condition.get("below")))
        if free is None or below is None:
            return False, "no reading", values
        values = {"free": round(free, 1), "threshold": below}
        return (free <= below), f"disk free {free:.0f}%", values

    if trigger == "battery":
        battery = _as_float(snapshot.get("battery_percent"))
        below = _as_float(condition.get("below"))
        if battery is None or below is None:
            return False, "no battery reading", values
        values = {"battery": round(battery, 1), "threshold": below}
        if bool(condition.get("charging")) != bool(snapshot.get("battery_charging")):
            if condition.get("charging") is not None:
                return False, "charge state does not match", values
        return (battery <= below), f"battery {battery:.0f}%", values

    if trigger == "time":
        values = {"time": now_dt.strftime("%H:%M")}
        return _time_matches(condition, now_dt), f"clock is {values['time']}", values

    return False, f"unknown trigger '{trigger}'", values


def _fill(template: str, rule: dict, values: dict, now_dt: datetime) -> str:
    """Substitute {port} {process} {value} {free} {battery} {hours} {time} {name}."""
    filled = str(template or "")
    table = dict(values)
    table["name"] = rule.get("name", "")
    table["time"] = now_dt.strftime("%H:%M")
    table["date"] = now_dt.strftime("%Y-%m-%d")
    for key, value in table.items():
        filled = filled.replace("{" + key + "}", str(value))
    return re.sub(r"\{[a-z_]+\}", "", filled).strip()


def check(snapshot: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """Evaluate every enabled rule against a snapshot. Returns what to do.

    Output shape::

        {"notify": [sentence, ...],      # to be spoken by the caller
         "work":   [{"rule": id, "task": "…"}],   # to be executed by the caller
         "fired":  ["rule name", ...],
         "skipped": [{"rule": id, "reason": "…"}],
         "evaluated": n}

    The function never speaks and never runs anything: delivery and execution
    belong to whoever called it, which is what keeps this module testable.
    """
    snapshot = dict(snapshot or {})
    now_ts = float(now if now is not None else time.time())
    now_dt = datetime.fromtimestamp(now_ts)
    result: dict[str, Any] = {"notify": [], "work": [], "fired": [], "skipped": [],
                              "evaluated": 0}
    data = _load()
    state = data["state"]

    # Port rules are edge-triggered, so the baseline must be updated AFTER the
    # evaluation pass, never before it — updating first would erase exactly the
    # newness the rule exists to detect.
    #
    # The very first check of a fresh install also does not fire: the machine
    # already has ports open, and none of them "just opened". Announcing twenty
    # listeners at boot is how a feature gets switched off on day one.
    first_baseline = not state.get("baseline_done")

    for rule in data["rules"]:
        if not rule.get("enabled", True):
            continue
        result["evaluated"] += 1
        if first_baseline and rule.get("when", {}).get("trigger") == "port":
            _note(result, rule, "first check — recording the baseline", outcome="baseline")
            continue
        try:
            matched, reason, values = _evaluate(rule, snapshot, state, now_ts, now_dt)
        except Exception as exc:                               # noqa: BLE001
            _note(result, rule, f"evaluation failed: {exc}", outcome="error")
            continue
        if not matched:
            continue
        if now_ts - float(rule.get("last_fired", 0) or 0) < float(rule.get("cooldown_s", DEFAULT_COOLDOWN)):
            _note(result, rule, f"cooldown ({reason})", outcome="cooldown")
            continue
        if _fired_this_hour(rule, state, now_ts) >= int(rule.get("max_per_hour", DEFAULT_MAX_PER_HOUR)):
            _note(result, rule, f"hourly cap reached ({reason})", outcome="capped")
            continue

        rule["last_fired"] = now_ts
        then = rule.get("then", {})
        if then.get("action") == "task":
            result["work"].append({"rule": rule.get("id", ""),
                                   "name": rule.get("name", ""),
                                   "task": _fill(then.get("task", ""), rule, values, now_dt)})
        else:
            result["notify"].append(_fill(then.get("message", rule.get("name", "")),
                                          rule, values, now_dt))
        result["fired"].append(rule.get("name", rule.get("id", "")))
        state.setdefault("fired", []).append({
            "id": rule.get("id", ""), "ts": round(now_ts, 3),
            "time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "why": reason[:160], "values": values,
        })
        activity_log.record("rules", f"rule fired: {rule.get('name')}",
                            detail=reason[:200],
                            why=f"condition met; action={then.get('action')}")

    # The baseline is the set of ports open at the *previous* check — the
    # currently-open set, not every port ever seen. A port that closes and
    # reopens is genuinely news again; one that simply stays open is not, which
    # is what keeps this edge-triggered rather than a minute-by-minute report.
    state["seen_ports"] = sorted({row["port"] for row in _listener_rows(snapshot)
                                  if row["port"]})[-200:]
    state["baseline_done"] = True

    _save(data)
    return result


def _note(result: dict, rule: dict, reason: str, outcome: str) -> None:
    result["skipped"].append({"rule": rule.get("name", rule.get("id", "")),
                              "reason": reason, "outcome": outcome})


def test_rule(needle: str, snapshot: Optional[dict] = None) -> str:
    """Dry-run one rule against the current snapshot — no firing, no limits."""
    data = _load()
    rule = _find(needle, data)
    if not rule:
        return f"No rule matches '{needle}'."
    now_ts = time.time()
    matched, reason, values = _evaluate(rule, dict(snapshot or {}), dict(data["state"]),
                                        now_ts, datetime.fromtimestamp(now_ts))
    verdict = "WOULD FIRE" if matched else "would not fire"
    return (f"{rule['name']}: {verdict} — {reason}"
            + (f" (values: {values})" if values else ""))


def stats() -> dict:
    data = _load()
    rules = data["rules"]
    return {
        "rules": len(rules),
        "enabled": len([r for r in rules if r.get("enabled", True)]),
        "fired_last_24h": len([f for f in data["state"].get("fired", [])
                               if time.time() - float(f.get("ts", 0)) < 86_400]),
        "rules_list": [{"name": r.get("name"), "enabled": r.get("enabled"),
                        "trigger": r.get("when", {}).get("trigger")} for r in rules],
    }


def _self_test() -> dict:
    import tempfile

    global RULES_PATH
    original = RULES_PATH
    details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            RULES_PATH = Path(tmp) / "rules.json"
            now = time.time()
            base = {"cpu_percent": 20.0, "ram_percent": 40.0, "disk_free_percent": 50.0,
                    "battery_percent": 80.0, "listeners": []}

            # cooldown_s=30 so the reopen case can be exercised in-test; the
            # production default is ten minutes.
            details["add_port_rule"] = add_rule("watch 4444", trigger="port", port=4444,
                                                cooldown_s=30,
                                                then={"action": "notify",
                                                      "message": "Port {port} opened by {process}"}
                                                ).get("ok") is True
            warm = check(base, now - 5)                        # establishes the baseline
            details["first_check_records_baseline"] = not warm["notify"]
            first = check({**base, "listeners": [{"port": 4444, "process": "nc"}]}, now)
            details["port_fires_once"] = len(first["notify"]) == 1 and "4444" in first["notify"][0]
            details["port_message_filled"] = "nc" in first["notify"][0]
            second = check({**base, "listeners": [{"port": 4444, "process": "nc"}]}, now + 5)
            details["port_not_level_triggered"] = not second["notify"]
            # Closes, then reopens: that is genuinely news again, and it must fire
            # even though the same port number was seen before.
            closed = check({**base, "listeners": []}, now + 40)
            reopened = check({**base, "listeners": [{"port": 4444, "process": "nc"}]}, now + 80)
            details["port_closing_is_silent"] = not closed["notify"]
            details["port_fires_again_when_it_reopens"] = bool(reopened["notify"])

            add_rule("cpu hot", trigger="cpu", above=85, for_ticks=2,
                     then={"action": "notify", "message": "CPU at {value}%"})
            details["cpu_needs_streak"] = not check(
                {**base, "cpu_percent": 95.0}, now + 30)["notify"]
            details["cpu_fires_on_second"] = bool(
                check({**base, "cpu_percent": 95.0}, now + 31)["notify"])

            add_rule("disk low", trigger="disk", free_below=5,
                     then={"action": "notify", "message": "Disk {free}% free"})
            details["disk_silent_when_fine"] = not check({**base, "disk_free_percent": 40.0},
                                                         now + 40)["notify"]
            details["disk_fires_when_low"] = bool(
                check({**base, "disk_free_percent": 3.0}, now + 41)["notify"])

            add_rule("battery low", trigger="battery", below=20,
                     then={"action": "notify", "message": "Battery {battery}%"})
            details["battery_fires"] = bool(
                check({**base, "battery_percent": 12.0}, now + 50)["notify"])

            # A rule whose action is a task must become queued work, not a
            # notification — proven on the disk trigger.
            add_rule("disk task", trigger="disk", free_below=10,
                     then={"action": "task", "task": "free up disk space"})
            low = check({**base, "disk_free_percent": 5.0}, now + 60)
            details["task_rules_become_work"] = (
                len(low["work"]) == 1 and low["work"][0]["task"] == "free up disk space"
            )

            add_rule("morning brief", trigger="time", at="08:30",
                     then={"action": "notify", "message": "Good morning"})
            eight_thirty = datetime(2026, 9, 27, 8, 30).timestamp()
            details["time_fires_at_the_minute"] = bool(check(base, eight_thirty)["notify"])
            details["time_quiet_otherwise"] = not check(
                base, datetime(2026, 9, 27, 12, 0).timestamp())["notify"]

            add_rule("capped", trigger="cpu", above=1, cooldown_s=30, max_per_hour=2,
                     then={"action": "notify", "message": "hot"})
            fires = 0
            for i in range(4):
                fires += len(check({**base, "cpu_percent": 99.0}, now + 100 + i * 60)["notify"])
            details["hourly_cap_holds"] = fires == 2, {"fires": fires}

            details["cooldown_reported"] = any(
                item["outcome"] in ("cooldown", "capped")
                for item in check({**base, "cpu_percent": 99.0}, now + 101)["skipped"])
            details["remove_rule"] = "Removed" in remove_rule("watch 4444")
            details["pause_rule"] = "Paused" in set_enabled("disk low", False)
            details["render_works"] = "watch rules" in render_rules().casefold()
            details["stats_shape"] = {"rules", "enabled", "fired_last_24h"} <= set(stats())
            details["bad_trigger_refused"] = add_rule("bad", trigger="moon", then={
                "action": "notify", "message": "x"}).get("ok") is False
            details["empty_message_refused"] = add_rule("bad2", trigger="cpu",
                                                        then={"action": "notify"}).get("ok") is False
    finally:
        RULES_PATH = original

    ok = all(value[0] if isinstance(value, tuple) else value for value in details.values())
    return {"ok": bool(ok), "details": {k: (v[0] if isinstance(v, tuple) else v)
                                        for k, v in details.items()}}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
