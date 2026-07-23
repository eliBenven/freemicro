# Contributing to FreeMicro

Thanks for helping turn macro pads into agent-status surfaces! There are two
kinds of contribution and **one of them needs no code at all.**

## 🥇 The highest-value contribution: a hardware report

FreeMicro's whole LED path hinges on facts we can only learn from physical
hardware (see [`SPEC.md` §4](SPEC.md)). If you own a Codex Micro — or *any*
VIA/QMK RGB pad — run the read-only probe and share the result:

```sh
pip install "freemicro[detect]"
freemicro detect --json
```

Then open a **Hardware Report** issue and paste the output. That single
paste materially advances the project and grows
[`hardware/capabilities.json`](hardware/capabilities.json). The probe never
writes to your device.

## 🛠️ Code contributions

### Setup

```sh
git clone https://github.com/eliBenven/freemicro && cd freemicro
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest        # should be green in under a second
ruff check .  # lint
```

### Project shape

```
src/freemicro/
  state/      hooks -> normalized AgentState, per-session store (pure, tested)
  renderers/  base registry + screen / busylight / micro-via / micro-qmk
  detector/   read-only HID probe
  input/      host-side preset loading
  cli.py      the `freemicro` command
```

### Principles

1. **The alert never depends on the pad.** Any change to renderer selection
   must keep the `screen` fallback reachable. There is a test for this; keep it
   green.
2. **Core stays dependency-free.** `hidapi` and `busylight-core` are *optional*
   extras. Importing `freemicro` must work with neither installed.
3. **Hooks must never break Claude Code.** The `hook` command swallows its own
   errors and exits 0. A status light is not worth interrupting someone's
   session.
4. **New agent? New file.** Adding Codex CLI / Cursor support should mean a new
   classifier in `state/`, not changes to the engine or renderers.
5. **Experimental means experimental.** Hardware renderers stay `experimental =
   True` and dormant (`available()` False) until validated on real hardware
   with a capability-DB entry.

### Adding a renderer

Subclass `freemicro.renderers.base.Renderer`, implement `available()` and
`render(state)`, and decorate the class with `@register`. Pick a `priority`
(higher = preferred as primary). Add a row to the renderer table in the README.

### Adding a new agent harness

Write a `classify(event) -> AgentState | None` for that agent's events, mirror
the tests in `tests/test_hooks.py`, and wire it into the CLI. The state engine
and every renderer come along for free.

### Commit / PR

- Keep PRs focused; one concern each.
- Add or update tests — `pytest` must stay green.
- Run `ruff check .` before pushing.
- Be kind. See [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Reporting bugs / ideas

Use the issue templates. For anything hardware-shaped, include your
`freemicro detect --json` output — it's almost always the first thing we'll ask
for.
