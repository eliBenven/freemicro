# Security policy

## Read this first: FreeMicro runs code by design

FreeMicro exists to make a macro pad press keys and run commands for you. So
before anything else:

- **A pad config is a program.** It can type into any application, run shell
  commands, run AppleScript, launch apps, and move and click the mouse. Trust a
  config exactly as much as you would trust a shell script from the same source.
- **The two macOS permissions are granted to your terminal, not to FreeMicro.**
  Input Monitoring and Accessibility attach to the enclosing app, so they apply
  to everything you run from that terminal.
- **There is no preset trust check yet.** Configs are not verified, hashed or
  sandboxed today. If you run a config you did not write, you are running their
  code.

None of that is a vulnerability - it is the documented design, written up in
full in [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md), including the
[preset-trust design](docs/SECURITY-MODEL.md#4-design-preset-trust) that must
ship alongside shareable presets.

## Supported versions

FreeMicro has not had a tagged release yet. Until `0.1.0` ships, the supported
version is **`main`**. After that, fixes land on the latest released minor
version and on `main`.

| Version | Supported |
|---|---|
| `main` | ✅ |
| Released versions | ✅ latest minor only |

## Reporting a vulnerability

**Please report privately first.** Do not open a public issue for a
vulnerability.

1. **Preferred:** GitHub → the repository's **Security** tab → **Report a
   vulnerability** (private security advisory). This creates a private thread
   with the maintainers and gives you a CVE path if one is warranted.
2. If you cannot use that, open a public issue titled **"security contact
   request"** containing **no details**, and a maintainer will reply with a
   private channel.

### What to include

The more of this you have, the faster it gets fixed:

- What an attacker can do, and what they need first (local code execution as the
  user? a browser tab? physical access? a malicious config?).
- Affected version or commit, macOS version, and whether a pad was attached.
- Reproduction steps, or a proof of concept.
- The output of `freemicro doctor --report`, which is redacted for exactly this
  purpose - it strips the contents of `shell` and `applescript` bindings and any
  path outside the config directory. **Read it before you send it anyway.**

### What to expect

This is a small project maintained by volunteers. Honest targets, not an SLA:

| | Target |
|---|---|
| Acknowledgement | within 5 days |
| Initial assessment | within 14 days |
| Fix or documented mitigation | depends on severity; you will get a plan, not silence |
| Public disclosure | coordinated with you, after a fix is available |

You will be credited in the advisory and in `CHANGELOG.md` unless you would
rather not be.

## Scope

### In scope

- Anything that lets a **config execute code without the user's knowledge**
  beyond what [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md) §1 documents - for example an action kind that runs code without being classified as such.
- Web UI weaknesses: a bypass of the loopback bind check, the bearer token, or
  the `Host` header check; a path traversal in static-file serving; anything
  that lets a web page or another local process write a config.
- **Leaks in the diagnostic report** - anything that gets a shell command, an
  AppleScript, a credential or a personal path into `freemicro doctor --report`
  output. This is meant to be safe to paste in public; if it is not, that is a
  bug worth reporting.
- Privilege escalation, or FreeMicro writing outside its config directory,
  `~/.claude/settings.json`, and its launchd plist.
- The hook installer or the daemon installer writing something exploitable into
  the files they own.
- Anything that makes FreeMicro send data off the machine. The core has no
  network access; if you find some, that is a serious finding.
- Dependency issues in the optional `hidapi` extra as they affect FreeMicro.

### Out of scope

These are documented behaviours, not vulnerabilities. Reports of them will be
closed with a link to the security model:

- A config you installed running its own `shell` or `applescript` bindings.
- A `text` binding typing a command into a focused terminal. That is what typing
  is; see the security model, §1.
- Anything requiring an attacker who already has code execution as your user - they can rewrite the config directly. Noted in the threat model, §3.4.
- The permissions being broad. That is macOS's model for Input Monitoring and
  Accessibility, not a FreeMicro choice.
- The device firmware, the ChatGPT desktop app, or the pad hardware. Report
  those to their vendors.
- Physical access to your keyboard.
- Missing hardening that has no exploit path (please still open a normal issue - those are welcome, just not as advisories).

## Safe harbour

If you make a good-faith effort to follow this policy, we will not pursue or
support any action against you for your research. Please:

- test only against your own machine and your own hardware;
- avoid accessing, modifying or exfiltrating anyone else's data;
- give us a reasonable chance to fix things before going public.

## Hardening checklist for users

- Read any config you did not write. Search it for `"shell"` and
  `"applescript"` before the first key press.
- Prefer a terminal you use only for FreeMicro, so the Accessibility and Input
  Monitoring grants do not cover everything else you run.
- Run `freemicro keys --dry-run` first. It prints what each key *would* do and
  performs nothing.
- Don't install the daemon on a shared machine. It runs unattended at login.
- Run `freemicro web` while you are editing, not as a service.
- After a config change: `freemicro keys --list` to see what FreeMicro resolved,
  and `freemicro daemon logs` if the daemon is running.
