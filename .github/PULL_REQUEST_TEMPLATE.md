<!-- Thanks for contributing to FreeMicro! -->

## What does this change?

<!-- A short description. Link any related issue. -->

## Type

- [ ] New renderer / hardware target
- [ ] New agent harness (Codex CLI, Cursor, …)
- [ ] Key bindings / default keymap
- [ ] Bug fix
- [ ] Docs
- [ ] Other

## Checklist

- [ ] `pytest` is green
- [ ] `ruff check .` is clean
- [ ] No test needs hardware, types anything, or opens a window
- [ ] Nothing imports `tkinter` (it can `abort()` the process, uncatchably)
- [ ] Core still imports without `hidapi` installed
- [ ] Hardware renderers stay `experimental` until validated with a capability-DB entry
- [ ] Updated the README / SPEC / CHANGELOG if behavior changed

## Security

Tick what applies; if none apply, say so.

- [ ] This adds or changes an **action kind**. If it can run code, launch an app
      or synthesise input, it is declared as such and covered by
      [`docs/SECURITY-MODEL.md`](../docs/SECURITY-MODEL.md) §1.
- [ ] This changes what ends up in `freemicro doctor --report`. The report is
      pasted into public issues - `shell`/`applescript` contents and paths
      outside the config directory must stay redacted, and
      `tests/test_diagnostics.py` still proves it.
- [ ] This touches the web UI's binding, token or `Host` checks.
- [ ] This changes what FreeMicro reads or writes outside its own config
      directory.
- [ ] None of the above.

## Hardware tested (if applicable)

<!--
Paste `freemicro detect --json`, say what the LED write actually did, and give
the firmware version from `freemicro doctor` - one unit on v0.4.1 is the whole
evidence base, so a second data point is genuinely valuable.
-->
