"""Input layer: pad events in, user-configured actions out.

The Codex Micro's keys do **not** emit ordinary scancodes - they are vendor
JSON-RPC notifications on the pad's ``0xFF00`` collection - so this package is
what makes the pad do anything at all. Three pieces, kept separate so each is
testable on its own:

* :mod:`freemicro.input.keys` - key *names* to macOS keystrokes (pure).
* :mod:`freemicro.input.actions` - the extensible action registry and the
  backends that deliver them.
* :mod:`freemicro.input.bridge` - routing device events to bound actions.

The bindings themselves live in :mod:`freemicro.padconfig`, because the same
user-owned file also carries the LED colours the renderers read.
"""

from __future__ import annotations

__all__: list = []
