"""Tests for the redacted diagnostic report.

The report exists to be pasted into a **public** issue tracker, so most of this
file is one assertion made many ways: *the secret is not in the output.* A
report that leaks a shell command with an API token in it would do more harm
than the bug it was helping to fix.
"""

from __future__ import annotations

import json

import pytest

from freemicro import diagnostics, padconfig

#: Strings that must never appear in any rendering of the report. Distinctive
#: enough that a substring match is meaningful.
SECRET_COMMAND = "curl -H 'Authorization: Bearer sk-TOPSECRET-42' https://acme.invalid"
SECRET_SCRIPT = 'display dialog "vault passphrase hunter2-TOPSECRET"'
SECRET_CWD = "/Users/somebody/Clients/AcmeCorp-Confidential"


def _config_with_secrets(tmp_path):
    document = {
        "version": 1,
        "bindings": {
            "AG00": {"action": "text", "text": "/resume", "submit": True},
            "AG01": {
                "action": "shell",
                "command": SECRET_COMMAND,
                "cwd": SECRET_CWD,
                "label": "deploy",
            },
            "AG02": {"action": "applescript", "script": SECRET_SCRIPT},
            "ACT06": {"action": "key", "key": "escape"},
        },
    }
    path = tmp_path / "keymap.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Redaction primitives
# ---------------------------------------------------------------------------

def test_redact_text_scrubs_the_home_directory(monkeypatch, tmp_path):
    home = tmp_path / "home" / "somebody"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "somebody")
    scrubbed = diagnostics.redact_text(f"failed to open {home}/secret.json")
    assert str(home) not in scrubbed
    assert "~" in scrubbed


def test_redact_text_scrubs_the_account_name(monkeypatch):
    monkeypatch.setenv("USER", "jrandomhacker")
    assert "jrandomhacker" not in diagnostics.redact_text("hi jrandomhacker")


def test_redact_text_handles_none_and_non_strings():
    assert diagnostics.redact_text(None) == ""
    assert diagnostics.redact_text(17) == "17"


def test_redact_path_keeps_config_paths_and_drops_everything_else():
    inside = padconfig.user_path()
    assert diagnostics.redact_path(inside).startswith("<config>")
    assert diagnostics.redact_path(inside).endswith("keymap.json")
    assert diagnostics.redact_path(SECRET_CWD) == "<path>"
    assert diagnostics.redact_path("/etc/passwd") == "<path>"
    assert diagnostics.redact_path(None) == ""


def test_redact_path_marks_files_shipped_with_the_package():
    marked = diagnostics.redact_path(padconfig.DEFAULT_CONFIG_PATH)
    assert marked.startswith("<freemicro>")
    assert marked.endswith("default_keymap.json")


def test_truncate_marks_that_it_truncated():
    long = "x" * 500
    out = diagnostics.truncate(long, 10)
    assert len(out) < len(long)
    assert "+490" in out


# ---------------------------------------------------------------------------
# The redaction that matters
# ---------------------------------------------------------------------------

def test_shell_and_applescript_contents_never_reach_the_report(tmp_path):
    path = _config_with_secrets(tmp_path)
    report = diagnostics.collect(probe_device=False, config_path=path)
    blob = json.dumps(report)
    text = diagnostics.render_text(report)
    for secret in (SECRET_COMMAND, SECRET_SCRIPT, SECRET_CWD, "sk-TOPSECRET-42",
                   "hunter2-TOPSECRET", "AcmeCorp-Confidential"):
        assert secret not in blob, f"{secret!r} leaked into the JSON report"
        assert secret not in text, f"{secret!r} leaked into the text report"


def test_the_report_still_says_a_shell_binding_exists(tmp_path):
    """Redaction must not cost us the diagnosis: structure survives."""
    path = _config_with_secrets(tmp_path)
    config = diagnostics.collect(probe_device=False, config_path=path)["config"]
    assert config["valid"] is True
    assert config["action_kinds"]["shell"] == 1
    assert config["action_kinds"]["applescript"] == 1
    assert config["unsafe_binding_count"] == 2
    shell = [b for b in config["bindings"] if b["action"] == "shell"][0]
    assert shell["input"] == "AG01"
    assert shell["redacted"] is True
    assert diagnostics.REDACTED in shell["detail"]
    assert "chars" in shell["detail"]


def test_harmless_bindings_are_reported_in_full(tmp_path):
    path = _config_with_secrets(tmp_path)
    config = diagnostics.collect(probe_device=False, config_path=path)["config"]
    text_binding = [b for b in config["bindings"] if b["input"] == "AG00"][0]
    assert text_binding["redacted"] is False
    assert text_binding["detail"]["text"] == "/resume"


def test_unsafe_kinds_match_the_action_registry():
    """If someone adds an arbitrary-code action kind, this test fails loudly."""
    from freemicro.input.actions import REGISTRY

    for kind in diagnostics.UNSAFE_KINDS:
        assert kind in REGISTRY, f"{kind} is redacted but no longer exists"
    # Any kind that takes a free-form command or script must be redacted.
    for kind, spec in REGISTRY.items():
        risky = {"command", "script"} & set(spec.fields)
        if risky:
            assert kind in diagnostics.UNSAFE_KINDS, (
                f"action {kind!r} takes {sorted(risky)} but is not redacted; "
                "add it to diagnostics.UNSAFE_KINDS"
            )


def test_no_absolute_home_paths_survive_anywhere(tmp_path, monkeypatch):
    home = tmp_path / "home" / "somebody"
    (home / ".freemicro").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "somebody")
    report = diagnostics.collect(probe_device=False)
    blob = json.dumps(report) + diagnostics.render_text(report)
    assert str(home) not in blob


def test_environment_section_reports_presence_not_values(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_KEYMAP", str(tmp_path / "secret-location.json"))
    report = diagnostics.collect(probe_device=False, config_path=None)
    assert report["environment"]["FREEMICRO_KEYMAP"] is True
    assert "secret-location" not in json.dumps(report)


# ---------------------------------------------------------------------------
# Shape and robustness
# ---------------------------------------------------------------------------

def test_collect_returns_every_section():
    report = diagnostics.collect(probe_device=False)
    for key in (
        "schema", "generated", "redaction", "freemicro", "system",
        "environment", "permissions", "device", "renderers", "hooks",
        "daemon", "config",
    ):
        assert key in report, f"missing section {key}"
    assert report["schema"] == diagnostics.SCHEMA
    assert report["freemicro"]["version"]


def test_device_section_is_absent_when_no_device_is_allowed():
    """conftest sets FREEMICRO_NO_DEVICE=1, so this must never open hardware."""
    device = diagnostics.collect(probe_device=True)["device"]
    assert device["present"] is False
    assert device["transport"] is None
    assert device["firmware"]["status"] == "missing"


def test_json_output_is_valid_json_and_text_output_is_readable():
    report = diagnostics.collect(probe_device=False)
    parsed = json.loads(diagnostics.render_json(report))
    assert parsed["schema"] == diagnostics.SCHEMA
    text = diagnostics.render_text(report)
    for heading in ("System", "Permissions", "Device", "Renderers", "Config"):
        assert heading in text


def test_bundle_gives_both_forms_from_one_collection():
    result = diagnostics.bundle(probe_device=False)
    assert set(result) == {"report", "text", "json"}
    assert json.loads(result["json"])["schema"] == diagnostics.SCHEMA
    assert result["text"].startswith("freemicro ")


def test_an_invalid_config_is_reported_not_raised(tmp_path):
    broken = tmp_path / "keymap.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    config = diagnostics.collect(probe_device=False, config_path=broken)["config"]
    assert config["valid"] is False
    assert config["error"]
    assert config["binding_count"] == 0


def test_a_config_that_fails_validation_is_reported_not_raised(tmp_path):
    bad = tmp_path / "keymap.json"
    bad.write_text(
        json.dumps({"version": 1, "bindings": {"AG00": {"action": "nope"}}}),
        encoding="utf-8",
    )
    config = diagnostics.collect(probe_device=False, config_path=bad)["config"]
    assert config["valid"] is False
    assert "nope" in config["error"]


def test_a_failing_section_does_not_lose_the_report(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(diagnostics, "_permissions_section", boom)
    report = diagnostics.collect(probe_device=False)
    assert "error" in report["permissions"]
    assert report["system"]["platform"]


def test_render_text_never_crashes_on_a_sparse_report():
    """`doctor --report` must survive a report assembled on a broken machine."""
    text = diagnostics.render_text({"schema": 1, "generated": ""})
    assert "diagnostic report" in text


@pytest.mark.parametrize("probe", [True, False])
def test_collect_is_side_effect_free_for_the_config(tmp_path, probe):
    path = _config_with_secrets(tmp_path)
    before = path.read_bytes()
    diagnostics.collect(probe_device=probe, config_path=path)
    assert path.read_bytes() == before
