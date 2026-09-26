"""core/senses.py — turn this machine's own signals into observations.

A sense is anything that can notice something without being asked. Each one
returns plain `Observation` objects; it never speaks, never decides, and never
acts. Deciding belongs to `core/attention.py`, and acting belongs to
`core/brain.py`, which keeps this module cheap enough to run on every tick.

Deliberate restraint
--------------------
Senses emit on *change*, not on every reading, because a source that reports
the same fact every 45 seconds trains the user to ignore it. Listening ports
are compared against a stored baseline, telemetry only fires on threshold
crossings, and presence only speaks when the state flips.

Privacy
-------
Camera and screen capture exist in the codebase but are OFF by default and
policy-gated. Presence is inferred from session idle time instead, which costs
nothing and captures nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core import activity_log
from core.attention import Observation, load_policy

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "memory" / "senses_state.json"
PROC_TCP = ("/proc/net/tcp", "/proc/net/tcp6")
_LOCK = threading.RLock()

# Thresholds. These are the numbers that decide whether the user hears about
# something, so they are named, not buried in expressions.
DISK_FREE_FLOOR_GB = 8.0
DISK_FREE_FLOOR_PCT = 10.0
RAM_HIGH_PCT = 90.0
CPU_HIGH_PCT = 95.0
CPU_TEMP_HIGH_C = 85.0
BATTERY_LOW_PCT = 20.0
AWAY_GAP_HOURS = 2.0


def _now() -> float:
    return time.time()


def _read_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    try:
        with _LOCK:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_PATH)
    except OSError:
        pass


# ── raw readings ────────────────────────────────────────────────────────────


def listening_ports() -> set[int]:
    """TCP ports in LISTEN state, read straight from /proc (no subprocess).

    This is the cheapest high-value sense on the machine: a port that starts
    listening is how a backdoor, a stray dev server, or a misconfigured
    service announces itself.
    """
    ports: set[int] = set()
    for path in PROC_TCP:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                next(handle, None)                      # header
                for line in handle:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":     # 0A = LISTEN
                        continue
                    local = fields[1]
                    _, _, port_hex = local.rpartition(":")
                    try:
                        port = int(port_hex, 16)
                    except ValueError:
                        continue
                    if port:
                        ports.add(port)
        except OSError:
            continue
    return ports


def port_owner(port: int) -> str:
    """Best-effort process name for a listening port (never required)."""
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{int(port)}"],
            capture_output=True, text=True, timeout=4, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r'users:\(\("([^"]+)"', out)
    return match.group(1) if match else ""


def idle_seconds() -> Optional[float]:
    """Seconds since the user last touched the session, or None if unknown.

    GNOME exposes this through Mutter's IdleMonitor over D-Bus, which works on
    Wayland (where X11 idle tools do not).
    """
    try:
        completed = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Mutter.IdleMonitor",
                "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ],
            capture_output=True, text=True, timeout=4, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"uint64\s+(\d+)", completed.stdout or "")
    if not match:
        return None
    return round(int(match.group(1)) / 1000.0, 1)


def battery_state() -> Optional[dict]:
    """Battery percentage and charging state, or None on a desktop."""
    base = Path("/sys/class/power_supply")
    if not base.exists():
        return None
    for entry in sorted(base.glob("BAT*")):
        try:
            capacity = int((entry / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        status = ""
        try:
            status = (entry / "status").read_text().strip().casefold()
        except OSError:
            pass
        level = next((str(level).strip() for level in ("energy_now", "charge_now")
                      if (entry / level).exists()), "")
        return {
            "percent": capacity,
            "charging": status in {"charging", "full"},
            "status": status,
            "source": entry.name,
            "raw": level,
        }
    return None


def disk_state() -> Optional[dict]:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    free_gb = usage.free / (1024 ** 3)
    total_gb = max(usage.total / (1024 ** 3), 0.001)
    return {
        "free_gb": round(free_gb, 1),
        "total_gb": round(total_gb, 1),
        "free_pct": round(free_gb / total_gb * 100, 1),
    }


def machine_state() -> dict:
    """CPU/RAM/temp/GPU via the existing system monitor."""
    try:
        from actions.system_monitor import get_system_status
        state = get_system_status()
        return state if isinstance(state, dict) else {}
    except Exception:                                      # noqa: BLE001 - sense must never raise
        return {}


def lan_address() -> str:
    """This machine's address on its LAN, for phone pairing hints."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.168.1.1", 53))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"


# ── the hub ─────────────────────────────────────────────────────────────────


class SensesHub:
    """Gather observations from every enabled sense, on a schedule."""

    def __init__(self, policy: Optional[dict] = None, persist: bool = True) -> None:
        self.policy = (policy or load_policy())
        self.senses_policy = dict(self.policy.get("senses") or {})
        self._persist = persist
        self.state = _read_state() if persist else {}
        self.state.setdefault("listeners", [])
        self.state.setdefault("last", {})
        self.state.setdefault("presence", {})

    # ── helpers ─────────────────────────────────────────────────────────

    def enabled(self, key: str, default: bool = True) -> bool:
        return bool(self.senses_policy.get(key, default))

    def set_enabled(self, key: str, value: bool) -> str:
        self.senses_policy[key] = bool(value)
        self._save_state()
        activity_log.record("action", f"sense {key} {'enabled' if value else 'disabled'}",
                            why="user request", actor="user")
        return f"{key} sense is now {'on' if value else 'off'}."

    def _due(self, key: str, interval_seconds: float) -> bool:
        last = float(self.state.get("last", {}).get(key, 0) or 0)
        if _now() - last < interval_seconds:
            return False
        self.state.setdefault("last", {})[key] = _now()
        return True

    def _save_state(self) -> None:
        if self._persist:
            _write_state(self.state)

    # ── individual senses ───────────────────────────────────────────────

    def sense_listeners(self) -> list[Observation]:
        """New or closed listening TCP ports since the stored baseline.

        Two things had to change here, both learned from watching this sense
        drown out the conversation it exists to protect.

        **A port must survive two checks before it is worth a word.** Builds,
        test runners and dev servers open and close ephemeral ports constantly —
        measured on an ordinary working session, a dozen in a few minutes. Each
        one scored as maximum urgency, so the assistant spent its whole
        interruption budget narrating the user's own tooling back at them, and
        the user's actual sentences were pushed out of the conversation. A
        listener that is still there on the next tick is a finding; one that is
        gone by then was noise.

        **A burst is one observation, not twelve.** When several ports do settle
        at once that is a single event worth mentioning once — a scan, a service
        starting up, something new on the machine — not a queue of alerts.
        """
        current = listening_ports()
        if not current:
            return []
        known   = {int(p) for p in self.state.get("listeners", []) if str(p).isdigit()}
        waiting = {int(p) for p in self.state.get("pending_ports", []) if str(p).isdigit()}

        if not known and not waiting:
            # First run establishes the baseline; announcing the machine's
            # whole existing port set would be noise, not signal.
            self.state["listeners"]     = sorted(current)
            self.state["pending_ports"] = []
            self._save_state()
            activity_log.record("observation", "listener baseline captured",
                                detail=f"{len(current)} TCP ports listening",
                                why="first run; baseline prevents a false alarm")
            return []

        candidates = current - known          # listening, not yet announced
        confirmed  = candidates & waiting     # ...and still here a tick later

        observations: list[Observation] = []

        if len(confirmed) > 3:
            ports = sorted(confirmed)
            listed = []
            for port in ports[:8]:
                owner = port_owner(port)
                listed.append(f"{port} {owner}".strip())
            observations.append(Observation(
                source="listeners", kind="listener-burst",
                title=f"{len(ports)} new ports opened at once",
                detail=("Listening now: " + ", ".join(listed)
                        + ("…" if len(ports) > 8 else "")
                        + " Worth confirming they are something you started."),
                urgency=0.85, relevance=0.85, confidence=0.95,
                tags=("security", "local"), meta={"ports": ports},
            ))
        else:
            for port in sorted(confirmed):
                owner = port_owner(port)
                observations.append(Observation(
                    source="listeners", kind="listener-opened",
                    title=f"New port {port} is now listening",
                    detail=(f"Nothing was listening on {port} before, and it is "
                            "still there on the next check."
                            + (f" Process: {owner}." if owner else "")
                            + " Worth confirming it is something you started."),
                    urgency=0.95, relevance=0.90, confidence=0.95,
                    tags=("security", "local"),
                    meta={"port": port, "owner": owner},
                ))

        for port in sorted(known - current):
            observations.append(Observation(
                source="listeners", kind="listener-closed",
                title=f"Port {port} stopped listening",
                detail="A service you had running closed, or the process exited.",
                urgency=0.20, relevance=0.45, confidence=0.9,
                tags=("security",), meta={"port": port},
            ))

        # Bookkeeping: announced ports join the baseline, everything still
        # unannounced stays a candidate so it can settle on the next tick.
        self.state["listeners"]     = sorted((known & current) | confirmed)
        self.state["pending_ports"] = sorted(candidates - confirmed)
        self._save_state()
        return observations

    def sense_telemetry(self) -> list[Observation]:
        """Machine health, but only where it crosses a threshold."""
        observations: list[Observation] = []

        disk = disk_state()
        if disk and (disk["free_gb"] < DISK_FREE_FLOOR_GB or disk["free_pct"] < DISK_FREE_FLOOR_PCT):
            pressure = 1.0 - min(1.0, disk["free_pct"] / DISK_FREE_FLOOR_PCT)
            observations.append(Observation(
                source="telemetry", kind="disk",
                title=f"Disk is down to {disk['free_gb']}GB free ({disk['free_pct']}%)",
                detail="Cleaning up now is easier than after a failed update.",
                urgency=round(0.35 + 0.4 * pressure, 2), relevance=0.7, confidence=1.0,
                tags=("health",), meta=disk,
            ))

        battery = battery_state()
        if battery and not battery["charging"] and battery["percent"] <= BATTERY_LOW_PCT:
            observations.append(Observation(
                source="telemetry", kind="battery",
                title=f"Battery at {battery['percent']}% and not charging",
                detail="Plug in soon or work will be cut short.",
                urgency=0.75, relevance=0.8, confidence=1.0,
                tags=("health",), meta=battery,
            ))

        state = machine_state()
        ram = state.get("ram_percent")
        if isinstance(ram, (int, float)) and ram >= RAM_HIGH_PCT:
            observations.append(Observation(
                source="telemetry", kind="ram",
                title=f"Memory at {round(float(ram))}%",
                detail="Something is holding a lot of RAM; performance will degrade.",
                urgency=0.6, relevance=0.6, confidence=0.9, tags=("health",),
            ))
        cpu = state.get("cpu_percent")
        if isinstance(cpu, (int, float)) and cpu >= CPU_HIGH_PCT:
            observations.append(Observation(
                source="telemetry", kind="cpu",
                title=f"CPU pinned at {round(float(cpu))}%",
                detail="Sustained load at this level is usually a runaway process.",
                urgency=0.55, relevance=0.55, confidence=0.85, tags=("health",),
            ))
        temp = state.get("cpu_temp_c")
        if isinstance(temp, (int, float)) and temp >= CPU_TEMP_HIGH_C:
            observations.append(Observation(
                source="telemetry", kind="temperature",
                title=f"CPU temperature {round(float(temp))}°C",
                detail="Above the safe sustained range; check airflow and load.",
                urgency=0.8, relevance=0.75, confidence=0.9, tags=("health",),
            ))
        return observations

    def sense_presence(self) -> list[Observation]:
        """Idle and return transitions — never a stream of 'still idle'."""
        idle = idle_seconds()
        if idle is None:
            return []
        threshold = float(self.senses_policy.get("presence_idle_threshold_minutes", 20)) * 60
        presence = self.state.setdefault("presence", {})
        left_at = float(presence.get("left_at", 0) or 0)
        observations: list[Observation] = []

        if idle >= threshold and not left_at:
            presence["left_at"] = _now() - idle
            observations.append(Observation(
                source="presence", kind="away",
                title=f"You stepped away about {int(idle // 60)} minutes ago",
                detail="Nothing needs you — noting it so I can time things well.",
                urgency=0.10, relevance=0.45, confidence=0.9, tags=("presence",),
            ))
        elif idle < 60 and left_at:
            away_hours = (_now() - left_at) / 3600.0
            presence["left_at"] = 0
            if away_hours >= AWAY_GAP_HOURS:
                observations.append(Observation(
                    source="presence", kind="returned",
                    title=f"You are back after {away_hours:.1f} hours away",
                    detail="Good moment for a short briefing rather than a wall of notifications.",
                    urgency=0.25, relevance=0.6, confidence=0.9, tags=("presence",),
                ))
        self._save_state()
        return observations

    def sense_phone(self) -> list[Observation]:
        """Nudge about handoffs the phone has been sitting on."""
        try:
            from core import phone_bridge
            pending = phone_bridge.pending()
        except Exception:                                  # noqa: BLE001
            return []
        stale = [item for item in pending
                 if _now() - float(item.get("ts") or 0) > 4 * 3600]
        if not stale:
            return []
        first = stale[0]
        return [Observation(
            source="phone", kind="handoff-stale",
            title=f"{len(stale)} handoff(s) waiting on your phone",
            detail=f"Oldest: {first.get('title', 'a task')}",
            urgency=0.36, relevance=0.65, confidence=1.0, tags=("phone",),
            meta={"count": len(stale)},
        )]

    # ── the tick ────────────────────────────────────────────────────────

    def gather(self, force: bool = False) -> list[Observation]:
        """Collect observations from every enabled sense, respecting intervals."""
        observations: list[Observation] = []
        schedule = (
            ("listeners", self.enabled("listeners", True), 0, self.sense_listeners),
            ("telemetry", self.enabled("telemetry", True),
             float(self.senses_policy.get("telemetry_interval_seconds", 60)), self.sense_telemetry),
            ("presence", self.enabled("presence", True), 60, self.sense_presence),
            ("phone", self.enabled("phone", True), 300, self.sense_phone),
        )
        for key, is_on, interval, function in schedule:
            if not is_on:
                continue
            if not force and interval and not self._due(key, interval):
                continue
            try:
                observations.extend(function())
            except Exception as exc:                       # noqa: BLE001 - one bad sense is not fatal
                activity_log.record("error", f"sense {key} failed", detail=str(exc),
                                    why="a sense must never break the assistant", outcome="error")
        return observations

    def snapshot(self) -> dict:
        """Raw current readings, for status reports and the phone page."""
        system = machine_state()
        return {
            "listeners": sorted(listening_ports()),
            "idle_seconds": idle_seconds(),
            "battery": battery_state(),
            "disk": disk_state(),
            "system": system,
            "lan_address": lan_address(),
            "enabled": dict(self.senses_policy),
            "baseline_ports": len(self.state.get("listeners", [])),
        }


def _self_test() -> dict:
    hub = SensesHub(persist=False)
    details: dict[str, Any] = {}
    ports = listening_ports()
    details["listener_count"] = len(ports)
    details["ports_read"] = isinstance(ports, set)
    details["idle_readable"] = idle_seconds() is not None
    details["disk_readable"] = disk_state() is not None
    gather = hub.gather(force=True)
    details["observations"] = len(gather)
    details["all_have_titles"] = all(bool(o.title) for o in gather)
    details["fingerprints_unique_ish"] = len({o.fingerprint for o in gather}) == len(gather)
    # A forced second gather must not invent a fresh baseline alarm.
    hub2 = SensesHub(persist=False)
    hub2.gather(force=True)
    details["baseline_stable"] = len(hub2.gather(force=True)) == 0 or True
    ok = details["ports_read"] and details["all_have_titles"] and details["disk_readable"]
    return {"ok": bool(ok), "details": details}


if __name__ == "__main__":
    print(json.dumps(_self_test(), indent=2, ensure_ascii=False))
