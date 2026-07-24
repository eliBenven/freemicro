"""The web UI's connection to the physical pad - preview, capture, and manners.

Everything in here exists to serve two features that only make sense with real
hardware attached: **live preview** (pick a colour, the pad changes under your
hand) and **live capture** (press a key, the UI tells you which id it is). Both
are optional. The editor is fully usable with no pad on the desk, and every
entry point below degrades to a `reason` string the browser shows instead of a
dead button.

The one hard rule: **only one process can usefully hold this device.**

macOS opens the Codex Micro *non-exclusively*, so "did the open succeed?" is not
a usable ownership test - two processes can both hold it and will then overwrite
each other's LEDs on every state change, with no way for the user to tell which
one is at fault (``docs/FACTORY-DEFAULTS.md`` §9). So instead of racing for the
handle we look for evidence of another claimant *before* opening:

1. a lock file at ``~/.freemicro/pad.lock`` naming a live pid,
2. another ``freemicro`` process that drives the pad (``run``/``keys``/
   ``watch``/``daemon``),
3. the ChatGPT desktop app, which drives the same LEDs over the same channel.

Any of those and we refuse to open, and say why. That is strictly better than
winning the fight: a config UI that silently stomps the daemon's lighting would
be indistinguishable, from the user's chair, from a bug.

Nothing here ever *performs* a binding. Capture reads ``v.oai.hid`` and reports
the id; it never constructs a :class:`~freemicro.input.bridge.Bridge`, because
identifying a key must not type into the terminal you are identifying it from.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence

from freemicro.config import config_home
from freemicro.device import ENV_NO_DEVICE
from freemicro.device.codex_micro import EVENT_JOYSTICK, EVENT_KEY
from freemicro.device.lighting import ZONE_AGENT_KEYS
from freemicro.padconfig import ENCODER_TICKS, LightingConfig, PadConfig, StateLight
from freemicro.state.engine import AgentState

#: Where a pad-owning FreeMicro process announces itself. Advisory, not a
#: kernel lock: the point is to give cooperating processes (this UI, a future
#: daemon) a way to notice each other rather than to enforce anything.
LOCK_NAME = "pad.lock"

#: How many recent input events the capture buffer keeps. Enough to scroll back
#: through a burst of dial detents, small enough to never matter.
EVENT_BUFFER = 250

#: How long one capture session listens before giving the device back. The pad
#: is shared; holding the run loop open forever because someone left a browser
#: tab open would be rude.
CAPTURE_SECONDS = 120.0

#: Said once, used in both places a stale lock can be noticed.
STALE_NOTICE = (
    "A stale pad lock was left behind by a process that is no longer running. "
    "Reclaimed it - nothing for you to do."
)


def lock_path() -> Path:
    """Path of the advisory pad lock."""
    return config_home() / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def lock_is_held() -> bool:
    """Is somebody *actually* holding the pad lock right now?

    The pid in the file is a hint, not evidence: pids get reused, and a killed
    owner leaves its file behind claiming a pid that now belongs to something
    unrelated. That is how a stale lock used to shut the pad out permanently
    and leave the user to work out that they had to delete a file by hand.

    ``flock`` is the real answer. The owner holds an exclusive advisory lock on
    the file for as long as it is alive, and the kernel drops it the moment the
    process does - crash, ``kill -9``, anything. So if we can take that lock,
    nobody holds it, whatever the file says.
    """
    path = lock_path()
    try:
        handle = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # somebody has it
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)


def read_lock() -> Optional[Dict[str, Any]]:
    """The current lock's contents, or ``None`` if it is absent or stale."""
    path = lock_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    return data


def reclaim_stale_lock() -> bool:
    """Delete a lock file that nobody is holding. Returns True if we did.

    Deliberately silent-and-automatic. The user did not create the stale file
    and cannot reasonably be asked to diagnose it; a message that says "delete
    this path yourself" is a bug report waiting to happen, not an instruction.

    Safe because it is not the only guard: :func:`contention` still refuses if
    a ``freemicro`` process that drives the pad is running, or if the ChatGPT
    app is. An owner that took the lock properly holds an ``flock`` and is
    never reclaimed out from under itself.
    """
    path = lock_path()
    if not path.exists() or lock_is_held():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _pgrep(pattern: str, full: bool = True) -> List[int]:
    args = ["pgrep", "-f", pattern] if full else ["pgrep", "-x", pattern]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    pids = []
    for line in (proc.stdout or "").split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def contention() -> Optional[str]:
    """Legacy one-string answer: why the pad is unusable *at all*.

    Only fatal conflicts count here - see :func:`contention_detail`, which is
    what the UI actually uses.
    """
    detail = contention_detail()
    return detail["input"] or None


def contention_detail() -> Dict[str, Any]:
    """What is contended, capability by capability.

    **Reading input and writing LEDs do not contend the same way**, and
    treating them as one thing was a real bug: the ChatGPT desktop app running
    used to disable key capture entirely, so someone trying FreeMicro for the
    first time - with the vendor app still installed and open, which is the
    normal case - pressed keys, saw nothing, and had no way to know why.

    Verified on hardware: macOS opens this device **non-exclusively**, and two
    processes read every key, the encoder and the stick simultaneously without
    interfering. What genuinely conflicts is *writing* lighting, because both
    processes push to the same channel and the last write wins.

    So:

    * another FreeMicro process holding the lock is **fatal** to both - it has
      the device and we would be fighting a peer for it;
    * the ChatGPT app is **advisory, lighting only** - preview still works, it
      may just be overwritten, and the user gets to decide;
    * input is only ever blocked by the fatal case.

    Returns ``{"input": str, "lighting": str, "fatal": bool, "notice": str}``,
    where the strings are empty when that capability is fine.
    """
    notice = ""
    lock = read_lock()
    if lock is not None and lock.get("pid") != os.getpid():
        if lock_is_held():
            owner = str(lock.get("owner") or "another FreeMicro process")
            reason = (
                f"{owner} (pid {lock.get('pid')}) is using the pad right now. "
                "Stop it and this page will notice within a few seconds."
            )
            return {"input": reason, "lighting": reason, "fatal": True,
                    "notice": notice}
        if reclaim_stale_lock():
            notice = STALE_NOTICE
    elif lock is None and lock_path().exists() and reclaim_stale_lock():
        notice = STALE_NOTICE

    others = [
        pid
        for pid in _pgrep(r"freemicro (run|keys|watch|daemon)")
        if pid != os.getpid()
    ]
    if others:
        reason = (
            "Another freemicro process is driving the pad (pid "
            f"{', '.join(str(p) for p in others)}). Stop it first - only one "
            "process can usefully hold this device."
        )
        return {"input": reason, "lighting": reason, "fatal": True,
                "notice": notice}

    if _pgrep("ChatGPT", full=False):
        return {
            "input": "",
            "lighting": (
                "The ChatGPT desktop app is running and drives these same "
                "LEDs, so a colour you preview here may be overwritten a "
                "moment later. Quit it for reliable colours - everything "
                "else, including pressing keys to identify them, works fine "
                "with it open."
            ),
            "fatal": False,
            "notice": notice,
        }
    return {"input": "", "lighting": "", "fatal": False, "notice": notice}


class PadLink:
    """Owns the web UI's access to the pad, or explains why it has none.

    One instance per server. It opens the process-wide shared handle rather
    than a second one of its own (see :func:`freemicro.device.shared_device`),
    holds the advisory lock only while it actually has the device, and hands it
    back on :meth:`close`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._device: Any = None
        #: The renderer behind the live preview, kept so the pad can be
        #: handed back. See :meth:`preview`.
        self._preview_renderer: Any = None
        self._holding_file_lock = False
        self._lock_handle: Optional[int] = None
        #: Set when a lock left by a dead process was cleared for the user.
        self.notice = ""
        self._events: Deque[Dict[str, Any]] = deque(maxlen=EVENT_BUFFER)
        self._seq = 0
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_error = ""
        self._capture_until = 0.0

    # -- availability -----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Everything the browser needs to decide what to enable.

        Never opens the device as a side effect: this is polled, and probing the
        bus on a timer is fine but *opening* on a timer is not.
        """
        from freemicro.device import device_transport, is_supported, unsupported_reason

        if os.environ.get(ENV_NO_DEVICE):
            return {
                "usable": False,
                "input_ok": False,
                "lighting_ok": False,
                "lighting_warning": "",
                "fatal": False,
                "notice": self.notice,
                "present": False,
                "transport": None,
                "open": self._device is not None,
                "capturing": self.capturing,
                "reason": (
                    f"{ENV_NO_DEVICE} is set, so FreeMicro is pretending no pad "
                    "is attached. Unset it to use live preview and capture."
                ),
            }
        if not is_supported():
            return {
                "usable": False,
                "input_ok": False,
                "lighting_ok": False,
                "lighting_warning": "",
                "fatal": False,
                "notice": self.notice,
                "present": False,
                "transport": None,
                "open": False,
                "capturing": False,
                "reason": unsupported_reason(),
            }
        transport = device_transport()
        if transport is None:
            return {
                "usable": False,
                "input_ok": False,
                "lighting_ok": False,
                "lighting_warning": "",
                "fatal": False,
                "notice": self.notice,
                "present": False,
                "transport": None,
                "open": False,
                "capturing": False,
                "reason": (
                    "No Codex Micro found. Plug in the USB cable or pair over "
                    "Bluetooth - both work - then reload this page."
                ),
            }
        detail = contention_detail()
        if detail["notice"]:
            self.notice = detail["notice"]
        # We already hold the device, so nothing can be contending for it.
        if self._device is not None and not detail["fatal"]:
            detail = dict(detail, input="", lighting=detail["lighting"])
        return {
            # "usable" is the old, coarse question: can we do anything at all.
            # It stays for compatibility, but the UI reads the two below.
            "usable": not detail["input"],
            "input_ok": not detail["input"],
            # Lighting is *advisory* when the vendor app is open: it still
            # works, it may just be overwritten. Blocking it outright is how a
            # first-time user concludes FreeMicro is broken.
            "lighting_ok": not detail["fatal"],
            "lighting_warning": detail["lighting"],
            "fatal": detail["fatal"],
            "present": True,
            "transport": transport,
            "open": self._device is not None,
            "capturing": self.capturing,
            "reason": detail["input"] or "",
            # Something we quietly fixed on the user's behalf, worth one line
            # of reassurance rather than a scary error they must act on.
            "notice": self.notice,
        }

    def _acquire(self, lighting: bool = False) -> Any:
        """Open the pad, or raise :class:`RuntimeError` with the reason.

        ``lighting=True`` asks for the surface that genuinely contends. Even
        then the only hard refusal is a *fatal* conflict - another FreeMicro
        process holding the device. The vendor app being open is a warning the
        caller passes on, not a veto: the write works, it may simply be
        overwritten, and that is the user's call to make.
        """
        if self._device is not None:
            return self._device
        state = self.status()
        blocked = state["reason"] if not lighting else (
            state["reason"] if state["fatal"] else ""
        )
        if blocked or not state["present"]:
            raise RuntimeError(
                blocked or state["reason"] or "the pad is not available"
            )
        from freemicro.device import shared_device

        device = shared_device()
        if device is None:
            raise RuntimeError(
                "Found a Codex Micro but macOS won't let this process open it.\n"
                "System Settings → Privacy & Security → Input Monitoring: add "
                "the terminal you launched FreeMicro from, then restart it."
            )
        self._device = device
        self._write_lock()
        return device

    def _write_lock(self) -> None:
        """Announce ourselves, and hold an ``flock`` for as long as we live.

        The file says *who*; the ``flock`` says *still here*. Keeping the
        descriptor open is the whole point: if this process dies in any way at
        all, the kernel drops the lock, and the next run reclaims the file
        instead of believing a dead pid.
        """
        path = lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return  # advisory only; failing to write it must not break preview
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(handle, 0)
            os.write(
                handle,
                (
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "owner": "freemicro web UI",
                            "since": time.time(),
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        except OSError:
            os.close(handle)
            return
        self._lock_handle = handle
        self._holding_file_lock = True

    def _drop_lock(self) -> None:
        if not self._holding_file_lock:
            return
        handle = self._lock_handle
        self._lock_handle = None
        try:
            lock_path().unlink()
        except OSError:
            pass
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(handle)
        self._holding_file_lock = False

    # -- live preview -----------------------------------------------------

    def preview(
        self,
        color: Any,
        effect: Any,
        brightness: float,
        speed: float,
        zones: Sequence[str] = (ZONE_AGENT_KEYS,),
        method: str = "rgbcfg",
    ) -> Dict[str, Any]:
        """Push one look to the pad immediately.

        The messages are built by :class:`MicroLedsRenderer` from a throwaway
        config, so preview and the real renderer cannot drift: whatever you see
        here is exactly what the state renderer would send for that look. No
        framing, no field names and no zone logic are duplicated in this file.

        ``method`` defaults to ``rgbcfg`` - ``v.oai.rgbcfg`` for the underglow
        and key backlight, ``v.oai.thstatus`` for the six Agent Keys. It used to
        default to ``preview``, which meant live preview did **nothing at all**:
        ``lights.preview`` is in the firmware's method table and answers
        ``{"result": null}``, but produces no visible change on this hardware
        over either transport. A preview that returns success and lights nothing
        is worse than no preview.
        """
        from freemicro.device.lighting import parse_color, parse_effect
        from freemicro.renderers.micro_leds import MicroLedsRenderer

        light = StateLight(
            color=parse_color(color),
            effect=parse_effect(effect),
            brightness=float(brightness),
            speed=float(speed),
        )
        config = PadConfig(
            bindings={},
            lighting=LightingConfig(
                enabled=True,
                zones=tuple(zones) or (ZONE_AGENT_KEYS,),
                method=method,
                # The look goes in as IDLE's colour and is painted with
                # `render`, NOT by sending `messages_for` output straight at the
                # device. Only `MicroLedsRenderer._send` registers a renderer as
                # driving the pad, and that registration is what arms the
                # atexit/SIGTERM guard. Bypassing it lit the pad with nothing
                # tracking it, so killing the web UI stranded the pad glowing
                # with no process left that could turn it off.
                states={AgentState.IDLE: light},
                # Never inherit the user's `on_exit`: a preview that honoured
                # `breath` would leave the pad pulsing after the server exits.
                on_exit="off",
            ),
        )
        with self._lock:
            device = self._acquire(lighting=True)
            # Kept on the instance, not local. A renderer dropped at the end of
            # this call is unreachable by the guard, which holds renderers
            # weakly - so the previous version could not have been rescued by
            # registration alone.
            self._close_preview_renderer()
            renderer = MicroLedsRenderer(device=device, config=config)
            self._preview_renderer = renderer
            # `messages_for`, not `render`: a preview is one colour on every
            # Agent Key, while `render` deliberately paints each key its own
            # project's state. `paint` is what puts it through the guarded send.
            messages = renderer.messages_for(AgentState.IDLE, light=light)
            renderer.paint(messages)
        return {"sent": len(messages), "described": light.describe()}

    def _close_preview_renderer(self) -> None:
        """Hand the pad back from the previous preview, if there was one.

        Idempotent and never raises: this runs on the teardown path, where a
        pad that has already gone away is the normal case rather than an error.
        """
        renderer = self._preview_renderer
        self._preview_renderer = None
        if renderer is None:
            return
        try:
            renderer.close()
        except Exception:  # noqa: BLE001 - teardown must not fail
            pass

    def blank(self, zones: Sequence[str] = (ZONE_AGENT_KEYS,)) -> Dict[str, Any]:
        """Turn the previewed zones off - how you hand the pad back."""
        return self.preview(0, "off", 0.0, 0.0, zones=zones)

    # -- live capture -----------------------------------------------------

    @property
    def capturing(self) -> bool:
        thread = self._capture_thread
        return thread is not None and thread.is_alive()

    def start_capture(self, seconds: float = CAPTURE_SECONDS) -> Dict[str, Any]:
        """Begin listening for ``v.oai.hid`` / ``v.oai.rad`` events.

        Runs the IOKit run loop on a worker thread so the HTTP server stays
        responsive. The loop stops itself after ``seconds`` even if the browser
        goes away, because a tab left open must not hold the device forever.
        """
        if self.capturing:
            return {"capturing": True, "until": self._capture_until}
        with self._lock:
            device = self._acquire()
        self._capture_error = ""
        self._capture_until = time.time() + float(seconds)

        def _run() -> None:
            from freemicro.device import DeviceError

            try:
                device.stream(self._on_message, seconds=float(seconds),
                              tick_interval=0.1)
            except DeviceError as exc:
                self._capture_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a dropped pad is normal
                self._capture_error = str(exc)

        thread = threading.Thread(target=_run, name="freemicro-webui-capture",
                                  daemon=True)
        self._capture_thread = thread
        thread.start()
        return {"capturing": True, "until": self._capture_until}

    def stop_capture(self) -> Dict[str, Any]:
        """Ask the capture loop to return at its next tick."""
        device = self._device
        if device is not None:
            device.request_stop()
        thread = self._capture_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._capture_thread = None
        return {"capturing": self.capturing, "error": self._capture_error}

    def _on_message(self, message: Dict[str, Any]) -> None:
        """Turn one protocol message into a UI event. Runs on the capture thread.

        Reads the same two notification methods the bridge reads, but *only*
        reads them - see the module docstring on why capture never dispatches.
        """
        method = message.get("m") or message.get("method")
        params = message.get("p")
        if params is None:
            params = message.get("params")
        event: Optional[Dict[str, Any]] = None

        if method == EVENT_KEY and isinstance(params, dict):
            key = params.get("k")
            if isinstance(key, str) and key:
                act = params.get("act")
                event = {
                    "kind": "key",
                    "input": key,
                    # Dial detents have no matching release and have been seen
                    # carrying act values other than 1, so a tick always counts
                    # as a press. Real keys keep the press/release distinction.
                    "pressed": True if key in ENCODER_TICKS else act == 1,
                    "act": act,
                }
        elif method == EVENT_JOYSTICK and isinstance(params, dict):
            try:
                event = {
                    "kind": "joystick",
                    "angle": float(params.get("a", 0.0)),
                    "distance": float(params.get("d", 0.0)),
                }
            except (TypeError, ValueError):
                event = None

        if event is None:
            return
        self._seq += 1
        event["seq"] = self._seq
        event["at"] = time.time()
        self._events.append(event)

    def events(self, since: int = 0) -> Dict[str, Any]:
        """Every buffered event newer than ``since``, plus the current sequence."""
        pending = [e for e in list(self._events) if e["seq"] > since]
        return {
            "seq": self._seq,
            "events": pending,
            "capturing": self.capturing,
            "error": self._capture_error,
            "until": self._capture_until,
        }

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Stop capturing, hand the pad back, release the handle, drop the lock."""
        self.stop_capture()
        # Before the handle goes: a preview left on screen must not outlive
        # the server that painted it.
        self._close_preview_renderer()
        if self._device is not None:
            from freemicro.device import close_shared

            close_shared()
            self._device = None
        self._drop_lock()


__all__ = [
    "CAPTURE_SECONDS",
    "EVENT_BUFFER",
    "LOCK_NAME",
    "PadLink",
    "contention",
    "lock_path",
    "read_lock",
]
