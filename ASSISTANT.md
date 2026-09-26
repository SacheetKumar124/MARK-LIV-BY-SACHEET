# JARVIS — proactive assistant subsystem

This document covers the layers added on top of the existing assistant: senses,
judgment, personal protection, cross-device continuity, and trust.

Nothing here starts automatically except the tick loop. Every capability is off
or opt-in until you turn it on, and every autonomous decision is written to an
audit trail you can read.

## What was added

| File | Role |
|---|---|
| `core/attention.py` | Scores every observation (urgency, relevance, novelty, confidence) and spends a limited interruption budget. Decides interrupt / queue / drop. |
| `core/senses.py` | Notices things: new listening ports, disk/battery/RAM/temp, session idle and return, security staleness, stale phone handoffs. |
| `core/brain.py` | The loop: senses → attention → a `[ATTENTION]` message Jarvis phrases himself → digest for everything held back. |
| `core/activity_log.py` | Append-only audit trail (`memory/activity_log.jsonl`) with a `why` on every entry. |
| `core/personal_watch.py` | Your own accounts, machine health, session locking and SOS. Own-identifiers-only, enforced in code. |
| `core/phone_bridge.py` | Handoffs, outbox, inbox and continuity notes between laptop and phone. |
| `actions/assistant_control.py` | Lets Jarvis manage its own attention, digest, audit and handoffs. |
| `actions/personal_watch.py` | Lets Jarvis run your protection checks. |
| `dashboard/assistant_api.py` | Read endpoints + the `/phone` page route. |
| `dashboard/static/jarvis_phone.html` | Mobile page: voice in, voice out, digest, handoffs. |
| `config/assistant_policy.json` | All thresholds, budgets, quiet hours and refusal lists. |
| `config/personal_watch.json` | Your identifiers and safety settings. Fill this in to enable. |

`main.py` gained one asyncio task (`_run_assistant_brain`); `dashboard/server.py`
gained two lines to include the assistant router.

## Things to try saying

- "Is my network safe?" → runs the fast local scan, verifies each port, states verdicts.
- "Did I miss anything?" → drains the digest of things it chose not to interrupt you for.
- "Why did you lock the screen?" → reads the audit trail back to you.
- "Don't interrupt me for 30 minutes" → spends nothing, mutes the budget.
- "Hand this to my phone" → queues the task for the phone page.
- "Am I exposed?" → identity check, but only for identifiers in `config/personal_watch.json`.
- "Lock the machine" / "SOS" → both require confirmation before they happen.

## Phone setup

1. Start Jarvis as usual; the dashboard listens on `0.0.0.0:8000`.
2. On the phone (same Wi-Fi), open `http://<laptop-ip>:8000/phone`.
3. Sign in on `/login` once, paste the token into the phone page; it is stored on the phone only.
4. Voice input uses the browser's speech recognition, so no app install is needed.
   Where that is unavailable, the page falls back to text.

## Deliberate limits

- **Not more interruptions.** The budget is 4 per hour, 24 per day, with a 3-minute
  gap, quiet hours 23:00–07:30, and exact-repeat suppression. Urgent items bypass
  the budget and quiet hours — never the duplicate check.
- **Never automatic:** unlocking, spending, sending messages as you, deleting files,
  installing software, changing credentials, destructive security actions.
- **Confirmation required:** locking, SOS, identity checks, enabling the watch,
  external scans.
- **Camera and screen senses are off**, even if asked for by voice: they log the
  request and point you at the policy file instead.
- **No physical capability.** Jarvis drives this machine's screen, speakers,
  keyboard and network, and nothing else.
- **Own identifiers only.** A lookup for anyone but you is refused in code and
  recorded as a refusal.

## Verify it yourself

```bash
python -c "from core import attention; print(attention._self_test())"        # scoring + budget
python -c "from core import senses;    print(senses._self_test())"           # senses
python -c "from core import brain;     print(brain._self_test())"            # the loop
python -c "from core import personal_watch as w; print(w._self_test())"      # boundaries
python -c "from core import phone_bridge as p; print(p._self_test())"        # handoffs
python -c "from dashboard.assistant_api import build_router; print(build_router(lambda r: True) is not None)"
```

Live check of the whole loop without speaking:

```bash
python -c "from core.brain import Brain; import json; print(json.dumps(Brain().tick(force=True), indent=1))"
```
