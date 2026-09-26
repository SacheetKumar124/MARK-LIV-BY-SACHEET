"""actions/assistant_control.py — Jarvis's own controls for attention, audit and phone.

Everything here is about the assistant managing itself: what it is allowed to
notice, what it has queued, why it did something, and what is waiting on the
phone. Registered as a normal action so the model can call it the same way it
calls a weather lookup — no special path.
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import activity_log, phone_bridge                      # noqa: E402
from core.brain import get_brain                                 # noqa: E402


def _handle(parameters: dict, reconnect=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().casefold()
    brain = get_brain()

    if action == "status":
        status = brain.status()
        attention = status["attention"]
        budget = attention["budget"]
        senses = status["senses"]
        # Only real on/off flags count as senses here: the same dict also holds
        # intervals and a note, which are not senses and must not be listed.
        sense_flags = senses.get("enabled") or {}
        active_senses = [key for key, value in sense_flags.items() if value is True]
        lines = [
            f"Attention: {'on' if attention['enabled'] else 'off'} "
            f"(interrupt floor {attention['interrupt_floor']:.2f}), "
            f"{attention['digest_queued']} queued, {status['last_result']['interrupts']} interrupt(s) last tick.",
            f"Interruption budget: {budget['hour_used']}/{budget['hour_limit']} this hour, "
            f"{budget['day_used']}/{budget['day_limit']} today.",
            attention["quiet_hours"],
            f"Senses on: {', '.join(active_senses) or 'none'}.",
            f"Listening ports baseline: {senses.get('baseline_ports')} "
            f"(now {len(senses.get('listeners') or [])}).",
            f"Idle: {senses.get('idle_seconds')}s. Phone: "
            f"{phone_bridge.state()['pending_handoffs']} pending handoff(s).",
        ]
        return "\n".join(lines)

    if action == "digest":
        return brain.digest_text(limit=int(params.get("limit") or 12))

    if action == "log":
        entries = activity_log.recent(limit=int(params.get("limit") or 15))
        if not entries:
            return "Nothing in the activity log yet."
        return "\n".join(
            f"[{item.get('time')}] {item.get('category')}: {item.get('action')}"
            + (f" — {item.get('detail')}" if item.get("detail") else "")
            for item in entries
        )

    if action == "explain":
        return activity_log.explain(query=params.get("value") or params.get("query"),
                                    limit=int(params.get("limit") or 12))

    if action == "mute":
        return brain.engine.mute(int(params.get("minutes") or 30))

    if action == "unmute":
        return brain.engine.unmute()

    if action == "quiet_hours":
        start, end = params.get("start"), params.get("end")
        if start and end:
            return brain.engine.set_quiet_hours(start=str(start), end=str(end))
        return brain.engine.describe_quiet_hours()

    if action == "senses":
        key = str(params.get("value") or "").strip().casefold()
        if key in {"on", "off", "enable", "disable", "true", "false", "1", "0"}:
            return "Say which sense: telemetry, presence, listeners, security, phone, screen or camera."
        if not key:
            on = sorted(k for k, v in brain.senses.senses_policy.items()
                        if v is True and k in {"telemetry", "presence", "listeners", "security", "phone", "screen", "camera"})
            return ("Enabled: " + (", ".join(on) or "none")
                    + ". Available: telemetry, presence, listeners, security, phone, screen, camera.")
        wants_on = str(params.get("state") or "on").casefold() not in {"off", "false", "0", "disable"}
        if key in {"screen", "camera"} and wants_on:
            activity_log.record("trust", f"sense {key} enabled", why="user request, privacy-sensitive",
                                actor="user")
            return (f"{key} capture would record your surroundings; I have logged the request but "
                    "left it off. Enable it in config/assistant_policy.json when you truly want it.")
        return brain.senses.set_enabled(key, wants_on)

    if action == "handoff":
        title = str(params.get("value") or params.get("title") or "").strip()
        if not title:
            return "Give me the task to hand to your phone."
        item = phone_bridge.handoff(title, str(params.get("detail") or ""), tag="manual")
        info = phone_bridge.pairing_info()
        return (f"Queued for your phone: {item['title']} (id {item['id']}). "
                f"It will appear at {info['phone_page']} when you open it.")

    if action == "phone":
        state = phone_bridge.state()
        pairing = state["pairing"]
        return (
            f"Phone bridge is {'on' if state['enabled'] else 'off'}. "
            f"{state['pending_handoffs']} pending handoff(s), {state['queued_messages']} queued "
            f"message(s), {state['new_inbox']} new note(s).\n"
            f"Open on your phone (same Wi-Fi): {pairing['phone_page']} — sign in once and it remembers you.\n"
            f"This machine is at {pairing['lan_ip']}."
        )

    if action == "addressing":
        # Whether JARVIS decides for itself that a sentence was not aimed at it
        # and stays quiet. This is the one setting that can make a perfectly
        # healthy session look broken — it hears you, and answers nothing — so
        # the user needs a way to change it out loud, mid-conversation, instead
        # of editing JSON and restarting.
        from memory.config_manager import (get_proactive_audio_enabled,
                                           save_proactive_audio_enabled)
        value = str(params.get("value") or params.get("state") or "").strip().casefold()
        if not value:
            on = get_proactive_audio_enabled()
            return (
                f"Proactive audio is {'on' if on else 'off'}. "
                + ("I decide some sentences are not addressed to me and stay quiet — "
                   "which can look like me ignoring you." if on else
                   "I answer everything I hear, including things not aimed at me.")
                + " Say 'only answer when I talk to you' or 'always answer me' to change it."
            )
        wants_on = value in {"on", "true", "yes", "1", "always", "proactive", "quiet", "room"}
        wants_off = value in {"off", "false", "no", "0", "addressed", "me", "everything", "always answer"}
        if not (wants_on or wants_off):
            return ("Say 'on' to stay quiet unless a sentence is aimed at me, "
                    "or 'off' to answer everything I hear.")
        if wants_on == get_proactive_audio_enabled():
            return f"Proactive audio is already {'on' if wants_on else 'off'}."
        save_proactive_audio_enabled(wants_on)
        if callable(reconnect):
            # The setting is read when the session is built, so it only takes
            # effect on a new one. Rebuilding here means "done" is true.
            reconnect(True, "proactive audio changed")
            return ("Done — proactive audio is "
                    + ("on: I will only answer sentences aimed at me." if wants_on
                       else "off: I will answer everything I hear.")
                    + " Reconnecting now to apply it.")
        return (f"Saved: proactive audio {'on' if wants_on else 'off'}. "
                "It applies from the next session.")

    if action == "policy":
        policy = brain.policy
        trust = policy.get("trust", {})
        return (
            f"Interruption floor {policy.get('min_interrupt_score')}, "
            f"budget {policy.get('budget', {}).get('interrupts_per_hour')}/hour.\n"
            "Never automatic: " + ", ".join(trust.get("never_automatic", [])) + ".\n"
            "Needs confirmation: " + ", ".join(trust.get("confirm_required", [])) + "."
        )

    return ("Unknown assistant_control action. Try: status, digest, log, explain, mute, unmute, "
            "quiet_hours, senses, addressing, handoff, phone, policy.")


TOOL = {
    "name": "assistant_control",
    "description": (
        "Manage your own proactivity and audit trail: what you have noticed but not "
        "said (digest), why you did something (log/explain), interruption budget "
        "(mute/quiet_hours), which senses are active, and handoffs to the user's phone. "
        "Use action='digest' when the user asks if they missed anything, and "
        "action='explain' when they ask why you did something. "
        "Use action='addressing' when the user says you are ignoring them, or asks "
        "you to stop answering things not aimed at you: value='off' makes you answer "
        "everything you hear, value='on' makes you answer only what is addressed to you."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | digest | log | explain | mute | unmute | quiet_hours | senses | addressing | handoff | phone | policy",
            },
            "value": {"type": "STRING", "description": "Action-specific value: search text, sense name, or handoff title."},
            "detail": {"type": "STRING", "description": "Extra detail for a handoff."},
            "minutes": {"type": "INTEGER", "description": "Minutes to hold interruptions (mute)."},
            "start": {"type": "STRING", "description": "Quiet-hours start, HH:MM."},
            "end": {"type": "STRING", "description": "Quiet-hours end, HH:MM."},
            "limit": {"type": "INTEGER", "description": "How many items to list."},
        },
        "required": ["action"],
    },
    "handler": _handle,
}
