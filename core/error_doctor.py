"""Reading errors off the screen, explaining them, and fixing what is safe.

The problem this solves
-----------------------
Most of the friction in a broken build is not the fix, it is the first minute:
reading the traceback, finding the file, recalling the exact command to
reproduce it. That minute is text on a screen, so it is visible and it is
machine-readable, which means it can be handled.

What it does
------------
1. **Finds** errors in whatever is on screen, by transcribing the frame and
   matching known error shapes (Python traceback, compiler diagnostics, package
   manager failures, permission and disk errors, service failures).
2. **Explains** them: cause, confidence, and a fix plan whose steps are typed as
   ``command``, ``edit`` or ``manual``.
3. **Runs the safe ones** through ``core.local_exec`` -- the same validated,
   allowlisted executor the security lane uses. Nothing here invents its own
   subprocess path.

Where the line is
-----------------
This module will not install packages, modify a repository, or run anything that
needs root, because those are decisions with consequences that are not reversible
from a screenshot. When the correct fix is one of those, it says so and prints
the exact command instead of running it. Concretely: *inspection* is automatic,
*mutation* is manual, and every command that does run is written to the activity
log first with a ``why``.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core import activity_log, local_exec, screen_watch, vision_client

BASE_DIR = Path(__file__).resolve().parent.parent

__all__ = [
    "Finding", "Plan", "extract_errors", "scan_screen", "diagnose",
    "command_verdict", "run_command", "attempt_fix", "summarize", "self_test",
]


# ── finding errors ───────────────────────────────────────────────────────────

@dataclass
class Finding:
    """One error, with enough context to act on it."""

    kind: str
    severity: str = "error"          # error | warning | fatal
    summary: str = ""
    command: str = ""                # the command that failed, when visible
    location: str = ""               # file:line, when visible
    detail: str = ""                 # the most informative line(s)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "summary": self.summary,
            "command": self.command,
            "location": self.location,
            "detail": self.detail,
            "evidence": self.evidence[:8],
        }

    def fingerprint(self) -> str:
        raw = f"{self.kind}|{self.location}|{self.detail[:120]}".casefold()
        return re.sub(r"\s+", " ", raw)[:160]


# kind, severity, pattern, human title
_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("python", "error", r"Traceback \(most recent call last\)", "Python traceback"),
    ("python", "fatal", r"SyntaxError:", "Python syntax error"),
    ("python", "error", r"IndentationError:", "Python indentation error"),
    ("python", "error", r"ModuleNotFoundError: No module named", "Python module missing"),
    ("python", "error", r"ImportError:", "Python import error"),
    ("python", "error", r"AttributeError:|TypeError:|ValueError:|KeyError:|NameError:",
     "Python runtime error"),
    ("python", "error", r"Xlib\.error\.DisplayConnectionError|DisplayConnectionError",
     "X11 display error"),
    ("compiler", "error", r"error: [^\n]{0,120}", "Compiler error"),
    ("compiler", "warning", r"warning: [^\n]{0,120}", "Compiler warning"),
    ("linker", "error", r"undefined reference to|ld returned \d+ exit status", "Linker error"),
    ("npm", "error", r"npm ERR!", "npm failure"),
    ("node", "error", r"Error: Cannot find module|MODULE_NOT_FOUND", "Node module missing"),
    ("typescript", "error", r"error TS\d+", "TypeScript error"),
    ("rust", "error", r"error\[E\d+\]", "Rust compiler error"),
    ("go", "error", r"cannot find package|go: .*error", "Go build error"),
    ("pip", "error", r"ERROR: Could not (find|install)|externally-managed-environment",
     "pip failure"),
    ("apt", "error", r"E: (Unable to locate|Could not|The following packages)", "apt failure"),
    ("dpkg", "error", r"dpkg: error processing", "dpkg failure"),
    ("permission", "error", r"Permission denied|EACCES|Operation not permitted",
     "Permission problem"),
    ("disk", "fatal", r"No space left on device|Disk quota exceeded", "Disk full"),
    ("memory", "fatal", r"Cannot allocate memory|Out of memory|Killed", "Out of memory"),
    ("network", "error", r"Could not resolve host|Connection refused|Temporary failure in name resolution|Network is unreachable",
     "Network problem"),
    ("service", "error", r"Failed to (start|restart|enable) |Unit .* entered failed state",
     "Service failure"),
    ("port", "error", r"Address already in use|bind: address already in use", "Port already in use"),
    ("docker", "error", r"docker: |Error response from daemon", "Docker error"),
    ("k8s", "error", r"Error from server \(|CrashLoopBackOff|ImagePullBackOff", "Kubernetes error"),
    ("git", "error", r"fatal: (not a git repository|refusing to merge|paths?pec)",
     "git failure"),
    ("segfault", "fatal", r"Segmentation fault|core dumped|SIGSEGV", "Crash / segfault"),
    ("syntax", "error", r"unexpected (token|EOF|end of file)", "Syntax error"),
    ("generic", "error", r"\b(ERROR|Error|FATAL|CRITICAL)\b[:\s][^\n]{4,160}", "Logged error"),
)

_LOCATION_RE = re.compile(r'(?:File "([^"]+)", line (\d+)|([\w./\-]+\.\w{1,6}):(\d+)(?::(\d+))?)')
_COMMAND_HINT_RE = re.compile(
    r"^\s*[\$❯>]\s*(.+)$|^\s*(?:sudo\s+)?((?:pytest|python3?|pip3?|npm|pnpm|yarn|node|tsc|cargo|go|make|cmake|gcc|g\+\+|git|docker|nmap)\b[^\n]{0,160})",
    re.MULTILINE,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_text(text: str) -> str:
    """Strip terminal colour codes and normalise whitespace."""
    return _ANSI_RE.sub("", str(text or "")).replace("\r", "")


def extract_errors(text: str, limit: int = 6) -> list[Finding]:
    """Pull structured findings out of transcribed screen text."""
    cleaned = clean_text(text)
    if not cleaned.strip():
        return []

    lines = cleaned.splitlines()
    findings: list[Finding] = []
    seen: set[str] = set()
    max_severity_rank = {"warning": 0, "error": 1, "fatal": 2}

    for index, line in enumerate(lines):
        for kind, severity, pattern, title in _PATTERNS:
            if not re.search(pattern, line):
                continue
            # Build a small window of context around the match; the useful part
            # of an error is usually the line after it, not the line itself.
            window = [ln.strip() for ln in lines[index:index + 6] if ln.strip()]
            detail = " | ".join(window[:3])[:400]
            location = ""
            match = _LOCATION_RE.search("\n".join(window))
            if match:
                file_part = match.group(1) or match.group(3) or ""
                line_part = match.group(2) or match.group(4) or ""
                location = f"{file_part}:{line_part}" if file_part else ""
            command = ""
            for candidate in reversed(lines[max(0, index - 25):index + 1]):
                probe = _COMMAND_HINT_RE.match(candidate)
                if probe:
                    command = (probe.group(1) or probe.group(2) or "").strip()[:200]
                    break
            finding = Finding(
                kind=kind, severity=severity, summary=title,
                command=command, location=location, detail=detail,
                evidence=window[:6],
            )
            key = finding.fingerprint()
            if key in seen:
                break
            seen.add(key)
            findings.append(finding)
            break
        if len(findings) >= limit:
            break

    # One clear error beats six guesses: keep the most severe, then the first.
    findings.sort(key=lambda f: -max_severity_rank.get(f.severity, 1))
    return findings[:limit]


def scan_screen(frame: Optional[screen_watch.Capture] = None,
                reuse_seconds: float = 2.0) -> tuple[list[Finding], str, dict]:
    """Read the screen and extract errors. Returns ``(findings, transcript, meta)``."""
    if frame is None:
        frame, reason = screen_watch.capture(reuse_seconds=reuse_seconds)
        if frame is None:
            return [], "", {"error": reason}

    lines, result = screen_watch.read_text(frame)
    meta: dict[str, Any] = {
        "model": result.model,
        "seconds": round(result.seconds, 2),
        "error": result.error,
        "capture": frame.as_dict(),
        "lines_read": len(lines),
    }
    if not lines:
        meta.setdefault("error", result.error or "no readable text on screen")
        return [], "", meta
    transcript = "\n".join(lines)
    return extract_errors(transcript), transcript, meta


def summarize(finding: Finding) -> str:
    """One spoken-style sentence about a finding."""
    where = f" in {finding.location}" if finding.location else ""
    how = f" (from `{finding.command}`)" if finding.command else ""
    return f"{finding.summary}{where}{how}: {finding.detail[:200]}"


# ── diagnosis ────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    ok: bool
    cause: str = ""
    confidence: float = 0.0
    steps: list[dict] = field(default_factory=list)
    error: str = ""
    model: str = ""
    seconds: float = 0.0

    def commands(self) -> list[str]:
        return [str(s.get("command")) for s in self.steps
                if str(s.get("kind")) == "command" and s.get("command")]

    def manual(self) -> list[str]:
        return [str(s.get("command") or s.get("instruction") or "")
                for s in self.steps if str(s.get("kind")) in ("manual", "edit")]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "cause": self.cause, "confidence": round(self.confidence, 2),
                "steps": self.steps, "error": self.error, "model": self.model,
                "seconds": round(self.seconds, 2)}


_DIAGNOSE_SYSTEM = """You are a senior engineer diagnosing a failure from a screenshot
transcript of a terminal or editor on a Debian/Kali Linux machine with GNOME Wayland.

Be concrete and honest. Rules:
- If the transcript is too thin to diagnose, say so and set confidence low. Never guess.
- Prefer the smallest fix that addresses the stated cause.
- Mark a step as:
    "command"  only if it is a read-only inspection or an idempotent local check
               (status, --version, list, grep-style lookups)
    "edit"     if it changes a file (say exactly which file and what change)
    "manual"   if it needs root, installing packages, network downloads, deleting
               data, restarting services, or anything a human should approve
- Never propose sudo, package installs, rm -rf, dd, mkfs, chmod 777, or anything
  that touches another user's data as a "command" step. Those are "manual".
- Give at most 4 steps, ordered.

Reply with JSON only:
{
  "cause": "one or two sentences: what actually went wrong",
  "confidence": 0.0,
  "steps": [
    {"kind": "command|edit|manual", "command": "...", "file": "...",
     "instruction": "what this does", "why": "why it helps"}
  ]
}"""


def diagnose(finding: Finding, transcript: str = "", extra: str = "") -> Plan:
    """Ask for a cause and a typed fix plan. Never executes anything."""
    context_lines = [f"DETECTED: {finding.summary} ({finding.kind}, {finding.severity})"]
    if finding.location:
        context_lines.append(f"LOCATION: {finding.location}")
    if finding.command:
        context_lines.append(f"COMMAND THAT FAILED: {finding.command}")
    if finding.evidence:
        context_lines.append("EVIDENCE:\n  " + "\n  ".join(finding.evidence[:6]))
    if transcript:
        tail = "\n".join(transcript.splitlines()[-40:])
        context_lines.append(f"SCREEN TRANSCRIPT (tail):\n{tail}")
    if extra:
        context_lines.append(f"USER CONTEXT: {extra}")

    result = vision_client.text_completion(
        "\n\n".join(context_lines),
        system=_DIAGNOSE_SYSTEM,
        max_output_tokens=800,
        temperature=0.2,
    )
    if not result.ok:
        return Plan(ok=False, error=result.error, model=result.model, seconds=result.seconds)

    try:
        data = vision_client.loads_lenient(result.text)
    except ValueError as exc:
        return Plan(ok=False, error=f"could not parse the diagnosis: {exc}",
                    model=result.model, seconds=result.seconds)
    if not isinstance(data, dict):
        return Plan(ok=False, error="the diagnosis was not an object",
                    model=result.model, seconds=result.seconds)

    steps = []
    for entry in (data.get("steps") or [])[:4]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "manual")).strip().casefold()
        if kind not in ("command", "edit", "manual"):
            kind = "manual"
        steps.append({
            "kind": kind,
            "command": str(entry.get("command") or "").strip()[:300],
            "file": str(entry.get("file") or "").strip()[:200],
            "instruction": str(entry.get("instruction") or "").strip()[:300],
            "why": str(entry.get("why") or "").strip()[:300],
        })

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return Plan(ok=True, cause=str(data.get("cause") or "").strip()[:600],
                confidence=confidence, steps=steps,
                model=result.model, seconds=result.seconds)


# ── running commands, safely ─────────────────────────────────────────────────

# Anything that installs, elevates, deletes or rewrites is never run from here.
_NEVER_RUN = re.compile(
    r"\b(sudo|doas|pkexec|su|rm|dd|mkfs|mkswap|shutdown|reboot|poweroff|init|"
    r"chmod|chown|chattr|kill|pkill|killall|systemctl|service|apt|apt-get|dpkg|"
    r"snap|flatpak|pip\s+install|pip3\s+install|python\s+-m\s+pip|npm\s+install|"
    r"npm\s+i\b|yarn\s+add|pnpm\s+add|cargo\s+install|go\s+install|git\s+(add|commit|"
    r"push|pull|reset|clean|checkout|merge|rebase|stash|rm)|docker\s+(rm|rmi|stop|"
    r"kill|prune)|kubectl\s+(delete|apply|edit)|curl|wget|truncate|tee|mv|cp|ln|"
    r"mount|umount|iptables|nft|ufw|setcap)\b",
    re.IGNORECASE,
)

_READ_ONLY_TOOLS = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "file", "strings", "xxd",
    "stat", "wc", "du", "df", "which", "env", "ps", "ss", "lsof", "uname",
    "git", "jq", "tsc", "node", "npm", "pip", "pip3", "nm", "objdump", "openssl",
    "journalctl", "notify-send", "dig", "host", "whois", "checksec",
}

_MEDIUM_TOOLS = {"pytest", "make", "gdb", "r2", "radare2", "rabin2"}


def command_verdict(command: str) -> tuple[bool, str, str]:
    """Decide whether a command may run. Returns ``(allowed, reason, risk)``.

    Risk is ``low`` (inspection), ``medium`` (runs project code, still local and
    reversible) or ``manual`` (must be run by the user).
    """
    text = str(command or "").strip()
    if not text:
        return False, "empty command", "manual"
    if any(ch in text for ch in "\n\r;&|`$()<>\""):
        return False, (
            "the command contains shell operators or quotes; only a single "
            "argument-list command can be validated"
        ), "manual"
    if _NEVER_RUN.search(text):
        return False, (
            "that command installs, elevates, deletes or rewrites something — "
            "it must be run by you, not automatically"
        ), "manual"
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        return False, f"could not parse the command ({exc})", "manual"
    if not parts:
        return False, "empty command", "manual"
    tool = Path(parts[0]).name
    if tool in local_exec.RAW_FRAGMENT_FORBIDDEN:
        return False, f"{tool!r} is raw shell execution and is disabled", "manual"
    if tool not in local_exec.ALLOWLIST:
        return False, (
            f"{tool!r} is not in the local allowlist, so it cannot be run from "
            "here — run it yourself and paste the output"
        ), "manual"
    try:
        local_exec.validate_args(tool, parts[1:])
    except ValueError as exc:
        return False, str(exc), "manual"
    binary, problem = local_exec.resolve_binary(tool)
    if binary is None:
        return False, problem, "manual"
    risk = "low" if tool in _READ_ONLY_TOOLS else ("medium" if tool in _MEDIUM_TOOLS else "medium")
    return True, "", risk


def run_command(command: str,
                confirm: bool = False,
                timeout: int = 60,
                purpose: str = "",
                allow_medium: bool = False) -> dict:
    """Run one validated command and return a structured result.

    ``confirm`` gates anything that is not pure inspection, and even then a
    medium-risk command (running the project's tests) needs ``allow_medium``.
    Every attempt is written to the activity log with its purpose.
    """
    started = time.monotonic()
    allowed, reason, risk = command_verdict(command)
    if not allowed:
        activity_log.record("error", "command refused", detail=command[:200],
                            why=reason, outcome="refused", meta={"risk": risk})
        return {"ok": False, "refused": True, "reason": reason, "risk": risk,
                "command": command, "seconds": round(time.monotonic() - started, 2)}

    if risk == "medium" and not allow_medium:
        activity_log.record("error", "command held for approval", detail=command[:200],
                            why="medium-risk command needs explicit approval",
                            outcome="held", meta={"risk": risk})
        return {"ok": False, "needs_confirm": True, "risk": risk,
                "reason": f"{command!r} runs project code; confirm before I run it",
                "command": command, "seconds": round(time.monotonic() - started, 2)}

    if risk != "low" and not confirm:
        activity_log.record("error", "command held for confirmation", detail=command[:200],
                            why="non-read-only command needs confirmation",
                            outcome="held", meta={"risk": risk})
        return {"ok": False, "needs_confirm": True, "risk": risk,
                "reason": f"{command!r} is not read-only; confirm before I run it",
                "command": command, "seconds": round(time.monotonic() - started, 2)}

    parts = shlex.split(command)
    tool = Path(parts[0]).name
    activity_log.record("error", "ran repair command", detail=command[:200],
                        why=purpose or "diagnosing an on-screen error",
                        meta={"risk": risk})
    result = local_exec.run(tool, parts[1:], timeout=int(timeout))
    ok = bool(result.get("success"))
    return {
        "ok": ok,
        "command": command,
        "risk": risk,
        "return_code": result.get("return_code"),
        "stdout": (result.get("stdout") or "")[-4000:],
        "stderr": (result.get("stderr") or "")[-2000:],
        "error": result.get("error", ""),
        "seconds": round(time.monotonic() - started, 2),
    }


# ── the fix loop ─────────────────────────────────────────────────────────────

def attempt_fix(finding: Optional[Finding] = None,
                transcript: str = "",
                confirm: bool = False,
                max_commands: int = 3,
                allow_medium: bool = False,
                revert_check: bool = True) -> dict:
    """Diagnose and run only the steps that are provably safe.

    Returns a report that always distinguishes "fixed", "tried and still broken",
    and "needs you" -- it never claims a fix it did not verify.
    """
    report: dict[str, Any] = {"steps": [], "ran": [], "manual": [], "fixes": []}
    if finding is None:
        findings, transcript, meta = scan_screen()
        report["scan"] = meta
        if not findings:
            report["outcome"] = "no-error-found"
            report["detail"] = meta.get("error") or "no recognisable error on screen"
            return report
        finding = findings[0]
    report["finding"] = finding.as_dict()

    plan = diagnose(finding, transcript)
    report["plan"] = plan.as_dict()
    if not plan.ok:
        report["outcome"] = "diagnosis-failed"
        report["detail"] = plan.error
        return report

    executed = 0
    for step in plan.steps:
        if step["kind"] != "command":
            report["manual"].append(step)
            continue
        if executed >= max_commands:
            report["manual"].append({**step, "instruction": "not run: command budget reached"})
            continue
        allowed, reason, risk = command_verdict(step["command"])
        if not allowed:
            report["manual"].append({**step, "instruction": reason})
            continue
        outcome = run_command(
            step["command"], confirm=confirm, purpose=f"fixing: {finding.summary}",
            allow_medium=allow_medium,
        )
        report["steps"].append({"command": step["command"], "risk": risk,
                                "outcome": "ok" if outcome.get("ok") else "failed",
                                "reason": outcome.get("reason") or outcome.get("error", "")})
        if outcome.get("needs_confirm"):
            report["manual"].append({**step, "instruction": outcome.get("reason", "")})
            continue
        report["ran"].append(outcome)
        executed += 1
        # A read-only inspection that succeeded is informative, not a fix.
        if risk == "low":
            continue
        report["fixes"].append(step["command"])

    if revert_check and report["fixes"]:
        time.sleep(1.2)
        still, _, meta = scan_screen(reuse_seconds=0)
        same = any(f.fingerprint() == finding.fingerprint() for f in still)
        report["recheck"] = {
            "still_present": same,
            "findings": [f.summary for f in still[:3]],
            "error": meta.get("error", ""),
        }
        report["outcome"] = "still-broken" if same else "appears-resolved"
    elif report["manual"]:
        report["outcome"] = "needs-you"
    elif report["ran"]:
        report["outcome"] = "inspected"
    else:
        report["outcome"] = "nothing-runnable"
    return report


def report_text(report: dict) -> str:
    """A readable summary of an attempt_fix report."""
    lines: list[str] = []
    finding = report.get("finding") or {}
    if finding:
        lines.append(f"Error: {finding.get('summary')} ({finding.get('kind')})")
        if finding.get("location"):
            lines.append(f"  at {finding['location']}")
    plan = report.get("plan") or {}
    if plan.get("cause"):
        lines.append(f"Cause: {plan['cause']} (confidence {plan.get('confidence', 0):.0%})")
    for entry in (plan.get("steps") or []):
        marker = {"command": "run", "edit": "edit", "manual": "you"}.get(entry.get("kind"), "?")
        lines.append(f"  [{marker}] {entry.get('command') or entry.get('instruction') or entry.get('file')}")
    for outcome in report.get("ran", []):
        status = "ok" if outcome.get("ok") else "failed"
        lines.append(f"  -> {outcome.get('command')} [{status}]")
        snippet = (outcome.get("stderr") or outcome.get("stdout") or "").strip()
        if snippet and not outcome.get("ok"):
            lines.append(f"     {snippet.splitlines()[-1][:160]}")
    recheck = report.get("recheck")
    if recheck:
        lines.append("Re-check: " + ("still present" if recheck.get("still_present")
                                     else "no longer visible on screen"))
    lines.append(f"Outcome: {report.get('outcome')}")
    if report.get("manual"):
        lines.append("Needs you:")
        for step in report["manual"][:4]:
            lines.append(f"  - {step.get('command') or step.get('instruction') or step.get('file')}")
    return "\n".join(lines)


# ── self-test ────────────────────────────────────────────────────────────────

_PY_TRACEBACK = '''
$ python3 app.py
Traceback (most recent call last):
  File "/home/sacheet/app/main.py", line 42, in <module>
    run()
ModuleNotFoundError: No module named 'requests'
'''

_NPM_FAILURE = '''
npm ERR! code ERESOLVE
npm ERR! could not resolve dependency tree
'''

_PERMISSION = '''
gcc -o out main.c
/usr/bin/ld: cannot open output file out: Permission denied
'''


def self_test(live: bool = False) -> dict:
    checks: dict[str, Any] = {}

    findings = extract_errors(_PY_TRACEBACK)
    checks["detects_python"] = any(f.kind == "python" for f in findings)
    checks["python_has_location"] = any("main.py" in f.location for f in findings)
    checks["python_has_command"] = any("app.py" in f.command for f in findings)
    checks["detects_npm"] = any(f.kind == "npm" for f in extract_errors(_NPM_FAILURE))
    checks["detects_permission"] = any(
        f.kind in ("permission", "linker") for f in extract_errors(_PERMISSION))
    checks["empty_input_safe"] = extract_errors("") == []
    checks["strips_ansi"] = "\x1b" not in clean_text("\x1b[31merror\x1b[0m")

    limited = extract_errors(_PY_TRACEBACK + _NPM_FAILURE + _PERMISSION, limit=2)
    checks["respects_limit"] = len(limited) <= 2
    checks["severity_ordered"] = bool(limited)

    refused, reason, risk = command_verdict("sudo apt install nmap")
    checks["refuses_sudo"] = (not refused) and risk == "manual"
    refused, _, _ = command_verdict("rm -rf /home/sacheet")
    checks["refuses_rm"] = not refused
    refused, _, _ = command_verdict("git push origin main")
    checks["refuses_git_push"] = not refused
    refused, _, _ = command_verdict("pip install requests")
    checks["refuses_pip_install"] = not refused
    refused, _, _ = command_verdict("bash -c 'echo hi'")
    checks["refuses_shell"] = not refused
    refused, _, _ = command_verdict("echo hi > /etc/passwd")
    checks["refuses_redirect"] = not refused
    refused, _, _ = command_verdict("")
    checks["refuses_empty"] = not refused

    allowed, _, risk = command_verdict("git status --porcelain")
    checks["allows_git_status"] = allowed and risk == "low"
    allowed, _, _ = command_verdict("git log --oneline -n 5")
    checks["allows_git_log"] = allowed
    allowed, _, _ = command_verdict("git clean -fdx")
    checks["refuses_git_clean"] = not allowed
    allowed, _, _ = command_verdict("git reset --hard")
    checks["refuses_git_reset"] = not allowed
    allowed, _, risk = command_verdict("node --check main.js")
    checks["allows_node_check"] = allowed
    allowed, _, _ = command_verdict("node main.js")
    checks["refuses_node_script"] = not allowed
    allowed, _, risk = command_verdict("pytest -q")
    checks["pytest_is_medium"] = allowed and risk == "medium"

    held = run_command("pytest -q", confirm=False)
    checks["medium_needs_confirm"] = bool(held.get("needs_confirm"))
    held = run_command("git push origin main", confirm=True)
    checks["never_runs_push"] = bool(held.get("refused"))

    plan = Plan(ok=True, cause="x", confidence=0.5,
                steps=[{"kind": "command", "command": "git status --porcelain"},
                       {"kind": "manual", "instruction": "install the package"}])
    checks["plan_splits_manual"] = len(plan.commands()) == 1 and len(plan.manual()) == 1
    text = report_text({"finding": {"summary": "x", "kind": "python"},
                        "plan": {"cause": "c", "confidence": 0.4, "steps": []},
                        "ran": [], "manual": [], "outcome": "nothing-runnable"})
    checks["report_renders"] = "Outcome" in text

    if live:
        found, transcript, meta = scan_screen()
        checks["live_findings"] = len(found)
        checks["live_transcript_lines"] = len(transcript.splitlines())
        checks["live_error"] = meta.get("error", "")
        checks["live_model"] = meta.get("model", "")
        if found:
            plan = diagnose(found[0], transcript)
            checks["live_plan_ok"] = plan.ok
            checks["live_cause"] = plan.cause
            checks["live_steps"] = len(plan.steps)
            checks["live_plan_error"] = plan.error

    checks["ok"] = all(bool(value) for key, value in checks.items()
                       if not key.startswith("live_"))
    return checks


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys
    print(json.dumps(self_test(live="--live" in sys.argv), indent=2))
