# Off-pad alerts: sound and macOS notifications

FreeMicro's pad is the only channel it has. That is the point of the product and
also its one blind spot: the moment you look away from the pad you miss
everything it is telling you, and the states that matter most - `waiting` on your
approval, `error` - are exactly the ones you are least likely to be staring at
the desk for.

Off-pad alerts close that gap with two channels that reach you when the pad
cannot, fired on the **same state transitions the LEDs already light on**:

* a short **sound** (`afplay` on a macOS system sound), and
* a native **Notification Center banner** (`osascript -e 'display notification'`).

Both use built-in macOS tools, so they add no dependency, and both are **off
until you opt in**, the same posture the LEDs take.

## Turning them on

Add an `alerts` block to `~/.freemicro/config.json`:

```json
"alerts": {
  "sound": {"done": "Glass", "waiting": "Ping", "error": "Basso"},
  "notify": ["waiting", "error"],
  "debounce_seconds": 8
}
```

There is no `alerts` block by default, and no block means no sound and no
notification, ever. A malformed or partial block is treated as "no alerts",
never as an error - this code runs inside the background daemon, and a typo must
not turn into a broken product.

### `sound`

A map from a state name (`idle`, `working`, `waiting`, `done`, `error`) to a
macOS system sound. A bare name resolves to `/System/Library/Sounds/<name>.aiff`;
the built-in names are `Basso`, `Blow`, `Bottle`, `Frog`, `Funk`, `Glass`,
`Hero`, `Morse`, `Ping`, `Pop`, `Purr`, `Sosumi`, `Submarine`, `Tink`. A state
with no entry plays nothing. A name that does not resolve to a file simply plays
nothing (fire-and-forget) rather than erroring.

The suggested set is a gentle chime on `done` and something more insistent on
`waiting` and `error`, because those two are where you are the blocker.

### `notify`

A list of state names that post a Notification Center banner. The default
recommendation is `["waiting", "error"]`. The banner carries a useful title and
body - for example *"Claude Code needs you / Waiting for your approval - myrepo"*
- and names the project (the winning session's folder) when FreeMicro knows it.

### `debounce_seconds`

Optional, default `8`. A state can flap - `waiting` to `working` and back inside
a second while an agent churns through permission prompts - and without a
debounce that would machine-gun banners and sounds. Each channel remembers when
it last fired for a given state and stays quiet until this many seconds have
passed. Set it to `0` to disable the debounce entirely.

## Confirming it works

```sh
freemicro alerts          # print what is configured, or that alerts are off
freemicro alerts --test   # fire each configured alert (or the shipped defaults)
```

`--test` is also how you surface the **one-time macOS permission prompt** that a
notification needs the first time: run it, approve your terminal (or Script
Editor) under System Settings → Notifications, then run it again. `--test
--dry-run` prints what would fire without making a sound or a banner.

## Where they fire

Alerts fire from the render loop in both `freemicro run` and the background
daemon, on the transition where the resolved state changes (the same line that
prints `state: …` and repaints the pad). Because the daemon fires them too, they
reach you with no terminal open at all.

## Guarantees

* **Never blocks the pad.** Every sound and banner is a fire-and-forget
  subprocess - spawned and never waited on - so a slow `afplay` or a wedged
  `osascript` cannot freeze the render loop.
* **Off by default.** No `alerts` block means silence.
* **Debounced.** A flapping state cannot spam you.
* **No new dependency.** `afplay` and `osascript` ship with macOS. On a machine
  without them, `freemicro alerts --test` says so and alerts are simply skipped.

## Implementation notes

The whole feature lives in `src/freemicro/alerts.py`: config parsing
(`AlertConfig.from_raw`, reading the `alerts` block off the already-loaded
config), the dispatcher (`Alerter`, with an injectable subprocess runner and
clock so tests assert what *would* be played and posted without a sound or a
banner), the fire-and-forget runner (`spawn`), and the notification copy. The
render loop in `cli._run_pipeline` builds one `Alerter` from the config and calls
`alert(state, previous, project=…)` at the transition point.
