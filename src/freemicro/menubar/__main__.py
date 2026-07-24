"""``python -m freemicro.menubar`` - the menu bar without any CLI wiring.

This exists so the status item is runnable *today*, before ``freemicro menubar``
is added to the CLI parser (see "Wiring required" in ``docs/MENUBAR.md``). Once
that lands, both entry points call the same :func:`freemicro.menubar.app.main`.
"""

from __future__ import annotations

import sys

from freemicro.menubar.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
