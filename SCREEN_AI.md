# Screen assistant (`screen_ai`)

Jarvis can now see the screen, read it, answer the messages waiting on it, and
read and fix the errors sitting in a terminal. One plugin, four layers:

| Layer | File | What it does |
|---|---|---|
| Eyes | `core/vision_client.py` | Gemini vision with a verified multi-model fallback chain, JSON repair, rate/budget limits |
| Screen | `core/screen_watch.py` | Capture, change detection, redaction, frame retention |
| Chat | `core/chat_agent.py` | Read conversations, draft in your voice, send with verification |
| Errors | `core/error_doctor.py` | Extract on-screen errors, diagnose, run safe inspections |
| Surface | `plugins/screen_ai.py` | One tool: action routing, the watch loop, spoken answers |

---

## Quick start

```
"Jarvis, what's on my screen?"          → see_screen
"Jarvis, what am I doing right now?"    → what_am_i_doing
"Jarvis, check my messages"             → check_messages
"Jarvis, what is this error?"           → analyse_error
"Jarvis, arm the screen assistant"      → arm(mode="draft")
"Jarvis, reply to them automatically"   → arm(mode="send", confirm=true)
```

Nothing is sent to anybody until you either list them in
`config/screen_ai.json` with `"auto_reply": true` **and** arm in `send` mode, or
explicitly ask for one specific message to be sent.

---

## The three modes

**Look on demand** — `see_screen`, `what_am_i_doing`, `read_text`,
`check_messages`, `analyse_error`. Single look, nothing stored beyond the frame
itself, nothing sent. Works whether or not the watch loop is running.

**Draft** — `arm(mode="draft")`. A background loop looks at the screen on a
timer. When somebody has written to you, it writes a reply and **holds it for
approval**. It tells you what it wants to send and waits.
`approvals` lists what is waiting, `discard_draft 0` drops one.

**Send** — `arm(mode="send", confirm=true)`. The same loop, but it sends.
Replies still have to clear every gate below.

Arming twice is a no-op, and the armed state survives a restart — if you armed it
and then closed the app, it comes back armed, because that is what arming meant.

---

## What is enforced in code, not asked of a prompt

These are not instructions to a model. They are checks that run before anything
leaves the machine, and they are the reason this can be left armed.

| Guard | Where | Behaviour |
|---|---|---|
| Contact allowlist | `chat_agent.can_auto_reply` | Unattended replies only to contacts with `auto_reply: true`. Unknown names are refused and the refusal is logged. |
| One reply per message | `chat_agent.already_answered` | A SHA-1 of app + contact + their message; the same incoming message is never answered twice, including across restarts. |
| Hourly caps + cooldown | `reply` config | Per-contact and global limits, plus a cooldown between replies to the same person. |
| Quiet hours | `reply.quiet_hours` | Nothing is sent between 23:00 and 07:30 (wraps midnight correctly). |
| Risky drafts held | `chat_agent.risk_scan` | A draft that promises something, agrees to a plan, states a time or day, or mentions money/credentials/addresses is queued for you instead of sent. |
| Sensitive screens refused | `screen_watch.privacy_verdict` | Password managers, banking, authenticators and private-browsing windows are never analysed — 23 patterns, matched against the app, the window title and readable text. |
| Redaction before upload | `screen_watch.redact_regions` | Configured rectangles are painted black *on the file that is sent*. |
| Verify before pressing enter | `chat_agent.send_reply` | Types into the box, then re-reads the screen to confirm the text is actually there, and only then sends. If it cannot confirm, it stops and says so. |
| Confirmed send for strangers | `plugins/screen_ai._a_send_reply` | A one-off message needs `confirm=true` and, for people not on the list, still gets refused. |
| No shell, no sudo, no installs | `error_doctor.command_verdict` | Commands are parsed as argument lists, matched against the shared allowlist, and refused if they elevate, delete, install or rewrite. |
| Everything logged | `core/activity_log.py` | Every reply, refusal and command is written with a `why`. Ask `explain` to hear it. |

---

## Actions

| Action | What it does |
|---|---|
| `status` | Armed state, look count, contacts, replies sent, drafts held |
| `see_screen` | Describe the screen; with `instruction` ("what does this say") answer a question about it |
| `what_am_i_doing` | Short activity summary of the focused window |
| `read_text` | Transcribe all readable text on screen |
| `check_messages` | Read the open conversation, say whether it is waiting on you |
| `draft_reply` | Write a reply in your voice; queues it, never sends |
| `send_reply` | Send an exact `message` (needs `confirm`) |
| `approvals` / `discard_draft` | List or drop the drafts waiting for you |
| `arm` / `disarm` / `watch_status` | Control and inspect the background loop |
| `learn_style` | Harvest real (their message → your reply) pairs into `memory/live_chat_examples.json` |
| `set_style` | Adjust the drafting voice ("shorter", "no emoji", "more casual") |
| `set_contact` / `contacts` | Add a person and allow or deny auto-reply |
| `privacy` | Report the guards, toggle sensitive-app blocking |
| `find_on_screen` / `click_text` | Locate text on screen; click it (needs `confirm`) |
| `analyse_error` | Read the error on screen, explain the cause, list a fix plan |
| `fix_error` | Run the safe inspection steps and re-check (needs `confirm`) |
| `run_command` | Run one validated command through the shared lane |
| `explain` | Why did it do that — filtered audit trail |
| `test` | Self-test of all four layers (add `confirm` for a live screen read) |

---

## Where the knobs are

| File | Holds |
|---|---|
| `config/screen_ai.json` | capture, vision models/timeouts/budget, privacy, reply policy, contacts |
| `memory/live_chat_style.json` | the drafting voice |
| `memory/live_chat_examples.json` | your real reply pairs (few-shot guidance) |
| `memory/screen_ai_runtime.json` | armed state and mode |
| `memory/screen_ai_state.json` | replies sent, dedupe map, drafts held, refusals |
| `memory/activity_log.jsonl` | the audit trail |
| `memory/screens/` | recent frames, trimmed to `capture.keep_frames` (12) |

The plugin re-reads these on every look, so edits take effect without a restart.

---

## Measured on this machine

| Thing | Number |
|---|---|
| Screenshot (xdg-desktop-portal, GNOME Wayland) | **0.5 s** |
| Read a conversation (11 messages, contact + senders) | **3.2 s**, confidence 0.95 |
| Draft a reply | **0.7 s** |
| Describe / "what am I doing" | **1.4 – 5.4 s** |
| Locate the compose box + verify text landed | **6.6 s** |
| Identical frame in the watch loop | **0 model calls** (hash match) |

Models actually answering: `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite`.
`gemini-flash-latest` was returning **503 high demand** while those answered, and
`gemini-2.5-flash` / `gemini-2.0-flash` are **404 retired** — which is exactly why
the chain exists and why retired names are remembered instead of retried.

---

## Honest limits

* **One screen at a time.** The portal returns the screen it chooses; on a
  multi-monitor setup you may get the focused one rather than the one you meant.
  Verified behaviour on this box: the same moment returned a chat window in one
  call and a browser in the next.
* **Wayland hides window titles.** `active_window_title()` returns nothing on
  GNOME Wayland by design, so the app name comes from the model reading the
  frame, not from the compositor.
* **A static screen still costs one look** the first time after any change; the
  hash only saves repeated looks at an *unchanged* frame.
* **Errors can only be fixed if the fix is inspection.** Anything needing root,
  an install, or a repository change is printed for you instead of run.
* **The typing path was verified with the input bridge stubbed**, not by typing
  into a live chat — no test message was sent to anybody. The locator found the
  compose box correctly; the enter key is only pressed after the text is
  confirmed present in the box.
* **Needs the Gemini key** already in `config/api_keys.json`; without it the
  plugin says so instead of failing silently.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "I cannot see the screen" | Portal refused (a desktop prompt may be waiting) — run `python3 tools/doctor.py --deep` |
| "I could not read the screen" | All vision models failing — `status` shows the budget and last error |
| "auto-reply is off for X" | Set `"auto_reply": true` for that contact, then re-arm |
| "quiet hours — holding replies" | Edit `reply.quiet_hours` or wait until morning |
| Drafts pile up | You are in `draft` mode: `approvals` to review, `arm(mode="send")` to stop holding |
| Nothing happens when armed | `watch_status` — most often the screen is unchanged, or no conversation is open |
