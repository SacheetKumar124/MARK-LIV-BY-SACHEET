"""JARVIS — screen intelligence plugin.

One tool, and through it: looking at the screen, reading it, answering the
messages waiting on it, fixing the errors on it, and clicking things it can find.

Layers underneath
-----------------
    core/vision_client.py   resilient Gemini vision (verified multi-model chain)
    core/screen_watch.py    capture, change detection, redaction, retention
    core/chat_agent.py      thread reading, style-aware drafting, verified sending
    core/error_doctor.py    error extraction, diagnosis, safe fixes

This file is the surface: argument validation, the background watch loop, and the
spoken answers. All the risky decisions live in the layers above, in code.

Operating modes
---------------
* **Look on demand** — ``see_screen``, ``check_messages``, ``what_am_i_doing``.
  Nothing is stored, nothing is sent, and it works whether or not the watch loop
  is running.
* **Draft** — ``arm(mode="draft")`` watches the screen and writes replies, but
  holds every one of them for approval. This is the honest default.
* **Send** — ``arm(mode="send", confirm=true)`` also sends them. It only ever
  sends to contacts listed in ``config/screen_ai.json`` with ``auto_reply: true``,
  inside the hourly caps and quiet hours, never twice for the same message, and
  never a draft that mentions money, credentials, plans or times.

Two things this plugin will not do, by design: send a message to somebody who is
not on your allowlist, and type into a conversation it could not verify it was
looking at.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core import activity_log, chat_agent, error_doctor, screen_watch, vision_client
from core import vision_budget

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "screen_ai.json"
RUNTIME_PATH = BASE_DIR / "memory" / "screen_ai_runtime.json"

PLUGIN = {
    "name": "screen_ai",
    "description": (
        "Gives JARVIS eyes on the desktop and hands on the keyboard: reads whatever is on "
        "screen, says what the user is doing, reads and answers messages waiting in "
        "WhatsApp / Telegram / Signal / Discord / Instagram, finds text on screen and clicks "
        "or types into it, transcribes the screen, and reads, explains and fixes errors that "
        "are visible (traceback, compiler, npm, pip, apt, service failures) by running "
        "inspection commands. Actions: status, see_screen, what_am_i_doing, read_text, "
        "check_messages, draft_reply, send_reply, approvals, arm, disarm, watch_status, "
        "learn_style, set_style, set_contact, contacts, privacy, find_on_screen, click_text, "
        "analyse_error, fix_error, run_command, explain, test. "
        "Use arm(mode='draft') to have it watch the screen and prepare replies, or "
        "arm(mode='send', confirm=true) to let it reply unattended to allowlisted contacts. "
        "Use see_screen / what_am_i_doing for 'what's on my screen', 'what am I doing', "
        "'what does this say'. Take ONE look per request: a second look in the same "
        "request is refused, because the picture you already hold is the current "
        "screen. Use analyse_error / fix_error for 'what is this error', "
        "'fix that error'. Do NOT use this for taking a picture with the webcam, or "
        "for sending a one-off message the user dictated. "
        "\n\n"
        "THIS PLUGIN CANNOT TYPE AND CANNOT SEND. There is no typing action here — "
        "'type_text' is not an action and will be refused. Anything that puts words "
        "on the screen for the user, including replying in a chat that is already "
        "open, is pc_automation's job: call pc_automation with action='reply' and "
        "message=<the exact words>. It looks once, types, and presses Enter. Never "
        "substitute a click, a menu or a search for that; if this plugin refuses to "
        "send, that is the signal to use pc_automation, not to try another route. "
        "send_reply below is for UNATTENDED auto-replies to allowlisted contacts only."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | see_screen | what_am_i_doing | read_text | check_messages | "
                    "draft_reply | send_reply | approvals | discard_draft | arm | disarm | "
                    "watch_status | learn_style | set_style | set_contact | contacts | "
                    "privacy | find_on_screen | click_text | analyse_error | fix_error | "
                    "run_command | explain | test"
                ),
            },
            "instruction": {
                "type": "STRING",
                "description": (
                    "Free text: a question about the screen ('what is this error', 'summarise "
                    "this page'), extra guidance for a draft, or a search term for "
                    "find_on_screen."
                ),
            },
            "message": {
                "type": "STRING",
                "description": "Exact reply text for send_reply.",
            },
            "contact": {
                "type": "STRING",
                "description": "Contact name for set_contact / a draft aimed at one person.",
            },
            "mode": {
                "type": "STRING",
                "description": "For arm: 'draft' (write replies, hold them for approval) or 'send' (also send them).",
            },
            "target": {
                "type": "STRING",
                "description": "Text to locate on screen for find_on_screen / click_text.",
            },
            "command": {
                "type": "STRING",
                "description": "A single command for run_command (no shell operators, no sudo, no installs).",
            },
            "interval_seconds": {
                "type": "NUMBER",
                "description": "For arm: how often to look at the screen. Minimum 8.",
            },
            "confirm": {
                "type": "BOOLEAN",
                "description": (
                    "Required for anything that acts on the machine or sends a message: "
                    "arm with mode='send', send_reply, click_text, run_command, fix_error. "
                    "Only pass true when the user asked for it in this conversation."
                ),
            },
        },
        "required": ["action"],
    },
}


# ── small helpers ────────────────────────────────────────────────────────────

def _log(player: Any, text: str) -> None:
    if player is None:
        return
    try:
        player.write_log(f"JARVIS: {text}")
    except Exception:
        pass


def _say(player: Any, text: str) -> None:
    """Ask the live session to speak now, while run() is still executing."""
    if player is None:
        return
    for attribute in ("request_say", "plugin_say"):
        method = getattr(player, attribute, None)
        if callable(method):
            try:
                method(text)
                return
            except Exception:
                continue


def _observe(title: str, detail: str = "", urgency: float = 0.4,
             relevance: float = 0.7, source: str = "screen") -> Optional[str]:
    """Feed a finding through the attention engine so it obeys the user's budget.

    Returns the message to speak when the engine decides this is worth an
    interruption, or ``None`` when it should stay quiet. Using the shared engine
    means screen events compete fairly with every other sense for the user's
    attention instead of shouting over them.
    """
    try:
        from core.attention import AttentionEngine, Observation  # noqa: PLC0415
        decision = AttentionEngine().observe(Observation(
            source=source, title=title, detail=detail[:400],
            urgency=urgency, relevance=relevance, confidence=0.9,
            kind="info", tags=("screen", "chat"),
        ))
        if decision.action == "interrupt":
            return f"[ATTENTION] {title}. {detail[:200]}"
        return None
    except Exception:
        # If the engine is unavailable, still report: silence would be worse.
        return f"[ATTENTION] {title}. {detail[:200]}"


def _load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(config: dict) -> str:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        return ""
    except OSError as exc:
        return f"could not write the config: {exc}"


def _runtime() -> dict:
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_runtime(state: dict) -> None:
    try:
        RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RUNTIME_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RUNTIME_PATH)
    except OSError:
        pass


def _watch_config() -> dict:
    value = _load_config().get("watch")
    return value if isinstance(value, dict) else {}


def _seconds_since_user_spoke() -> float | None:
    """How long since the user last said anything, or None when unknown.

    The live session already publishes this — it calls
    ``vision_budget.note_user_speech()`` on every user transcription — so the
    watch loop can be polite about it without reaching into main.py.
    """
    try:
        return vision_budget.status().get("seconds_since_speech")
    except Exception:                                          # noqa: BLE001
        return None


def _clamp_interval(value: Any) -> float:
    cfg = _watch_config()
    minimum = float(cfg.get("min_interval_seconds", 8) or 8)
    try:
        requested = float(value)
    except (TypeError, ValueError):
        requested = float(cfg.get("interval_seconds", 25) or 25)
    return max(minimum, min(600.0, requested))


# ── the watch loop ───────────────────────────────────────────────────────────

class _Watcher:
    """Background loop: look, read, decide, reply — on a timer.

    One instance per process. ``start`` is idempotent so arming twice cannot
    produce two loops competing over the keyboard, which would be a genuinely
    dangerous failure mode.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._player: Any = None
        self.mode = "draft"
        self.interval = 25.0
        self.ticks = 0
        self.skips = 0
        self.replies_sent = 0
        self.drafts_held = 0
        self.last_tick: float = 0.0
        self.last_summary: str = ""
        self.last_error: str = ""
        self.last_checksum: str = ""
        self._started_at: float = 0.0
        # The ceiling: at most this many frames in any rolling hour, whatever the
        # interval says. A background loop photographing the screen is a feature;
        # one that can do it forever without a limit is a bug.
        self.captures_this_hour: list[float] = []
        self._capture_cap: int = 0

    def _capture_ceiling_reached(self, cfg: dict) -> bool:
        """Trim the rolling-hour window and answer whether the cap is spent."""
        cap = int(cfg.get("max_captures_per_hour", 24) or 24)
        self._capture_cap = cap
        cutoff = time.time() - 3600
        self.captures_this_hour = [t for t in self.captures_this_hour if t >= cutoff]
        return len(self.captures_this_hour) >= cap

    # -- lifecycle --

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self, mode: str, interval: float, player: Any = None) -> str:
        with self._lock:
            self.mode = "send" if str(mode).casefold() == "send" else "draft"
            self.interval = _clamp_interval(interval)
            self._player = player
            if self.running:
                return (f"Already watching the screen every {self.interval:.0f}s "
                        f"in {self.mode} mode.")
            self._stop.clear()
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._loop, name="jarvis-screen-watch", daemon=True,
            )
            self._thread.start()
        state = _runtime()
        state.update({"armed": True, "mode": self.mode,
                      "interval": self.interval, "armed_at": self._started_at})
        _save_runtime(state)
        return (f"Watching the screen every {self.interval:.0f} seconds in "
                f"{self.mode} mode.")

    def stop(self) -> str:
        with self._lock:
            was_running = self.running
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
        state = _runtime()
        state.update({"armed": False, "disarmed_at": time.time()})
        _save_runtime(state)
        return "Stopped watching the screen." if was_running else "The screen watch was not running."

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                        # noqa: BLE001
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                activity_log.record("screen", "watch tick failed", detail=self.last_error,
                                    why="background screen watch", outcome="error")
            if self._stop.wait(self.interval):
                break

    # -- one cycle --

    def tick(self) -> dict:
        started = time.monotonic()
        result: dict[str, Any] = {"tick": self.ticks + 1, "skips": [], "actions": [],
                                  "notices": []}

        # ── Before any capture: is this even the watch's turn? ───────────────
        # The screenshots this loop takes are the ones the user noticed as a
        # storm: eight frames in ninety seconds while they were talking to
        # JARVIS. Three cheap questions fix that, and all three are asked
        # BEFORE the screen is photographed, because a skipped frame costs
        # nothing while a taken one costs a file and often a model call.
        cfg_watch = _watch_config()
        if self._capture_ceiling_reached(cfg_watch):
            self._stop.set()
            notice = ("Screen watch stopped itself: it reached its hourly ceiling of "
                      f"{self._capture_cap} looks. Ask me to re-arm it when you want "
                      "it watching again.")
            self.last_summary = notice
            activity_log.record("screen", "watch stopped at its hourly ceiling",
                                detail=notice, why="a background loop must not be able "
                                "to photograph the screen without limit")
            result["skips"].append("hourly capture ceiling reached — watch stopped")
            return result
        speech_gap = _seconds_since_user_spoke()
        if speech_gap is not None and speech_gap < float(
                cfg_watch.get("pause_while_user_talking_seconds", 20) or 20):
            result["skips"].append(f"user is talking ({speech_gap:.0f}s ago)")
            self.skips += 1
            self.ticks += 1
            self.last_tick = time.time()
            return result
        if not vision_budget.peek():
            # The user's own request already owns the camera this minute; that
            # look is theirs, and a second frame would be one of the duplicates
            # the one-look rule exists to prevent.
            result["skips"].append("a look was already taken for the user's request")
            self.skips += 1
            self.ticks += 1
            self.last_tick = time.time()
            return result
        # Also honour the same ceiling the plugin-level limit uses, measured on
        # captures rather than ticks: a tick that skips still costs a screenshot.
        self.captures_this_hour.append(time.time())

        frame, reason = screen_watch.capture()
        if frame is None:
            self.last_error = reason
            result["error"] = reason
            return result

        # Cheap early-out: the same frame means the same conversation, and a
        # model call would tell us nothing new. This is what makes a 25-second
        # watch affordable to leave running.
        if self.last_checksum and frame.checksum == self.last_checksum:
            self.skips += 1
            self.ticks += 1
            self.last_tick = time.time()
            result["skips"].append("screen unchanged")
            return result
        self.last_checksum = frame.checksum

        cfg = _watch_config()
        if cfg.get("respond_while_away_only"):
            try:
                from core import senses  # noqa: PLC0415
                idle = senses.idle_seconds()
                threshold = float(cfg.get("idle_seconds_for_away", 120) or 120)
                if idle is not None and idle < threshold:
                    result["skips"].append(f"user is active (idle {idle:.0f}s)")
                    self.ticks += 1
                    self.last_tick = time.time()
                    return result
            except Exception:
                pass

        thread = chat_agent.read_thread(frame)
        result["thread"] = thread.as_dict()
        self.ticks += 1
        self.last_tick = time.time()

        if thread.error:
            self.last_error = thread.error
            result["error"] = thread.error
            return result
        if thread.app in ("", "other", "none") or not thread.contact:
            if cfg.get("only_when_messaging_app", True):
                result["skips"].append("no conversation open")
                return result

        pending, why = chat_agent.pending_reply(
            thread, float(_load_config().get("reply", {}).get("min_confidence", 0.55) or 0.55))
        if not pending:
            result["actions"].append(f"nothing to answer ({why})")
            return result

        if chat_agent.already_answered(thread):
            result["actions"].append("this message was already answered")
            return result

        allowed, policy_reason = chat_agent.can_auto_reply(thread.contact)
        if not allowed:
            chat_agent.record_refusal(thread.contact, thread, policy_reason)
            result["actions"].append(f"held: {policy_reason}")
            return result

        draft = chat_agent.draft_reply(thread)
        if not draft.ok:
            chat_agent.record_refusal(thread.contact, thread, f"draft failed: {draft.error}")
            result["actions"].append(f"could not draft a reply: {draft.error}")
            return result

        reply_cfg = _load_config().get("reply", {})
        if draft.risky and reply_cfg.get("block_risky_drafts", True):
            chat_agent.queue_for_approval(
                thread.contact, thread, draft,
                "draft touches " + ", ".join(draft.risky))
            self.drafts_held += 1
            notice = f"{thread.contact} wrote; my draft mentions {', '.join(draft.risky[:2])}"
            spoken = _observe(notice, f"Draft waiting for approval: {draft.text}",
                              urgency=0.7, relevance=0.9)
            result["actions"].append("queued (risky draft)")
            if spoken:
                result["notices"].append(spoken)
            self._tell(spoken)
            return result

        if self.mode != "send":
            chat_agent.queue_for_approval(thread.contact, thread, draft, "draft mode")
            self.drafts_held += 1
            notice = f"Draft ready for {thread.contact}"
            spoken = _observe(notice, f"Waiting for your go-ahead: {draft.text}",
                              urgency=0.5, relevance=0.85)
            self._tell(spoken)
            result["actions"].append("queued (draft mode)")
            result["draft"] = draft.text
            return result

        send = chat_agent.send_reply(draft.text, thread, verify_thread=True)
        chat_agent.record_reply(
            thread.contact, thread, draft.text,
            outcome="sent" if send.ok else "failed", note=send.detail,
        )
        result["actions"].append(f"send: {send.detail}")
        result["sent"] = send.ok
        activity_log.record(
            "action", f"auto-replied to {thread.contact}",
            detail=f"them: {(thread.last.text if thread.last else '')[:120]} | "
                   f"me: {draft.text[:160]} | {send.detail[:120]}",
            why=f"unattended reply, {self.mode} mode, triggered by watch loop",
            outcome="ok" if send.ok else "failed",
            meta={"app": thread.app, "verified": send.verified, "risky": draft.risky},
        )
        if send.ok:
            self.replies_sent += 1
            spoken = _observe(
                f"Answered {thread.contact}",
                f"They said “{(thread.last.text if thread.last else '')[:80]}”; I replied “{draft.text[:100]}”.",
                urgency=0.35, relevance=0.8)
            self._tell(spoken)
        else:
            self._tell(f"[ATTENTION] I could not reply to {thread.contact}: {send.detail}")

        # Housekeeping on a slow cadence so it never dominates a tick.
        if self.ticks % 20 == 0:
            screen_watch.cleanup()
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        return result

    def _tell(self, spoken: Optional[str]) -> None:
        if spoken:
            _say(self._player, spoken)
            _log(self._player, spoken.replace("[ATTENTION] ", ""))

    # -- reporting --

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "mode": self.mode,
                "interval_seconds": self.interval,
                "ticks": self.ticks,
                "skipped": self.skips,
                "replies_sent": self.replies_sent,
                "drafts_held": self.drafts_held,
                "last_tick_age": (round(time.time() - self.last_tick, 1) if self.last_tick else None),
                "uptime_seconds": (round(time.time() - self._started_at, 1) if self._started_at else 0),
                "last_summary": self.last_summary,
                "last_error": self.last_error,
            }


_WATCHER = _Watcher()


def _restore_arm_state() -> None:
    """Resume a watch after a restart — now only when asked for, by config.

    This used to resume unconditionally, on the reasoning that a user who armed
    a watch expects it to survive a restart. In practice it meant a watch armed
    once, weeks earlier, came back every launch and photographed the screen in
    the background — which is the storm of 27 September, and there is no way to
    tell "still wanted" from "forgotten" from a JSON flag.

    So it is opt-in now: set ``watch.resume_after_restart`` to true in
    config/screen_ai.json to get the old behaviour. Arming in the running app
    (with a spoken confirmation) works exactly as before.
    """
    if not _watch_config().get("resume_after_restart"):
        state = _runtime()
        if state.get("armed"):
            state.update({"armed": False, "disarmed_at": time.time(),
                          "why": "not resumed after restart (resume_after_restart is off)"})
            _save_runtime(state)
            activity_log.record("screen", "screen watch not resumed after restart",
                                why="a background watch must be armed deliberately, "
                                "not inherited from a previous run")
        return
    state = _runtime()
    if not state.get("armed"):
        return
    mode = str(state.get("mode") or "draft")
    interval = state.get("interval") or 25
    try:
        _WATCHER.start(mode, float(interval))
        activity_log.record("screen", f"resumed {mode} screen watch after restart",
                            why="armed state persisted in memory/screen_ai_runtime.json")
    except Exception:
        pass


# ── actions ──────────────────────────────────────────────────────────────────

def _a_status(params: dict, player: Any) -> str:
    vision = vision_client.stats()
    watch = _WATCHER.status()
    frames = screen_watch.recent_frames(limit=1)
    state = chat_agent.state_snapshot()
    lines = [
        f"Screen assistant: {'armed' if watch['running'] else 'idle'}"
        + (f" ({watch['mode']} mode, every {watch['interval_seconds']:.0f}s)" if watch["running"] else ""),
        f"Eyes: {vision['calls_last_hour']} looks in the last hour, "
        f"{'key present' if vision['key_present'] else 'NO API KEY'}",
        f"Contacts with auto-reply: "
        f"{len([c for c in (_load_config().get('contacts') or []) if isinstance(c, dict) and c.get('auto_reply')])}"
        f" of {state['contacts_configured']}",
        f"Replies sent: {state['replies_total']} total, {state['replies_last_hour']} this hour"
        f"; drafts held: {state['queued_for_approval']}; declined: {state['refusals']}",
        f"Style examples learned: {state['style_examples']}",
    ]
    if watch["last_error"]:
        lines.append(f"Last error: {watch['last_error'][:160]}")
    if frames:
        lines.append(f"Newest frame: {frames[0]['age_seconds']}s old")
    spoken = "; ".join(lines[:3]) + "."
    _log(player, "\n".join(lines))
    return spoken


def _a_see_screen(params: dict, player: Any) -> str:
    allowed, why = vision_budget.claim("screen_ai.see_screen")
    if not allowed:
        return why
    question = str(params.get("instruction") or "").strip()
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"

    if question:
        prompt = (
            "You are the eyes of a desktop assistant. Answer the question about this "
            "screenshot.\n\nReply with JSON only:\n"
            '{"answer": "a direct 1-3 sentence answer", "app": "app in focus", '
            '"detail": "anything else worth knowing", "confidence": 0.0}\n\n'
            f"Question: {question}"
        )
        data, result = vision_client.analyze_json(frame.path, prompt, max_output_tokens=500)
        if data is None:
            return f"I looked but could not read the screen: {result.error}"
        allowed, why = screen_watch.privacy_verdict(
            app=str(data.get("app") or ""), title="", visible_text="")
        if not allowed:
            return f"I stopped myself: {why}"
        answer = str(data.get("answer") or "").strip() or "I could not tell from this screen."
        detail = str(data.get("detail") or "").strip()
        activity_log.record("observation", "looked at the screen",
                           detail=f"q={question[:120]} a={answer[:160]}",
                           why="user asked a question about the screen")
        return f"{answer}{(' ' + detail) if detail else ''}"

    analysis, result, path = screen_watch.describe(frame)
    if not analysis:
        return f"I looked but could not read the screen: {result.error}"
    summary = str(analysis.get("summary") or "").strip()
    activity = str(analysis.get("activity") or "").strip()
    notable = analysis.get("notable") or []
    spoken = f"{summary}"
    if activity:
        spoken += f" ({activity})"
    if notable:
        spoken += f" Worth noting: {'; '.join(str(n) for n in notable[:2])}"
    activity_log.record("observation", "described the screen",
                        detail=f"{analysis.get('app')}: {summary[:200]}",
                        why="user asked what is on screen")
    _log(player, f"{analysis.get('app')} — {summary}")
    return spoken or "I can see the screen but nothing notable is on it."


def _a_what_am_i_doing(params: dict, player: Any) -> str:
    allowed, why = vision_budget.claim("screen_ai.what_am_i_doing")
    if not allowed:
        return why
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    analysis, result, _ = screen_watch.describe(frame)
    if not analysis:
        return f"I could not read the screen: {result.error}"
    activity = str(analysis.get("activity") or "").strip()
    summary = str(analysis.get("summary") or "").strip()
    app = str(analysis.get("app") or "something")
    notable = analysis.get("notable") or []
    spoken = f"You are in {app}: {activity or summary}"
    if notable:
        spoken += f". I can also see {'; '.join(str(n) for n in notable[:2])}"
    activity_log.record("observation", "reported current activity",
                        detail=f"{app}: {activity or summary}",
                        why="user asked what they are doing")
    _log(player, spoken)
    return spoken


def _a_read_text(params: dict, player: Any) -> str:
    allowed, why = vision_budget.claim("screen_ai.read_text")
    if not allowed:
        return why
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    lines, result = screen_watch.read_text(frame)
    if not lines:
        return f"I could not read any text: {result.error or 'nothing legible'}"
    joined = "\n".join(lines)
    activity_log.record("observation", f"transcribed the screen ({len(lines)} lines)",
                        detail=joined[:300], why="user asked me to read the screen")
    _log(player, joined[:2000])
    head = " ".join(lines[:3])[:300]
    return f"I read {len(lines)} lines. It starts: {head}"


def _a_check_messages(params: dict, player: Any) -> str:
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    thread = chat_agent.read_thread(frame)
    if thread.error:
        return f"I could not read the conversation: {thread.error}"
    if not thread.contact:
        return "No conversation is open on screen right now."

    pending, why = chat_agent.pending_reply(thread)
    _log(player, f"{thread.app_label} / {thread.contact}: {len(thread.messages)} messages read")
    activity_log.record("observation", f"read a {thread.app_label} thread with {thread.contact}",
                        detail=thread.transcript[:300], why="user asked me to check messages")
    if not pending:
        return f"{thread.contact} on {thread.app_label}: {why}."
    last = thread.last.text if thread.last else ""
    return (f"{thread.contact} on {thread.app_label} is waiting for an answer: "
            f"“{last[:200]}”. I can draft a reply.")


def _a_draft_reply(params: dict, player: Any) -> str:
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    thread = chat_agent.read_thread(frame)
    if thread.error:
        return f"I could not read the conversation: {thread.error}"
    pending, why = chat_agent.pending_reply(thread)
    if not pending:
        return f"Nothing needs an answer: {why}."

    extra = str(params.get("instruction") or "").strip()
    draft = chat_agent.draft_reply(thread, extra=extra)
    if not draft.ok:
        return f"I could not draft a reply: {draft.error}"

    chat_agent.queue_for_approval(thread.contact, thread, draft, "draft requested")
    _log(player, f"Draft for {thread.contact}: {draft.text}")
    warning = f" It mentions {', '.join(draft.risky)} — check before sending." if draft.risky else ""
    spoken = (f"Here is a reply to {thread.contact}: “{draft.text}”. Say the word and I "
              f"will send it.{warning}")
    activity_log.record("action", f"drafted a reply for {thread.contact}",
                        detail=f"them: {(thread.last.text if thread.last else '')[:120]} | me: {draft.text}",
                        why="draft requested")
    return spoken


def _a_send_reply(params: dict, player: Any) -> str:
    message = str(params.get("message") or "").strip()
    if not message:
        return "I need the exact text to send."

    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    thread = chat_agent.read_thread(frame)
    if thread.error:
        return f"I could not read the conversation, so I did not type anything: {thread.error}"

    policy = chat_agent.contact_policy(thread.contact)
    unattended = bool(params.get("confirm")) or bool(
        policy and policy.get("auto_reply") and _load_config().get("reply", {}).get("auto_send"))
    if not unattended:
        return (f"I have the wording ready for {thread.contact or 'the open chat'}, but I will "
                f"not send it without confirmation. Say it again with confirm=true.")
    if policy is None and not _load_config().get("reply", {}).get("allow_unknown_contacts"):
        # An empty contact name means nothing read as an open conversation; say
        # that plainly rather than producing " is not in my contact list".
        who = thread.contact or "the open conversation"
        # Deliberately redirects the caller instead of just refusing. The model
        # reads this string, and a bare refusal is what produced five more
        # attempts at the same wrong action during a real session — it treated a
        # "no" as a puzzle to solve another way. Saying which tool owns this
        # ends the loop in one step.
        return (f"{who} is not on my unattended-reply allowlist, so I will not send "
                "this by myself — that gate is for automatic replies, not for words "
                "the user dictated. To send a message the user asked for, call "
                "pc_automation with action='reply' and message=<the exact words>: it "
                "looks at the screen, types into the open chat and presses Enter. "
                "Do not retry this action.")

    risky = chat_agent.risk_scan(message)
    if risky and not params.get("confirm"):
        return f"That message mentions {', '.join(risky)}, so I need explicit confirmation."

    result = chat_agent.send_reply(message, thread, verify_thread=True)
    if result.ok:
        chat_agent.record_reply(thread.contact, thread, message, outcome="sent",
                                note=result.detail)
        activity_log.record("action", f"sent a reply to {thread.contact}",
                            detail=message[:200], why=result.method,
                            meta={"verified": result.verified})
        return f"Sent to {thread.contact}: “{message}” ({result.detail})."

    chat_agent.record_reply(thread.contact, thread, message, outcome="failed",
                            note=result.detail)
    activity_log.record("action", f"failed to send to {thread.contact}",
                        detail=result.detail, why="verified send failed", outcome="failed")
    return f"I did not send it: {result.detail}"


def _a_approvals(params: dict, player: Any) -> str:
    queued = chat_agent.pending_approvals(limit=8)
    if not queued:
        return "Nothing is waiting for my approval."
    lines = []
    for index, item in enumerate(queued):
        lines.append(f"{index}: {item.get('contact')} — they said "
                     f"“{str(item.get('incoming'))[:90]}” → draft "
                     f"“{str(item.get('draft'))[:120]}” ({item.get('reason')})")
    _log(player, "\n".join(lines))
    top = queued[0]
    return (f"{len(queued)} draft(s) waiting. Newest: for {top.get('contact')}, "
            f"“{str(top.get('draft'))[:160]}”. Say 'send the draft' to send it, or "
            f"'discard draft 0' to drop it.")


def _a_discard_draft(params: dict, player: Any) -> str:
    index = 0
    text = str(params.get("instruction") or "").strip()
    if text.isdigit():
        index = int(text)
    elif text:
        for word in text.split():
            if word.isdigit():
                index = int(word)
                break
    return chat_agent.clear_approval(index)


def _a_arm(params: dict, player: Any) -> str:
    mode = str(params.get("mode") or "draft").strip().casefold()
    if mode not in ("draft", "send"):
        mode = "draft"
    # Arming is a decision with a cost — a background loop that photographs the
    # screen every few seconds — so it needs the user's own confirmation in both
    # modes. Until 27 September this check only covered 'send', and a draft watch
    # could be started by a tool call alone, which is how a storm began.
    if not params.get("confirm"):
        return (("Arming the screen watch needs an explicit confirmation — it takes a "
                 "screenshot every few seconds until it is stopped. Ask the user first, "
                 "then call again with confirm=true"
                 + (" and mode='send'." if mode == "send" else " and mode='draft'.")))
    message = _WATCHER.start(mode, params.get("interval_seconds") or 25, player)
    activity_log.record("screen", f"screen watch armed ({mode})",
                        detail=message, why="user asked for the screen assistant to watch",
                        actor="user")
    return message


def _a_disarm(params: dict, player: Any) -> str:
    message = _WATCHER.stop()
    activity_log.record("screen", "screen watch disarmed", detail=message,
                        why="user asked it to stop", actor="user")
    return message


def _a_watch_status(params: dict, player: Any) -> str:
    status = _WATCHER.status()
    if not status["running"]:
        return "I am not watching the screen. Say 'arm the screen assistant' to start."
    return (f"Watching in {status['mode']} mode every {status['interval_seconds']:.0f}s: "
            f"{status['ticks']} looks, {status['skipped']} skipped as unchanged, "
            f"{status['drafts_held']} drafts held, {status['replies_sent']} replies sent.")


def _a_learn_style(params: dict, player: Any) -> str:
    frame, reason = screen_watch.capture()
    if frame is None:
        return f"I cannot see the screen: {reason}"
    thread = chat_agent.read_thread(frame)
    if not thread.messages:
        return f"I found nothing to learn from: {thread.error or 'no conversation open'}."
    result = chat_agent.learn_style(thread.messages, thread.contact)
    _log(player, result)
    return f"{result} I will sound more like you in {thread.contact or 'your chats'} from now on."


def _a_set_style(params: dict, player: Any) -> str:
    instruction = str(params.get("instruction") or "").strip()
    if not instruction:
        style = chat_agent.load_style()
        return ("My drafting voice: " + "; ".join(
            f"{k}={v}" for k, v in style.items() if isinstance(v, str))[:600])
    style = chat_agent.load_style()
    lower = instruction.casefold()
    if "shorter" in lower or "brief" in lower:
        style["length"] = "one short sentence, no more" if "short" in lower else style.get("length")
    if "no emoji" in lower or "stop emoji" in lower:
        style["emoji"] = "never use emoji"
    if "emoji" in lower and "no emoji" not in lower:
        style["emoji"] = "use one emoji where it fits"
    if "formal" in lower:
        style["formality"] = "moderately formal, but still friendly"
    if "casual" in lower:
        style["formality"] = "low: contractions, no boilerplate"
    if "lowercase" in lower:
        style["capitalisation"] = "all lowercase"
    if "hindi" in lower or "urdu" in lower:
        style["language"] = "match the incoming language, including Hindi/Urdu in Latin script"
    style["last_instruction"] = instruction[:200]
    message = chat_agent.save_style(style)
    _log(player, f"{message} ({instruction[:80]})")
    return f"Noted: {instruction}"


def _a_set_contact(params: dict, player: Any) -> str:
    contact = str(params.get("contact") or "").strip()
    if not contact:
        return "Which contact should I update?"
    instruction = str(params.get("instruction") or "").strip().casefold()
    config = _load_config()
    contacts = config.setdefault("contacts", [])
    entry = None
    for candidate in contacts:
        if isinstance(candidate, dict) and str(candidate.get("name", "")).casefold() == contact.casefold():
            entry = candidate
            break
    created = False
    if entry is None:
        entry = {"name": contact, "aliases": [], "auto_reply": False,
                 "tone": "", "language": "", "notes": ""}
        contacts.append(entry)
        created = True

    if any(word in instruction for word in ("allow", "enable", "auto", "yes", "reply")):
        entry["auto_reply"] = True
    if any(word in instruction for word in ("stop", "disable", "no auto", "never")):
        entry["auto_reply"] = False
    if "tone" in instruction:
        entry["tone"] = instruction.split("tone", 1)[-1].strip(" :=")[:120]
    if instruction:
        entry["notes"] = (str(entry.get("notes") or "") + " " + instruction).strip()[:300]

    problem = _save_config(config)
    if problem:
        return f"I could not save that: {problem}"
    verb = "Added" if created else "Updated"
    state = "on" if entry.get("auto_reply") else "off"
    activity_log.record("screen", f"{verb.lower()} contact {contact} (auto-reply {state})",
                        why="user asked", actor="user")
    return (f"{verb} {contact} with auto-reply {state}. "
            f"{'I will only reply unattended once you arm me in send mode.' if entry.get('auto_reply') else 'I will only draft for them.'}")


def _a_contacts(params: dict, player: Any) -> str:
    contacts = [c for c in (_load_config().get("contacts") or []) if isinstance(c, dict)]
    if not contacts:
        return "No contacts are configured, so I will not message anyone unattended."
    lines = [f"{c.get('name')}: auto-reply {'on' if c.get('auto_reply') else 'off'}"
             for c in contacts]
    _log(player, "\n".join(lines))
    allowed = [c.get("name") for c in contacts if c.get("auto_reply")]
    return (f"{len(contacts)} contacts configured. Unattended replies allowed for: "
            f"{', '.join(str(a) for a in allowed) if allowed else 'nobody'}.")


def _a_privacy(params: dict, player: Any) -> str:
    config = _load_config().get("privacy", {})
    instruction = str(params.get("instruction") or "").strip().casefold()
    if instruction:
        if "off" in instruction and "block" in instruction:
            config["block_sensitive_apps"] = False
        elif "on" in instruction or "block" in instruction:
            config["block_sensitive_apps"] = True
        full = _load_config()
        full["privacy"] = config
        problem = _save_config(full)
        if problem:
            return problem
    blocked = config.get("blocked_apps") or []
    regions = config.get("redact_regions") or []
    # Self-check against a literal name, so the guard is proven every time this is
    # asked. The reason string is deliberately not repeated here: it reads as
    # "detected on screen", which would be a false claim about a self-test.
    session_ok, _reason = screen_watch.privacy_verdict(app="KeePassXC")
    guard = "refuses it" if not session_ok else "did NOT refuse it"
    return (f"Privacy: sensitive-app blocking is "
            f"{'on' if config.get('block_sensitive_apps', True) else 'off'}, "
            f"{len(blocked)} app patterns, {len(regions)} redacted region(s). "
            f"Self-check (literal 'KeePassXC' as the app name): the guard {guard}.")


def _a_find_on_screen(params: dict, player: Any) -> str:
    target = str(params.get("target") or params.get("instruction") or "").strip()
    if not target:
        return "What should I look for on screen?"
    point, data, reason = screen_watch.locate_text(target)
    if point is None:
        return f"I cannot find “{target}” on screen: {reason}"
    _log(player, f"found '{target}' at {point}")
    return (f"I can see “{target}” at {point[0]}, {point[1]} pixels. "
            f"Say 'click it' and I will.")


def _a_click_text(params: dict, player: Any) -> str:
    target = str(params.get("target") or params.get("instruction") or "").strip()
    if not target:
        return "What should I click?"
    if not params.get("confirm"):
        return f"Clicking “{target}” changes something on your machine — confirm and I will click it."
    point, data, reason = screen_watch.locate_text(target)
    if point is None:
        return f"I cannot find “{target}” to click: {reason}"
    try:
        from core import desktop_input  # noqa: PLC0415
        desktop_input.click(point[0], point[1])
    except Exception as exc:
        return f"I found it but could not click: {exc}"
    activity_log.record("action", f"clicked “{target}”",
                        detail=f"at {point}", why=f"user asked; screen said {str(data.get('context'))[:80]}",
                        actor="user")
    return f"Clicked “{target}”."


def _a_analyse_error(params: dict, player: Any) -> str:
    findings, transcript, meta = error_doctor.scan_screen()
    if not findings:
        return (f"I cannot see a recognisable error on screen"
                f"{': ' + meta.get('error') if meta.get('error') else '.'}")
    finding = findings[0]
    plan = error_doctor.diagnose(finding, transcript)
    _log(player, error_doctor.summarize(finding))
    activity_log.record("error", f"analysed an on-screen error: {finding.summary}",
                        detail=finding.detail[:300], why="user asked about the error")
    spoken = error_doctor.summarize(finding)
    if plan.ok:
        spoken += f" Cause: {plan.cause}"
        steps = plan.commands()
        if steps:
            spoken += f" I can run: {'; '.join(steps[:2])}."
        if plan.manual():
            spoken += f" The rest needs you: {plan.manual()[0][:120]}."
    else:
        spoken += f" I could not diagnose it: {plan.error}"
    return spoken


def _a_fix_error(params: dict, player: Any) -> str:
    if not params.get("confirm"):
        return ("I will diagnose the error first and show you what I would run. Say it again "
                "with confirm=true and I will run the inspection steps myself.")
    report = error_doctor.attempt_fix(confirm=True)
    text = error_doctor.report_text(report)
    _log(player, text)
    outcome = report.get("outcome")
    if outcome == "appears-resolved":
        return "I ran the safe steps and the error is no longer on screen."
    if outcome == "still-broken":
        return ("I tried what was safe to try and the error is still there. "
                + (f"Next for you: {report['manual'][0].get('command') or report['manual'][0].get('instruction')}"
                   if report.get("manual") else "I have no further safe step."))
    if outcome == "needs-you":
        first = (report.get("manual") or [{}])[0]
        return (f"The fix needs you: {first.get('command') or first.get('instruction')}. "
                f"{report.get('plan', {}).get('cause', '')}")
    if outcome == "no-error-found":
        return "There is no recognisable error on screen for me to fix."
    return text.splitlines()[-1] if text else "I did not find anything I could safely run."


def _a_run_command(params: dict, player: Any) -> str:
    command = str(params.get("command") or params.get("instruction") or "").strip()
    if not command:
        return "What command should I run?"
    verdict, reason, risk = error_doctor.command_verdict(command)
    if not verdict:
        activity_log.record("error", "command refused", detail=command[:200], why=reason,
                            outcome="refused")
        return f"I will not run that: {reason}"

    outcome = error_doctor.run_command(
        command,
        confirm=bool(params.get("confirm")),
        purpose=str(params.get("instruction") or "user asked for this command"),
        allow_medium=bool(params.get("confirm")),
    )
    if outcome.get("needs_confirm"):
        return f"{outcome['reason']}. Say it again with confirm=true and I will run it."
    if outcome.get("refused"):
        return f"I will not run that: {outcome.get('reason')}"

    _log(player, f"$ {command}\n{(outcome.get('stdout') or outcome.get('stderr') or '')[:600]}")
    tail = (outcome.get("stdout") or outcome.get("stderr") or "").strip()
    first = tail.splitlines()[-1][:200] if tail else "no output"
    return (f"Ran `{command}` (exit {outcome.get('return_code')}) in "
            f"{outcome.get('seconds')}s. {first}")


def _a_explain(params: dict, player: Any) -> str:
    query = str(params.get("instruction") or "").strip()
    entries = activity_log.recent(limit=400)
    screen_entries = [e for e in entries
                      if str(e.get("action", "")).casefold().find("reply") >= 0
                      or str(e.get("category", "")) in ("screen", "action")
                      or "screen" in str(e.get("why", "")).casefold()]
    if query:
        needle = query.casefold()
        screen_entries = [e for e in screen_entries
                          if needle in json.dumps(e, ensure_ascii=False).casefold()]
    if not screen_entries:
        return "I have no screen or reply activity recorded."
    lines = []
    for entry in screen_entries[:10]:
        lines.append(f"{entry.get('time')} — {entry.get('action')}"
                     + (f" [{entry.get('outcome')}]" if entry.get("outcome") != "ok" else ""))
        if entry.get("detail"):
            lines.append(f"    {str(entry['detail'])[:180]}")
        if entry.get("why"):
            lines.append(f"    why: {str(entry['why'])[:140]}")
    _log(player, "\n".join(lines))
    newest = screen_entries[0]
    return (f"Most recently: {newest.get('action')} — "
            f"{str(newest.get('detail'))[:200]}")


def _a_test(params: dict, player: Any) -> str:
    live = bool(params.get("confirm"))
    results: dict[str, Any] = {
        "vision_client": vision_client.self_test(live=live),
        "screen_watch": screen_watch.self_test(live=live),
        "chat_agent": chat_agent.self_test(live=live),
        "error_doctor": error_doctor.self_test(live=live),
    }
    failed = {name: [k for k, v in report.items() if v is False]
              for name, report in results.items()}
    failed = {name: bad for name, bad in failed.items() if bad}
    _log(player, json.dumps(results, indent=2, default=str)[:2500])
    if failed:
        return f"Self-test found problems: {failed}"
    return (f"All four layers pass{' including a live screen read' if live else ''}. "
            f"Vision models: {', '.join(vision_client.model_chain()[:3])}")


ACTIONS: dict[str, Callable[[dict, Any], str]] = {
    "status": _a_status,
    "see_screen": _a_see_screen,
    "look": _a_see_screen,
    "screenshot": _a_see_screen,
    "what_am_i_doing": _a_what_am_i_doing,
    "read_text": _a_read_text,
    "check_messages": _a_check_messages,
    "messages": _a_check_messages,
    "draft_reply": _a_draft_reply,
    "send_reply": _a_send_reply,
    "approvals": _a_approvals,
    "pending": _a_approvals,
    "discard_draft": _a_discard_draft,
    "arm": _a_arm,
    "start": _a_arm,
    "disarm": _a_disarm,
    "stop": _a_disarm,
    "watch_status": _a_watch_status,
    "learn_style": _a_learn_style,
    "set_style": _a_set_style,
    "set_contact": _a_set_contact,
    "contacts": _a_contacts,
    "privacy": _a_privacy,
    "find_on_screen": _a_find_on_screen,
    "find": _a_find_on_screen,
    "click_text": _a_click_text,
    "click": _a_click_text,
    "analyse_error": _a_analyse_error,
    "analyze_error": _a_analyse_error,
    "fix_error": _a_fix_error,
    "run_command": _a_run_command,
    "explain": _a_explain,
    "test": _a_test,
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one action. Never raises; always says what happened."""
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "").strip().casefold().replace("-", "_")
    if not action:
        return ("Tell me what to do with the screen: see_screen, what_am_i_doing, "
                "check_messages, arm, analyse_error, fix_error…")

    handler = ACTIONS.get(action)
    if handler is None:
        return (f"I do not know the screen action '{action}'. I can do: "
                f"{', '.join(sorted(set(ACTIONS)))}")

    try:
        return handler(params, player) or "Done."
    except Exception as exc:                                # noqa: BLE001
        activity_log.record("screen", f"action '{action}' failed",
                            detail=f"{exc.__class__.__name__}: {exc}",
                            why="plugin dispatch", outcome="error")
        return f"Sir, the screen assistant failed on '{action}': {exc.__class__.__name__}: {exc}"


_restore_arm_state()


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys
    print(run({"action": sys.argv[1] if len(sys.argv) > 1 else "status",
               "confirm": "--live" in sys.argv,
               "mode": "draft"}))
