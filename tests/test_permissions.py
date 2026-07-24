"""The macOS permission probes.

The probes themselves are one ctypes call each and their *answer* depends on
the machine, so what is asserted here is the contract around them: they never
raise, never prompt, never open a settings pane by accident, and the words
they hand a user name the right app and the right pane.
"""

from __future__ import annotations

import pytest

from freemicro import permissions


def test_input_monitoring_answers_without_raising():
    granted, detail = permissions.input_monitoring()
    assert granted in (True, False, None)
    assert isinstance(detail, str) and detail


def test_accessibility_answers_without_raising():
    granted, detail = permissions.accessibility()
    assert isinstance(granted, bool)
    assert isinstance(detail, str) and detail


def test_undecided_is_distinct_from_denied(monkeypatch):
    """"macOS hasn't asked yet" must not be reported as "you were denied".

    Denied sends someone to a settings pane to flip a switch. Undecided means
    the switch does not exist there yet, and telling them otherwise sends them
    hunting for a checkbox they cannot find.
    """
    class FakeIOKit:
        class IOHIDCheckAccess:
            restype = None
            argtypes = None

            def __call__(self, _request):
                return 2  # kIOHIDAccessTypeUnknown

        IOHIDCheckAccess = IOHIDCheckAccess()

    monkeypatch.setattr(permissions, "_iokit", lambda: FakeIOKit())
    granted, detail = permissions.input_monitoring()
    assert granted is None
    assert "not been asked" in detail


def test_denied_is_reported_as_denied(monkeypatch):
    class FakeIOKit:
        class IOHIDCheckAccess:
            restype = None
            argtypes = None

            def __call__(self, _request):
                return 1  # kIOHIDAccessTypeDenied

        IOHIDCheckAccess = IOHIDCheckAccess()

    monkeypatch.setattr(permissions, "_iokit", lambda: FakeIOKit())
    assert permissions.input_monitoring() == (False, "denied")


@pytest.mark.parametrize("key,pane_word", [
    ("input_monitoring", "Input Monitoring"),
    ("accessibility", "Accessibility"),
])
def test_fix_text_names_the_pane_the_app_and_the_restart(key, pane_word):
    text = permissions.fix_text(key)
    assert pane_word in text
    assert "System Settings" in text
    # The step everyone forgets, and the reason "I granted it and nothing
    # changed" is the most common report.
    assert "QUIT AND REOPEN" in text
    assert permissions.host_app() in text


def test_host_app_prefers_the_bundle_it_knows(monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "com.googlecode.iterm2")
    assert permissions.host_app() == "iTerm"


def test_host_app_falls_back_to_term_program(monkeypatch):
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "Ghostty")
    assert permissions.host_app() == "Ghostty"


def test_host_app_always_says_something(monkeypatch):
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert permissions.host_app() == "your terminal app"


def test_panes_are_the_real_deep_links():
    assert permissions.PANE_INPUT_MONITORING.endswith("Privacy_ListenEvent")
    assert permissions.PANE_ACCESSIBILITY.endswith("Privacy_Accessibility")
    assert all(
        pane.startswith("x-apple.systempreferences:")
        for pane in (permissions.PANE_INPUT_MONITORING,
                     permissions.PANE_ACCESSIBILITY)
    )


def test_open_pane_is_never_called_by_a_probe(monkeypatch):
    """No probe may open a window. `start` opens panes; nothing else does."""
    opened = []
    monkeypatch.setattr(permissions, "open_pane", lambda url: opened.append(url))
    permissions.input_monitoring()
    permissions.accessibility()
    permissions.fix_text("accessibility")
    assert opened == []
