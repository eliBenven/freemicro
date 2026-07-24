"""The visual pad configurator: a local web UI for ``~/.freemicro/keymap.json``.

Until this existed, the only way to remap a key or change an LED colour was to
hand-edit JSON. For a tactile device with thirteen keys, a dial and a stick that
is the wrong medium - you cannot see which rectangle in a text file is the key
under your finger, and you certainly cannot see what ``#304FFE`` looks like on
frosted plastic. So: a picture of the pad you click on, and a colour picker that
changes the real hardware while you drag it.

What it is *not*: a service. It is a tool you start, use, and stop. It binds
loopback only, it requires a token printed in your terminal, and it hands the
pad back when you quit. See ``docs/WEB-UI.md`` for the threat model.

Layout of the package
---------------------
=================== ======================================================
:mod:`server`       HTTP, auth, static assets. No business logic.
:mod:`api`          ``(status, payload)`` handlers. No HTTP.
:mod:`configio`     Load/validate/atomically-save the config document.
:mod:`padlink`      Live preview and key capture, plus one-owner manners.
:mod:`layout`       The physical shape of the pad, as data.
``static/``         The page itself: one HTML file, one CSS, one JS.
=================== ======================================================

Entry point::

    from freemicro.webui import serve
    serve(open_browser=True)
"""

from __future__ import annotations

from freemicro.webui.server import (
    BindRefused,
    check_bind_host,
    create_server,
    serve,
)

__all__ = ["BindRefused", "check_bind_host", "create_server", "serve"]
