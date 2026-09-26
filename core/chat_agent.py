"""Reading messages and answering them: the chat half of the screen plugin.

The pipeline, end to end
------------------------
    screenshot -> read the conversation -> is the last message theirs and
    unanswered? -> draft in *your* voice -> policy check -> type it, verify it
    landed, press enter -> record why

Three things in that chain are where this kind of feature usually goes wrong,
and each is handled explicitly here:

**Reading.** A chat pane is read by the model, but the answer is forced into a
schema (:class:`Thread`), and low-confidence readings are refused rather than
acted on. A misread message is worse than a missed one, because the reply is
wrong about something that was never said.

**Voice.** Replies are drafted from a style profile plus real example pairs
harvested from previous conversations (``memory/live_chat_style.json`` and
``memory/live_chat_examples.json``). Without that, "your" replies read like a
call-centre script, which is exactly what friends notice first.

**Sending.** Typing into the wrong window is the failure that cannot be undone
-- a draft sent to the wrong chat is sent. So the send path locates the compose
box, verifies the text actually appeared in the frame before pressing enter, and
re-reads the thread afterwards to confirm the message is there. If any step
cannot be verified, it stops and says so.

Policy is enforced in code, not asked of the model: only contacts you listed with
``auto_reply: true`` are eligible, within hourly caps, cooldowns and quiet hours,
and never twice for the same incoming message.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core import screen_watch, vision_client

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "screen_ai.json"
STATE_PATH = BASE_DIR / "memory" / "screen_ai_state.json"
STYLE_PATH = BASE_DIR / "memory" / "live_chat_style.json"
EXAMPLES_PATH = BASE_DIR / "memory" / "live_chat_examples.json"

__all__ = [
    "Message", "Thread", "Draft", "SendResult",
    "read_thread", "pending_reply", "draft_reply", "send_reply",
    "load_style", "save_style", "load_examples", "learn_style",
    "contact_policy", "can_auto_reply", "record_reply", "recent_replies",
    "state_snapshot", "self_test",
]

_LOCK = threading.RLock()


# ── messaging applications ───────────────────────────────────────────────────

@dataclass(frozen=True)
class AppProfile:
    """How to recognise a messenger and where its parts live on screen."""

    key: str
    label: str
    patterns: tuple[str, ...]
    compose_hints: str


APPS: tuple[AppProfile, ...] = (
    AppProfile(
        "whatsapp", "WhatsApp",
        ("whatsapp", "web.whatsapp", "whatsapp web"),
        "the message box at the bottom of the conversation pane (bottom right), "
        "labelled 'Type a message'",
    ),
    AppProfile(
        "telegram", "Telegram",
        ("telegram", "web.telegram", "telegram web"),
        "the message box at the bottom of the chat pane, labelled 'Write a message'",
    ),
    AppProfile(
        "signal", "Signal",
        ("signal", "signal desktop"),
        "the message box at the bottom of the conversation pane",
    ),
    AppProfile(
        "discord", "Discord",
        ("discord",),
        "the message box at the bottom of the channel, labelled 'Message #'",
    ),
    AppProfile(
        "instagram", "Instagram",
        ("instagram",),
        "the message box at the bottom of the direct-message pane",
    ),
    AppProfile(
        "messenger", "Messenger",
        ("messenger", "facebook messenger", "m.me"),
        "the message box at the bottom of the chat pane",
    ),
    AppProfile(
        "element", "Element",
        ("element", "matrix"),
        "the message composer at the bottom of the room",
    ),
)


def detect_app(text: str) -> Optional[AppProfile]:
    haystack = str(text or "").casefold()
    if not haystack.strip():
        return None
    for profile in APPS:
        if any(pattern in haystack for pattern in profile.patterns):
            return profile
    return None


# ── thread model ─────────────────────────────────────────────────────────────

@dataclass
class Message:
    sender: str          # "me" | "them"
    text: str
    index: int = 0

    def as_dict(self) -> dict:
        return {"sender": self.sender, "text": self.text}


@dataclass
class Thread:
    """One conversation as it appears on screen right now."""

    app: str = ""
    app_label: str = ""
    contact: str = ""
    messages: list[Message] = field(default_factory=list)
    confidence: float = 0.0
    model: str = ""
    seconds: float = 0.0
    capture: dict = field(default_factory=dict)
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.contact) and bool(self.messages) and not self.error

    @property
    def last(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None

    @property
    def last_is_from_them(self) -> bool:
        last = self.last
        return bool(last and last.sender == "them")

    @property
    def transcript(self) -> str:
        lines = []
        for message in self.messages:
            speaker = "Me" if message.sender == "me" else (self.contact or "Them")
            lines.append(f"{speaker}: {message.text}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "app": self.app,
            "contact": self.contact,
            "messages": [m.as_dict() for m in self.messages],
            "last_from_me": not self.last_is_from_them,
            "confidence": round(self.confidence, 2),
            "model": self.model,
            "seconds": round(self.seconds, 2),
            "capture": self.capture,
            "error": self.error,
        }


_THREAD_PROMPT = """Read this screenshot of a messaging application.

Reply with JSON only, no commentary:
{
  "messaging_app": "whatsapp|telegram|signal|discord|instagram|messenger|element|other|none",
  "conversation_with": "exact name shown at the top of the open conversation, or empty",
  "messages": [{"sender": "me|them", "text": "exact message text as shown"}],
  "confidence": 0.0
}

Rules:
- Only include messages you can actually read; never invent or paraphrase.
- Include the MOST RECENT messages, in order, oldest first. Up to 15.
- "me" means the message is on the right / from the account holder.
- If no conversation is open, set "conversation_with": "" and "messages": [].
- Preserve the original language, spelling and emoji.
- confidence is your own certainty in this reading, 0.0 to 1.0."""


def read_thread(frame: Optional[screen_watch.Capture] = None,
                reuse_seconds: float = 2.0) -> Thread:
    """Read the conversation open on screen. Never raises."""
    if frame is None:
        frame, reason = screen_watch.capture(reuse_seconds=reuse_seconds)
        if frame is None:
            return Thread(error=reason)

    data, result = vision_client.analyze_json(frame.path, _THREAD_PROMPT, max_output_tokens=1000)
    if data is None:
        return Thread(error=result.error or "could not read the screen",
                      model=result.model, seconds=result.seconds,
                      capture=frame.as_dict())

    app_hint = str(data.get("messaging_app") or "")
    profile = detect_app(app_hint) or detect_app(str(data.get("conversation_with") or ""))
    contact = str(data.get("conversation_with") or "").strip()

    messages: list[Message] = []
    raw_messages = data.get("messages")
    if isinstance(raw_messages, list):
        for index, entry in enumerate(raw_messages[:20]):
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            sender = "me" if str(entry.get("sender", "")).strip().casefold() == "me" else "them"
            messages.append(Message(sender=sender, text=text, index=index))

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    thread = Thread(
        app=profile.key if profile else (app_hint or "other"),
        app_label=profile.label if profile else (app_hint or "unknown app"),
        contact=contact,
        messages=messages,
        confidence=confidence,
        model=result.model,
        seconds=result.seconds,
        capture=frame.as_dict(),
    )

    allowed, why = screen_watch.privacy_verdict(
        app=thread.app_label, title=contact,
        visible_text=" ".join(m.text for m in messages[:4]),
    )
    if not allowed:
        thread.error = why
        thread.messages = []
    if profile is None and contact:
        thread.notes.append(f"unrecognised messaging app ({app_hint!r})")
    return thread


def pending_reply(thread: Thread, min_confidence: float = 0.55) -> tuple[bool, str]:
    """Is there a message from them that has not been answered?

    Deliberately conservative: an unclear read is reported as *no* pending reply
    rather than as one, because a wrong "yes" here produces a message the user
    never wanted to send.
    """
    if thread.error:
        return False, thread.error
    if not thread.contact:
        return False, "no conversation is open on screen"
    if not thread.messages:
        return False, "no messages could be read"
    if thread.confidence and thread.confidence < float(min_confidence):
        return False, (
            f"the conversation was read with only {thread.confidence:.0%} confidence — "
            "not acting on an unclear screen"
        )
    if not thread.last_is_from_them:
        return False, "the last message is already yours"
    return True, f"unanswered message from {thread.contact}"


# ── voice ────────────────────────────────────────────────────────────────────

DEFAULT_STYLE: dict[str, Any] = {
    "tone": "casual, warm, brief — like texting a close friend",
    "language": "match the language of the incoming message",
    "length": "one short sentence, occasionally two; never a paragraph",
    "emoji": "sparing — at most one, only when the other person used one",
    "formality": "low: contractions, no greetings boilerplate after the first reply",
    "capitalisation": "mostly lowercase, like the person actually types",
    "catchphrases": [],
    "avoid": [
        "assistant phrasing such as 'How may I help you' or 'As an AI'",
        "formal sign-offs, signatures, or your name at the end",
        "promising anything, agreeing to plans, or confirming times/dates",
        "sharing addresses, passwords, bank details, or anything financial",
        "inventing facts the person did not say",
    ],
    "never_do": "never claim to be somewhere or do something that was not stated",
}


def load_style() -> dict:
    """The current voice profile, with defaults filled in."""
    style = dict(DEFAULT_STYLE)
    try:
        loaded = json.loads(STYLE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                style[key] = value
    except (OSError, ValueError):
        pass
    return style


def save_style(style: dict) -> str:
    merged = load_style()
    if isinstance(style, dict):
        for key, value in style.items():
            merged[key] = value
    try:
        STYLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STYLE_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return f"could not save the style profile: {exc}"
    return "Style profile updated."


def load_examples(limit: int = 12) -> list[dict]:
    """Recent (their message, your reply) pairs used as few-shot guidance."""
    try:
        data = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        return []
    clean = [p for p in pairs if isinstance(p, dict) and p.get("them") and p.get("me")]
    return clean[-max(1, int(limit)):]


def learn_style(messages: list[Message], contact: str = "",
                max_pairs: int = 200) -> str:
    """Harvest real reply pairs from an observed conversation.

    This is how the voice gets closer over time: every time the plugin reads a
    thread where the person genuinely replied, the (them -> me) pair is kept as
    an example. Nothing is generated here -- these are the user's own words.
    """
    pairs = []
    for index in range(len(messages) - 1):
        current, following = messages[index], messages[index + 1]
        if current.sender == "them" and following.sender == "me":
            pairs.append({"them": current.text[:300], "me": following.text[:300],
                          "contact": contact[:60]})
    if not pairs:
        return "No reply examples visible in this conversation."

    try:
        existing = []
        try:
            data = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("pairs"), list):
                existing = [p for p in data["pairs"] if isinstance(p, dict)]
        except (OSError, ValueError):
            existing = []

        seen = {(p.get("them"), p.get("me")) for p in existing}
        added = 0
        for pair in pairs:
            key = (pair["them"], pair["me"])
            if key in seen:
                continue
            seen.add(key)
            existing.append(pair)
            added += 1

        EXAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXAMPLES_PATH.write_text(
            json.dumps({"pairs": existing[-int(max_pairs):]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"could not save the examples: {exc}"
    return f"Learned {added} new reply example(s); {len(existing)} on file."


# ── drafting ─────────────────────────────────────────────────────────────────

@dataclass
class Draft:
    ok: bool
    text: str = ""
    error: str = ""
    risky: list[str] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "risky": self.risky,
                "model": self.model, "seconds": round(self.seconds, 2),
                "error": self.error}


_RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(i'?ll be there|i will be there|see you|meet you|on my way)\b", "commits to being somewhere"),
    (r"\b(yes|sure|ok(ay)?)[,.]? (i'?ll|i will|let'?s)\b", "agrees to a plan"),
    (r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", "states a time"),
    (r"\b(tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "states a day"),
    (r"\b(send|transfer|pay|paid|owe|money|dollars?|rupees?|rs\.?|account number|iban|card)\b", "financial"),
    (r"\b(password|otp|pin|code)\b", "credential"),
    (r"\b(my address|home address|live at|flat \d|house \d)\b", "home address"),
    (r"\b(i am an ai|as an ai|assistant|language model|jarvis)\b", "breaks character"),
)


def risk_scan(text: str) -> list[str]:
    """Reasons a draft should not be sent unattended."""
    found: list[str] = []
    haystack = str(text or "").casefold()
    for pattern, label in _RISK_PATTERNS:
        if re.search(pattern, haystack) and label not in found:
            found.append(label)
    return found


_DRAFT_SYSTEM = """You write text messages on behalf of a real person, in their voice.

You are given: a style profile, real examples of how this person writes, and the
tail of the current conversation. Write the single next message they would send.

Hard rules:
- Reply only to the last message. Do not answer older ones.
- Match the language of the incoming message.
- Keep it short: one sentence, at most two. No paragraphs.
- No greetings unless the other person just greeted them.
- No signature, no name at the end, no assistant phrasing.
- Never promise, agree to plans, confirm times, or share personal/financial
  details. If the incoming message asks for any of that, reply with a short
  holding message that assumes the person will answer themselves.
- Never invent facts, events or feelings that were not in the conversation.
- If nothing sensible can be said, reply with an empty string.
- Output ONLY the message text, nothing else. No quotes around it."""


def _draft_prompt(thread: Thread, style: dict, examples: list[dict],
                  extra: str = "") -> str:
    lines = ["STYLE PROFILE:"]
    for key in ("tone", "language", "length", "emoji", "formality",
                "capitalisation", "never_do"):
        if style.get(key):
            lines.append(f"- {key}: {style[key]}")
    if style.get("catchphrases"):
        lines.append(f"- phrases this person uses: {', '.join(map(str, style['catchphrases'][:6]))}")
    avoid = style.get("avoid") or []
    if avoid:
        lines.append("- avoid: " + "; ".join(map(str, avoid[:6])))

    if examples:
        lines.append("\nREAL EXAMPLES OF HOW THEY WRITE (incoming -> their reply):")
        for pair in examples[-8:]:
            lines.append(f"  {str(pair.get('them'))[:160]}  ->  {str(pair.get('me'))[:160]}")

    lines.append("\nCURRENT CONVERSATION (oldest first):")
    for message in thread.messages[-12:]:
        speaker = "Me" if message.sender == "me" else (thread.contact or "Them")
        lines.append(f"  {speaker}: {message.text}")
    lines.append(f"\nThe last message is from {thread.contact or 'them'}. "
                 "Write my next message.")
    if extra:
        lines.append(f"\nExtra instruction from the user: {extra}")
    return "\n".join(lines)


def _clean_draft(text: str, style: dict) -> str:
    """Turn a model reply into something a person would actually send.

    Order matters and was wrong in the first version: a speaker prefix has to
    come off *before* surrounding quotes are stripped, because ``Me: "hey"``
    leaves the quote in place and the message goes out with stray quotation
    marks. Newlines are collapsed afterwards, so a model that answers in two
    lines does not send a two-line message.
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    for prefix in ("Me:", "me:", "Me :", "Reply:", "reply:", "Message:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    cleaned = re.sub(r"\s*\n+\s*", " ", cleaned).strip()
    # A model asked for one line sometimes returns three; keep the first
    # sentence or two rather than sending a wall of text.
    if len(cleaned) > 320:
        parts = re.split(r"(?<=[.!?])\s+", cleaned)
        cleaned = " ".join(parts[:2])[:320]
    return cleaned


def draft_reply(thread: Thread,
                extra: str = "",
                style: Optional[dict] = None,
                max_output_tokens: int = 160) -> Draft:
    """Compose the next message in the user's voice. Never sends anything."""
    if not thread.ok:
        return Draft(ok=False, error=thread.error or "nothing to reply to")
    if not thread.last_is_from_them:
        return Draft(ok=False, error="the last message is already mine")

    active_style = style or load_style()
    examples = load_examples()
    prompt = _draft_prompt(thread, active_style, examples, extra)

    result = vision_client.text_completion(
        prompt,
        system=_DRAFT_SYSTEM,
        max_output_tokens=max_output_tokens,
        temperature=0.6,
    )
    if not result.ok:
        return Draft(ok=False, error=result.error, model=result.model, seconds=result.seconds)

    text = _clean_draft(result.text, active_style)
    if not text:
        return Draft(ok=False, error="the model had nothing sensible to say",
                     model=result.model, seconds=result.seconds)

    risky = risk_scan(text)
    draft = Draft(ok=True, text=text, risky=risky,
                  model=result.model, seconds=result.seconds)
    return draft


# ── state & policy ───────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"replies": [], "seen": {}, "queued": [], "refusals": []}


def _load_state() -> dict:
    with _LOCK:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _default_state()
                base.update(data)
                return base
        except (OSError, ValueError):
            pass
        return _default_state()


def _save_state(state: dict) -> None:
    with _LOCK:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_PATH)
        except OSError:
            pass


def config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reply_config() -> dict:
    value = config().get("reply")
    return value if isinstance(value, dict) else {}


def contact_policy(name: str) -> Optional[dict]:
    """The configured rule for a contact, or ``None`` when they are unknown."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    for entry in config().get("contacts") or []:
        if not isinstance(entry, dict):
            continue
        aliases = [str(entry.get("name", ""))] + [str(a) for a in (entry.get("aliases") or [])]
        if any(alias.strip().casefold() == wanted for alias in aliases if alias.strip()):
            return entry
    return None


def _parse_hhmm(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    match = re.match(r"\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*$", str(text or ""))
    if not match:
        return fallback
    return max(0, min(23, int(match.group(1)))), max(0, min(59, int(match.group(2))))


def _in_quiet_hours(now_ts: float, start: str, end: str) -> bool:
    moment = time.localtime(now_ts)
    start_h, start_m = _parse_hhmm(start, (23, 0))
    end_h, end_m = _parse_hhmm(end, (7, 30))
    minutes = moment.tm_hour * 60 + moment.tm_min
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    if start_mins == end_mins:
        return False
    if start_mins < end_mins:
        return start_mins <= minutes < end_mins
    return minutes >= start_mins or minutes < end_mins


def can_auto_reply(contact: str, now_ts: Optional[float] = None) -> tuple[bool, str]:
    """Whether an unattended reply to this contact is allowed right now."""
    cfg = reply_config()
    now = float(now_ts or time.time())

    policy = contact_policy(contact)
    if policy is None:
        if cfg.get("allow_unknown_contacts"):
            policy = {"auto_reply": True}
        else:
            return False, (
                f"{contact or 'this contact'} is not in the allowlist "
                "(config/screen_ai.json -> contacts)"
            )
    if not policy.get("auto_reply"):
        return False, f"auto-reply is off for {contact}"

    if cfg.get("quiet_hours", {}).get("enabled", True):
        quiet = cfg.get("quiet_hours") or {}
        if _in_quiet_hours(now, quiet.get("start", "23:00"), quiet.get("end", "07:30")):
            return False, "quiet hours — holding replies until morning"

    state = _load_state()
    replies = [entry for entry in state.get("replies", []) if isinstance(entry, dict)]
    hour_ago = now - 3600
    cooldown = float(cfg.get("cooldown_seconds", 45) or 45)
    per_contact_hour = int(cfg.get("max_per_contact_per_hour", 4) or 4)
    global_hour = int(cfg.get("max_per_hour", 12) or 12)

    recent_all = [e for e in replies if float(e.get("ts") or 0) >= hour_ago]
    if len(recent_all) >= global_hour:
        return False, f"hourly reply cap reached ({global_hour})"

    mine = [e for e in recent_all if str(e.get("contact", "")).casefold() == contact.casefold()]
    if len(mine) >= per_contact_hour:
        return False, f"already replied {len(mine)}x to {contact} in the last hour"
    if mine:
        last_ts = max(float(e.get("ts") or 0) for e in mine)
        if now - last_ts < cooldown:
            return False, f"cooldown: {cooldown - (now - last_ts):.0f}s since the last reply"
    return True, ""


def message_key(thread: Thread) -> str:
    last = thread.last
    raw = f"{thread.app}|{thread.contact}|{last.text if last else ''}".casefold()
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def already_answered(thread: Thread, max_age_seconds: float = 6 * 3600) -> bool:
    key = message_key(thread)
    state = _load_state()
    seen = state.get("seen") or {}
    stamp = float(seen.get(key) or 0)
    if not stamp:
        return False
    return (time.time() - stamp) < float(max_age_seconds)


def record_reply(contact: str, thread: Thread, text: str, outcome: str = "sent",
                 note: str = "") -> None:
    state = _load_state()
    entry = {
        "ts": round(time.time(), 1),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "contact": contact[:60],
        "app": thread.app,
        "incoming": (thread.last.text if thread.last else "")[:280],
        "reply": text[:280],
        "outcome": outcome,
        "note": note[:200],
        "key": message_key(thread),
    }
    state.setdefault("replies", []).append(entry)
    state["replies"] = state["replies"][-500:]
    state.setdefault("seen", {})[entry["key"]] = entry["ts"]
    # Keep the dedupe map from growing forever.
    cutoff = time.time() - 7 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if float(v or 0) >= cutoff}
    _save_state(state)


def record_refusal(contact: str, thread: Thread, reason: str) -> None:
    state = _load_state()
    state.setdefault("refusals", []).append({
        "ts": round(time.time(), 1),
        "contact": contact[:60],
        "incoming": (thread.last.text if thread.last else "")[:200],
        "reason": reason[:200],
    })
    state["refusals"] = state["refusals"][-200:]
    _save_state(state)


def queue_for_approval(contact: str, thread: Thread, draft: Draft, reason: str) -> None:
    state = _load_state()
    state.setdefault("queued", []).append({
        "ts": round(time.time(), 1),
        "contact": contact[:60],
        "incoming": (thread.last.text if thread.last else "")[:280],
        "draft": draft.text[:400],
        "reason": reason[:200],
    })
    state["queued"] = state["queued"][-100:]
    _save_state(state)


def pending_approvals(limit: int = 20) -> list[dict]:
    state = _load_state()
    return list(reversed(state.get("queued", [])[-max(1, int(limit)):]))


def clear_approval(index: int = 0) -> str:
    state = _load_state()
    queued = state.get("queued", [])
    if not queued:
        return "Nothing is waiting for approval."
    try:
        position = len(queued) - 1 - int(index)
        removed = queued.pop(position)
    except (IndexError, ValueError):
        return "No draft at that position."
    _save_state(state)
    return f"Discarded the draft for {removed.get('contact')}."


def recent_replies(limit: int = 10) -> list[dict]:
    state = _load_state()
    return list(reversed(state.get("replies", [])[-max(1, int(limit)):]))


def state_snapshot() -> dict:
    state = _load_state()
    now = time.time()
    replies = [e for e in state.get("replies", []) if isinstance(e, dict)]
    return {
        "replies_total": len(replies),
        "replies_last_hour": len([e for e in replies if float(e.get("ts") or 0) >= now - 3600]),
        "queued_for_approval": len(state.get("queued", [])),
        "refusals": len(state.get("refusals", [])),
        "tracked_messages": len(state.get("seen", {})),
        "contacts_configured": len([c for c in (config().get("contacts") or []) if isinstance(c, dict)]),
        "style_examples": len(load_examples(limit=500)),
    }


# ── sending ──────────────────────────────────────────────────────────────────

@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    method: str = ""
    verified: bool = False
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "verified": self.verified, "method": self.method,
                "seconds": round(self.seconds, 2), "detail": self.detail}


_COMPOSE_PROMPT = (
    "The screen shows a messaging app. Find the message input box / compose field "
    "for the OPEN conversation (usually at the bottom of the conversation pane).\n\n"
    "Reply with JSON only:\n"
    '{"found": true|false, "x": 0-1000, "y": 0-1000, "confidence": 0.0, '
    '"description": "what you found"}\n\n'
    "x/y are normalised: 0 is the left/top edge, 1000 the right/bottom edge. Give the "
    "centre of the input box. If there is no open conversation, set found false."
)


def _type_into_frame(text: str, frame: screen_watch.Capture) -> SendResult:
    """Put *text* into the compose box, verifying focus and content first."""
    started = time.monotonic()
    from core import desktop_input  # local import: keeps this module import-safe

    located, result = vision_client.analyze_json(
        frame.path, _COMPOSE_PROMPT, max_output_tokens=250,
    )
    if located is None:
        return SendResult(False, f"could not read the screen: {result.error}")
    if not located.get("found"):
        return SendResult(False, "could not find the message input box on screen")

    width = frame.width or screen_watch.screen_size()[0]
    height = frame.height or screen_watch.screen_size()[1]
    if not width or not height:
        return SendResult(False, "screen size unknown, cannot click the input box")
    try:
        x = int(max(0, min(1000, float(located.get("x", 0)))) / 1000 * width)
        y = int(max(0, min(1000, float(located.get("y", 0)))) / 1000 * height)
    except (TypeError, ValueError):
        return SendResult(False, "model returned unusable coordinates for the input box")

    try:
        desktop_input.click(x, y)
        time.sleep(0.35)
    except Exception as exc:
        return SendResult(False, f"could not focus the message box: {exc}")

    method = "typed"
    try:
        from core import kali_compat  # noqa: PLC0415
        copied, detail = kali_compat.clipboard_copy(text)
        if copied:
            desktop_input.hotkey("ctrl", "v")
            method = f"clipboard ({detail})"
        else:
            desktop_input.typewrite(text)
    except Exception:
        try:
            desktop_input.typewrite(text)
            method = "typed"
        except Exception as exc:
            return SendResult(False, f"could not enter the text: {exc}")

    time.sleep(0.6)
    after, reason = screen_watch.capture(reuse_seconds=0)
    if after is None:
        return SendResult(False, f"entered the text but could not verify it: {reason}")
    changed = not screen_watch.same_screen(frame, after, threshold=3)
    if not changed:
        return SendResult(False, "the screen did not change — the text did not land; nothing was sent")

    verify, result = vision_client.analyze_json(
        after.path,
        "Look at the message input box of this messaging app. Is there unsent text in it?\n"
        'Reply with JSON only: {"has_text": true|false, "text": "what it says", "confidence": 0.0}',
        max_output_tokens=250,
    )
    if verify is None or not verify.get("has_text"):
        return SendResult(False, "the text is not visible in the input box; nothing was sent",
                          method=method, seconds=time.monotonic() - started)
    return SendResult(True, "text is in the input box, verified", method=method,
                      verified=True, seconds=time.monotonic() - started)


def send_reply(text: str,
               thread: Optional[Thread] = None,
               confirm: bool = False,
               press_enter: bool = True,
               verify_thread: bool = True) -> SendResult:
    """Type a reply into the open conversation and (optionally) send it.

    The order matters: the frame is captured *before* anything is clicked, the
    input box is located in that frame, the text is entered and then confirmed
    present, and only then is enter pressed. Every early return leaves the
    conversation untouched.
    """
    started = time.monotonic()
    body = str(text or "").strip()
    if not body:
        return SendResult(False, "nothing to send")
    if len(body) > 600:
        return SendResult(False, "that reply is too long to send as a message")

    frame, reason = screen_watch.capture()
    if frame is None:
        return SendResult(False, reason)

    if thread is None:
        thread = read_thread(frame)
    if thread.error:
        return SendResult(False, f"could not read the conversation: {thread.error}")

    allowed, why = screen_watch.privacy_verdict(
        app=thread.app_label, title=thread.contact, visible_text=body[:200],
    )
    if not allowed:
        return SendResult(False, why)

    typed = _type_into_frame(body, frame)
    if not typed.ok:
        return SendResult(False, typed.detail, method=typed.method,
                          seconds=time.monotonic() - started)

    if not press_enter:
        return SendResult(True, f"{typed.detail}; left unsent as requested",
                          method=typed.method, verified=True,
                          seconds=time.monotonic() - started)

    from core import desktop_input  # noqa: PLC0415

    try:
        desktop_input.press("enter")
    except Exception as exc:
        return SendResult(False, f"the text is in the box but enter failed: {exc}",
                          method=typed.method, seconds=time.monotonic() - started)

    if not verify_thread:
        return SendResult(True, "sent (not verified)", method=typed.method,
                          seconds=time.monotonic() - started)

    time.sleep(1.0)
    after, reason = screen_watch.capture()
    if after is None:
        return SendResult(True, f"sent, but could not verify afterwards: {reason}",
                          method=typed.method, seconds=time.monotonic() - started)

    check = read_thread(after)
    if check.error:
        return SendResult(True, f"sent, verification unavailable: {check.error}",
                          method=typed.method, seconds=time.monotonic() - started)

    if check.messages and check.last and check.last.sender == "me":
        match = check.last.text.strip().casefold()
        if body.strip().casefold()[:40] in match or match[:40] in body.strip().casefold():
            return SendResult(True, "sent and confirmed in the conversation",
                              method=typed.method, verified=True,
                              seconds=time.monotonic() - started)
        return SendResult(True, "sent; the newest message reads differently than drafted",
                          method=typed.method, seconds=time.monotonic() - started)
    if check.messages and not check.last_is_from_them:
        return SendResult(True, "sent (message is no longer theirs)",
                          method=typed.method, seconds=time.monotonic() - started)
    return SendResult(False, "enter was pressed but the message is not visible — check the chat",
                      method=typed.method, seconds=time.monotonic() - started)


# ── self-test ────────────────────────────────────────────────────────────────

def self_test(live: bool = False, send: bool = False) -> dict:
    """Offline policy/parsing checks; ``live`` reads the screen for real.

    ``send`` must never be implied: it is the one flag that types into a real
    conversation, so it defaults to off even when ``live`` is on.
    """
    checks: dict[str, Any] = {}

    checks["detect_whatsapp"] = bool(detect_app("WhatsApp Web"))
    checks["detect_telegram"] = bool(detect_app("Telegram Desktop"))
    checks["detect_none"] = detect_app("LibreOffice Calc") is None

    thread = Thread(
        app="whatsapp", app_label="WhatsApp", contact="Test Friend",
        messages=[Message("me", "hey", 0), Message("them", "you around?", 1)],
        confidence=0.9,
    )
    checks["pending_true"] = pending_reply(thread)[0]
    answered = Thread(app="whatsapp", contact="Test Friend", confidence=0.9,
                      messages=[Message("them", "hi", 0), Message("me", "yo", 1)])
    checks["pending_false_when_mine"] = not pending_reply(answered)[0]
    unclear = Thread(app="whatsapp", contact="Test Friend", confidence=0.2,
                     messages=[Message("them", "hi", 0)])
    checks["pending_false_when_unclear"] = not pending_reply(unclear)[0]
    checks["pending_false_no_contact"] = not pending_reply(
        Thread(app="whatsapp", messages=[Message("them", "hi")], confidence=1.0))[0]

    checks["risk_flags_promise"] = bool(risk_scan("yes I'll be there at 8pm"))
    checks["risk_flags_money"] = bool(risk_scan("I'll transfer the money today"))
    checks["risk_clean_chat"] = not risk_scan("haha that's wild, what happened next")
    checks["risk_flags_ai_talk"] = bool(risk_scan("As an AI I cannot do that"))

    cleaned = _clean_draft('Me: "hey, sounds good"\nsecond line', load_style())
    checks["draft_clean_strips_prefix"] = cleaned.startswith("hey, sounds good")
    checks["draft_clean_short"] = len(_clean_draft("x " * 400, load_style())) <= 320

    style = load_style()
    checks["style_has_tone"] = bool(style.get("tone"))
    checks["style_has_avoid_list"] = bool(style.get("avoid"))

    policy_missing = contact_policy("definitely-not-configured-contact-xyz")
    checks["unknown_contact_blocked"] = policy_missing is None
    blocked, reason = can_auto_reply("definitely-not-configured-contact-xyz")
    checks["unknown_contact_refused"] = (not blocked) and bool(reason)

    checks["message_key_stable"] = message_key(thread) == message_key(
        Thread(app="whatsapp", contact="Test Friend", confidence=0.9,
               messages=[Message("them", "you around?", 1)]))
    checks["message_key_changes"] = message_key(thread) != message_key(
        Thread(app="whatsapp", contact="Test Friend", confidence=0.9,
               messages=[Message("them", "different", 1)]))

    checks["quiet_hours_midnight_wrap"] = _in_quiet_hours(
        time.mktime((2026, 9, 20, 1, 0, 0, 0, 0, -1)), "23:00", "07:30")
    checks["quiet_hours_day_clear"] = not _in_quiet_hours(
        time.mktime((2026, 9, 20, 14, 0, 0, 0, 0, -1)), "23:00", "07:30")

    checks["config_loads"] = isinstance(reply_config(), dict)
    checks["state_shape"] = "replies" in state_snapshot() or "replies_total" in state_snapshot()
    checks["examples_load"] = isinstance(load_examples(), list)

    if live:
        read = read_thread()
        # "No conversation is open" is a legitimate live result, not a defect: it
        # means the screen is not showing a chat. Only a transport/read failure
        # counts against the self-test.
        checks["live_read_works"] = not read.error
        checks["live_conversation_open"] = read.ok
        checks["live_app"] = read.app_label
        checks["live_contact"] = read.contact
        checks["live_messages"] = len(read.messages)
        checks["live_confidence"] = read.confidence
        checks["live_error"] = read.error
        checks["live_pending"], checks["live_pending_reason"] = pending_reply(read)
        if read.ok and read.last_is_from_them:
            draft = draft_reply(read)
            checks["live_draft_ok"] = draft.ok
            checks["live_draft"] = draft.text
            checks["live_draft_risky"] = draft.risky
        if send:
            draft = draft_reply(read)
            if draft.ok:
                sent = send_reply(draft.text, read, verify_thread=True)
                checks["live_send_ok"] = sent.ok
                checks["live_send_detail"] = sent.detail

    checks["ok"] = all(bool(value) for key, value in checks.items()
                       if not key.startswith("live_"))
    return checks


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys

    def _failure_keys(report: dict) -> list[str]:
        """Keys that are False and genuinely indicate a problem.

        ``live_conversation_open`` and ``live_pending`` are observations about
        whatever happens to be on screen, so they are excluded -- reporting them
        as failures would train the reader to ignore this list.
        """
        informational = {"live_conversation_open", "live_pending", "live_messages",
                         "live_draft_risky", "live_draft", "live_send_detail"}
        return [key for key, value in report.items()
                if value is False and key not in informational]

    print(json.dumps(self_test(live="--live" in sys.argv, send="--send" in sys.argv), indent=2))
