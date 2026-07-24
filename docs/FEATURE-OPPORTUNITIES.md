# Feature opportunities — where FreeMicro goes beyond the vendor app

> Design research, 2026-07-23. Written against the **verified** protocol in
> [`PROTOCOL.md`](PROTOCOL.md). Nothing here is committed roadmap; it is a menu
> with costs attached. Anything resting on behaviour we have *not* seen on
> hardware is tagged **UNVERIFIED** and should be probed before it is planned.

## The thesis in one paragraph

The pad emits **no keyboard scancodes**. Whoever listens on the `0xFF00` vendor
channel owns the device completely — every key, the stick, and every LED. The
ChatGPT desktop app happens to be that listener today, and it spends that total
ownership on a deliberately small product: one action per input, a fixed action
catalogue, one app, one agent, lighting hardcoded to Codex thread state. The
opportunity is not "do what they do, but open." It is that **we control input
and output on the same wire**, so the pad can become a programmable, context-
aware control surface with a six-cell display attached — which is a different
category of thing.

## Raw materials (verified on hardware)

| Primitive | What it gives us | Code seam |
|---|---|---|
| `v.oai.hid` `{k, act, ag}` | Key id + **press *and* release** for AG00–AG05, ACT06–ACT12, ENC_CLK | `input/bridge.py: Bridge.decode` |
| `v.oai.hid` → `ENC_CW` / `ENC_CC` | **Encoder rotation**, one event per detent (discrete ticks, no velocity from the device) | `input/bridge.py: Bridge.decode` |
| `v.oai.rad` `{a, d}` | **Analog thumbstick** — angle + distance, both 0–1, streamed continuously, returns to exactly `{0,0}` on release | `input/bridge.py: JoystickTracker` |
| `lights.preview` | Live backlight + underglow: effect, brightness, speed, magic, packed RGB | `device/lighting.py: preview_message` |
| `v.oai.thstatus` | **Each of the six Agent Keys independently** (c/b/e/s + syncKeys/syncAmbient) | `device/lighting.py: thread_entry`, `all_agent_keys` |
| Effects 0–6 | off, solid, snake, rainbow, breath, gradient, shallowBreath | `device/lighting.py: parse_effect` |
| `device.status` | firmware, profile_index, layer_index, **battery %, is_charging** | not yet wired |
| **Transport** | **USB *and* Bluetooth LE, both fully duplex** — input, lighting and `device.status` all verified wireless | `device/codex_micro.py` |
| Host side | Type text / keystrokes (AppleScript), run shell, read Claude Code hooks | `input/actions.py: Backend`, `state/hooks.py` |

**The complete input inventory** is therefore: six Agent Keys, seven action
keys, an encoder that reports press *and* both rotation directions, and an
analog thumbstick — 16 discrete inputs plus one continuous axis, every one of
them free of scancodes and ours to define.

**The pad is genuinely untethered.** Everything in this document works with the
cable unplugged, which quietly changes the product: this is an ambient display
and control surface that works at your desk *or* on the couch, not a peripheral.
That makes battery awareness (§4.7) and connection liveness (§7.7) real
features rather than housekeeping.

Effort scale: **S** = under a day · **M** = a few days · **L** = a week or more,
or needs design work first.

---

## 0. Protocol gaps to close before planning around them

These are cheap probes that unblock whole sections below. Do them first; several
ideas here are worthless if the answer is no.

> **Closed 2026-07-23.** Two gaps that blocked large parts of this document are
> now resolved on hardware, and `PROTOCOL.md` is authoritative:
> - **The encoder reports rotation.** `ENC_CW` / `ENC_CC` arrive over
>   `v.oai.hid` like any key, one event per detent — *discrete ticks, no
>   device-side velocity*. Everything knob-dependent below is unblocked (§1.7).
>   Settled at the same time: `v.oai.rad` is the **thumbstick**, not the dial —
>   it returns to exactly `{0,0}` on release, which an encoder cannot do. The
>   deadzone/edge-trigger design in §1.5 stands, and the dial is an *additional*
>   surface rather than the same one counted twice.
> - **Bluetooth LE is fully duplex.** Input, `lights.preview`, `v.oai.thstatus`
>   and `device.status` all work wireless. The only difference is write framing
>   (USB: 63 bytes, no report-id prefix; BLE: 64 bytes with `0x06` at
>   `buffer[0]`). See §9 — and note the silent-failure trap it introduces, which
>   changes how `freemicro doctor` must be written (§7.2).

| Question | Why it matters | Cost |
|---|---|---|
| **Are ACT06–ACT12 individually addressable?** `v.oai.thstatus` is confirmed for the six Agent Keys. `lights.preview.backlight` looks global. | Decides whether per-key affordances (§1.4, §6.3) work on the whole pad or only the top row. **UNVERIFIED** | S |
| **How many simultaneous key-downs are reported?** We get down/up, but not whether the firmware reports 3+ concurrently. | Chords (§1.3) collapse to two-key if rollover is limited. **UNVERIFIED** | S |
| **What does `mp.write_info` / `mp.write_artwork` / `ui.active_screen` actually drive?** Their existence implies a display or a home screen. | If arbitrary text lands on a screen, §8 becomes the biggest feature in this document. **UNVERIFIED** | S |
| **Does `thstatus` fight `lights.preview`?** Each call replaces prior state; ordering/precedence between the two methods is uncharacterised. | The LED compositor (§4.1) must know which one wins. **UNVERIFIED** | S |
| **What is the `magic` field?** Documented as present, purpose unknown. | Possibly a second animation parameter — free expressiveness if so. **UNVERIFIED** | S |

Add the answers to `hardware/capabilities.json` and `PROTOCOL.md` as they land.

---

## 1. Input expressiveness

The vendor app is one-action-per-input. We get press *and* release timestamps
for every key and a continuous analog stream, so the physical pad has far more
distinct gestures in it than 14 buttons.

### 1.1 Tap / double-tap / triple-tap / long-press — **S**
- **What.** Every input id gains suffixed variants: `AG00`, `AG00.double`,
  `AG00.triple`, `AG00.hold`. Bind each independently.
- **Beats.** The vendor app has exactly two gestures, on the Agent Keys only
  (single tap = focus, double tap = raise window), and they are not rebindable.
  We turn 14 physical keys into ~40 bindable actions with no hardware change.
- **How.** `Bridge.decode` currently discards `act == 0` and fires on key-down.
  Replace with a small per-key state machine fed by `(key, act, monotonic())`:
  emit `.hold` at the hold threshold *while still held* (so the user gets
  feedback before releasing), and buffer taps for a multi-tap window
  (~250 ms) before emitting. The window adds latency **only to keys that
  actually have a `.double` binding** — resolve against the loaded `PadConfig`
  and fire immediately when no multi-tap binding exists. That conditional is the
  whole trick; without it every key feels laggy.
- **Effort:** S. It is one class plus config plumbing; the bridge is already a
  pure decode function with tests.

### 1.2 Key-hold-as-modifier (layer shift) — **S**
- **What.** Hold `ACT12` and the other keys temporarily resolve against a
  different binding table. Momentary layers, like a real keyboard.
- **Beats.** The vendor app has one flat map. This multiplies the map by the
  number of modifier keys you are willing to give up, at zero cognitive cost —
  hold-to-shift is the most learnable layer idiom there is.
- **How.** A `modifier` action kind that sets `active_layer` for the duration of
  the hold; `PadConfig.action_for` takes the active layer into account. Config
  gains `layers: {name: {bindings}}` alongside the existing flat `bindings`.
- **Effort:** S, and it composes with 1.1 (a modifier key can still have a tap
  action).

### 1.3 Chords — **M**
- **What.** `AG00+AG01` fires a third action, distinct from either key alone.
- **Beats.** Nothing comparable exists in the vendor app.
- **How.** Same state machine as 1.1: on key-down, if any chord in the config
  contains this key, wait a short coalescing window (~40 ms) for co-pressed
  keys, then match the largest chord. Suppress the individual keys' actions when
  a chord matched.
- **Effort:** M — the coalescing window makes single presses feel worse if
  applied globally, so it must be opt-in per key, and rollover is **UNVERIFIED**
  (see §0). Ship 1.1 and 1.2 first; chords are the least-loved of the three.

### 1.4 Chord/layer discovery light ("hardware tooltip") — **M**
- **What.** Hold a modifier and the six Agent Keys immediately light in the
  colours of what they now do on that layer. Release and they return to agent
  state.
- **Beats.** This is only possible if you own input *and* lighting on the same
  wire at the same time. The vendor app owns both and does not do it. It is the
  single clearest "you couldn't build this on their stack" demo.
- **How.** On modifier-down, push a `v.oai.thstatus` frame built from the target
  layer's per-binding `color`; on modifier-up, pop back to the agent-state
  frame. Requires the LED compositor (§4.1) so the transient frame does not
  permanently clobber the status frame.
- **Effort:** M. Limited to the top row unless ACT keys turn out to be
  individually addressable (**UNVERIFIED**, §0).

### 1.5 Analog joystick as a continuous control — **M**
- **What.** Stop throwing away the analog data. Three modes per binding:
  - **flick** (today's behaviour — discrete, once per flick);
  - **repeat** — hold a direction and the bound keystroke repeats at a rate
    proportional to `d` (scrub a long history, page through a diff, move a
    cursor slowly at 30 % deflection and fast at 100 %);
  - **scrub** — map `d` directly to a value and emit it continuously (volume,
    a slider in a TUI, brightness).
- **Beats.** The vendor app hard-quantises the stick to four directions and one
  action each. The hardware is analog; quantising it is a product decision we do
  not have to copy.
- **How.** `JoystickTracker` already sees every sample. Add `mode` to
  `JoystickConfig`; for `repeat`, drive a timer whose interval is
  `lerp(slow_ms, fast_ms, (d - deadzone) / (1 - deadzone))`; for `scrub`, emit a
  value event that a `scrub` action consumes. Directions stay configurable —
  including **8-way**, which costs nothing since `direction_for(angle)` already
  divides the circle by the length of the `directions` list.
- **Effort:** M. Rate-limiting and re-arm hysteresis are the fiddly parts, and
  the existing `REARM_RATIO` logic gives us a head start.

### 1.6 Gesture arcs — **M**
- **What.** Recognise stroke shapes: a clockwise sweep, a flick-out-and-back, an
  L-shape. Bind each to an action.
- **Beats.** Genuinely novel for this class of device; the vendor app has four
  directions.
- **How.** Buffer `(angle, distance)` samples between the deadzone crossing and
  the return to centre, resample to a fixed-length polyline, and classify with a
  handful of hard-coded templates (total angular sweep, direction reversals,
  quadrants visited). Do **not** reach for a general recognizer; five templates
  cover everything anyone will bind.
- **Effort:** M. Worth it mainly as a differentiator demo — expect low daily use.
  Rank it below 1.1–1.5.

### 1.7 The dial: bindable rotation, acceleration, and dial modes — **S**
- **What.** `ENC_CW` / `ENC_CC` become first-class bindable inputs, with three
  layers on top of the raw detent stream:
  - **acceleration** — fast turns move in larger steps than slow ones, so one
    flick crosses a long range and a slow turn still lands on an exact value;
  - **dial modes** — press-and-hold (or a modifier key, §1.2) switches *what the
    dial controls*: reasoning effort, model, scroll, volume, layer, brightness.
    The current mode shows on the Agent Keys for as long as you hold;
  - **turn-while-held** — holding any key while turning is a second axis, free,
    since we already track hold state for §1.2.
- **Beats.** The vendor app's knob has a **single mode** ("Reasoning only"), one
  fixed meaning per direction, and a click that opens a settings panel. Ours is
  a general-purpose value control that any binding can claim, and the mode
  picker is a hold-gesture on the pad rather than a window on your screen.
- **How.** The device sends **discrete ticks and no velocity**, so acceleration
  is entirely host-side: keep a short ring buffer of tick timestamps and emit a
  step count of `1`, `2` or `N` based on the inter-tick interval (a simple
  two-threshold ladder beats a curve here — it is predictable under the
  fingers, which is the only thing that matters). Ticks arrive through
  `v.oai.hid` exactly like keys, so `Bridge.decode` needs one extra branch, not
  a new code path. Emit `ENC_CW`/`ENC_CC` with a `steps` count that actions can
  consume.
- **Effort:** S. Newly unblocked, and it reuses the hold/multi-tap machinery
  from §1.1–1.2.

### 1.8 Dial → reasoning effort, with the gauge to match — **M**
- **What.** Turn the dial to set how hard the agent thinks, and watch the six
  Agent Keys fill as a live gauge while you do it. Click to reset to default.
  This is the vendor app's headline knob feature, done on an agent it does not
  support.
- **Beats.** Two ways. First, it works with Claude Code at all. Second, their
  knob has no persistent display — you turn it and a slider appears *on your
  screen*, which defeats the point of reaching for a physical control. Ours
  shows the current level **on the pad**, where your hand already is, and it is
  still visible ten seconds later.
- **How.** Be precise about the mechanism, because this is the part that is easy
  to overclaim: **Claude Code exposes no numeric reasoning dial.** What exists is
  a *ladder* — thinking-level keywords in the prompt, and model selection. So:
  1. FreeMicro holds `reasoning_level` as its own state (the `state` action kind,
     §5.2) — it is our variable, not something we read back from the agent;
  2. `ENC_CW`/`ENC_CC` step it, with acceleration from §1.7;
  3. a light source renders it as a six-segment gauge (§4.2) — this is the
     single best use of that gauge, and it is why §4.2 ranks where it does;
  4. on submit, a `prefix` action prepends the level's configured text to the
     prompt. The **ladder is config, not code** (`levels: [{label, prefix,
     model}]`), because the right rungs differ per agent and will change as
     agents change. Ship a sane Claude Code default; let people edit it.
- **Honest limitation.** The level is *write-only* — we set it, we cannot query
  what the agent actually did with it, and nothing stops a prompt from
  contradicting it. Show it as "what FreeMicro will send," never as "what the
  model is doing."
- **Effort:** M. The dial and gauge are S; the per-agent ladder and the submit
  integration are where the work is.

---

## 2. Context awareness

The vendor app has one global config. Every idea in this section is "the same
key should do different things depending on where you are," which is where a
14-key pad stops running out of room.

### 2.1 Per-project / per-repo keymaps — **S**
- **What.** Config resolution order: `$PWD/.freemicro/keymap.json` → nearest
  git-toplevel `.freemicro/keymap.json` → `~/.freemicro/keymap.json` → built-in
  default. Layers merge; a repo file overrides only the keys it names.
- **Beats.** The vendor app is global-only. A repo can ship its own pad layout
  the way it ships `.editorconfig` — `ACT08` runs *this* project's test command.
- **How.** Extend `padconfig.load()` with a search path and a shallow merge.
  Resolve against the focused terminal's cwd; the daemon (§7.1) already has to
  track sessions, and every Claude Code hook payload carries `cwd`.
- **Effort:** S. Highest value-per-line in the document.
- **Caveat.** Executing a `shell` action out of a repo file is remote code
  execution by `git clone`. Repo-level configs must be **trust-prompted on first
  sight** (hash the file, ask once, remember) — non-negotiable, ship it with the
  feature, not after.

### 2.2 Per-focused-app layers — **M**
- **What.** The active layer follows the frontmost application. Terminal →
  Claude Code bindings; browser → tab/navigation bindings; Figma → its own.
- **Beats.** The vendor app has no notion of focus at all; its actions assume
  the Codex window.
- **How.** macOS: subscribe to `NSWorkspaceDidActivateApplicationNotification`
  via PyObjC (no polling). Linux: X11 `_NET_ACTIVE_WINDOW` / a Wayland
  compositor-specific call — flag Wayland as best-effort. Map bundle id → layer
  in config. **Separately**, forward the focused app to the pad with
  `host.focused_app` so whatever the firmware does with it keeps working — note
  that method is host→device, so it gives us nothing to read; it is an output.
- **Effort:** M, mostly because focus detection is the least portable thing in
  the project.

### 2.3 Agent-state layers — **S**
- **What.** Bindings that change with the resolved agent state. While `waiting`,
  `ACT10`/`ACT11` become **approve** / **reject**; while `working` they become
  interrupt/nothing; while `idle` they are prompt shortcuts.
- **Beats.** Approve/reject as *the same physical key that is currently glowing
  amber* is the entire point of a pad next to your keyboard. The vendor app's
  keys have static functions and independently-coloured LEDs; ours converge.
- **How.** The state store already resolves a state (`StateStore.resolve`). Feed
  it into `PadConfig.action_for` as another layer selector. Zero new device work.
- **Effort:** S. Pair with §4 so the LED and the action always agree.

### 2.4 Time-of-day, DND, and on-call profiles — **S**
- **What.** Scheduled brightness/behaviour profiles: dim after 22:00, full
  brightness and a red `error` breath while on-call, "quiet" mode where only
  `waiting` lights at all.
- **Beats.** The vendor app has a global brightness slider and one auto-dim
  timer. The meaningful difference is **per-state** policy: dim *idle* to
  nothing, but never dim *waiting* — that is the state whose whole job is to
  interrupt you. A global dimmer cannot express that.
- **How.** A `profiles` block in config with `when` predicates (time range,
  focused app, `on_call: true` toggled by an action or a shell probe); the
  compositor scales brightness per source.
- **Effort:** S once the compositor exists.

### 2.5 Idle/presence-aware escalation — **S**
- **What.** If `waiting` persists for N seconds, escalate: increase brightness,
  raise breath speed, then flash. Everything happens on the pad; there is no
  second surface to escalate to.
- **Beats.** The vendor app's amber is static — it looks the same after two
  seconds and after ten minutes. A blocked agent is a cost that grows; the light
  should say so.
- **How.** `SessionState` already carries a timestamp and `age()`. The render
  loop derives an escalation tier and modulates `b`/`s` on the outgoing frame.
- **Effort:** S. Small, and it is the feature that actually saves you money.

---

## 3. Multi-agent — the strongest structural advantage

The vendor app binds the six Agent Keys to six **Codex chats**. We can bind them
to six of *anything that reports state*, which is a far larger set.

### 3.1 Agent Key = live agent session, across tools — **M**
- **What.** AG00–AG05 map to the six most recent live sessions from *any*
  supported agent — Claude Code, Codex CLI, Cursor, Aider — each key showing
  that session's own state.
- **Beats.** The vendor app is Codex-only by construction. This is the headline
  comparison and the reason the project exists.
- **How.** The state store is already one JSON file per session with a TTL, so
  the data model needs no change; it needs a **stable slot assignment** (sticky
  once assigned, freed on TTL expiry, so keys do not shuffle under your fingers
  mid-task) and a per-session label. Per-key colour goes out as one
  `v.oai.thstatus` array of six entries — exactly the call shape we already
  build in `all_agent_keys`, generalised to per-entry colours. Non-Claude agents
  need adapters: Codex CLI and Aider have no hook system, so start with a
  **log/PTY watcher** adapter and be honest in the docs that it is heuristic.
- **Effort:** M for Claude Code multi-session (mostly free), L to make the
  other-agent adapters trustworthy.

### 3.2 Tap-to-focus a session — **M**
- **What.** Tap a glowing key to bring that agent's terminal to the front and
  focus its pane. Double-tap to raise without focus, long-press to interrupt it.
- **Beats.** The vendor app can focus a Codex chat. Ours focuses a *terminal
  pane* — tmux window, iTerm tab, VS Code terminal — which is where real agent
  work lives.
- **How.** The hook must capture identity at `SessionStart`: `TMUX_PANE`,
  `TERM_SESSION_ID`, `ITERM_SESSION_ID`, `WINDOWID`, plus `cwd`. Focus by
  `tmux switch-client -t` / `tmux select-pane -t`, or AppleScript for iTerm and
  Terminal. Degrade honestly: if we captured nothing usable, say so rather than
  focusing the wrong window.
- **Effort:** M. The terminal-emulator matrix is the work, not the protocol.

### 3.3 Attention queue — **S**
- **What.** The six keys are not "sessions" but "**things that need you, most
  urgent first**." A key lights only when its session is `waiting` or `error`;
  pressing it jumps you there and clears it.
- **Beats.** This is a genuinely different product from the vendor app's
  status mirror. With four agents running, a status mirror makes you read six
  lights; a queue makes you press the leftmost lit one. It is the difference
  between a dashboard and a work surface.
- **How.** Sort `StateStore.sessions()` by `needs_you` then age, take six, emit
  `thstatus`. Built entirely on things that already exist.
- **Effort:** S. Strong candidate for the flagship demo.

### 3.4 Worktree / branch keys — **M**
- **What.** Assign keys to git worktrees or branches rather than sessions. Key
  colour = that branch's CI status; press = `cd` there / open its agent.
- **Beats.** No equivalent. It reframes the pad as a project switcher.
- **How.** `git worktree list --porcelain` for the roster; CI status from §4.4.
- **Effort:** M.

---

## 4. LEDs as an information display

Six independently addressable RGB cells with seven effects, plus two ambient
zones. The vendor app spends all of that on one enum with six values. That is
the biggest gap in this document.

### 4.1 The LED compositor (enabling architecture) — **M**
- **What.** Not a user feature — the thing every other LED feature needs. A
  registry of **light sources**, each producing a partial frame
  (per-key colour/effect/brightness/speed + ambient zones), composited by
  priority into one frame, emitted at a fixed rate.
- **Beats.** It is why we can show CI status *and* agent state *and* a
  hold-to-preview overlay without them fighting. Their app has one source.
- **How.** Mirror the pattern `input/actions.py` already establishes: a
  `@light_source` decorator with a registry, exactly as `@action` works today —
  same ergonomics, same testability, and third parties get both extension points
  for free. Then:
  - a fixed ~10 Hz tick that **coalesces** — the protocol note is explicit that
    each call replaces the previous one, so a burst renders as its final frame
    only, and animated effects restart if you re-send unchanged frames
    (`MicroLedsRenderer.render` already guards against exactly this);
  - diff against the last sent frame; send nothing when unchanged;
  - transient sources (§1.4, §6.3) get top priority and a TTL;
  - one `lights.preview` for ambient + one `v.oai.thstatus` array per frame.
- **Effort:** M. Do this before anything in §4.2–4.8, or you will write the same
  arbitration logic six times.

### 4.2 The six keys as a six-segment gauge — **S** *(after 4.1)*
- **What.** A generic bar-graph source: value 0–1 → N of 6 keys lit, with the
  partial segment dimmed proportionally (`b` is a float, so we get free
  sub-segment resolution).
- **Beats.** No analogue in the vendor app.
- **How.** One source, many bindings: context budget, battery, test pass rate,
  build progress, pomodoro remaining, download progress. Colour carries the
  semantic (green→amber→red), fill carries the magnitude.
- **Effort:** S — it is ~30 lines once §4.1 lands, and it is the workhorse
  behind half of §4.

### 4.3 Effects as data encodings — **S**
- **What.** Treat the seven firmware effects as a small visual vocabulary rather
  than decoration, and use it consistently:

| Effect | Encodes | Example |
|---|---|---|
| `solid` (1) | a settled, terminal state | done, error, CI green |
| `shallowBreath` (6) | in progress, healthy | agent working |
| `breath` (4) + high `speed` | needs you, urgency ∝ speed | waiting, escalating (§2.5) |
| `snake` (2) | activity with direction/progress | long-running command, deploy in flight |
| `gradient` (5) | a range or spread | context budget across keys |
| `rainbow` (3) | "unknown / no data" | source offline — deliberately unmistakable |
| `off` (0) | nothing assigned | unbound key |

- **Beats.** The vendor app uses colour only. A second dimension roughly doubles
  the states you can distinguish at a glance without learning new colours.
- **How.** A documented convention plus defaults in `default_keymap.json`.
- **Effort:** S. Mostly a docs-and-defaults decision, but it must be made once,
  centrally, or the vocabulary drifts.

### 4.4 CI / build / PR status — **M**
- **What.** A key per pipeline: `gh run list` / `gh pr status` for the current
  repo, mapped to colour + effect (queued = snake, passing = solid green,
  failing = solid red, review requested = amber).
- **Beats.** The vendor app shows chat state only. This is the most-requested
  ambient-display use case for any desk light, and we already have the surface.
- **How.** A polling light source shelling out to `gh` (already authenticated on
  most dev machines — no OAuth flow to build, which is the whole reason to
  prefer it over the REST API). Backoff and cache; never block the render loop.
- **Effort:** M.

### 4.5 Local signals: tests, git dirt, long-running commands — **S**
- **What.** Cheap, no-network sources: last test run result, `git status
  --porcelain` dirty/clean, uncommitted-for-N-hours warning, and a progress
  snake while a `shell` action with `wait: true` is still running.
- **Beats.** Not offered at all. The last one is notable: it closes the loop on
  our own actions — you press the key, the key tells you it is still working.
- **How.** Light sources with file-mtime or process-liveness triggers.
- **Effort:** S each.

### 4.6 Context / token budget — **M**
- **What.** A gauge showing how full the current session's context window is,
  so `/compact` stops being a surprise.
- **Beats.** Nothing comparable, on any pad.
- **How.** Claude Code hook payloads carry `transcript_path`; the JSONL there
  can be sized and estimated. This is **approximate** and will drift with
  format changes — label it as an estimate in the UI and the docs, and degrade
  to "unknown" (rainbow, §4.3) rather than showing a confidently wrong number.
- **Effort:** M, and it carries ongoing maintenance risk. Worth it anyway; this
  is the number people most want and cannot see.

### 4.7 Battery and charge state — **S**
- **What.** `device.status` returns `battery` and `is_charging`. Warn at 15 %,
  expose the number in `freemicro status` and the menubar (§7.3), and dim the
  ambient scene automatically on low battery.
- **Beats.** The vendor Settings pane does not surface battery in the documented
  UI. This matters more than it first looks: the pad is **verified fully
  functional over BLE**, so people will genuinely run it untethered, and a
  wireless pad that dies silently mid-session is a bad pad. It is also free —
  the same `device.status` round-trip we need anyway as a liveness probe (§7.7)
  carries the battery number with it, so this costs one field, not one feature.
- **How.** Poll `device.status` on a slow timer (60 s+; it shares the wire with
  input events, so measure before tightening). Escalate to a dedicated key flash
  below threshold, with a snooze. Suppress the warning entirely while
  `is_charging`.
- **Effort:** S.

### 4.8 Timers and pomodoro — **S**
- **What.** Start a timer from a key; the six keys drain as a gauge; a flash and
  a colour change at zero.
- **Beats.** Not offered. It is also the friendliest possible demo of §4.2 for
  someone who does not yet care about CI colours.
- **Effort:** S after §4.2.

### 4.9 Light shows — choreographed sequences — **S** *(after 4.1)*
- **What.** Short, event-triggered choreography across the six Agent Keys and
  both ambient zones. Tests go green and a green wipe runs left-to-right. A PR
  merges and the keys converge on the centre. A deploy finishes and the
  underglow pulses once. An `error` arrives and the pad does a two-beat red
  double-flash you recognise from across the room without reading anything.
- **Beats.** The vendor app's lighting is a state mirror: one enum in, one
  static colour out. It has no notion of an *event* — a thing that happened and
  is now over — and the only sequenced lighting in the firmware appears to be
  the built-in voice states. Events are most of what happens in a dev loop, and
  a 400 ms flourish is a fundamentally better way to signal one than latching a
  colour you then have to clear.
- **Why it earns a slot beyond the obvious.** A ten-second GIF of the pad
  running a wipe is the most persuasive artefact this project can produce. For
  an open-source tool whose whole pitch is "your desk tells you things," the
  demo *is* the documentation. That is an honest adoption argument, not a
  decorative one.
- **How.** A show is a list of keyframes, not an animation:

  ```json
  {"action": "show", "frames": [
    {"ms": 90, "keys": {"0": "#34C759"}},
    {"ms": 90, "keys": {"0": "#34C759", "1": "#34C759"}},
    {"ms": 400, "keys": "all", "color": "#34C759", "effect": "solid"}
  ]}
  ```

  The compositor (§4.1) plays it as a **transient top-priority source with a
  TTL**, so it overlays the status frame and yields back automatically when it
  ends — no save/restore bookkeeping, no way to strand the pad mid-show. Ship a
  handful of generator primitives over the six key indices — `wipe`, `chase`,
  `converge`, `alternate`, `pulse` — so nobody hand-writes keyframes for the
  common cases. Bindable as an action (any key can fire one) and subscribable to
  events (any light source can request one on a transition).
- **Constraints, and they are the whole design.** Each lighting call *replaces*
  the previous one and rapid bursts visibly coalesce into their final frame, so
  frames must be **held ~90 ms or longer** — below that you are not animating,
  you are dropping frames. That is fine: choreography reads better at 6–10 fps
  than smooth interpolation would, and it keeps us inside the wire budget. Three
  hard rules:
  1. the show shares the channel with **input events** — cap the frame rate and
     never let a queued show frame delay a keypress dispatch;
  2. a real state change **preempts instantly**. `waiting` must never wait for a
     celebration to finish. Interruption is a feature, not an edge case;
  3. shows are **skippable and rate-limited** — the fourth green wipe in a
     minute is noise. Suppress repeats within a window.
- **Also worth trying:** the `sk` / `sa` (syncKeys / syncAmbient) flags on
  `v.oai.thstatus` may let one call drive keys and ambient together, which would
  halve the traffic for whole-pad choreography. Their exact behaviour is
  **UNVERIFIED**.
- **Effort:** S for the player plus five primitives once §4.1 exists. The
  keyframe format is deliberately boring so shows are shareable through the
  preset registry (§5.3) — "light show packs" are the one piece of this project
  that non-programmers can contribute.

### 4.10 Ambient scenes — **S**
- **What.** The non-event counterpart to 4.9: a slow, low-brightness idle look
  you actually want on your desk at 1 a.m. — underglow on `gradient` or
  `breath`, keys nearly dark, tied to the time-of-day profiles in §2.4.
- **Beats.** The vendor app offers a brightness slider and an auto-dim timer.
  "Dimmer" and "calmer" are not the same thing, and only one of them is a
  design decision.
- **How.** A lowest-priority light source that fills whatever the status sources
  leave unclaimed. Falls away the instant anything real needs the surface.
- **Effort:** S. Mostly taste, and it should be a preset rather than a setting.

---

## 5. Extensibility

The action registry in `input/actions.py` is already the right shape — a
decorator, a spec with required/optional fields, load-time validation, and a
`Backend` seam that makes everything dry-runnable. Extensibility is mostly
*exposing* what is already there.

### 5.1 Third-party action kinds via entry points — **S**
- **What.** `pip install freemicro-jira` and `{"action": "jira.transition"}`
  starts working, listed by `freemicro keys --list`, validated at config load.
- **Beats.** The vendor app has a closed catalogue of ~34 built-ins plus
  first-party "Skills." Ours is open by design and needs no review process.
- **How.** Discover `freemicro.actions` entry points at startup and import them;
  the `@action` decorator does the rest. Namespace third-party kinds with a
  `vendor.` prefix so a plugin cannot shadow `text` or `shell`.
- **Effort:** S. The registry pattern is already built and tested.

### 5.2 Built-in action kinds worth adding — **S–M**

| Kind | What it does | Effort |
|---|---|---|
| `sequence` | Run a list of actions in order, with optional `delay_ms` between | S |
| `http` | POST a webhook, with a template body — the universal escape hatch | S |
| `layer` | Switch/toggle/momentary-hold a layer (§1.2) | S |
| `confirm` | Wrap a destructive action in a two-press guard (§6.3) | S |
| `state` | Toggle a named FreeMicro flag other rules read (`on_call`, quiet mode) | S |
| `if` | Branch on a shell predicate's exit code (§6.2) | M |
| `mcp` | Invoke an MCP tool on a configured server | M |
| `agent` | Send a prompt to a *specific* agent session, not just the focused window | M |

`mcp` deserves a note: it is the one that turns the pad into an agent control
surface rather than a keyboard. It also drags in a client, transport config, and
auth — do not put it in the first extensibility release.

### 5.3 Shareable presets, registry-not-marketplace — **S**
- **What.** `freemicro preset install claude-code-tui` pulls a keymap from a
  curated index; `freemicro preset export` publishes yours.
- **Beats.** The vendor app has no sharing at all — every user rebuilds the same
  layout by hand.
- **How.** A `presets/index.json` in this repo pointing at raw URLs or gists;
  install = fetch, validate against the config schema, diff against current,
  confirm, write. **Presets containing `shell`/`applescript` actions must show
  those commands and require explicit confirmation** — same trust rule as §2.1.
- **Effort:** S. Deliberately no backend, no accounts, no hosted marketplace
  (see §10).

---

## 6. Workflow automation

### 6.1 Macro sequences — **S**
- **What.** One key runs several actions: type `/clear`, wait, type a prompt,
  submit. Or: stage, commit with a generated message, push.
- **Beats.** One action per input is the vendor app's hard limit.
- **How.** The `sequence` action kind (§5.2). Delays are real, not cosmetic —
  synthetic keystrokes into a TUI need settling time.
- **Effort:** S.

### 6.2 Conditional actions — **M**
- **What.** `if` on a shell predicate: the same key commits when the tree is
  dirty and pushes when it is clean; approves when the agent is `waiting` and
  interrupts when it is `working`.
- **Beats.** No equivalent. Combined with §2.3 it is how 14 keys start feeling
  like 40.
- **How.** `{"action": "if", "test": "git diff --quiet", "then": {...},
  "else": {...}}`. Cap the predicate's runtime hard (~200 ms) — a slow `if`
  makes the whole pad feel broken.
- **Effort:** M, mostly in keeping evaluation off the event thread.

### 6.3 Confirmation-guarded destructive actions — **S**
- **What.** Bind `git push --force` or `rm -rf`; the first press arms the action
  and **turns that key amber**; a second press within 3 s executes; anything else
  cancels and the key returns to normal.
- **Beats.** Uniquely ours: the confirmation UI *is the key you are already
  touching*. No dialog, no context switch, no second device. The vendor app
  cannot do this because its lighting is hardcoded to thread state.
- **How.** `confirm` action kind + a transient, TTL'd light source at top
  priority in the compositor (§4.1).
- **Effort:** S. Small, safe, and it demos in ten seconds.

### 6.4 Record and replay — **M**
- **What.** `freemicro record` captures the *dispatches you actually performed*
  on the pad and writes them out as a `sequence` binding.
- **Beats.** Nothing like it exists here. Note the framing: we record **our own
  dispatch log**, which we already produce (`Dispatch` objects), not the OS
  keystroke stream. That makes it reliable and permission-free — a general
  keystroke recorder would be neither.
- **How.** A recording flag on the bridge; serialise dispatches to a keymap
  fragment; prompt for a key to bind it to.
- **Effort:** M.

### 6.5 Undo — **do not build the general case**
- **What people will ask for.** Undo the last pad action.
- **Reality.** We type into other people's applications. We cannot un-type,
  un-push, or un-`rm`. A convincing "undo" would be a lie.
- **What to build instead.** Undo for **FreeMicro-owned state only** — layer
  switches, mode toggles, profile changes — plus §6.3 for everything
  destructive. Say plainly in the docs that undo does not cross the process
  boundary.
- **Effort:** S for the honest version.

---

## 7. Onboarding and UX

This is where the vendor app is genuinely strong and we are genuinely weak: it
is a signed app with a settings pane, and we are a Python CLI that needs two
macOS permissions and a terminal window left open.

### 7.1 The daemon — **M** *(architectural prerequisite)*
- **What.** One long-lived process owns the HID handle; the CLI, the menubar,
  the web editor, and the hooks all talk to it over a Unix socket.
- **Why it is not optional.** Only one process can hold the device. Today
  `freemicro run` shares a handle *within* one process (`shared_device`); the
  moment a second surface exists, they collide. Every remaining item in this
  section assumes this exists.
- **How.** `launchd` LaunchAgent on macOS, `systemd --user` on Linux; a small
  line-JSON socket protocol; `freemicro` subcommands become thin clients that
  fall back to in-process mode when no daemon is running.
- **Effort:** M.

### 7.2 `freemicro doctor` — permission preflight — **S**
- **What.** One command that checks and *explains*: device present; **transport
  (USB or BLE) and whether writes are framed correctly for it**; Input
  Monitoring granted (we can open `0xFF00`); Accessibility granted (osascript
  can type); hooks installed and pointing at this binary; daemon running;
  battery level; **and whether the ChatGPT/Codex app is running and contending
  for the pad.** Every failure prints the exact fix, and where possible opens
  the right System Settings pane.
- **Beats.** The vendor Settings pane shows connection and Input Monitoring
  status. Ours covers strictly more, including the contention case that will
  otherwise be our single most common bug report.
- **The rule that makes or breaks this command.** **Return codes are not a
  health signal.** A malformed BLE write still returns `kIOReturnSuccess` and is
  silently discarded by the device — so a doctor that checks "did the write
  succeed" will cheerfully report a green board while the pad does nothing,
  which is worse than no doctor at all. The only valid check is a **round trip**:
  send `device.status` and wait for the reply. Everything else is a guess.
  Apply the same rule everywhere we assert health, not just in `doctor`.
- **How.** Small probes plus a curated message per failure, each one built on a
  round-trip where a round-trip is possible. `open
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"`.
- **Effort:** S. Highest support-cost-avoided per hour of work in the document.

### 7.3 Menubar status item — **M**
- **What.** State colour at a glance, session list, pause/resume, quiet mode,
  reload config, open the editor.
- **Beats.** Parity, not advantage — but its absence is the most visible way we
  look less finished than the vendor app.
- **How.** `rumps` (PyObjC) on macOS. Keep it a thin daemon client so it stays
  optional and the core stays zero-dependency.
- **Effort:** M.

### 7.4 Web keymap editor with **live preview on the real device** — **L**
- **What.** A local page served by the daemon: a pad diagram, click a key, pick
  an action, pick a colour — and the physical key changes **as you hover the
  swatch**, before you save.
- **Beats.** This is the flagship. The vendor app lets you pick a glyph from a
  fixed set and assign a shortcut; it has no lighting editor at all, and
  certainly no live preview. `lights.preview` is explicitly the live,
  not-persisted-to-flash path — the firmware was built for this and the vendor
  app does not use it this way.
- **How.** Daemon serves static HTML + a WebSocket; hover → `lights.preview` /
  `v.oai.thstatus` frame at top compositor priority with a short TTL, so
  releasing the hover restores agent state automatically. Save writes the same
  JSON schema people hand-edit today — the file stays the source of truth, the
  editor is a convenience, and neither one is privileged.
- **Effort:** L. Sequence it after §4.1 and §7.1, which it depends on entirely.

### 7.5 Zero-config dictation — bind `fn`, and the backend upgrade it needs — **M**
- **What.** Push-to-talk should require **no setup in the dictation app at all**.
  Wispr Flow's trigger is the **`fn` key**, and it acts on it whenever it is
  running — so if the pad's mic key simply emits `fn`, dictation works the
  moment Wispr Flow is open, with nothing to configure on either side. Same
  story for macOS's built-in dictation, which is also an `fn` trigger.
- **Beats.** Today `default_keymap.json` ships `ACT06` as
  `ctrl+option+cmd+d` and asks the user to go set that same combo inside Wispr
  Flow's settings. That works, but it is a configuration step in a *second
  application* before the pad does anything — the exact kind of onboarding
  friction that loses people in the first five minutes. Binding `fn` deletes the
  step entirely. It also degrades well: no dictation app running, nothing
  happens.
- **The catch, and it is a real one.** **We cannot currently send `fn`.** The
  AppleScript backend goes through System Events, whose `keystroke` / `key code`
  commands accept only `command`, `control`, `option` and `shift` — which is
  exactly what `MODIFIER_ORDER` in `input/keys.py` encodes. There is no `fn`
  modifier at that layer, because macOS handles `fn` below the synthetic-event
  API. So this feature is not a config change; it needs a different delivery
  path.
- **How.** Add a **CGEvent backend** (`Quartz.CGEventCreateKeyboardEvent` +
  `CGEventSetFlags`) alongside `AppleScriptBackend`. The `Backend` seam in
  `input/actions.py` already exists precisely so this is a drop-in. It buys
  three things at once:
  1. `fn` becomes expressible via `kCGEventFlagMaskSecondaryFn`;
  2. **true key-hold** — post key-down on press and key-up on release, which
     turns push-to-talk into actual *hold*-to-talk. The current keymap comment
     correctly notes that synthetic events cannot hold a key and tells users to
     switch their dictation app to toggle mode. With CGEvent and the press/release
     data we already get from `v.oai.hid`, that limitation goes away;
  3. lower latency, since every keystroke today spawns an `osascript` process.
- **UNVERIFIED.** Whether a *synthetic* `fn` flag actually triggers Wispr Flow
  is not established. Apps that watch for `fn` often tap at the IOHID level,
  where a CGEvent-injected flag may not register. This is a ten-minute test
  (post the event, see if Flow opens) and it should be run **before** this is
  planned — if synthetic `fn` does not take, the current explicit-hotkey binding
  stays the right default and the CGEvent backend is still worth having for
  hold-to-talk and latency alone.
- **Effort:** M — the backend is the work, not the binding. Keep AppleScript as
  the fallback for anyone without PyObjC, since it is the zero-dependency path
  and it works for everything except `fn` and key-hold.

### 7.6 Simulator / no-hardware mode — **S**
- **What.** `freemicro simulate` — a keyboard-driven virtual pad that emits real
  protocol messages into the bridge, with an ASCII rendering of the LED frame.
- **Beats.** Not a user feature; a **contributor** feature. Almost nobody has
  this hardware. Without a simulator the project can only be developed by people
  who own the device, which caps the contributor pool at approximately the
  maintainer.
- **How.** `RecordingBackend` already proves the seam works. Add a fake device
  that accepts sends and synthesises `v.oai.hid` / `v.oai.rad` messages —
  including `ENC_CW` / `ENC_CC`, so dial acceleration (§1.7) is tunable without
  hardware.
- **Effort:** S, and it pays for itself immediately in the test suite.

### 7.7 Connection liveness and reconnect — **M**
- **What.** The daemon treats the pad as something that comes and goes: detects
  disconnect, reconnects automatically, re-applies the current LED frame on
  reattach, and switches framing when the transport changes underneath it.
- **Why this is now a feature and not housekeeping.** A tethered pad is either
  plugged in or not. A **BLE** pad sleeps, wanders out of range, gets picked up
  and carried to the couch, and comes back on a different transport with
  **different write framing** (63 bytes unprefixed on USB, 64 bytes with `0x06`
  at `buffer[0]` on BLE). Get that wrong and — per the trap in §7.2 — nothing
  errors. The lights just quietly stop being true, which is the worst possible
  failure for a status display: it does not go dark, it goes *stale*, and you
  keep trusting it.
- **How.** Three pieces, all in the daemon (§7.1):
  1. **Heartbeat.** A periodic `device.status` round-trip doubles as the
     liveness probe and the battery poll (§4.7). Miss N in a row → declare the
     link down.
  2. **Transport-aware framing.** Detect USB vs BLE at open time (product string
     and `Transport` property differ) and select the writer accordingly. This
     belongs in `device/codex_micro.py` behind the existing `Device.send`, so
     nothing above it ever knows which transport it is on.
  3. **Say so.** Link down → `run` prints it and the menu bar shows it, exactly
     as when no pad is present. There is no fallback surface to fall through to
     any more, which makes *announcing* the drop the whole of the mitigation:
     silence and "nothing needs you" must never look the same.
- **Effort:** M, and it is the difference between a demo and something you leave
  running for a week.

---

## 8. The unexplored firmware surface — **ALL UNVERIFIED**

Speculative. Every item here needs a probe before it is worth an hour of
planning; several may turn out to be no-ops or vendor-app-specific.

| Method | Plausible use | Confidence |
|---|---|---|
| `mp.write_info` | Named "media metadata," but if it renders arbitrary text somewhere visible it becomes a **status line**: current branch, session name, `waiting on: Bash(rm -rf)`. That would beat every light-based idea in §4 combined. | **UNVERIFIED**, high value, probe first |
| `mp.write_artwork` | If it accepts a bitmap, per-key or per-screen custom glyphs — beating the vendor app's fixed 34-icon "Edit keycap" set with arbitrary user artwork. | **UNVERIFIED**, format unknown |
| `ui.active_screen` | Implies the device has multiple screens/views. Possibly switchable to a view we can drive. | **UNVERIFIED**, read-only probe is safe |
| `ui.home_accent_color` | A persistent accent independent of the live lighting path — a "which profile am I on" indicator that survives our process exiting. | **UNVERIFIED**, low risk, low value |
| `fs.list/read/write/readbin/writebin/delete` | A device filesystem. Best case: **the pad carries its own keymap** and works identically on any machine you plug it into, which no competitor offers. Also where artwork and screens likely live — `fs.list` alone would explain a lot. | **UNVERIFIED**. `fs.list`/`fs.read` are safe; **`fs.write`/`fs.delete` can plausibly brick a device with an ESP32 serial-ROM recovery path.** Do not write until we can restore. |
| `host.focused_app` | Host→device only; gives us no context to read. Send it for whatever firmware behaviour it enables, and to look like a well-behaved host. | **UNVERIFIED** effect |
| `wlsdk.<name>` | A Work Louder widget SDK. If it is a documented extension point, it may be the *intended* way to put custom content on the device. | **UNVERIFIED**, worth a search for public SDK docs |
| `device.status.layer_index` | If the device changes `layer_index` on its own (an on-device layer control we have not identified), polling it is a free extra input. | **UNVERIFIED** |

**Rule for this section:** read-only probes (`fs.list`, `fs.read`,
`ui.active_screen`, `device.status`) are fair game now. Writes to `fs.*` wait
until there is a documented recovery path. `sys.bootloader` should never be
called by anything but an explicitly-flagged command.

---

## 9. Cross-platform

The vendor app is macOS-only. Being the thing that works on Linux is a real,
defensible position — and Linux is where a large share of terminal-first agent
users are.

| Platform | Transport | Keystroke synthesis | Focus detection | Risk |
|---|---|---|---|---|
| **macOS** | IOKit `IOHIDDevice` (working — `hidapi.open_path` fails on this device, see `PROTOCOL.md`) | AppleScript System Events (working, but **cannot express `fn` and cannot hold a key** — see §7.5); CGEvent backend fixes both | NSWorkspace | Input Monitoring **and** Accessibility grants; the permission UX is the whole battle |
| **Linux** | `hidraw` — should be simpler than macOS: no permission dialog, just a udev rule for `303a:8360` | `ydotool` (uinput, needs a daemon/group) or `wtype` (Wayland) or `xdotool` (X11) | X11 `_NET_ACTIVE_WINDOW`; Wayland is compositor-specific | Synthesis is the hard part, not HID. Wayland has no portable synthetic-input API — be explicit that it is best-effort |
| **Windows** | `HidD_*` via `ctypes`, or `hidapi` (which generally works there) | `SendInput` via `ctypes` | `GetForegroundWindow` | Least risky of the three technically; smallest overlap with the audience |

**Transport is a second portability axis, and it cuts across all three rows.**
The pad speaks USB *and* BLE, with different write framing (63 bytes unprefixed
vs 64 bytes with `0x06` at `buffer[0]`; feature-type reports are rejected over
BLE). Any port must handle both, and must treat a successful write as
meaningless — the round-trip rule in §7.2 is not macOS-specific. The good news
is that the split belongs entirely inside `device/codex_micro.py` behind
`Device.send`, so it is one file's problem on every platform rather than a
concern that leaks upward.

- **Effort:** M for Linux HID transport (likely easier than the macOS path we
  already shipped), M for Linux keystroke synthesis (tool-dependent), M for
  Windows.
- **How.** The seams already exist: `Backend` abstracts synthesis, and
  `device/codex_micro.py` isolates transport behind `open_device()` /
  `Device.send` / `Device.stream`. Port those two files, not the project.
- **Priority.** Linux next, Windows on demand. The Path B capture guidance in
  `LED-STRATEGY.md` already assumes Linux/Windows tooling, so contributors will
  be on those platforms anyway.

---

## 10. Top 10, ship-first

Ranked by leverage — what unblocks the most other work, or best demonstrates
something the vendor app structurally cannot do.

> **Re-ranked 2026-07-23** after encoder rotation and full BLE duplex were
> confirmed. Two changes: **dial → reasoning effort enters at #5** (newly
> unblocked, owner-requested, and a direct head-to-head with the vendor app's
> headline knob feature), and **Linux support moves just below the line** — it
> is still valuable, but it serves an audience we do not have yet, while
> everything now above it serves the owner and the first users today. The
> untethered pad also folds reconnect handling into #2 rather than adding a row.

| # | Item | Effort | Why it is here |
|---|---|---|---|
| 1 | **`freemicro doctor` preflight** (§7.2) | S | Cheapest item that most changes whether a stranger succeeds. Two macOS permissions plus vendor-app contention will otherwise be every support thread we ever have. Now also the home of the **round-trip rule** — a malformed BLE write returns success and is silently discarded, so a doctor built on return codes would report green while the pad does nothing. |
| 2 | **Daemon + socket IPC, with liveness and reconnect** (§7.1, §7.7) | M | Only one process can own the device. Menubar, editor, hooks and CLI all collide until this exists. Wireless raises the stakes: a BLE pad sleeps, wanders off, and returns on different write framing, and the failure mode is a *stale* display rather than a dark one. The heartbeat that fixes it also carries the battery number. |
| 3 | **LED compositor + light-source registry** (§4.1) | M | Turns "LEDs show agent state" into "LEDs are a display." Every idea in §4, plus §1.4 and §6.3, depends on it. Mirrors the existing `@action` pattern, so it is a known-good shape. |
| 4 | **Gesture layer: tap/double/hold + hold-as-modifier** (§1.1–1.2) | S | Roughly triples the bindable surface for one small state machine on a decode path that is already pure and tested. Best capability-per-line ratio in the document. |
| 5 | **Dial → reasoning effort, with the six-key gauge** (§1.7–1.8, §4.2) | M | Newly unblocked by `ENC_CW`/`ENC_CC`, and explicitly wanted. It is the vendor app's *headline* knob feature, on an agent they do not support, done better: their knob pops a slider on screen, ours shows the level on the pad where your hand already is. Also the best possible justification for the gauge in §4.2. Keep the effort ladder in config — the mechanism is agent-specific and will move. |
| 6 | **Attention queue on the six Agent Keys** (§3.3) | S | The multi-agent story in its sharpest form, and it is nearly free — the state store already holds everything needed. Reframes the pad from dashboard to work surface. |
| 7 | **Per-project keymaps + trust prompt** (§2.1) | S | Config that follows the repo is what makes a 14-key pad stop running out of keys. Ship the trust prompt *with* it, never after. |
| 8 | **Agent-state layers + confirm guard** (§2.3, §6.3) | S | Approve/reject on the key that is currently glowing amber. The clearest thing that is only possible when one process owns input and light on one wire. |
| 9 | **Simulator / no-hardware mode** (§7.6) | S | Contributor-facing, so it looks low-priority and is not. Almost nobody owns this hardware; without it, the contributor pool is one person. Now also the only way to tune dial acceleration without a pad in hand. |
| 10 | **Web keymap editor with live device preview** (§7.4) | L | The flagship demo and the strongest single answer to "why not just use the official app." Ranked last only because it needs #2 and #3 first — start it the moment they land. |

**Just below the line:** **Linux support (§9)** — dropped out on this pass, not
devalued; it is still the clearest structural answer to a macOS-only vendor app,
and the transport work is one file. Promote it the moment a second contributor
shows up. **Light shows (§4.9)** — S once the compositor lands, and the best demo
asset the project can produce; if the README needs a GIF before it needs a
feature, promote this into the table. **Dial modes beyond reasoning effort
(§1.7)** — nearly free once #5 ships, so treat them as the same work item.
Analog joystick modes (§1.5) — high delight, but pick a mode and ship it rather
than building all three. CI status (§4.4) — the most requested ambient display,
gated on §4.1. Context budget gauge (§4.6) — the number people most want and
cannot see, held back only by estimation risk. Probing `mp.write_info` (§8) — an
hour of probing that could reorder this entire table if the device turns out to
render text.

---

## 11. Explicitly out of scope

Being clear about what we will not build is what keeps the rest shippable.

| Not building | Why |
|---|---|
| **A hosted action marketplace** | Accounts, moderation, and uptime for a device with a small user base. A git-hosted `presets/index.json` gets 90 % of the value for ~1 % of the work (§5.3). Revisit only if the index gets unwieldy. |
| **A general keystroke recorder / whole-keyboard remapper** | Karabiner, Hammerspoon, and AutoHotkey are better at this and already installed. We are the *pad's* listener, not a system-wide input layer. Recording our own dispatch log (§6.4) is the version that is both easy and reliable. |
| **General undo** | We type into other applications. We cannot un-type or un-push. A confirm guard is honest; an undo button is not (§6.5). |
| **Firmware reflash / custom firmware** | `SPEC.md` and `LED-STRATEGY.md` already settled this: the shipping unit is ESP32, `work_louder/micro` QMK is RP2040-only, and there is no unbrickable UF2 path. Everything in this document is achievable over the existing vendor channel anyway. |
| **`fs.write` / `fs.delete` experiments** | Writing an unknown filesystem on a device with a serial-ROM recovery path is how you brick hardware nobody can replace yet. Read-only probes now; writes when there is a documented restore (§8). |
| ~~**Bluetooth transport**~~ — **retracted 2026-07-23** | This row previously said BLE could wait. That was wrong, and on a false premise: BLE is **fully duplex today** — input, lighting and `device.status` all verified with the cable out. The earlier "LEDs need USB" belief came from malformed writes, not an incapable transport. BLE is **in scope**, and the untethered pad is a positioning advantage (§7.7). |
| **An Electron/SwiftUI desktop app** | A menubar item plus a locally-served editor delivers the same value with a fraction of the surface, keeps the core zero-dependency, and does not fork per platform (§7.3, §7.4). |
| **Cloud sync, accounts, telemetry** | Config is a JSON file; sync it with the tool you already sync dotfiles with. Telemetry on a developer tool that reads your agent's state is a trust cost we do not need to pay. |
| **Chasing Codex thread-state parity** | There is no public API for Codex chat state, and the vendor app is better positioned to show it than we will ever be. We win by driving *other* agents and *other* data, not by reimplementing theirs. |
| **Shipping our own keycap glyph set** | Interesting only if `mp.write_artwork` turns out to accept bitmaps (**UNVERIFIED**). Until then it is art direction for a feature that may not exist. |
| **Voice / dictation implementation** | The current design — bind a hotkey and let the user's dictation app own it — is correct. Implementing capture, VAD, and transcription would be a second product. |
| **Smooth host-driven animation** (per-frame interpolation, fades, 30 fps effects) | Each lighting call replaces the previous one and rapid sends visibly coalesce, so "smooth" is not achievable — you would be flooding the wire that also carries key events in order to drop most of your frames. Use the seven firmware effects for continuous motion (§4.3), and **paced keyframe choreography at 6–10 fps for events (§4.9)**. Those two cover the space; interpolation between them does not exist on this hardware. |
