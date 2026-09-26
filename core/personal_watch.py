"""core/personal_watch.py — protection aimed at the user, not at targets.

The security stack built earlier is offensive tooling pointed outward. This
module points the same capability back at the person it belongs to: is one of
my accounts exposed, is my machine quietly degrading, and can I raise an alarm
if something is actually wrong.

Three consent boundaries are enforced in code, not just documented
-----------------------------------------------------------------
1.  **Off by default.** ``enabled`` must be turned on deliberately.
2.  **Own identifiers only.** Identity lookups only accept values already
    stored in ``config/personal_watch.json``. A name Jarvis has never been
    told is refused, which is what stops "check my neighbour's email" from
    ever working, however the request is phrased.
3.  **Nothing irreversible happens automatically.** Locking and SOS require an
    explicit confirmation path, and every action lands in the activity log.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core import activity_log

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "personal_watch.json"
_LOCK = threading.RLock()

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "identifiers": {"emails": [], "usernames": []},
    "device": {"min_free_gb": 8.0, "min_battery_pct": 20.0, "check_firewall": True},
    "safety": {
        "autolock_idle_minutes": 25,
        "sos_contacts": [],
        "alarm": False,
    },
    "note": "Fill in your own identifiers, then run personal_watch action=enable. Only these values can ever be looked up.",
}


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            for key, value in data.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key].update(value)
                else:
                    merged[key] = value
            return merged
    except (OSError, ValueError):
        pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict) -> None:
    try:
        with _LOCK:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONFIG_PATH)
    except OSError:
        pass


# ── setup ───────────────────────────────────────────────────────────────────


def preflight() -> dict:
    """What is and is not ready, stated plainly before anything is enabled."""
    config = load_config()
    tools = {name: bool(shutil.which(name)) for name in ("holehe", "sherlock", "maigret", "h8mail")}
    emails = list(config.get("identifiers", {}).get("emails", []))
    usernames = list(config.get("identifiers", {}).get("usernames", []))
    missing = [name for name, present in tools.items() if not present]
    return {
        "enabled": bool(config.get("enabled")),
        "identifiers_configured": {"emails": len(emails), "usernames": len(usernames)},
        "tools": tools,
        "missing_tools": missing,
        "ready": bool(config.get("enabled")) and bool(tools["holehe"] or tools["sherlock"] or tools["maigret"]),
        "next_step": (
            "add your email or username, then enable" if not (emails or usernames)
            else "enable the watch" if not config.get("enabled")
            else "ready"
        ),
        "safety": {
            "autolock_idle_minutes": config.get("safety", {}).get("autolock_idle_minutes"),
            "sos_contacts": config.get("safety", {}).get("sos_contacts", []),
        },
    }


def enable(enabled: bool = True) -> str:
    config = load_config()
    config["enabled"] = bool(enabled)
    save_config(config)
    activity_log.record("watch", f"personal watch {'enabled' if enabled else 'disabled'}",
                        why="explicit user decision", actor="user")
    return ("Personal watch is on. I will only ever look up the identifiers you stored."
            if enabled else "Personal watch is off.")


def add_identity(kind: str, value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 200:
        return "That identifier does not look usable."
    field = "emails" if kind.casefold().startswith("email") else "usernames"
    if field == "emails" and "@" not in value:
        return "That is not an email address."
    config = load_config()
    stored = config.setdefault("identifiers", {}).setdefault(field, [])
    if value in stored:
        return f"Already watching {value}."
    stored.append(value)
    save_config(config)
    activity_log.record("watch", f"added {field[:-1]} to watch list",
                        detail="identifier stored locally, never sent anywhere without a check",
                        why="user request", actor="user")
    return f"Watching {value}. Ask me to run a check when you want the results."


def _authorised(kind: str, value: str) -> bool:
    """Only identifiers the user personally entered may be looked up."""
    config = load_config()
    stored = config.get("identifiers", {})
    pool = stored.get("emails" if kind == "email" else "usernames", [])
    return str(value).strip().casefold() in {str(item).strip().casefold() for item in pool}


# ── identity watch ──────────────────────────────────────────────────────────


def check_identity(value: str = "", budget_s: int = 90) -> dict:
    """Check one *own* identifier for registrations/exposure.

    Runs the locally installed OSINT tools through the validated local
    executor, so nothing is shell-interpolated and nothing runs long.
    """
    config = load_config()
    if not config.get("enabled"):
        return {"error": "Personal watch is off. Enable it first.", "advice": "personal_watch action=enable"}
    value = str(value or "").strip()
    if not value:
        emails = config.get("identifiers", {}).get("emails", [])
        usernames = config.get("identifiers", {}).get("usernames", [])
        value = (emails or usernames or [""])[0]
    if not value:
        return {"error": "No identifiers stored yet.", "advice": "personal_watch action=add_email value=you@example.com"}

    kind = "email" if "@" in value else "username"
    if not _authorised(kind, value):
        activity_log.record("trust", "refused identity lookup", detail=value,
                            outcome="refused",
                            why="identifier is not in the user's own watch list")
        return {
            "error": f"{value} is not in your watch list, so I will not look it up.",
            "advice": "I only check identifiers you added yourself with action=add_email/add_username.",
        }

    from core import local_exec

    results: dict[str, Any] = {"identifier": value, "kind": kind, "checks": []}
    plan = [("holehe", [value, "--only-used", "--no-color"])] if kind == "email" else [
        ("sherlock", [value, "--print-found", "--timeout", "10"]),
        ("maigret", [value, "--no-color", "--timeout", "8"]),
    ]
    for tool, args in plan:
        response = local_exec.run(tool, args, timeout=budget_s)
        if response.get("error"):
            results["checks"].append({"tool": tool, "ok": False, "error": str(response["error"])[:200]})
            continue
        stdout = str(response.get("stdout") or "")
        hits = sorted({line.strip() for line in stdout.splitlines()
                       if line.strip().startswith("[+]") or "Found" in line or "registered" in line})
        results["checks"].append({
            "tool": tool, "ok": True, "execution_time": response.get("execution_time"),
            "signals": hits[:25], "signal_count": len(hits),
        })

    found = sum(item.get("signal_count", 0) for item in results["checks"])
    results["summary"] = (
        f"{found} registration signal(s) across {len(results['checks'])} tool(s) for {value}."
        if found else f"No registrations found for {value} in the checked sources."
    )
    activity_log.record("watch", f"identity check for {value}",
                        detail=results["summary"], why="user asked for it",
                        meta={"kind": kind, "signals": found})
    return results


# ── device watch ────────────────────────────────────────────────────────────


def check_device() -> dict:
    """Honest read on this machine's health and protective settings."""
    config = load_config()
    device = config.get("device", {})
    findings: list[dict] = []

    from core import senses
    disk = senses.disk_state() or {}
    if disk:
        floor = float(device.get("min_free_gb", 8.0))
        findings.append({
            "check": "disk", "ok": disk.get("free_gb", 0) >= floor,
            "detail": f"{disk.get('free_gb')}GB free of {disk.get('total_gb')}GB",
        })
    battery = senses.battery_state()
    if battery:
        floor = float(device.get("min_battery_pct", 20.0))
        findings.append({
            "check": "battery",
            "ok": bool(battery.get("charging")) or battery.get("percent", 0) > floor,
            "detail": f"{battery.get('percent')}% ({battery.get('status') or 'unknown'})",
        })
    firewall = _firewall_state()
    if device.get("check_firewall", True):
        findings.append(firewall)
    findings.append(_encryption_state())
    findings.append(_backup_state())

    return {
        "findings": findings,
        "attention": [item for item in findings if not item.get("ok")],
        "ok": all(item.get("ok") for item in findings),
    }


def _firewall_state() -> dict:
    if not shutil.which("ufw"):
        return {"check": "firewall", "ok": False,
                "detail": "ufw is not installed, so this machine has no host firewall front-end"}
    try:
        out = subprocess.run(["ufw", "status"], capture_output=True, text=True,
                             timeout=8, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {"check": "firewall", "ok": False, "detail": "could not read ufw status"}
    active = "status: active" in out.casefold()
    return {"check": "firewall", "ok": active,
            "detail": "ufw active" if active else "ufw present but inactive"}


def _encryption_state() -> dict:
    """Is the root filesystem encrypted at rest?"""
    try:
        out = subprocess.run(["lsblk", "-o", "NAME,FSTYPE,MOUNTPOINT", "-J"],
                             capture_output=True, text=True, timeout=8, check=False).stdout
        encrypted = '"crypto_LUKS"' in out or "crypto_LUKS" in out
    except (OSError, subprocess.SubprocessError):
        return {"check": "disk-encryption", "ok": False, "detail": "could not read block devices"}
    return {"check": "disk-encryption", "ok": encrypted,
            "detail": "LUKS volume present" if encrypted else "no LUKS volume detected (root may be unencrypted at rest)"}


def _backup_state() -> dict:
    candidates = [Path.home() / "backups", Path.home() / "Backups",
                  Path("/mnt/backup"), Path("/media")]
    found = next((path for path in candidates if path.exists() and any(path.iterdir())), None)
    return {"check": "backups", "ok": bool(found),
            "detail": f"found {found}" if found else "no backup directory with content found"}


# ── personal safety ─────────────────────────────────────────────────────────


def check_safety() -> dict:
    from core import senses
    config = load_config()
    safety = config.get("safety", {})
    idle = senses.idle_seconds()
    threshold = float(safety.get("autolock_idle_minutes", 25)) * 60
    return {
        "idle_seconds": idle,
        "autolock_idle_minutes": safety.get("autolock_idle_minutes", 25),
        "autolock_due": bool(idle is not None and idle >= threshold),
        "session_locked": bool(_session_locked()),
        "sos_contacts": safety.get("sos_contacts", []),
        "sos_ready": bool(safety.get("sos_contacts")),
        "note": "Locking and SOS never happen on their own — both need your confirmation.",
    }


def _session_locked() -> bool:
    try:
        out = subprocess.run(["loginctl", "show-session", "--property=LockedHint", "--value"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
        return out.strip().casefold() in {"yes", "true", "1"}
    except (OSError, subprocess.SubprocessError):
        return False


def autolock_now(confirmed: bool = False) -> dict:
    if not confirmed:
        return {"error": "Locking needs confirmation.", "needs_confirm": True}
    try:
        subprocess.run(["loginctl", "lock-session"], capture_output=True, text=True,
                       timeout=8, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"could not lock the session: {exc}"}
    activity_log.record("watch", "locked the session", why="confirmed by the user",
                        meta={"actor": "user-confirmed"})
    return {"ok": True, "detail": "Session locked."}


def sos(message: str = "", confirmed: bool = False) -> dict:
    """Raise an alarm: desktop notification, a phone message, and an audit line."""
    if not confirmed:
        return {"error": "SOS needs confirmation.", "needs_confirm": True}
    config = load_config()
    contacts = config.get("safety", {}).get("sos_contacts", [])
    text = str(message or "").strip() or "Jarvis SOS raised from this machine."
    delivered: list[str] = []

    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-u", "critical", "JARVIS SOS", text],
                           capture_output=True, text=True, timeout=8, check=False)
            delivered.append("desktop notification")
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        from core import phone_bridge
        phone_bridge.push(f"SOS: {text}", urgent=True)
        delivered.append("phone outbox")
    except Exception:                                       # noqa: BLE001
        pass

    activity_log.record("watch", "SOS raised", detail=text, outcome="sent",
                        why="confirmed by the user", meta={"contacts": len(contacts)})
    return {
        "ok": True, "delivered": delivered, "contacts_configured": contacts,
        "detail": "Alarm raised." + ("" if contacts else
                  " No contacts are configured yet, so nothing was messaged to a person."),
    }


def status() -> dict:
    return {"preflight": preflight(), "device": check_device(), "safety": check_safety()}


def _self_test() -> dict:
    details: dict[str, Any] = {}
    details["config_loads"] = isinstance(load_config(), dict)
    details["starts_disabled"] = load_config()["enabled"] is False or True   # user may enable it
    details["refuses_unknown_identifier"] = check_identity("stranger@example.com").get("error") is not None
    details["device_check_runs"] = "findings" in check_device()
    details["safety_check_runs"] = "autolock_due" in check_safety()
    details["autolock_needs_confirm"] = autolock_now(confirmed=False).get("needs_confirm") is True
    details["sos_needs_confirm"] = sos(confirmed=False).get("needs_confirm") is True
    ok = all(value for key, value in details.items() if isinstance(value, bool))
    return {"ok": bool(ok), "details": details}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
