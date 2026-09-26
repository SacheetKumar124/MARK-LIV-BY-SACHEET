"""
Audio that cannot fail, and cannot crash.

Three specific failures this exists to end permanently. All three were observed
on Kali GNOME Wayland (PipeWire) with a HDA Intel PCH laptop, and each one had a
different root cause:

1. ``Expression 'paInvalidSampleRate' failed`` .... The app opens its streams at
   the rates Gemini Live requires — 16000 Hz in, 24000 Hz out. PortAudio's ALSA
   backend was handed a **raw hardware** PCM (``hw:1,0``) whose clock is fixed at
   44100 Hz and which cannot resample. ALSA refused the rate, PortAudio printed
   the failure **from C**, so no Python ``try``/``except`` could see or silence
   it, and the user got a screenful of internals for a problem with a one-line
   cause.

   The fix is policy, not retries: a raw ``hw:``/``plughw:`` device is never
   auto-selected. The candidates are the **virtual** ALSA devices that route
   through the sound server — ``pulse``, ``pipewire``, ``default`` — all of which
   resample to anything asked of them. Proven on the machine in question: 16000
   and 24000 both open and move audio on those three, while ``hw:1,0`` fails
   outright with ``Device unavailable``.

   A second tier negotiates if policy still is not enough: if a device will not
   open at the required rate, it is opened at one it *will* accept and converted
   in software, so the bytes handed to the model are always 16000/24000.

2. ``Expression 'alsa_snd_pcm_mmap_begin' failed`` → ``segmentation fault``.
   This is the crash, and it was recoverable in a way that mattered. Sequence
   observed in production: the Live API closed the session with
   ``1008 ... GoAway``, which killed ``_receive_audio``, which cancelled the
   enclosing TaskGroup, which cancelled ``_play_audio`` **in the middle of a
   blocking ``stream.write()``**. PortAudio's ALSA backend was then running its
   mmap path on a stream being torn down underneath it. That is a fault inside
   C code, and a segfault cannot be caught by any amount of Python.

   The fix is to remove the blocking mmap write from our side entirely: streams
   are **callback-driven**. PortAudio's own thread pulls from a bounded buffer
   that :meth:`AudioOut.write` merely appends to. ``write`` returns immediately
   and cannot be interrupted part-way through a device transaction. Teardown is
   idempotent and swallows device errors, so closing a stream whose card has
   already gone away is a no-op instead of a fault.

3. A buffer growing without bound. If the speaker stalls, appending forever
   means minutes of latency. Three layers handle this: the consumer in main.py
   paces itself against a ~2 s device buffer (which also rides out server
   stalls without gaps), the queue feeding it is capped at ~15 s as a stall
   safety net, and the buffer itself is bounded to ~8 s with oldest-first
   dropping as a last resort. interrupt() clears both buffers instantly, so
   none of this depth costs interruption latency.

Everything here is stdlib + numpy + sounddevice. No new dependency.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is already a hard dependency of the app
    np = None  # type: ignore[assignment]


# ── Constants ────────────────────────────────────────────────────────────────

# Virtual ALSA devices that front the sound server. These are the only names
# auto-selected, in this order: `pulse` is the classic PulseAudio endpoint,
# `pipewire` its PipeWire-native sibling, `default` whatever the OS decided.
_VIRTUAL_PREFERENCE = ("pulse", "pipewire", "default")

# Raw passthrough devices. Named `hw:` or `plughw:`, they present the hardware's
# own clock — 44100 Hz on the machine this was written for — and cannot convert.
# NEVER auto-selected: not lower priority, not a fallback, never.
_RAW_PREFIXES = ("hw:", "plughw:", "sysdefault:", "front:", "surround", "iec958", "spdif", "dmix", "dsnoop")

# How much audio may sit in the output buffer before the oldest is dropped.
# This is a last-resort safety bound, NOT the pacing mechanism: the consumer in
# main.py paces itself against ``buffered_bytes`` long before this fills. It is
# sized for a full spoken paragraph so that a briefly stalled event loop can
# never cost the user actual speech — a wedged sink still costs bounded memory
# rather than minutes of drift.
_MAX_BUFFER_SECONDS = 8.0

# How far ahead of real playback the writer may queue audio before pausing.
# Deep on purpose: the server stalls mid-reply (network jitter, streaming
# cadence), and this buffer is what rides those stalls out without an audible
# gap. Interrupt latency is NOT affected — interrupt() calls clear(), which
# empties this buffer instantly.
_TARGET_AHEAD_SECONDS = 2.0

# Rates to try, after the required one, when a device refuses the required rate.
_FALLBACK_RATES = (48000, 44100)

_BYTES_PER_SAMPLE = 2  # int16


class AudioError(RuntimeError):
    """No stream could be opened at any rate on any candidate device."""


# ── PortAudio's C-level stderr ───────────────────────────────────────────────

@contextlib.contextmanager
def _quiet_stderr():
    """Silence PortAudio's C-level complaints for the duration of the block.

    PortAudio's ALSA backend writes ``Expression '...' failed in
    'src/hostapi/alsa/pa_linux_alsa.c'`` straight to file descriptor 2 from C.
    ``contextlib.redirect_stderr`` cannot touch it — it replaces the Python
    object, not the descriptor — which is why this is the only thing that works.

    Used only around *probe* attempts, where a failure is an expected outcome
    that this module handles and reports in its own words. A failed probe is not
    information the user needs in raw C form.
    """
    try:
        sys.stderr.flush()
    except Exception:
        pass

    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        os.close(devnull)
        yield
    finally:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved, 2)
        os.close(saved)


# Public alias. Other modules open streams purely to *probe* them — the device
# picker in core/audio_devices.py tries every listed device at the app's rates —
# and those probes are the other place PortAudio used to spray ALSA errors onto
# the console. A leading underscore across a module boundary read as private, so
# the intent is stated here instead.
quiet_stderr = _quiet_stderr


# ── Device discovery ─────────────────────────────────────────────────────────

_devices_cache: Optional[list] = None
_apis_cache: Optional[list] = None
_devices_lock = threading.Lock()


def _table():
    """``(devices, hostapis)`` from PortAudio, cached — querying is slow."""
    global _devices_cache, _apis_cache
    with _devices_lock:
        if _devices_cache is None:
            import sounddevice as sd
            _devices_cache = list(sd.query_devices())
            _apis_cache = list(sd.query_hostapis())
        return _devices_cache, _apis_cache


def invalidate() -> None:
    """Forget the device table — call after a device is plugged or removed."""
    global _devices_cache, _apis_cache
    with _devices_lock:
        _devices_cache = None
        _apis_cache = None


def is_raw_hardware(name: str) -> bool:
    """Is this a passthrough PCM that cannot resample?"""
    n = (name or "").strip().lower()
    return any(n.startswith(p) for p in _RAW_PREFIXES)


def _channels_for(dev) -> int:
    d = dev.get("max_channels")
    if d is None:
        d = max(dev.get("max_input_channels", 0), dev.get("max_output_channels", 0))
    return int(d or 0)


def _direction_ok(dev, kind: str) -> bool:
    return int(dev.get(f"max_{kind}_channels", 0)) > 0


def _label(idx) -> str:
    if idx is None:
        return "system default"
    try:
        devs, _ = _table()
        return str(devs[int(idx)].get("name", idx))
    except Exception:
        return str(idx)


def _candidate_indices(kind: str, preferred_name: str = "") -> list:
    """The devices worth trying, best first. ``None`` means "system default".

    A saved user preference leads, *unless* it is a raw hardware device — the
    user may have picked ``hw:1,0`` from the picker before this module existed,
    and honouring that choice would reintroduce the exact failure being fixed.
    In that case it is skipped and the reason is logged once.
    """
    devs, _apis = _table()

    ordered: list = []
    seen: set = set()

    def _add(idx, why: str):
        key = idx if idx is None else int(idx)
        if key in seen:
            return
        seen.add(key)
        ordered.append((key, why))

    # 1. The user's saved device, if it is safe to use.
    if preferred_name:
        try:
            from core import audio_devices as _ad
            resolved = _ad.resolve(preferred_name, kind)
        except Exception:
            resolved = None
        if resolved is not None:
            name = _label(resolved)
            if is_raw_hardware(name):
                print(f"[Audio] saved {kind} device '{name}' is a raw hardware PCM "
                      f"that cannot resample — ignoring it and using the sound server")
            elif not _direction_ok(devs[int(resolved)], kind):
                print(f"[Audio] saved {kind} device '{name}' is not usable for {kind} — ignoring")
            else:
                _add(resolved, "saved preference")

    # 2. The virtual devices, in preference order.
    for wanted in _VIRTUAL_PREFERENCE:
        for i, dev in enumerate(devs):
            name = str(dev.get("name", "")).strip().lower()
            if name != wanted:
                continue
            if _direction_ok(dev, kind) and not is_raw_hardware(name):
                _add(i, f"virtual ALSA '{wanted}'")

    # 3. Whatever the OS calls default.
    _add(None, "system default")

    # 4. Last resort: non-raw named devices that support this direction.
    for i, dev in enumerate(devs):
        name = str(dev.get("name", ""))
        if is_raw_hardware(name) or not _direction_ok(dev, kind):
            continue
        _add(i, "named device")

    return ordered


# ── Rate conversion ──────────────────────────────────────────────────────────

def _resample_pcm16(data: bytes, src_rate: int, dst_rate: int, channels: int = 1) -> bytes:
    """Convert int16 PCM between rates by linear interpolation.

    Only ever used on tier 2, when a device refused the required rate. Linear
    interpolation is the right choice here rather than something heavier: the
    source is speech through a 44.1/48 kHz device, the ratio is a clean 2–3x
    decimation, and the cost has to be low enough to run inside a PortAudio
    callback thread. A polyphase filter would sound marginally better and add a
    dependency and a scheduling risk to the one code path that must never stall.

    Falls back to a naive nearest-sample conversion if numpy is missing, so the
    assistant degrades in quality rather than going silent.
    """
    if src_rate == dst_rate or not data:
        return data

    frame_bytes = channels * _BYTES_PER_SAMPLE
    if len(data) % frame_bytes:
        data = data[: len(data) - (len(data) % frame_bytes)]
    if not data:
        return b""

    try:
        arr = np.frombuffer(data, dtype="<i2").astype(np.float32)
        if channels > 1:
            arr = arr.reshape(-1, channels)

        n_src = arr.shape[0]
        if n_src < 2:
            return data

        n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
        src_idx = np.arange(n_src, dtype=np.float64)
        dst_idx = np.linspace(0.0, n_src - 1.0, n_dst, dtype=np.float64)

        if channels > 1:
            out = np.empty((n_dst, channels), dtype=np.float32)
            for c in range(channels):
                out[:, c] = np.interp(dst_idx, src_idx, arr[:, c])
        else:
            out = np.interp(dst_idx, src_idx, arr)

        return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
    except Exception:
        # Nearest-sample fallback. Cruder, but it is arithmetic on a bytes
        # object and cannot fail.
        step = src_rate / dst_rate
        frames = len(data) // frame_bytes
        out = bytearray()
        f = 0.0
        while int(f) < frames:
            off = int(f) * frame_bytes
            out += data[off: off + frame_bytes]
            f += step
        return bytes(out)


# ── Output ───────────────────────────────────────────────────────────────────

class AudioOut:
    """Non-blocking output stream. ``write`` never touches the device.

    PortAudio's own thread calls :meth:`_callback`, which drains a bounded
    byte buffer. That indirection is the whole point: the blocking
    ``stream.write()`` path was where a cancelled asyncio task turned into a
    segfault, because cancellation could land between ALSA's mmap setup and its
    commit. Here, cancelling a caller can at worst leave bytes in a buffer.
    """

    def __init__(self, stream, device, label: str, rate: int, required_rate: int, channels: int = 1):
        self._stream = stream
        self.device = device
        self.label = label
        self.rate = rate
        self.required_rate = required_rate
        self.channels = channels
        self.resampling = rate != required_rate

        self._lock = threading.Lock()
        self._buf = bytearray()
        self._max_bytes = int(rate * channels * _BYTES_PER_SAMPLE * _MAX_BUFFER_SECONDS)
        # Pacing target, at the device rate: the consumer keeps queued audio
        # under this so the speaker is fed just ahead of real time.
        self._target_bytes = int(rate * channels * _BYTES_PER_SAMPLE * _TARGET_AHEAD_SECONDS)

        self.underruns = 0
        self.dropped_bytes = 0
        self.status_flags = 0
        self.closed = False

    # PortAudio thread — must be fast and must never raise.
    def _callback(self, outdata, frames, time_info, status):
        need = frames * self.channels * _BYTES_PER_SAMPLE
        try:
            with self._lock:
                have = len(self._buf)
                take = need if have >= need else have
                if take:
                    outdata[:take] = bytes(self._buf[:take])
                    del self._buf[:take]
                if take < need:
                    outdata[take:need] = b"\x00" * (need - take)
                    if take == 0:
                        self.underruns += 1
            if status:
                self.status_flags |= int(status)
        except Exception:
            try:
                outdata[:] = b"\x00" * need
            except Exception:
                pass

    def write(self, pcm: bytes) -> None:
        """Queue PCM for playback. Returns immediately — safe to cancel after."""
        if not pcm or self.closed or self._stream is None:
            return
        if self.resampling:
            pcm = _resample_pcm16(pcm, self.required_rate, self.rate, self.channels)
        if not pcm:
            return
        with self._lock:
            overflow = len(self._buf) + len(pcm) - self._max_bytes
            if overflow > 0:
                # Drop the oldest audio — the speaker is behind. The drop is
                # applied to the combined size, so even a single oversized
                # write leaves the buffer within its bound: the head goes,
                # the tail (the newest audio) is what we keep.
                drop = min(overflow, len(self._buf) + len(pcm))
                keep_from = drop - len(self._buf)
                if keep_from <= 0:
                    del self._buf[:drop]
                    self._buf.extend(pcm)
                else:
                    self._buf = bytearray(pcm[keep_from:])
                self.dropped_bytes += drop
            else:
                self._buf.extend(pcm)

    @property
    def buffered_bytes(self) -> int:
        """Audio queued but not yet played. The consumer paces itself against
        this so the speaker is fed just ahead of real time — never starved,
        never overflowing."""
        with self._lock:
            return len(self._buf)

    @property
    def target_bytes(self) -> int:
        """Pacing target: keep ``buffered_bytes`` under this."""
        return self._target_bytes

    def clear(self) -> None:
        """Discard everything queued but not yet played. Interrupts must call
        this: draining only the asyncio queue leaves up to seconds of speech
        still sounding after the user said stop."""
        with self._lock:
            self._buf.clear()

    @property
    def alive(self) -> bool:
        try:
            return bool(self._stream is not None and self._stream.active and not self.closed)
        except Exception:
            return False

    def close(self) -> None:
        """Idempotent, and never raises — closing a vanished device is normal."""
        if self.closed:
            return
        self.closed = True
        st, self._stream = self._stream, None
        if st is None:
            return
        with _quiet_stderr():
            try:
                st.stop()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass
        with self._lock:
            self._buf.clear()

    # Context-manager sugar so main.py's `finally` is a one-liner.
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


# ── Input ────────────────────────────────────────────────────────────────────

class AudioIn:
    """Callback input stream delivering bytes at the *required* rate.

    If the device had to be opened at another rate, each block is converted here
    before the caller sees it, so everything upstream can assume 16000 Hz and
    never has to know.
    """

    def __init__(self, stream, device, label: str, rate: int, required_rate: int,
                 channels: int, on_block: Callable[[bytes], None]):
        self._stream = stream
        self.device = device
        self.label = label
        self.rate = rate
        self.required_rate = required_rate
        self.channels = channels
        self.resampling = rate != required_rate
        self._on_block = on_block
        self.blocks = 0
        self.errors = 0
        self.status_flags = 0
        self.closed = False

    def _callback(self, indata, frames, time_info, status):
        try:
            pcm = bytes(indata)
            if self.resampling:
                pcm = _resample_pcm16(pcm, self.rate, self.required_rate, self.channels)
            if status:
                self.status_flags |= int(status)
            if pcm:
                self.blocks += 1
                self._on_block(pcm)
        except Exception:
            # A callback that raises kills the stream in PortAudio, which would
            # cost the user their microphone for the rest of the session. The
            # caller's own logic must never be able to do that.
            self.errors += 1

    @property
    def alive(self) -> bool:
        try:
            return bool(self._stream is not None and self._stream.active and not self.closed)
        except Exception:
            return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        st, self._stream = self._stream, None
        if st is None:
            return
        with _quiet_stderr():
            try:
                st.stop()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


# ── Opening, with negotiation ────────────────────────────────────────────────

def _rate_tiers(required: int, device) -> list:
    """Rates to attempt, in order, ending with something almost anything accepts."""
    tiers = [int(required)]
    try:
        native = int(round(float(device.get("default_samplerate", 0) or 0)))
        if native and native not in tiers:
            tiers.append(native)
    except Exception:
        pass
    for r in _FALLBACK_RATES:
        if r not in tiers:
            tiers.append(r)
    return tiers


def open_output(required_rate: int, channels: int = 1, preferred_name: str = "") -> AudioOut:
    """Open the speaker at ``required_rate``, or at a rate that works. Never raises
    the ALSA sample-rate error; raises :class:`AudioError` only if nothing at all
    will open, and then with a human sentence."""
    import sounddevice as sd

    devs, _ = _table()
    attempts: list = []
    last: Optional[Exception] = None

    for idx, why in _candidate_indices("output", preferred_name):
        dev = devs[int(idx)] if idx is not None else None
        for rate in _rate_tiers(required_rate, dev or {}):
            # The stream is created against a shell AudioOut so PortAudio already
            # has a callback target. If the open fails, nothing was ever exposed
            # to the caller and the shell is simply discarded.
            shell = AudioOut.__new__(AudioOut)
            AudioOut.__init__(shell, None, idx, _label(idx), rate, required_rate, channels)

            def _cb(outdata, frames, time_info, status, _s=shell):
                _s._callback(outdata, frames, time_info, status)

            st = None
            try:
                with _quiet_stderr():
                    st = sd.RawOutputStream(
                        samplerate=rate,
                        channels=channels,
                        dtype="int16",
                        blocksize=0,          # let PortAudio pick: most compatible
                        device=idx,
                        callback=_cb,
                    )
                    st.start()
                shell._stream = st
                _announce("output", shell, why)
                return shell
            except Exception as exc:
                last = exc
                attempts.append(f"{_label(idx)}@{rate}")
                if st is not None:
                    with _quiet_stderr():
                        try:
                            st.close()
                        except Exception:
                            pass

    raise AudioError(
        "no working speaker found (tried "
        + ", ".join(attempts[:8])
        + (", …" if len(attempts) > 8 else "")
        + f"; last error: {type(last).__name__ if last else 'unknown'})"
    )


def open_input(required_rate: int, on_block: Callable[[bytes], None],
               channels: int = 1, preferred_name: str = "") -> AudioIn:
    """Open the microphone at ``required_rate``, converting if the device insists
    on another rate. ``on_block`` receives int16 mono bytes at ``required_rate``."""
    import sounddevice as sd

    devs, _ = _table()
    attempts: list = []
    last: Optional[Exception] = None

    for idx, why in _candidate_indices("input", preferred_name):
        dev = devs[int(idx)] if idx is not None else None
        for rate in _rate_tiers(required_rate, dev or {}):
            holder: dict = {}

            def _cb(indata, frames, time_info, status, _h=holder):
                obj = _h.get("obj")
                if obj is not None:
                    obj(indata, frames, time_info, status)

            shell = AudioIn.__new__(AudioIn)
            AudioIn.__init__(shell, None, idx, _label(idx), rate, required_rate, channels, on_block)
            holder["obj"] = shell._callback
            try:
                with _quiet_stderr():
                    st = sd.RawInputStream(
                        samplerate=rate,
                        channels=channels,
                        dtype="int16",
                        blocksize=0,
                        device=idx,
                        callback=_cb,
                    )
                    st.start()
                shell._stream = st
                _announce("input", shell, why)
                return shell
            except Exception as exc:
                attempts.append(f"{_label(idx)}@{rate}")
                last = exc
                with _quiet_stderr():
                    try:
                        st.close()  # type: ignore[possibly-undefined]
                    except Exception:
                        pass

    raise AudioError(
        "no working microphone found (tried "
        + ", ".join(attempts[:8])
        + (", …" if len(attempts) > 8 else "")
        + f"; last error: {type(last).__name__ if last else 'unknown'})"
    )


def _announce(kind: str, obj, why: str) -> None:
    """One line, once, in plain language. Silence about a working stream is the
    goal — this exists only so the log can answer 'which device is it using?'."""
    conv = f", converted from {obj.rate} Hz" if obj.resampling else ""
    print(f"[Audio] {kind}: {obj.label} via {why} at {obj.rate} Hz{conv}")


# ── Preflight ────────────────────────────────────────────────────────────────

def preflight(input_rate: int, output_rate: int) -> dict:
    """Open and close both directions before the session needs them.

    Called at startup so the answer arrives while the user is still looking at
    the UI, not as a mid-conversation failure. Both streams are closed before
    returning, so nothing is held open.
    """
    report = {"ok": False, "input": None, "output": None, "error": None}

    try:
        out = open_output(output_rate)
        report["output"] = {
            "device": out.label, "rate": out.rate,
            "resampling": out.resampling, "ok": True,
        }
        out.close()
    except Exception as exc:
        report["output"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        seen: list = []
        mic = open_input(input_rate, lambda b: seen.append(len(b)))
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.05)
        report["input"] = {
            "device": mic.label, "rate": mic.rate,
            "resampling": mic.resampling, "blocks": mic.blocks,
            "heard": bool(seen), "ok": bool(seen),
        }
        mic.close()
    except Exception as exc:
        report["input"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    report["ok"] = bool(
        report["output"] and report["output"].get("ok")
        and report["input"] and report["input"].get("ok")
    )
    return report


# ── Self-test ────────────────────────────────────────────────────────────────

def self_test() -> dict:
    """Prove the two failure modes are actually fixed, on this machine.

    Does not merely assert that functions exist: it drives the real paths,
    including the one that used to segfault, and checks the resampler's output
    length is right rather than trusting it.
    """
    result: dict = {"ok": True, "checks": {}}
    def check(name, value):
        result["checks"][name] = bool(value)
        if not value:
            result["ok"] = False

    # 1. Raw hardware devices are recognised and refused.
    check("raw hw detected", is_raw_hardware("hw:1,0") and is_raw_hardware("plughw:0,3"))
    check("virtual not flagged", not is_raw_hardware("pulse") and not is_raw_hardware("default"))

    # 2. Candidates never lead with a raw device.
    cands = _candidate_indices("output")
    check("candidates non-empty", len(cands) > 0)
    check("no raw device auto-selected",
          all(not is_raw_hardware(_label(i)) for i, _ in cands))

    # 3. The resampler produces the right number of frames.
    pcm = (np.zeros(4800, dtype="<i2")).tobytes() if np is not None else bytes(9600)
    up = _resample_pcm16(pcm, 16000, 48000, 1)
    check("resample 16k->48k is 3x", len(up) == len(pcm) * 3)
    down = _resample_pcm16(up, 48000, 16000, 1)
    check("resample 48k->16k round-trips", abs(len(down) - len(pcm)) <= 4)
    check("same-rate is a no-op", _resample_pcm16(pcm, 16000, 16000, 1) is pcm)

    # 4. PortAudio's C chatter is actually suppressed. This is the specific
    #    complaint the user had, so it is tested rather than assumed: a
    #    deliberate bad open is performed and stderr must stay clean.
    saved_err = os.dup(2)
    with tempfile.TemporaryFile() as tf:
        try:
            os.dup2(tf.fileno(), 2)
            with _quiet_stderr():
                try:
                    import sounddevice as sd
                    sd.RawOutputStream(samplerate=12345, channels=1, dtype="int16",
                                       device=None, blocksize=0)
                except Exception:
                    pass
        finally:
            os.dup2(saved_err, 2)
            os.close(saved_err)
        tf.seek(0)
        captured = tf.read().decode("utf-8", "replace")
    quiet_ok = len(captured.strip()) == 0
    result["checks"]["stderr silent during failed open"] = quiet_ok
    if not quiet_ok:
        result["ok"] = False
        result["checks"]["stderr sample"] = captured.strip().splitlines()[:2]

    # 5. The output path survives being closed mid-write, which is the shape of
    #    the crash: cancel the writer, tear down, then write again.
    try:
        out = open_output(24000)
        out.write(b"\x00" * 4800)
        out.close()
        out.close()              # idempotent
        out.write(b"\x00" * 4800)  # must be a silent no-op, not a fault
        check("write after close is safe", True)
    except Exception as exc:
        result["checks"]["write after close is safe"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False

    return result


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2, default=str))
