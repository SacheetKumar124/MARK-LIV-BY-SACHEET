# 💬 WHATSAPP — the complete guide

**For:** J.A.R.V.I.S. on Mark-LIV (Kali GNU/Linux Rolling · GNOME 50.2 · Wayland).
**Purpose:** one document that answers, for every messaging sentence the user can
say, *which chat am I in, where must I go, and what do I press — exactly once.*

**Environment:** WhatsApp Web in **Mozilla Firefox** on GNOME 50.2 / Wayland.
The target is the Web app plus other social/comms web boxes; every rule below
is written so it holds in the browser, not against a hypothetical desktop app.

Everything here is implemented in `actions/pc_automation.py` and routed by
`core/prompt.txt` ([WHATSAPP]). Nothing in this file is aspirational; if you
change behaviour, change the code and this file together.

---

## 1. The one rule above all rules

> **Look once. Type. Enter. Verify once.**

Screenshots are expensive and a second look at the same screen proves nothing —
the screen does not change because JARVIS asked twice. Every vision tool in
Mark-LIV shares one budget, `core/vision_budget.py`: **one look per request.**
A request begins when the user speaks. A second capture inside the same request
is refused with one sentence that says what to do instead:

> *"One look per request — the screen was already captured a moment ago and that
> picture is in your context. Answer from it…"*

The user asking again — "look again", a new name, a new sentence — **is** a new
request, so nothing about the rule blocks a genuine second look. It only stops
the machine from photographing the same screen eight times for one sentence.

---

## 2. Which action to call — the decision tree

The model never does the steps by hand. It calls **one** `pc_automation` action
and the skill does the rest:

| Situation | Call this | What happens on screen |
|---|---|---|
| A chat is open and it IS the person named (or no name given) | `action='reply'`, `message=…` | One look confirms the open chat and the caret; type; Enter; one look confirms the sent bubble. **No search, no click, no other key.** |
| A chat is open but it's somebody else | `action='whatsapp'`, `contact=…`, `message=…` | Notes who is on screen, searches the chat list, opens the named person, aims at the message box, types, Enter, verifies. |
| No conversation open at all | `action='whatsapp'`, `contact=…`, `message=…` | Focuses/launches WhatsApp Web, searches, opens, types, verifies. |
| A phone number instead of a name | `action='whatsapp'`, `task='… +91…'`, `message=…` | Opens the `wa.me` deep link with the text already in the box; verifies and sends — it never types the message a second time. |
| "Where am I?" / "which chat is open?" | `action='context'` | One look, cached for 25 s: focused app, the open conversation's **exact name**, caret state, unread count. |
| "What did they say?" (open chat) | `action='current_chat'` | Reads the open conversation and lists the recent messages with senders. |
| Read anything else on screen | `action='read_screen'` | One look, transcribes/summarises. Budgeted like every other look. |

### The hard rules that come with it

1. **An open chat is never re-searched.** If the user is already talking to
   "Rayan Ali" and says *"tell him I'm coming"*, the message goes into the
   conversation that is on screen. Walking back to the chat list is how a
   message gets delayed by twenty seconds and lands in the wrong thread.
2. **The chat that is open when a name is NOT given IS the target.** The user is
   mid-conversation and simply wants words in it.
3. **A named person always wins over convenience.** If the open chat is someone
   else, JARVIS says so and goes to the right chat — it never "replies here"
   just because the caret was handy.
4. **Never type into an unverified field.** The caret is either seen in the
   message box (from the same look that found the chat) or aimed at and verified
   before a single character is sent. No verification → JARVIS refuses and says
   why. A blind keystroke is how a message ends up in a search bar or an editor.
5. **Enter is only pressed after the message is provably in the compose box**
   (or when the caret was verified in this same request — the fast path — with a
   *recovery* check that a second Enter happens only if a frame shows the text
   still sitting unsent).
6. **A send is only claimed when a frame confirms the sent bubble.** Otherwise
   JARVIS says plainly that the send is UNVERIFIED. It never describes a message
   as sent when it was not.
7. **`screen_ai` cannot type and cannot send.** If it refuses, that refusal is
   the answer — the route is `pc_automation`, never a different tool.
8. **Search with WhatsApp's own shortcut: `Ctrl+Alt+/`.** Never `Ctrl+K` — that
   is *Firefox's* "focus the search bar" shortcut, and on a machine where the
   web app does not swallow it the contact name is typed into the browser's
   search box and Enter fires a web search instead of opening a chat. The
   search box is the field at the top of the **left sidebar**, and the verify
   question says so explicitly; a geometric click on it is the fallback.
9. **State before navigation.** Establish, from one frame, whether WhatsApp Web
   is open and focused and *which chat is active* — read the header at the top
   of the message pane, or the highlighted row in the left sidebar when no
   conversation is open. Only then decide whether to type here or go searching.
10. **An overlay is dismissed once.** A cookie banner, notification card or
   modal hides both the conversation and the caret. Escape closes the transient
   ones, the frame is taken again, and if it is still there JARVIS proceeds with
   what it can see rather than fighting the screen. One dismissal, never a loop.

---

## 3. Worked examples

**"Jarvis, tell Rayan Ali I'll send the videos tonight."**
1. One look: is Rayan's chat open? — No: it is *Siksha Guru*.
2. Context remembers: open chat = Siksha Guru, asked for Rayan Ali.
3. Focus WhatsApp, search "Rayan Ali" with `Ctrl+Alt+/`, open the chat.
4. The look that confirms the chat opened also reports the compose caret.
5. Type the words → Enter → one look confirms the sent bubble.
6. JARVIS: *"Message sent to Rayan Ali — VERIFIED on screen. (You had Siksha
   Guru's chat open; I moved to Rayan's.)"*

**"Reply here: on my way."** (Rayan's chat is open)
1. One look: chat = Rayan Ali, caret already in the message box.
2. Type → Enter → verify. Nothing else is touched.
3. JARVIS: *"Typed and sent in the open chat with Rayan Ali — VERIFIED."*

**"Text Ehsan good luck on his test."** (WhatsApp is in the background)
1. One look at the desktop → WhatsApp isn't focused.
2. Focus the WhatsApp tab; look again — maybe Ehsan's chat was open behind it.
3. Still not Ehsan → search, open, aim, type, Enter, verify.

**"Message +91 98765 43210 that I'm late."**
1. The number is recognised; the `wa.me` deep link opens the exact chat with the
   text already in the compose box.
2. Verify the prefill → Enter → verify the sent bubble. The text is never typed
   twice, so the recipient can't get it doubled.

---

## 4. What JARVIS says when it goes wrong

| Failure | Spoken answer (the honest one) |
|---|---|
| A different chat is open and no name was given | "The open conversation is with X in Y, not WhatsApp — tell me who to message." |
| The search found nobody | "Could not open the chat with NAME: no chat matching 'NAME' appeared." |
| Caret could not be verified | "The chat is open, but I could not verify the message box. I won't type blind — say retry." |
| Send not confirmed | "Enter was pressed but the sent bubble was not confirmed." (never "sent") |
| An overlay was covering the view | Escape once, re-read the frame, then proceed — or say what can be seen if it stayed |
| WhatsApp on the QR screen | "WhatsApp Web is showing a login/QR screen — scan it once, then ask again." |
| Keyboard backend missing | "I cannot drive the keyboard yet — start `ydotoold` (systemctl --user enable --now ydotoold)." |
| Number without a country code | "That number has no country code — give it in international form (+91…)." |

---

## 5. State JARVIS keeps about the desktop

`memory/pc_context.json` — written after every verified step, read before every
ambiguous one:

```json
{
  "app": "Google Chrome",
  "chat_app": "whatsapp",
  "chat": "Rayan Ali",
  "compose_ready": true,
  "unread": 3,
  "note": "replied in the open chat with Rayan Ali",
  "seen_at": 1790450000.0
}
```

Trustworthy for 25 seconds (`_CONTEXT_FRESH`). After that, the next decision
takes a fresh look. `action='context'` serves it without a second screenshot.

Every action is also recorded in `memory/activity_log.jsonl`
(`category: pc_automation`) — that's the "why did you do that?" trail.

---

## 6. Timings that make it reliable on GNOME Wayland

| Moment | Wait | Why |
|---|---|---|
| After launching a browser/tab | 6.0 s | paint + JIT |
| After opening a chat from search | 3.0 s | route animation |
| After a shortcut | 0.8 s | focus ring / popup |
| After typing the name in search | 1.7 s | contact-list filter (React) |
| Before grabbing a verification frame | 0.4 s | compositor breath |
| After Enter, before checking the sent bubble | 1.2 s | message render |

These live at the top of `actions/pc_automation.py`. If a step is flaky on a
slower machine, raise the settle, never remove the verification.

---

## 7. Screen captures: where they go, and the one-look budget

* Captures go through `core/screen_watch.py` (portal → GNOME Shell → `grim`).
  `mss` is an X11-only last resort; on Wayland it returns a blank frame, which
  is why every frame is measured before use (`_looks_blank`).
* Frames are written to `memory/screens/` and pruned to the newest 12
  (`capture.keep_frames` in `config/screen_ai.json`).
* **The budget:** `core/vision_budget.py`. `note_user_speech()` opens it,
  `claim(tool)` takes the single look, and any later claim inside the same
  request gets `REFUSAL` instead of a capture. Armed/unattended flows with no
  speech fall back to one look per 45 s.
* A WhatsApp message normally costs **two** captures: the look that finds the
  chat (which also answers the caret question) and the look that confirms the
  send. It used to cost six to eight.

---

## 8. Self-checks

```bash
cd ~/simple-jarvis/Mark-LIV

python3 core/vision_budget.py        # the one-look gate: 10 assertions
python3 actions/pc_automation.py     # routes, name matching, screenshot path
python3 tools/doctor.py              # input / screenshot / clipboard / audio backends
```

A healthy WhatsApp send reads like this in the terminal:

```
[JARVIS] 📞 pc_automation
[JARVIS] 🔧 pc_automation  {'action': 'reply', 'message': 'on my way'}
[Vision] 📤 ... → main session (once)
[JARVIS] 📤 pc_automation → Typed and sent in the open chat with Rayan Ali — VERIFIED on screen
```

If you see more than one `[Vision] 🖥️ Screen:` line for a single sentence, the
budget is not in play — restart into the current build and check that
`core/vision_budget.py` is importable (`python3 -c "from core import vision_budget"`).
