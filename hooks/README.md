# Claude Code hooks

FreeMicro drives itself entirely from Claude Code's lifecycle hooks. You don't
edit anything here by hand - run:

```sh
freemicro install
```

That merges a single command, `freemicro hook`, into your Claude Code
`settings.json` for the events FreeMicro cares about
(`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
`SessionEnd`). The merge is **idempotent** (safe to run twice) and
**non-destructive** (it never removes hooks it didn't add).

Each hook invocation reads the event JSON on stdin, classifies it into an
`AgentState`, and updates the per-session state store. The classifier and
installer live in the package:

- `src/freemicro/state/hooks.py` - event → state mapping
- `src/freemicro/hooks_install.py` - the settings merge

Preview what would be written without touching your settings:

```sh
freemicro install --dry-run
```
