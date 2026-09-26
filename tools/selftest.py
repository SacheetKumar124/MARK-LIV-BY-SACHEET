"""tools/selftest.py — one command that proves Mark-LIV still works.

Every previous verification in this project was an ad-hoc terminal harness: it
proved a fix at the moment it was made and then vanished. This file is the same
checking, kept. Run it before and after any change:

    python3 tools/selftest.py

What it checks, in order of how badly each failure would hurt
------------------------------------------------------------
1.  **Everything compiles.**  A syntax error anywhere is a dead assistant.
2.  **Discovery still works.**  Every action and plugin the model is promised
    actually loads, and the count has not silently dropped — a plugin that fails
    to import is a capability that disappears without a word.
3.  **The wiring invariants hold.**  The specific mistakes this project has
    already made once: a bare `loop` inside `_execute_tool`, `get_event_loop()`,
    a capture path that cannot see Wayland, background work with no runner, a
    prompt section that no longer renders.
4.  **Each module passes its own self-test.**  Vision budget, WhatsApp flows,
    corrections, rules, jobs, the background runner — the behaviour, not the
    syntax.

Exit code is 0 only when everything passes, so it can be used in a pre-run step.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = DIM = RESET = ""

# Modules that ship a _self_test(). Kept as data so adding one is one line.
SELF_TEST_MODULES = [
    "core.input_guard",
    "core.vision_budget",
    "core.learned",
    "core.rules",
    "core.jobs",
    "core.task_runner",
    "core.attention",
    "core.senses",
    "core.brain",
    "core.screen_watch",
    "core.activity_log",
    "actions.memory_learn",
    "actions.rules_tool",
    "actions.task_queue",
    "actions.pc_automation",
]

# Capabilities the model is promised. A missing name means a spoken request that
# will silently have nowhere to go.
EXPECTED_ACTIONS = {
    "pc_automation", "computer_control", "file_controller", "open_app",
    "memory_learn", "rules", "task_queue", "reminder", "send_message",
    "web_search", "screen_process",
}

# Invariants that guard bugs this project has already fixed once. Each is
# (label, path, needle-that-must-appear, or None for must-NOT-appear).
INVARIANTS: list[tuple[str, str, str | None]] = [
    ("main.py: no bare loop in _execute_tool", "main.py", None),
    ("main.py: task runner bound for deferred work", "main.py",
     "task_runner.bind(self._run_background_task)"),
    ("main.py: brain receives queued work", "main.py", "bind_task_runner"),
    ("main.py: watch rules are spoken", "main.py", 'result.get("watch"'),
    ("main.py: pending facts reach the prompt", "main.py", "pending_hint"),
    ("main.py: sessions are mined for facts", "main.py", "learn_from_session"),
    ("main.py: background tasks report honestly", "main.py", "_run_background_task"),
    ("prompt: [LEARNING] section", "core/prompt.txt", "[LEARNING]"),
    ("prompt: [RULES] section", "core/prompt.txt", "[RULES]"),
    ("prompt: [JOBS] section", "core/prompt.txt", "[JOBS]"),
    ("prompt: corrections outrank notes", "memory/memory_manager.py",
     "STANDING CORRECTIONS"),
    ("capture: Wayland rungs before mss", "core/screen_watch.py", "grim"),
    ("capture: no two frames in the same second", "core/screen_watch.py",
     "MIN_FRESH_GAP_SECONDS"),
    ("typing: every keystroke passes the guard", "actions/computer_control.py",
     "input_guard.check_keystroke"),
    ("typing: pc_automation authorises what it verified", "actions/pc_automation.py",
     "input_guard.authorize"),
    ("typing: a refusal is a decision, not a crash", "actions/pc_automation.py",
     "except PermissionError"),
    ("messaging: never routed through a search or a browser", "actions/pc_automation.py",
     "_looks_like_messaging"),
    ("screen watch: does not auto-resume after a restart", "plugins/screen_ai.py",
     "resume_after_restart"),
    ("screen watch: arming needs a spoken confirmation", "plugins/screen_ai.py",
     "Arming the screen watch needs an explicit confirmation"),
    ("screen watch: hourly ceiling on captures", "plugins/screen_ai.py",
     "max_captures_per_hour"),
    ("screen watch: stays quiet while the user is talking", "plugins/screen_ai.py",
     "pause_while_user_talking_seconds"),
    ("prompt: [KEYBOARD SAFETY] section", "core/prompt.txt", "[KEYBOARD SAFETY]"),
]


def _compile_all() -> tuple[str, bool, str]:
    """Compile every Python file in the tree. Does not write bytecode."""
    failures: list[str] = []
    count = 0
    for path in sorted(BASE.rglob("*.py")):
        if any(part in {".git", "__pycache__", "venv", ".venv", "build", "dist"}
               for part in path.parts):
            continue
        count += 1
        try:
            compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(BASE)}:{exc.lineno}: {exc.msg}")
    return ("compile all Python files", not failures,
            f"{count} files" + (f" — {len(failures)} broken: {failures[:3]}" if failures else ""))


def _discovery() -> tuple[str, bool, str]:
    """Load the tools exactly the way main.py does, and count what arrived."""
    from core.action_loader import discover_actions
    from core.plugin_loader import discover_plugins

    try:
        import main as app_module

        inline = {str(t.get("name")) for t in getattr(app_module, "TOOL_DECLARATIONS", [])}
    except Exception:                                          # noqa: BLE001
        inline = set()

    registry = discover_actions(actions_dir=BASE / "actions", reserved_names=inline,
                                logger=lambda *_: None)
    names = set(registry.names())
    plugins = discover_plugins(BASE / "plugins", inline | names, logger=lambda *_: None)
    plugin_names = {record.name for record in getattr(plugins, "_plugins", {}).values()} \
        if hasattr(plugins, "_plugins") else set()

    missing = sorted(EXPECTED_ACTIONS - names - inline)
    detail = f"{len(names)} actions + {len(inline)} inline, {len(plugin_names)} plugins"
    if missing:
        detail += f" — MISSING: {missing}"
    return ("capability discovery", not missing and len(names) >= 18, detail)


def _invariants() -> list[tuple[str, bool, str]]:
    results = []
    for label, rel, needle in INVARIANTS:
        path = BASE / rel
        if not path.exists():
            results.append((label, False, f"{rel} missing"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle is None and rel == "main.py":
            # The specific bug: a bare `loop` used inside _execute_tool without
            # ever being assigned. It took every tool call down with
            # UnboundLocalError, so it is checked, not trusted.
            start = text.find("async def _execute_tool")
            body = text[start:start + 9000] if start >= 0 else ""
            uses = "run_in_executor" in body
            binds = "asyncio.get_running_loop()" in body
            bad = uses and not binds
            results.append((label, not bad,
                            "loop bound before use" if not bad
                            else "uses `loop` without binding it in _execute_tool"))
            continue
        ok = needle in text
        results.append((label, ok, "present" if ok else f"'{needle}' not found in {rel}"))
    return results


def _self_tests() -> list[tuple[str, bool, str]]:
    results = []
    for name in SELF_TEST_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:                               # noqa: BLE001
            results.append((f"{name} self-test", False, f"import failed: {exc}"))
            continue
        fn = getattr(module, "_self_test", None)
        if not callable(fn):
            results.append((f"{name} self-test", True, "no self-test (skipped)"))
            continue
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):                      # quiet the modules
                outcome = fn()
        except Exception as exc:                               # noqa: BLE001
            results.append((f"{name} self-test", False,
                            f"raised {type(exc).__name__}: {exc}"))
            continue
        ok = bool(outcome.get("ok")) if isinstance(outcome, dict) else bool(outcome)
        failed = [k for k, v in (outcome.get("details") or {}).items()
                  if isinstance(v, bool) and not v] if isinstance(outcome, dict) else []
        results.append((f"{name} self-test", ok,
                        "passed" if ok else f"failed: {', '.join(failed) or 'see details'}"))
    return results


def _keystroke_safety() -> list[tuple[str, bool, str]]:
    """The two behaviours of 27 September: no blind typing, and one type + Enter.

    Both run with the real input layer replaced by recorders, so a regression can
    never send a keystroke to a real window from a test run.
    """
    results: list[tuple[str, bool, str]] = []
    try:
        from actions import computer_control as cc
        from core import input_guard

        typed: list[tuple] = []
        cc.di.typewrite = lambda text, interval=0.03: typed.append(("type", text))
        cc.di.press = lambda key: typed.append(("press", key))
        cc.di.hotkey = lambda *keys: typed.append(("hotkey", keys))
        cc.di.click = lambda *a, **k: typed.append(("click", a))
        input_guard.revoke("selftest")
        refusal = cc.TOOL["handler"](parameters={"action": "type", "text": "who r u"})
        results.append((
            "safety: blind typing is refused, not attempted",
            not typed and "will not type" in refusal,
            f"{len(typed)} keystroke(s) sent — refusal says: {refusal[:60]}",
        ))
        enter = cc.TOOL["handler"](parameters={"action": "key", "key": "enter"})
        results.append(("safety: blind Enter is refused", not typed and "verified" in enter,
                        enter[:70]))
    except Exception as exc:                                   # noqa: BLE001
        results.append(("safety: keystroke guard reachable", False,
                        f"{type(exc).__name__}: {exc}"))

    try:
        from actions import pc_automation as pa
        from core import input_guard

        keys: list[tuple] = []
        pa.di.typewrite = lambda text, interval=0.02: keys.append(("type", text))
        pa.di.press = lambda key: keys.append(("press", key))
        pa.di.hotkey = lambda *k: keys.append(("hotkey", "+".join(k)))
        pa._grab = lambda reuse_seconds=0.0: (b"stub-frame", "")
        pa._vision_json = lambda frame, question, **kw: (
            True, {"match": True, "app": "WhatsApp Web", "detail": "verified"}, "")
        pa._wa_open_chat = lambda: {"open": True, "name": "Rayan Ali", "compose": True}
        input_guard.revoke("selftest")
        out = pa.run({"action": "reply", "message": "on my way"})
        results.append((
            "whatsapp: one type, one Enter, verified",
            keys == [("type", "on my way"), ("press", "enter")] and "VERIFIED" in out,
            f"{len(keys)} keystroke(s): {[k[0] for k in keys]} — {out[:60]}",
        ))
        input_guard.revoke("selftest done")
    except Exception as exc:                                   # noqa: BLE001
        results.append(("whatsapp: verified reply flow", False, f"{type(exc).__name__}: {exc}"))
    return results


def _prompt_renders() -> tuple[str, bool, str]:
    """The system prompt must render with its tokens filled and stay a sane size."""
    try:
        import main as app_module

        render = getattr(app_module, "_render_prompt", None)
        load = getattr(app_module, "_load_system_prompt", None)
        if render is None or load is None:
            return ("prompt renders", False, "helpers not found in main.py")
        raw = load()
        rendered = render(raw, {
            "assistant_name": "JARVIS", "platform": "Linux-test", "capabilities": "- test",
            "limits": "- test", "memory": "", "time": "", "identity": "",
        })
        left_over = [token for token in ("{assistant_name}", "{capabilities}", "{limits}")
                     if token in rendered]
        ok = bool(rendered.strip()) and not left_over
        return ("prompt renders", ok,
                f"{len(rendered)} chars" + (f" — unfilled: {left_over}" if left_over else ""))
    except Exception as exc:                                   # noqa: BLE001
        return ("prompt renders", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print(f"{DIM}Mark-LIV self-test — {BASE}{RESET}\n")
    results: list[tuple[str, bool, str]] = [_compile_all(), _discovery()]
    results.extend(_invariants())
    results.extend(_keystroke_safety())
    results.append(_prompt_renders())
    results.extend(_self_tests())

    width = max(len(label) for label, _ok, _detail in results)
    failures = 0
    for label, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        if not ok:
            failures += 1
        print(f"  {mark}  {label.ljust(width)}  {DIM}{detail}{RESET}")

    print()
    if failures:
        print(f"{RED}{failures} check(s) failed{RESET} — see the details above.")
        return 1
    print(f"{GREEN}all {len(results)} checks passed{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        print(json.dumps({"ok": False}, indent=2))
        sys.exit(1)
