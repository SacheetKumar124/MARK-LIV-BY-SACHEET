# ⚙️ MARK LIV (54)
### The Personal AI Assistant That Learns, Watches, and Keeps Its Hands Honest — by Sacheet

![Python](https://img.shields.io/badge/Python-3.11%2B-blue) ![Platform](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux%20(Kali%20Wayland)-success) ![Tests](https://img.shields.io/badge/Self--test%20suite-43%2F43%20passing-brightgreen) ![License](https://img.shields.io/badge/License-CC%20BY--NC%204.0-lightgrey)

A real-time voice AI that can hear, see, understand, and control your computer. Built on the Gemini Live API for native audio streaming — zero subscriptions, total digital autonomy.

---

## ✨ Overview

**MARK LIV is the release where JARVIS stops being a tool you operate and becomes a system you trust.**

The earlier Marks gave the assistant a voice, senses, judgement, and hands. This Mark gives it four things nobody had built yet:

1. **A memory that grows by itself** — it learns facts from your conversations, remembers being *wrong*, and asks you to confirm what it overheard before it keeps it.
2. **Standing orders** — *"when a new port opens, tell me"*, *"when the CPU stays above 85%, find what is using it"*, *"every day at 8:30, brief me"* — watch rules that run forever without a model call.
3. **Deferred work** — *"in twenty minutes, check whether the download stopped"*, *"at 15:30, run the tests"* — real tasks, queued on disk, that survive a restart and report back.
4. **Honest hands** — typing is a *permission*, not a decision. Every keystroke passes a guard that refuses to type into a window nobody verified. A message is never a web search. A screenshot is never taken twice by accident.

And the release became **provably** stable: a committed regression suite (`tools/selftest.py`) runs 43 checks — compile, discovery, wiring invariants, keystroke safety, the WhatsApp flow — in one command, so a fix can never silently un-fix something else.

It's not just an assistant — it's an extension of your digital life.

---

## 🆕 What's New Since Mark LIII

### 🧠 A memory that learns by itself — and remembers being wrong

`core/learned.py` + the `memory_learn` tool. Three habits, all by voice:

| You say | What happens |
|---|---|
| *"No — I message Rayan Ali, not Rayan"* | Stored as a **standing correction**, rendered **first and in full** in the system prompt, ahead of identity and every other note. It changes the next answer, not just this one. |
| *"what have you picked up?"* | Facts mined from your sessions are read back, one line each. **approve** / **reject** by voice — a rejected fact is never proposed again. |
| *"when did I last message Rayan?"* | Answered from the audit trail and past sessions (`recall_history`) instead of a confident guess. |

Session mining is **local and deterministic** — regexes over your own words, no second model call. An explicit *"remember that…"* is stored at once; everything else waits for your confirmation, because a memory that fills with facts you never said is worse than a short one.

### 📡 Watch rules — "when X, do Y"

`core/rules.py` + the `rules` tool. Triggers the machine can actually measure:

| Trigger | Measures | Fields |
|---|---|---|
| `port` | a **newly listening** port (edge-triggered — one that stays open stays silent) | `port`, `process` |
| `cpu` / `ram` | load, in either direction | `above` / `below`, `for_ticks` |
| `disk` | free space | `free_below` |
| `battery` | charge | `below` |
| `time` | the clock | `at` `HH:MM`, `days` |

Actions: **notify** (say something — `{port}`, `{process}`, `{value}`, `{free}`, `{battery}` are filled in) or **task** (do real work through the same registry as a spoken command). Every rule carries a cooldown (default 10 min) and an hourly cap, because a rule that speaks every tick is a rule you switch off. A fired rule arrives as `[WATCH]` and is spoken in one sentence — it is **not** subject to the attention budget, because you already decided it was worth interrupting for. `rules action='test'` dry-runs a rule with no side effect.

### 🗂️ Queued work — "do this later"

`core/jobs.py` + the `task_queue` tool. A reminder *says* something at a time; a queued job **does** something:

- **Durable** — `memory/jobs.json`. A job queued before a restart still runs; one that came due while the app was closed runs on the next tick, *saying how late it is*.
- **Bounded retries** — two retries, then parked as failed with the reason, and the failure is spoken. An infinite retry on a task that cannot succeed is worse than a visible failure.
- **Serialised** — background work runs one at a time, so a scan and a chat automation never fight over the desktop.
- **Same tools as you** — jobs run through `pc_automation`: the same registry, verification, audit trail and `undo` as anything asked for out loud.

*"`when the CPU drops below 50`"* works too — conditions are parsed from your words.

### 🖐️ The keystroke guard — typing is a permission

`core/input_guard.py`. Real sessions caught JARVIS typing into a window nobody asked him to touch — and on Wayland an application is **forbidden** from asking which window has focus, so no amount of checking could have prevented it in the old design. The fix is structural:

- **Typing, pasting and Enter require a live permit** — issued only by code that has just read a screenshot and confirmed the text field, expiring in 30 s, impossible to mint from the conversation.
- **Terminals are refused outright** — including JARVIS's own console.
- **Combinations that lose the window are refused** — `alt+F4`, `alt+tab`, `super`, `ctrl+alt+t`. Ask for the window by name instead of navigating blind.
- **Harmless keys pass** — copy, escape, arrows. The user never feels the guard.
- **A refusal is final and says what to do**: *"I will not type into a window I have not verified. Use pc_automation — it looks first."*

### 👁️ One look per request — the screenshot storm, ended

`core/vision_budget.py`. Three independent tools could look at the screen, and nothing said how many times *one request* may look — so "take a screenshot" became eight captures. Now a shared gate allows **one look per request**, enforced in code across `screen_process`, `screen_ai` and `pc_automation`. Speaking again is a new request, so a genuine *"look again"* costs nothing.

The background screen watch was tamed in the same pass:

| Rule | Effect |
|---|---|
| No auto-resume after restart | A watch armed weeks ago can never come back on its own |
| Arming needs your spoken confirmation | Both modes — a tool call alone cannot start it |
| 30 s floor between looks, 24/hour ceiling | The watch **stops itself** and says so at the cap |
| Skips while you are talking (20 s) | A background loop never outbids the person in the room |
| 1.25 s frame reuse in the capture path | Three tools checking at once produce **one** photograph |

### 💬 WhatsApp: one look, type, Enter — verified

`WHATSAPP.md` holds the full playbook. The short version:

- **Chat already open** → look once (which chat is open *and* is the caret in the box), type, Enter, one look to prove the send. No menus, no chat list, no re-searching.
- **Chat not open** → WhatsApp's own search shortcut (`Ctrl+Alt+/` — not Firefox's `Ctrl+K`, a real bug caught and fixed), open, **verify which conversation actually opened by name** before typing anything.
- **Wrong chat opened** → nothing typed, nothing sent, honest refusal.
- **Messaging intent wins routing** — *"send Rayan a message"* can never fall through to a web search or an app launch. Those skills refuse the request instead of opening a website for it.

### 🔇 It is never silently deaf again

A health watch says the thing out loud — *"I heard you but produced no reply"* — and rotates the session on the second stall, because a Live session that stops producing turns does not restart on its own. Proactive audio is **off by default**: an assistant that occasionally answers the room is a far better failure than one that ignores you with no error and no log line.

### 🩺 Screenshots that cannot lie

On Wayland, the old capture path grabbed the empty XWayland root and fed the model a **solid black frame**. The capture chain now walks portal → GNOME Shell D-Bus → grim, with mss as an X11-only last resort, and **every frame is measured** — a uniform frame (std < 2.0) is rejected instead of trusted. All rungs proven live on this machine: a real 1080p desktop, not 15 KB of black.

### ✅ `tools/selftest.py` — the suite that keeps it honest

```
python3 tools/selftest.py
```

**43 checks**: every file compiles · capability discovery (26 actions + 8 inline tools, 2 plugins) · wiring invariants from real past bugs (the bare `loop` that broke every tool call, the Wayland capture order, background work with no runner) · keystroke safety with the input layer replaced by recorders (blind typing refused, **0 keystrokes sent**) · the WhatsApp flow producing exactly `type → Enter` with the send verified · each module's own self-test. Exit code 0 only when everything passes — usable as a pre-run gate.

---

## 🧑‍🎤 The Face

The centre of the HUD is an animated human head — **real measured facial geometry** (MediaPipe's canonical model), with the skull, neck and rigs generated at startup and drawn entirely in software:

- **no new dependencies** — runs on the PyQt6 and numpy the app already needed;
- **no GPU, no shaders, no driver** — a VM, a remote session and a 2013 laptop render identically;
- **one 25 KB asset**; everything else is a formula.

**Lip-sync you can actually read**: ~50 mouth shapes a second from the audio's formants *and* the transcript — lips close on *m/b/p*, spread on *i/e*, round on *u/o*. One rule set covers Latin, Cyrillic and Greek via Unicode decomposition; scripts that hide pronunciation fall back to the audio-only shape — less detail, never wrong. It breathes, blinks, makes saccades, and looks away while thinking, meets your eyes while listening, sleeps when asleep. ⚙ → **HUD** swaps it for a reactor core driven by the real audio level.

---

## 🚀 Capabilities

### Core
| Feature | Description |
|---|---|
| 🧠 **Learning Memory** | Corrections that outrank everything, facts proposed for your approval, history recalled from sessions + audit trail |
| 📡 **Watch Rules** | Standing "when X, do Y" on ports, CPU, RAM, disk, battery, scan age and the clock |
| 🗂️ **Queued Work** | Durable deferred tasks — at / after / when — that survive restarts and report back |
| 🖐️ **Keystroke Guard** | Permit-based typing; terminals and window-losing combos refused in code |
| 👁️ **One-Look Budget** | One screenshot per request across all three vision tools; the watch capped and confirmed |
| 💬 **Verified Messaging** | WhatsApp Web: one look → type → Enter → proof on screen; wrong chat = nothing sent |
| 🧑‍🎤 Holographic Avatar | Software-rendered human head with real lip-sync — zero GPU, zero new dependencies |
| 🔇 Self-Echo Guard | Never answers its own last sentence — the tail of its own voice is recognised and dropped |
| 🪪 Runtime Self-Knowledge | Name, OS, abilities **and limits** generated from the live system each session |
| 🧩 Plugin System | Drop a single `.py` into `plugins/` — a new skill on next launch, crash-isolated |
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 💓 Affective Dialog | Hears the emotion in your voice and adapts its tone |
| ♾️ Unlimited Sessions | Sliding-window compression + resumption handles — a drop no longer wipes the conversation |
| 🧠 Persistent Memory | No size limit, nothing silently forgotten; the prompt carries a budgeted core, the rest is one lookup away |
| 👁️ Memory Panel | Every stored fact, when it was learned, one-click forget |
| ↩️ Undo | Reverses its own file and setting actions — "undo" in any language |
| ⚠️ Real Confirmation | Shutdown, restart, WiFi wait for a **button you press** — the model cannot confirm irreversible actions |
| 🧠 Judgement | Urgency × relevance × novelty × confidence against an interruption budget with quiet hours; the rest queues into a digest |
| 👀 Senses | Ports, machine health, presence — emits on change, not on a timer |
| 📱 Phone as a Second Node | Voice from your phone mutes the laptop mic; handoffs and an outbox carry work between the two |
| 🛡️ Personal Protection | Identity breach watch, device sweeps, lock + SOS — all opt-in, all code-gated |
| 🌅 Morning Briefing | Greets you, reads the time, recaps yesterday, fetches live news |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` — grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (systemd / LaunchAgent / Task Scheduler) |
| 🎮 Game Updater | Steam and Epic update checks on demand |
| 📋 Clipboard Intelligence | Copy any text → Translate / Summarise / Explain / Fix |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — the avatar retints with it |
| 🎚️ Push-to-Talk | Mic stays closed until you hold **Ctrl+Space** |
| 🩺 Self-Diagnosis | `python3 tools/doctor.py` — distro, session type, per-capability backend, live readings |
| ✅ Regression Suite | `python3 tools/selftest.py` — 43 checks proving the machinery still works |

### Kali & Wayland native
Written against a real Kali GNOME Wayland session, not a developer's X11 machine. Every capability walks a verified backend cascade, so nothing silently does nothing:

| Capability | Backend it actually uses on Kali |
|---|---|
| Screenshots | xdg-desktop-portal → GNOME Shell D-Bus → grim → mss (X11 only) — **every frame measured, never black** |
| Typing / clicking | ydotool + ydotoold (systemd user unit included) — behind the keystroke guard |
| Volume / mute | wpctl (PipeWire) → pactl → pamixer |
| Brightness | logind SetBrightness over polkit — no root, no extra package |
| Clipboard | wl-copy/wl-paste → xclip → pyperclip |
| Notifications | notification-daemon D-Bus (works even when notify-send is broken) |
| Window actions | GNOME keybindings for maximise and snap |

---

## 🔐 Safety Model

The rules that matter are enforced in **code**, not requested in a prompt:

| Boundary | How it is enforced |
|---|---|
| Typing anywhere at all | `core/input_guard.py` — a permit from a verified frame; terminals and forbidden combos refused outright |
| Sending a message | Verified send: the text must appear in the box; the send must appear as a bubble; wrong chat = nothing typed |
| Messaging → navigation | A messaging request can never route to a web search or an app launch — those skills refuse it |
| Taking a screenshot | One look per request; the background watch needs confirmation, pauses while you talk, and stops at its hourly ceiling |
| Auto-replying as you | Only contacts with `auto_reply: true`; never the same message twice; hourly caps; risky drafts held for approval |
| Looking up a person | Only identifiers in `config/personal_watch.json`; anything else refused in code |
| Destructive actions | Confirmation-gated, undoable where possible, every one written to the audit trail |
| Shell access | Argument-list allowlist, no shell interpolation — never blanket `bash -c` |

Every autonomous action is appended to `memory/activity_log.jsonl` with a **why**, so *"why did it do that?"* always has an answer.

---

## ⚡ Quick Start

```bash
git clone https://github.com/SacheetKumar124/MARK-LIII-BY-SACHEET.git
cd MARK-LIII-BY-SACHEET/Mark-LIV
pip install -r requirements.txt --break-system-packages
python main.py
```

Put your Gemini API key in `config/api_keys.json`, then run. First launch walks you through the key, your name, and the assistant name.

> **Kali / Debian / Ubuntu:** system Python is externally managed (PEP 668), so plain `pip install` is refused. Use a virtualenv, or add `--user --break-system-packages` as shown. `setup.py` detects the marker and adds the flags for you.
>
> **Check your machine first:** `python3 tools/doctor.py` tells you which backends resolve and what is missing — before you debug anything.
>
> **After any change:** `python3 tools/selftest.py` — 43 checks, exit code 0 only when everything passes.

---

## 📋 Requirements

| Requirement | Details |
|---|---|
| OS | Linux (verified on **Kali GNOME Wayland**), macOS, Windows |
| Python | 3.11 or newer — developed and verified on **3.14** |
| Microphone | Required for voice interaction |
| API Key | Free Gemini API key in `config/api_keys.json` |
| Optional | `ydotoold` for real keyboard/mouse input on Wayland (unit file included) |

Some OS-specific dependencies are deliberately not bundled to keep the repo light. On a `ModuleNotFoundError`, install the named package.

---

## 🗂️ Project Structure

```
Mark-LIV/
├── main.py                      # Live session, audio I/O, tool dispatch, health watch
├── ui.py                        # PyQt6 HUD — avatar, log panel, plugin manager, camera
├── setup.py                     # First-run configuration wizard
│
├── core/                        # The engine
│   ├── prompt.txt               # Personality, routing, [KEYBOARD SAFETY], [LEARNING], [RULES], [JOBS]
│   ├── learned.py               # Corrections, fact proposals, session mining, history recall
│   ├── rules.py                 # Watch-rule engine — triggers, rate limits, dry runs
│   ├── jobs.py                  # Durable deferred-work queue
│   ├── task_runner.py           # The seam between "do this later" and the real tools
│   ├── input_guard.py           # Keystroke permits, terminal refusal, forbidden combos
│   ├── vision_budget.py         # One look per request — the screenshot-storm gate
│   ├── attention.py             # Judgement — scoring, interruption budget, digest, quiet hours
│   ├── senses.py                # Ports, health, presence, staleness — emits on change
│   ├── brain.py                 # The loop: observe → score → decide → act → speak
│   ├── screen_watch.py          # Capture cascade, frame measurement, change detection, redaction
│   ├── chat_agent.py            # Read threads, draft in your voice, verified sending
│   ├── error_doctor.py          # Read on-screen errors, diagnose, run safe inspections
│   ├── vision_client.py         # Vision with a self-ordering model chain
│   ├── activity_log.py          # Audit trail — every autonomous act with a reason
│   ├── personal_watch.py        # Identity, device and personal-safety watches
│   ├── phone_bridge.py          # Session handoff, outbox, phone inbox
│   ├── audio_stream.py          # Device policy, rate negotiation, crash-proof streams
│   ├── desktop_input.py         # Wayland (ydotool) / X11 input bridge
│   ├── kali_compat.py           # Session-aware backend cascade
│   ├── local_exec.py            # Validated command executor with a tool allowlist
│   └── installer.py             # Dependency installer (PEP 668 aware)
│
├── plugins/                     # Drop-in skills (crash-isolated)
│   └── screen_ai.py             # Eyes and hands — screen understanding, chat watch, error fixing
│
├── actions/                     # Voice-callable actions (26)
│   ├── pc_automation.py         # The hands — verified WhatsApp, apps, files, media, typing
│   ├── memory_learn.py          # Corrections, fact review, history
│   ├── rules_tool.py            # Watch rules by voice
│   ├── task_queue.py            # Deferred work by voice
│   ├── computer_control.py      # Raw keyboard/mouse — behind the keystroke guard
│   ├── screen_processor.py      # Screen + webcam vision into the Live session
│   └── …                        # search, files, messaging, media, reminders, dev agent, and more
│
├── dashboard/                   # Phone control — FastAPI + WebSocket, QR pairing
├── memory/                      # Persistent state (all local)
│   ├── long_term.json           # Identity, preferences, projects, corrections, sessions
│   ├── learned.json             # Fact proposals awaiting your approval
│   ├── rules.json               # Your watch rules
│   ├── jobs.json                # Your queued work
│   └── activity_log.jsonl       # The audit trail
│
├── config/                      # Keys, policy, scope locks, systemd unit
└── tools/
    ├── doctor.py                # Compatibility and capability report
    └── selftest.py              # 43-check regression suite — one command, exit-code gate
```

---

## 📚 Further Reading

| Document | Covers |
|---|---|
| `SELF_LEARNING.md` | The learning memory, watch rules, queued jobs and the keystroke guard — in full |
| `WHATSAPP.md` | The complete messaging playbook — state detection, search, verification, every failure mode |
| `SCREEN_AI.md` | Screen AI — what it reads, how drafting works, every safety gate |
| `ASSISTANT.md` | The judgement, senses, phone and protection layers |

---

## 🗺️ Roadmap

| Area | Next |
|---|---|
| Voice identity | On-device speaker profile — answer only you, ignore the TV and the room |
| Confirm-before-send | Type the draft, speak it back, wait for your yes before Enter |
| Screen memory | Continuous understanding — "what was I working on an hour ago?" |
| Learned tolerance | Derive the interruption budget from how you respond to interruptions |
| Nightly watch | Scheduled sweeps that report only what changed since yesterday |
| Workflow recording | Watch you do a task once, replay it on demand with undo behind every step |

---

## 🙏 Credits

Built by **Sacheet** on the Mark LI–LIII foundation by [FatihMakes](https://github.com/FatihMakes) — the audio pipeline, judgement layer, senses, screen understanding, protection and Kali compatibility are additions to that base, and the LIV intelligence, safety and verification layers are new work on top of it.

| Asset | Source | Licence |
|---|---|---|
| `core/face_model.obj` | [MediaPipe](https://github.com/google-ai-edge/mediapipe) canonical face model | Apache 2.0 |

---

## 🔒 Your Data

Everything stays on your machine. There is no server, no telemetry, no account.

| What | Where | Notes |
|---|---|---|
| Gemini API key | `config/api_keys.json` | **Plaintext** — treat it like a password file |
| What the assistant remembers | `memory/` | Delete the files and it forgets everything |
| Audit trail | `memory/activity_log.jsonl` | Every autonomous action, with its reason |

All are listed in `.gitignore`. **If you have ever committed `config/api_keys.json` publicly, revoke the key** — removing the file in a later commit does not remove it from the history.

Your voice is streamed to Google's Gemini Live API while a session is open; that is the one thing that leaves your computer, and it stops when you mute or close the app.

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
|---|---|
| GitHub | [@SacheetKumar124](https://github.com/SacheetKumar124) |
| Instagram | @jamie |
| Discord | alone_slave |
