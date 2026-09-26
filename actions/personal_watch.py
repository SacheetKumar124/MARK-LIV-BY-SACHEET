"""actions/personal_watch.py — the user's own protection, as a callable action.

Deliberately separate from the offensive tooling: this action exists so the
user can ask "am I exposed?", "is my machine healthy?", "lock it" and "raise an
alarm", and so Jarvis can answer without reaching for a scanner aimed at
somebody else.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import personal_watch as watch                        # noqa: E402


def _handle(parameters: dict) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().casefold()
    value = str(params.get("value") or "").strip()
    confirmed = bool(params.get("confirm"))

    if action == "preflight":
        report = watch.preflight()
        lines = [
            f"Personal watch is {'on' if report['enabled'] else 'off'}.",
            f"Identifiers: {report['identifiers_configured']['emails']} email(s), "
            f"{report['identifiers_configured']['usernames']} username(s).",
            "Tools: " + ", ".join(f"{name} {'yes' if ok else 'no'}"
                                  for name, ok in report["tools"].items()),
        ]
        if report["missing_tools"]:
            lines.append("Missing: " + ", ".join(report["missing_tools"]))
        lines.append(f"Next step: {report['next_step']}.")
        return "\n".join(lines)

    if action == "enable":
        return watch.enable(True)

    if action == "disable":
        return watch.enable(False)

    if action in {"add_email", "add_username"}:
        kind = "email" if action == "add_email" else "username"
        if not value:
            return f"Give me the {kind} to watch."
        return watch.add_identity(kind, value)

    if action == "check_identity":
        result = watch.check_identity(value)
        if result.get("error"):
            return result["error"] + " " + str(result.get("advice", ""))
        lines = [result["summary"]]
        for check in result["checks"]:
            if check.get("ok"):
                lines.append(f"- {check['tool']}: {check.get('signal_count', 0)} signal(s)"
                             + (f" in {check.get('execution_time')}s" if check.get("execution_time") else ""))
                for signal in (check.get("signals") or [])[:6]:
                    lines.append(f"    {signal}")
            else:
                lines.append(f"- {check['tool']}: unavailable ({check.get('error', 'unknown')})")
        return "\n".join(lines)

    if action == "check_device":
        report = watch.check_device()
        lines = [("Everything looks healthy." if report["ok"]
                  else f"{len(report['attention'])} thing(s) worth fixing:")]
        for finding in report["findings"]:
            mark = "ok" if finding.get("ok") else "ATTENTION"
            lines.append(f"- [{mark}] {finding.get('check')}: {finding.get('detail')}")
        return "\n".join(lines)

    if action == "check_safety":
        safety = watch.check_safety()
        idle = safety.get("idle_seconds")
        return (
            f"Idle for {int(idle)}s (autolock at {safety['autolock_idle_minutes']} min). "
            f"Session {'locked' if safety['session_locked'] else 'unlocked'}. "
            f"SOS contacts: {len(safety['sos_contacts'])}. "
            + ("Autolock is due." if safety["autolock_due"] else "No autolock needed yet.")
        )

    if action == "autolock":
        result = watch.autolock_now(confirmed=confirmed)
        if result.get("needs_confirm"):
            return "Locking the session changes your screen — confirm it and I will lock immediately."
        return result.get("detail") or result.get("error", "Done.")

    if action == "sos":
        result = watch.sos(message=value, confirmed=confirmed)
        if result.get("needs_confirm"):
            return "SOS raises an alarm on your desktop and phone. Confirm and I will send it now."
        return result.get("detail", "Alarm raised.") + " Delivered via: " + \
            ", ".join(result.get("delivered", [])) + "."

    if action == "status":
        report = watch.status()
        return json.dumps({
            "enabled": report["preflight"]["enabled"],
            "device_ok": report["device"]["ok"],
            "device_attention": report["device"]["attention"],
            "safety": report["safety"],
        }, indent=1)

    return ("Unknown personal_watch action. Try: preflight, enable, disable, add_email, "
            "add_username, check_identity, check_device, check_safety, autolock, sos, status.")


TOOL = {
    "name": "personal_watch",
    "description": (
        "The user's own protection: check whether THEIR email/username is exposed, "
        "audit this machine's health and security settings, and (with confirmation) "
        "lock the session or raise an SOS. Only identifiers the user stored locally "
        "can be checked — refuse any request to look up another person."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | preflight | enable | disable | add_email | add_username | check_identity | check_device | check_safety | autolock | sos",
            },
            "value": {"type": "STRING", "description": "Identifier to add/check, or the SOS message."},
            "confirm": {"type": "BOOLEAN", "description": "Required for autolock and sos."},
        },
        "required": ["action"],
    },
    "handler": _handle,
}
