# Releasing FreeMicro

The package is PyPI-ready but **has never been published**. This is the exact
sequence to do it, and the checks that have to pass first.

`freemicro` is unclaimed on PyPI as of this writing; that is worth confirming
before you start, because the name is the one thing you cannot change later
without breaking everyone's install command.

Two halves, deliberately kept apart by
[`.github/workflows/release.yml`](../.github/workflows/release.yml):

* **Pushing a tag** builds the artifacts, verifies them, and opens a **draft**
  GitHub release. It publishes nothing, anywhere.
* **Publishing to PyPI** is a separate, manually dispatched job behind an
  environment approval and a typed confirmation phrase.

A tag is easy to push by accident. A PyPI upload cannot be taken back.

The [Wiring required](#wiring-required) section at the end lists the changes
still needed in files that this document's author did not own.

## Before anything else

```sh
./.venv/bin/python -m pytest        # must be green; never touches hardware
./.venv/bin/python -m ruff check .  # line-length 88
./.venv/bin/freemicro selftest      # the loop itself, end to end
```

Then, on a machine with a pad, the two things no test can assert - that the LEDs
actually change, and that the diagnostic report is safe to publish:

```sh
freemicro lights --cycle            # five colours, 1.5s each, by eye
freemicro doctor --report           # read it before strangers do
```

That second one matters: `doctor --report` is what bug reporters paste into
public issues. It redacts `shell`/`applescript` contents and every path outside
the config directory, but read the output anyway. Anything personal in there is
a release blocker, not a follow-up.

## 1. Version and changelog

The version lives in exactly two places and they must agree:

```sh
grep -n version pyproject.toml src/freemicro/__init__.py
```

Verify mechanically rather than by eye - `release.yml` checks the tag against
the built wheel, but nothing catches these two disagreeing with each other:

```sh
./.venv/bin/python - <<'PY'
import re, pathlib, freemicro
declared = re.search(
    r'^version\s*=\s*"([^"]+)"',
    pathlib.Path("pyproject.toml").read_text(), re.M
).group(1)
assert declared == freemicro.__version__, (declared, freemicro.__version__)
print("version:", declared)
PY
```

Which number to pick - semantic versioning, with one project-specific rule:

| Change | Bump |
|---|---|
| Anything that changes what an existing pad config does | **minor** while `0.x`, **major** after |
| A new action kind, a new command, a new renderer | **minor** |
| Fixes, docs, protocol notes, a new firmware in `compat.KNOWN_GOOD` | **patch** |
| A firmware that needs *different code paths* | **minor**, and say so loudly |

Then move the `## [Unreleased]` heading in `CHANGELOG.md` to
`## [X.Y.Z] - YYYY-MM-DD`, open a fresh `Unreleased` above it, and update the
link definitions at the bottom:

```markdown
[Unreleased]: https://github.com/eliBenven/freemicro/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/eliBenven/freemicro/releases/tag/vX.Y.Z
```

Rules that keep this changelog worth reading:

- Every entry says what changed **for the user**, not which module moved.
- Anything unverified stays under **Known limitations**. Never quietly promote
  "probably works" to "works" - that is the one habit that would make this
  project's honesty claims worthless.
- If a fix came from somebody's hardware report, credit them.
- If a firmware version was added to `compat.KNOWN_GOOD`, say **which
  behaviours were re-verified on it, and by whom**.

## 2. Build

```sh
rm -rf dist build *.egg-info
./.venv/bin/python -m pip install --upgrade build twine
./.venv/bin/python -m build         # produces dist/*.whl and dist/*.tar.gz
./.venv/bin/python -m twine check --strict dist/*
```

`twine check` validates the long description renders on PyPI. It is the one
failure mode you cannot see locally and cannot fix after upload.

## 3. Verify the artifact, not the source tree

This is the step people skip, and it is the only one that catches a missing data
file. Install the built wheel into a **clean** venv and run it from a directory
that has nothing to do with the repo, with no `PYTHONPATH`:

```sh
python3 -m venv /tmp/fm-check
/tmp/fm-check/bin/python -m pip install --upgrade pip
/tmp/fm-check/bin/python -m pip install dist/freemicro-*.whl
cd /tmp
env -u PYTHONPATH /tmp/fm-check/bin/freemicro --version
env -u PYTHONPATH /tmp/fm-check/bin/freemicro doctor
env -u PYTHONPATH /tmp/fm-check/bin/freemicro doctor --report
env -u PYTHONPATH /tmp/fm-check/bin/freemicro keys --list   # reads the packaged keymap
env -u PYTHONPATH /tmp/fm-check/bin/freemicro renderers
/tmp/fm-check/bin/python -m pip list                        # freemicro + pip only
```

`keys --list` is the load-bearing one: it reads `default_keymap.json` out of the
installed package, so it fails loudly if the wheel shipped without it. Repeat
with the sdist (`pip install dist/freemicro-*.tar.gz`) - a wheel can be fine
while the sdist is missing files the build needs. Confirm the sdist carries the
docs a source consumer needs:

```sh
tar -tzf dist/freemicro-*.tar.gz | grep -E "default_keymap.json|LICENSE|README|CHANGELOG|SECURITY"
```

## 4. Tag

```sh
git add pyproject.toml src/freemicro/__init__.py CHANGELOG.md
git commit -m "Release X.Y.Z"
git push origin main
git tag -a vX.Y.Z -m "FreeMicro X.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag runs `.github/workflows/release.yml`, which rebuilds the
artifacts, runs `twine check --strict`, **checks the tag against the built
version**, installs the wheel into a clean venv and smoke-tests it, then opens a
draft GitHub release with the artifacts attached. Nothing is published.

## 5. Upload

TestPyPI first, always. It is free and it is the only rehearsal you get.

```sh
./.venv/bin/python -m twine upload --repository testpypi dist/*
pipx install --pip-args="--index-url https://test.pypi.org/simple/" freemicro
freemicro --version && freemicro doctor
pipx uninstall freemicro
```

Then the real thing. **A version number on PyPI can never be reused**, even
after deleting the release - so this is the point of no return.

**Preferred: the workflow**, so the upload is built from the tag by CI and gated
by an environment approval:

> GitHub → Actions → **Release** → **Run workflow**
> · `tag`: `vX.Y.Z`
> · `confirm`: `publish to pypi`
> then approve the `pypi` environment when it asks.

Manual fallback, if the workflow is not usable:

```sh
./.venv/bin/python -m twine upload dist/*
```

Authenticate with an API token (`__token__` as the username), scoped to this
project once it exists. Prefer a Trusted Publisher on GitHub Actions if you set
one up - it removes the long-lived token entirely (see [W7](#w7--repository-settings--required-not-a-code-change)).

## 6. Confirm and publish the release

```sh
pipx install freemicro
freemicro --version
freemicro start          # the actual first-run experience, on a clean machine
```

Then finish the draft GitHub release: paste the changelog section, **keep the
Known limitations part** (for this project it is the most useful thing in the
notes), and publish.

```sh
gh release view vX.Y.Z --web
# or:
gh release edit vX.Y.Z --notes-file /tmp/notes.md --draft=false
```

The artifacts are already attached by the workflow, so people can verify them
independently.

## 7. Update the README

The Quickstart currently reads:

```sh
pipx install git+https://github.com/eliBenven/freemicro    # not on PyPI yet
```

Once published, that becomes `pipx install freemicro` and the "not on PyPI yet"
note comes out - of the README, of this file, and of any badge.

## After the release

- [ ] `pip install freemicro` from a clean venv, one last time.
- [ ] Open a fresh `Unreleased` section in `CHANGELOG.md`.
- [ ] Announce it wherever hardware reports might come from. Firmware coverage
      is this project's biggest blind spot, and only strangers can fix it.

## Fixing a bad release

PyPI files can never be replaced, only yanked. If a release is broken: yank it
in the PyPI web UI, then ship a patch release with the fix.

```sh
git tag -a vX.Y.Z+1 -m "FreeMicro X.Y.Z+1"
```

Do not delete the tag, and do not force-push over a release commit. Add a
`### Fixed` entry saying what the bad version got wrong - a changelog that
quietly hides a bad release is worse than the bad release.

## When a new firmware shows up

Somebody files a hardware report with a firmware `compat.KNOWN_GOOD` has never
seen. That is the intended flow, not a problem:

1. Reproduce what you can, or ask the reporter to run the checks named in
   `compat.VERIFIED_BEHAVIOUR`.
2. **Protocol unchanged** → add the version to `compat.KNOWN_GOOD`, and record
   in the changelog which behaviours were re-verified and by whom. Adding a
   version nobody actually tested turns `compat` into a liar, which is worse
   than having no compatibility check at all.
3. **Protocol changed** → do not silently branch on version. Write the
   difference into `docs/PROTOCOL.md` first, then decide what the code does.
4. Update `hardware/capabilities.json` either way.

## Why `pipx` is the recommended install

Three reasons, all of them things that actually break:

1. **The binary has to be on `PATH` at an absolute location.** Claude Code's
   hooks and the launchd agent both store an absolute path to `freemicro`; pipx
   gives a stable one under `~/.local/bin` that survives shell configuration.
2. **`~/.local` is not a TCC-protected folder.** A virtualenv under `~/Desktop`,
   `~/Documents` or `~/Downloads` cannot be read by a launchd agent, so the
   daemon dies before Python starts. pipx never puts it there.
3. **Isolation.** FreeMicro's core has no dependencies, but `[all]` pulls in
   `hidapi`; nobody should have to weigh that against their system Python.

## What is deliberately *not* in the wheel

`hardware/capabilities.json` is documentation and a crowdsourcing target, not
runtime data - no code reads it. It ships in the sdist and lives in the repo.
The only runtime data file is `src/freemicro/default_keymap.json`, which is
pinned by the `artifacts` entry in `pyproject.toml`.

---

## Wiring required

Changes needed in files outside this work's ownership. Each is specified
precisely enough to apply directly.

### W1 - `freemicro doctor --report` (`src/freemicro/cli.py`) · **required**

`.github/ISSUE_TEMPLATE/bug_report.yml` asks reporters for this command's
output. `src/freemicro/diagnostics.py` exists and is tested; only the CLI
surface is missing.

**In `build_parser()`**, on the `doctor` subparser (currently
`dr = sub.add_parser("doctor", …)`), add three flags:

```python
    dr.add_argument(
        "--report", action="store_true",
        help="print a redacted diagnostic report to attach to a bug report",
    )
    dr.add_argument(
        "--json", action="store_true",
        help="with --report, emit JSON instead of text",
    )
    dr.add_argument(
        "--no-probe", dest="probe_device", action="store_false", default=True,
        help="with --report, don't open the pad (skips firmware and battery)",
    )
```

**At the very top of `cmd_doctor()`**, before any other work:

```python
    if getattr(args, "report", False):
        from freemicro import diagnostics

        report = diagnostics.collect(
            probe_device=getattr(args, "probe_device", True),
            config_path=Path(args.config).expanduser() if args.config else None,
        )
        if getattr(args, "json", False):
            print(diagnostics.render_json(report))
        else:
            print(diagnostics.render_text(report))
        return 0
```

`diagnostics.collect()` never raises and honours `FREEMICRO_NO_DEVICE`, so this
returns 0 even on a machine where everything else is broken - which is the whole
point of a diagnostic command.

### W2 - firmware compatibility in `doctor` (`src/freemicro/cli.py`) · **required**

`cmd_doctor` prints the raw firmware string today. Make it say whether that
version is one anybody has tested. Inside the `if status is not None:` block,
replace:

```python
        if status.get("version"):
            bits.append(f"firmware {status['version']}")
```

with:

```python
        from freemicro import compat

        firmware = compat.check_status(status)
        bits.append(compat.summary_line(firmware))
```

and after the `check(status is not None, …)` call that follows it, add:

```python
    if status is not None and not firmware.ok:
        # Never a failure - a firmware we have not tested is information.
        for line in firmware.message.splitlines():
            print(f"          {line}")
```

Do **not** turn this into a `check(...)`. `FirmwareReport.blocks` is `False` by
design, and `doctor`'s exit code must not depend on a version string.

### W3 - README (`README.md`) · **required**

1. Add a row to the commands table:

   | `freemicro doctor --report` | A redacted diagnostic bundle to attach to a bug report - strips `shell`/`applescript` contents and personal paths. |

2. Add a **Security** section, before Contributing:

   > ## Security
   >
   > FreeMicro types into other applications and runs shell commands - that is
   > the product, not a side effect. **A pad config is a program**: treat one you
   > did not write exactly like a shell script from the same source. There is no
   > preset trust check yet.
   >
   > [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md) covers what a config can
   > do, what the two macOS permissions really grant (they are granted to your
   > *terminal*, not to FreeMicro, so they cover everything you run from it), the
   > threat model, and the preset-trust design that has to ship alongside
   > shareable presets. To report a vulnerability, see [`SECURITY.md`](SECURITY.md).

3. In the "One unit, one firmware" bullet under **Honest status**, add: "…
   `freemicro doctor` tells you when your firmware differs from the one we
   tested, and never blocks you over it."

### W4 - `pyproject.toml` · **recommended**

Add the new docs to the project URLs so they appear in PyPI's sidebar:

```toml
[project.urls]
Changelog = "https://github.com/eliBenven/freemicro/blob/main/CHANGELOG.md"
Security = "https://github.com/eliBenven/freemicro/blob/main/SECURITY.md"
```

And confirm the sdist ships `CHANGELOG.md`, `SECURITY.md` and `docs/` - step 3's
`tar -tzf` check is what catches it if not.

### W5 - action risk tiers (`src/freemicro/input/actions.py`) · **required before shareable presets**

Per [`docs/SECURITY-MODEL.md` §4.2](SECURITY-MODEL.md#42-risk-tiers-declared-at-the-action-registry):
add a **required** `risk` keyword to the `@action` decorator, store it on
`ActionSpec`, and set it for the eight existing kinds - `none` → `none`;
`text`/`key`/`hold`/`mouse` → `input`; `app` → `launch`;
`shell`/`applescript` → `execute`.

Required with no default is the point: a future action kind that runs code
cannot be added without somebody deciding that it does. Add a test that fails if
any registered kind lacks a `risk`, and one that fails if
`src/freemicro/default_keymap.json` ever gains an `execute` binding - the
shipped default must stay approval-free.

`tests/test_diagnostics.py::test_unsafe_kinds_match_the_action_registry`
already fails loudly if a new kind takes a `command` or `script` parameter and
is not listed in `diagnostics.UNSAFE_KINDS`; keep that in sync too.

### W6 - preset trust (`src/freemicro/trust.py`, new) · **required before shareable presets**

The full design and an implementation checklist are in
[`docs/SECURITY-MODEL.md` §4](SECURITY-MODEL.md#4-design-preset-trust). None of
it can be deferred past the release that lets a config arrive from anywhere
other than the user's own text editor: on that day, `git clone` becomes remote
code execution.

### W7 - repository settings · **required, not a code change**

- **Enable private vulnerability reporting.** Settings → Code security →
  *Private vulnerability reporting* → Enable. Without it, the "Report a
  vulnerability" link in `.github/ISSUE_TEMPLATE/config.yml` 404s and people
  will file security bugs publicly.
- **Create the `pypi` environment** with a required reviewer. Settings →
  Environments → New environment → `pypi` → *Required reviewers*. Until you do,
  the publish job's only gate is the typed confirmation phrase.
- **Configure PyPI trusted publishing** for `pypa/gh-action-pypi-publish`
  (PyPI → project → Publishing → add a GitHub publisher: this repo, workflow
  `release.yml`, environment `pypi`). Otherwise replace that step with a
  `PYPI_API_TOKEN` secret.
- **Decide on a security contact address.** `SECURITY.md` routes everything
  through GitHub private advisories and deliberately publishes no email address.
  If you want one listed, add it there.

### W8 - CI runner note · **watch this one**

`.github/workflows/ci.yml` runs macOS tests on `macos-latest` (arm64) for Python
3.11 - 3.13 and on `macos-13` (x86_64) for 3.9/3.10, because
`actions/setup-python` publishes no macOS arm64 builds for those two versions.
The legacy job is `continue-on-error: true` on purpose - it is supplementary
coverage, and when GitHub retires the `macos-13` image it should be **deleted**,
not repaired. Do not remove 3.9 from `test-linux`; that job is what actually
guarantees the 3.9 floor.
