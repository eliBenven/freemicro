<!-- Thanks for contributing to FreeMicro! -->

## What does this change?

<!-- A short description. Link any related issue. -->

## Type

- [ ] New renderer / hardware target
- [ ] New agent harness (Codex CLI, Cursor, …)
- [ ] Input layout / preset
- [ ] Bug fix
- [ ] Docs
- [ ] Other

## Checklist

- [ ] `pytest` is green
- [ ] `ruff check .` is clean
- [ ] The `screen` fallback is still always reachable (renderer changes)
- [ ] Core still imports with neither `hidapi` nor `busylight-core` installed
- [ ] Hardware renderers stay `experimental` until validated with a capability-DB entry
- [ ] Updated the README / SPEC / CHANGELOG if behavior changed

## Hardware tested (if applicable)

<!-- Paste `freemicro detect --json` and say what the LED write actually did. -->
