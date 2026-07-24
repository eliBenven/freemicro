# The menu bar item

The pad is an **ambient** device. Its whole value is that you see it without
looking at it — so the thing on screen that reports on it should be ambient too.
A window competing with your terminal is the wrong shape; a status item in the
corner of the menu bar is the right one, and it is also the natural home for the
two macOS permissions and the "is the ChatGPT app fighting me for the LEDs"
warning, because those are things you need told, not things you go looking for.

```
freemicro menubar          # once the CLI is wired (see "Wiring required")
python -m freemicro.menubar # works today
```

## What it shows

```
●  Working
───────────────
Connected over Bluetooth
Battery 91%
Firmware v0.4.1
Pad driven by the background daemon (pid 4120, up 380s)
───────────────
Input Monitoring is off
ChatGPT app is running
───────────────
✓ LED Control
  Open Config…
  Run Doctor…
───────────────
  Quit FreeMicro
```

* **The bar glyph** is the resolved agent state across every live session, in
  the state's colour. Shape carries the state as well as colour
  (`○ ◍ ◐ ● ✖`), so it still reads if you cannot tell amber from green.
* **Connection and transport** — USB or Bluetooth, straight from IOKit's
  registry. No open, no permission needed, so it stays honest even when Input
  Monitoring is missing (in which case the row says so).
* **Battery and firmware** come from a `device.status` round trip. See
  "Who owns the pad" below for when that happens and when it does not.
* **The warnings block only exists when something is wrong.** Each row is
  clickable and lands you where the fix is: the two permission rows open the
  exact System Settings pane, the ChatGPT row explains the conflict.
* **LED Control** mirrors `lighting.enabled` in your pad config — the same
  opt-in `freemicro lights --enable` writes. It is the kill switch: FreeMicro
  never seizes the LEDs, and one click hands them back.
* **Open Config** opens the bundled web editor when it is present, otherwise
  reveals `~/.freemicro/keymap.json` in Finder.
* **Run Doctor** runs the same checks as `freemicro doctor` and shows them as a
  readable list, not a terminal transcript.

## Who owns the pad

**Only one process can usefully hold the Codex Micro.** The daemon (or
`freemicro run`) normally has it, and a menu bar that opened the device behind
their back would produce the exact failure this project keeps warning about: two
owners repainting the same LEDs, with nothing in either program's output hinting
at the other.

So the menu bar reads, and almost never opens:

| What | Where it comes from | Contention |
| --- | --- | --- |
| Agent state, session count | `StateStore` — files on disk | none |
| Connected, transport | `device_transport()`, an IOKit registry lookup | none |
| Input Monitoring, Accessibility | `IOHIDCheckAccess`, `AXIsProcessTrusted` | none |
| Pad owner | the daemon's `flock`, *tested* rather than read | none |
| ChatGPT running | `pgrep`, on a 10-second clock | none |
| Battery, firmware | `~/.freemicro/status.json`, or a `device.status` round trip **only when the lock proves nobody has the pad** | avoided |

The one write is `lighting.enabled` in the pad config — deliberately the *file*
and not the hardware, because the file is the only thing the menu bar and
whichever process holds the pad both agree on.

The menu bar takes its own `~/.freemicro/menubar.lock` so a second copy cannot
start. It never takes `pad.lock`, which would make every other FreeMicro command
believe the pad was busy when it is not.

### The status cache

`~/.freemicro/status.json` is the shared drop point for the last successful
`device.status` reply:

```json
{"version": "v0.4.1", "battery": 91, "is_charging": false,
 "updated_at": 1753290000.0, "source": "daemon"}
```

Whoever has the pad is the right process to refresh it. When nobody does, the
menu bar refreshes it itself, at most once a minute, on a background thread.
A reading is shown with its age once it is over five minutes old, and is dropped
entirely when the pad is not currently on the bus — a battery percentage from a
pad that is no longer here is history, not status.

## No dependency, and why

The core of FreeMicro has **no dependencies**, and that earns real things: it
installs on a stock Python 3.9, it runs from a launchd job with no site-packages
to go stale, and nobody compiles anything to light an LED. Adding PyObjC (a
large binary dependency) or `rumps` (which pulls PyObjC in) for one status item
and one menu would spend that for very little.

So `menubar/cocoa.py` is a ~200-line ctypes bridge over the Objective-C runtime:
send a message, make a string, make a colour, create one class at runtime so
menu items have something to target. This is the house style rather than a new
idea — `device/codex_micro.py` already drives IOKit through ctypes, and
`permissions.py` calls `IOHIDCheckAccess` the same way.

Three ctypes rules that file obeys, because breaking any of them crashes the
process rather than raising:

1. Every `objc_msgSend` call declares its signature. `objc_msgSend` is variadic
   in C; without `argtypes` floats go in the wrong registers.
2. Callback trampolines are kept alive forever. An `IMP` collected while the
   runtime still points at it is a hard crash.
3. Nothing raises across the boundary. An exception escaping a callback into
   Objective-C's stack takes the interpreter with it.

`tkinter` is never imported, anywhere, for any reason.

**If you decide a dependency is wanted after all**, it must be an optional extra
(`pip install freemicro[menubar]`), never a core one, and `import freemicro`
must keep working without it.

## Design notes

* **Colour means exactly one thing** — the state dot. Warnings are plain text.
  A menu full of coloured glyphs is a debug panel.
* **Idle has no colour.** The palette's idle is a near-black slate, invisible on
  a dark menu bar; idle is also the state we least want to draw the eye. It is
  drawn in whatever colour macOS uses for secondary text, so it is legible and
  quiet in both appearances.
* **A disconnected pad is never an error.** It drops on sleep, on range, on a
  nudged cable. The row says "Pad not connected" and the item goes on working —
  agent state comes from the hook store, not the hardware, so the dot stays true
  while the pad is asleep. There is deliberately no badge on the bar glyph for a
  drop: it would blink several times a day and mean nothing.
* **Rows appear only when they say something.** No "Battery: unknown". A row
  that is empty half the time teaches people to stop reading it.
* **Nothing slow runs on the main thread.** `pgrep`, a `device.status` round
  trip against a pad that has just gone out of range, a config write, a daemon
  restart — all on workers, with results marshalled back through a queue the run
  loop drains. The menu itself is rebuilt in `menuNeedsUpdate:`, immediately
  before it opens, so there is no work at all while it is closed.
* **The run loop is ours**: `nextEventMatchingMask:untilDate:…` in a Python
  `while`, rather than `[NSApp run]`. Ctrl-C works, and the periodic refresh has
  a natural home without another callback trampoline.

## Layout

| File | Contains | Needs |
| --- | --- | --- |
| `menubar/model.py` | Snapshot → menu rows. Pure. | nothing |
| `menubar/status.py` | Gathers a snapshot by reading. | the rest of FreeMicro |
| `menubar/checks.py` | `Run Doctor` as structured results. | the rest of FreeMicro |
| `menubar/cocoa.py` | The ctypes Objective-C bridge. | macOS |
| `menubar/app.py` | The status item and its run loop. | macOS, a GUI session |

`import freemicro.menubar` never loads AppKit — `app` is imported lazily inside
`main()`, so the package stays importable on Linux, in CI, and inside a launchd
job with no GUI session. There is a test that asserts exactly this.

`tests/test_menubar.py` covers the model and the gathering layer and needs no
hardware, no GUI session and no menu bar. That is the whole reason the model is
a pure function of a snapshot.

## Wiring required

These are changes in files the menu bar does not own. Everything below is
optional in the sense that the menu bar works without it — but each one closes a
real gap.

### 1. The `freemicro menubar` subcommand (`src/freemicro/cli.py`)

Add a command function:

```python
def cmd_menubar(args: argparse.Namespace) -> int:
    """Show FreeMicro's status in the macOS menu bar."""
    from freemicro.menubar import main as menubar_main

    return menubar_main(None)
```

and register it in `build_parser()`:

```python
mb = sub.add_parser(
    "menubar", help="show status in the macOS menu bar (connection, battery, state)"
)
mb.set_defaults(func=cmd_menubar)
```

plus a line in the module docstring's subcommand list:

```
``menubar``   Status in the macOS menu bar: connection, battery, state, LED switch.
```

No `pyproject.toml` change is needed: there is no new dependency, and
`packages = ["src/freemicro"]` already picks up the subpackage.

### 2. Let the daemon refresh the status cache (`src/freemicro/cli.py`)

Right now battery and firmware only appear when *nobody* holds the pad, because
that is the only time the menu bar may open it. The process that already has the
device is the right one to publish the reading.

Do **not** call `device.self_test()` from the daemon's tick — it pumps the run
loop, and the daemon is already inside one. Instead, send the request from the
tick and pick the reply up in the handler that is already running:

In `_run_pipeline`, keep the connected device and a clock:

```python
current = {"device": None}
status_at = [0.0]
```

set `current["device"] = dev` at the end of `rebuild(dev)` and `None` in
`dropped()`; then in `tick()`:

```python
    now = time.time()
    if current["device"] is not None and now - status_at[0] > 60:
        status_at[0] = now
        try:
            current["device"].send({"m": "device.status", "id": 77})
        except Exception:
            pass
```

and in `handle(message)`, before dispatching to the bridge:

```python
    result = message.get("result")
    if isinstance(result, dict) and "battery" in result:
        from freemicro.menubar.status import write_status

        write_status({
            "version": result.get("version"),
            "battery": result.get("battery"),
            "is_charging": bool(result.get("is_charging")),
            "source": "daemon",
        })
        return
```

`write_status` is atomic and swallows its own I/O errors, so this cannot affect
the daemon's reliability.

### 3. Reload the pad config when it changes (`src/freemicro/cli.py`)

`_run_pipeline` loads `padconfig` once at startup, so toggling **LED Control**
from the menu is invisible to a running daemon. The menu bar currently works
around this by running `launchctl kickstart -k` on the daemon — blunt, and
visible as a one-second gap in the pad's lighting.

The proper fix is an mtime check in `tick()`: stat `pad.source` every couple of
seconds, and on a change reload the config and rebuild the renderers (the same
work `rebuild(dev)` already does). Once that lands, delete
`freemicro.menubar.status.restart_pad_owner()` and its call site in
`menubar/app.py`.

### 4. Share one structured `doctor` (`src/freemicro/cli.py`)

`menubar/checks.py` asks the same questions as `cmd_doctor` and duplicates the
wording. The fix is for the CLI's doctor to be built on a function that
*returns* results rather than prints them:

```python
def run_doctor_checks(config: Optional[Path] = None) -> list[DoctorCheck]: ...
```

with `cmd_doctor` printing them and `menubar/checks.py` rendering them. The one
behaviour the menu bar needs preserved: when `daemon.lock_holder()` shows another
process owns the pad, the `device.status` round trip must be reported as
*skipped, and why* rather than attempted — the menu bar must not fight for the
device to run a diagnostic.

### 5. Optional: start it at login (`src/freemicro/daemon.py`)

`daemon.py` hard-codes `LABEL = "com.freemicro.daemon"` and its own argv. A
second LaunchAgent (`com.freemicro.menubar`, running `freemicro menubar`,
`RunAtLoad` true, `KeepAlive` true) would put the status item back after every
login. The cleanest shape is to parameterise `install()` / `uninstall()` /
`status()` on label and argv rather than copy the file. Until then, users can
add it themselves, or just launch it from a terminal.

## Known limits

* macOS only. On anything else `freemicro menubar` prints why and exits 2.
* It needs a GUI session. Running it over SSH, or from a LaunchDaemon rather
  than a LaunchAgent, gets you no status item.
* Battery and firmware are as fresh as whoever last held the pad — see the
  status cache above, and wiring item 2.
* Toggling **LED Control** restarts a running daemon so it re-reads the config.
  Wiring item 3 removes that.
