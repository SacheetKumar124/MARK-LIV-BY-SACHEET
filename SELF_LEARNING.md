# 🧠 Teaching JARVIS to learn, watch and work later

Four systems, all reachable by voice, all on this machine only. Nothing here
needs a new dependency, and nothing here calls a second model.

---

## 1. Corrections — it remembers being wrong

**Say:** *"No, that's not right — I message Rayan Ali, not Rayan."*
**Tool:** `memory_learn action='correct'`, with `wrong` and `right`.

A correction is stored in its own category and rendered **first and in full** in
the system prompt, ahead of identity and everything else. That ordering is the
whole point: ordinary notes compete for a character budget, a rule that
overrides behaviour must not.

```
STANDING CORRECTIONS — the user told me these after I got them wrong.
  - NOT: my friend is Rayan. INSTEAD: the friend I message is Rayan Ali.
```

*To undo one:* *"forget that correction about Rayan"* → `action='forget_correction'`.
*To read them:* *"what have I told you to stop doing?"* → `action='corrections'`.

---

## 2. Facts it noticed — confirmed before they are kept

At the end of every session, the transcript is mined **locally and
deterministically** (regexes over your own words — no model call) for facts:

| You said | What happens |
|---|---|
| *"remember that my flight is on the 14th"* | stored at once — you asked for storage |
| *"I prefer dark mode everywhere"* | proposed, waits for your yes |
| *"my sister is named Ayesha"* | proposed — a name heard once is worth confirming |
| *"I'm working on the Jarvis HUD"* | proposed as a project |
| *"call me Sach"* | proposed as a name |

**Say:** *"what have you picked up?"* → `memory_learn action='review'` reads each
one back in a line; *"keep the first one"* / *"drop that"* →
`action='approve'` / `action='reject'`.

A rejected fact is remembered as rejected and is **never proposed again**. When
anything is waiting, the system prompt carries a single line saying so — and
nothing at all when the queue is empty, so a quiet memory costs zero tokens.

---

## 3. Watch rules — "when X, do Y"

**Say:**
- *"When a new port opens, tell me which process."*
- *"If the CPU stays above 85%, find out what's using it."*
- *"Warn me when the disk drops below 5%."*
- *"Every day at 8:30, brief me."*
- *"When my last scan is more than a week old, remind me."*

**Tool:** `rules action='add'` with a `trigger` and either a `message` (say
something) or a `task` (do something).

| Trigger | What it measures | Fields |
|---|---|---|
| `port` | a newly listening port (edge-triggered) | `port`, `process` |
| `cpu` / `ram` | load | `above` or `below`, `for_ticks` |
| `disk` | free space | `free_below` |
| `battery` | charge | `below` |
| `time` | the clock | `at` (`HH:MM`), `days` |

Rules are **yours**: versioned in `memory/rules.json`, each with a cooldown
(default 10 minutes) and an hourly cap (default 6). A rule that speaks every
tick is a rule you turn off, so it cannot.

Also: *"what are you watching?"* (`list`), *"pause the disk rule"* (`pause`),
*"would that rule fire right now?"* (`test` — a dry run, no side effect),
*"stop watching ports"* (`remove`).

A fired rule arrives as a `[WATCH]` message and is spoken in one sentence, in
your language. It is **not** subject to the attention engine's "is this worth
interrupting for" budget — you already decided it was.

---

## 4. Queued work — "do this later"

A reminder *tells* you something at a time. A queued job **does** something at a
time and reports back after.

**Say:** *"In twenty minutes, check whether the download stopped and tell me the
speed."* · *"At 15:30, run the tests."* · *"When the CPU drops below 50, kick off
the scan."* · *"Queue it for tomorrow at nine."*

**Tool:** `task_queue action='add'` with a `task` instruction, plus `at`,
`after_seconds` or a `condition`.

- **Durable:** `memory/jobs.json` — a job queued before a restart still runs, and
  a job that came due while the app was closed runs on the next tick, saying how
  late it is.
- **Bounded retries:** two retries, then it is parked as failed with the reason,
  and the failure is spoken. An infinite retry on a task that cannot succeed is
  worse than a visible failure.
- **One at a time:** background work is serialised, so a scan and a chat
  automation never fight over the desktop.
- **Same tools as you:** a queued task runs through `pc_automation`, the same
  registry, the same verification, the same audit trail and `undo`.

*"What's queued?"* (`list`) · *"cancel the download check"* (`cancel`) ·
*"do it now"* (`run_now`).

---

## Where it lives

| File | What it is |
|---|---|
| `core/learned.py` | corrections, proposals, session mining, history search |
| `core/rules.py` | the watch engine — triggers, rate limits, dry run |
| `core/jobs.py` | the durable deferred-work queue |
| `core/task_runner.py` | the seam between "do this later" and the real tools |
| `actions/memory_learn.py` · `actions/rules_tool.py` · `actions/task_queue.py` | the voice-facing tools |
| `memory/corrections` (in `long_term.json`) · `memory/learned.json` · `memory/rules.json` · `memory/jobs.json` | the stores |
| `tools/selftest.py` | 29 checks that prove all of the above still works |

Run the suite any time:

```bash
python3 tools/selftest.py
```

---

## 5. The keystroke guard — typing is a permission, not a decision

On 27 September JARVIS typed "who r u" into a window nobody asked him to touch
and pressed Enter. Nothing raised an exception: `computer_control` types into
whatever has focus, and on Wayland an application is not allowed to ask which
window that is. So the fix is not a plea in the prompt — it is a permit system in
`core/input_guard.py`.

* **Typing, pasting and Enter need a live permit.** A permit is issued only by
  code that has just read a screenshot and confirmed the text field, expires in
  30 seconds, and cannot be created from the conversation. The model can ask to
  type; it cannot authorise itself.
* **`computer_control` refuses blind keystrokes**, and says so: *"I will not
  type into a window I have not verified."* The real path is `pc_automation`,
  which looks first.
* **Terminals are refused outright** — including JARVIS's own console — as are
  the combinations that lose the window (`alt+F4`, `alt+tab`, `super`,
  `ctrl+alt+t`).
* **A message is never a web search.** "text Rayan" cannot route to a browser; a
  messaging request wins over an app-launch or a search, and those skills refuse
  the request rather than opening a website for it.

### The screen watch stopped being a storm

The eight frames in ninety seconds were the `screen_ai` watch loop, armed once
and photographing the screen every 8–25 seconds. Now:

* it does **not** resume after a restart (`watch.resume_after_restart`), so a
  watch armed weeks ago cannot come back on its own;
* arming needs a **spoken confirmation** in both modes, not just `send`;
* the floor is **30 s** between looks, and a rolling **24-per-hour ceiling** stops
  the watch and says so;
* a tick is skipped while the user is **talking** (20 s) or when the user's own
  request already owns the camera — a background loop must never outbid the
  person in the room;
* `core/screen_watch.capture()` itself reuses any frame younger than **1.25 s**,
  so three tools checking the screen in the same second produce one photograph.

---

## The limits, stated plainly

- **Proposals wait for you.** Nothing but an explicit "remember that…" is stored
  without confirmation. That is a deliberate cost: a memory that fills with
  facts you never said is worse than a short one.
- **Senses are the only triggers.** If the machine cannot measure it, it cannot
  watch for it. "Tell me when my friend replies" needs the screen watcher, not a
  rule.
- **Rules run on the brain tick** (default 45 s), so a five-second port is not
  guaranteed to be seen. Time rules fire within a two-minute window.
- **Jobs report, they do not narrate.** You get one sentence when a job finishes,
  not a running commentary.
