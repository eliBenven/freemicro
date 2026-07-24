"""A small, deliberately paranoid localhost HTTP server.

Standard library only - ``http.server`` and ``secrets``. FreeMicro's core is
dependency-free on Python 3.9 and a config UI is not a good enough reason to
change that, so there is no framework, no build step and no npm here.

Why the paranoia
----------------
This process can **type keystrokes, run shell commands and execute AppleScript**
on the user's machine, because that is what the config it edits describes. Any
other local program that could reach this API could write itself a binding and
press it. So the surface is closed down three ways, and the threat model is
written out honestly in ``docs/WEB-UI.md``:

1. **Loopback only.** :func:`check_bind_host` refuses to bind anything that is
   not ``127.0.0.1``/``::1``. Not a warning - a ``ValueError``. There is no flag
   to expose this on a network, and adding one would be a mistake.
2. **A per-run bearer token.** 32 bytes from :mod:`secrets`, printed once in the
   URL, required on every ``/api/`` request and compared with
   :func:`hmac.compare_digest`. Localhost is not a trust boundary on a shared
   machine: without the token, any process that can open a socket could drive
   this API.
3. **Host-header pinning.** Requests whose ``Host`` is not a loopback name are
   rejected, which closes DNS rebinding - an attacker's page resolving their own
   domain to 127.0.0.1 to reach us with the browser's blessing.

The token is a URL parameter for the **initial page load only**. That load sets a
session cookie, and every request after it authenticates on the cookie, so the
secret never has to live in the address bar, in browser history, in a copied
link or in a ``Referer``.

Why a cookie and not "keep the token in the page"
-------------------------------------------------
The first version stripped the token from the URL and held it in JavaScript.
Correct about history, and it stranded the user: pressing **Cmd-R** - a
completely ordinary thing to do on a config page - threw away the only copy of
the token and produced a scolding 403 telling them to go and find the URL in a
terminal they may well have closed. Reload, back/forward and a second tab now
all simply work.

The cookie is ``HttpOnly`` (JavaScript cannot read it, which is strictly better
than the page holding the secret), ``SameSite=Strict`` (no cross-site request
carries it), ``Path=/`` and session-scoped (no ``Expires``: it dies with the
browser session). ``Secure`` is deliberately *not* set, because this is plain
HTTP on loopback and the flag would stop the cookie working at all.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from freemicro.webui.api import Api

#: The only addresses we will bind. Everything else is a hard refusal.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

#: Static assets, served by exact name. No path joining from user input, so
#: there is no traversal to get wrong.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/app.css": ("text/css; charset=utf-8", "app.css"),
    "/app.js": ("text/javascript; charset=utf-8", "app.js"),
}

#: Header the page sends its token in.
TOKEN_HEADER = "X-FreeMicro-Token"

#: Session cookie set by the first token-authenticated page load. Everything
#: after that authenticates on this, so reloading the page works.
COOKIE_NAME = "freemicro_session"

#: What a user who is not authenticated is told. Written to be *useful*: the
#: old text said "open the exact URL FreeMicro printed", which assumes they
#: still have that terminal and that the old URL is still valid - after a
#: restart it is not, because the token is regenerated every run.
UNAUTHORISED_TEXT = (
    "FreeMicro could not verify this page's session.\n\n"
    "That usually means the freemicro process was restarted (each run makes a "
    "new one-time link) or the browser session ended.\n\n"
    "To get back in: run `freemicro config --web` again and open the link it "
    "prints. Nothing is lost - your settings live in your config file, not in "
    "this page."
)


class BindRefused(ValueError):
    """Raised when asked to bind anything that is not loopback."""


def check_bind_host(host: str) -> str:
    """Return ``host`` if it is loopback, else raise :class:`BindRefused`.

    Separate from the server so it is trivially testable and impossible to skip.
    ``0.0.0.0`` in particular is called out because it is the one someone will
    reach for when they want to open the UI from a laptop on the sofa - and it
    would expose a keystroke-injection API to the whole network.
    """
    if host not in LOOPBACK_HOSTS:
        raise BindRefused(
            f"refusing to bind {host!r}: the FreeMicro web UI can type "
            "keystrokes and run shell commands, so it is loopback-only. "
            f"Use one of {', '.join(LOOPBACK_HOSTS)}."
        )
    return host


def _host_is_loopback(header: Optional[str]) -> bool:
    """Is the request's ``Host`` header one of ours? Blocks DNS rebinding."""
    if not header:
        return False
    name = header.rsplit(":", 1)[0] if header.count(":") == 1 else header
    name = name.strip("[]")
    return name in LOOPBACK_HOSTS


class _Handler(BaseHTTPRequestHandler):
    """Routing, auth and serialisation. No business logic lives here."""

    server_version = "FreeMicro"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet by default - the terminal belongs to whatever else is running."""
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        set_session: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_session:
            # Session-scoped (no Expires), unreadable from JavaScript, never
            # sent cross-site. No Secure flag: this is http on loopback, and
            # setting it would stop the cookie being sent at all.
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict",
            )
        # Nothing here should be cached, remembered or leaked onward.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 4_000_000:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _session_cookie(self) -> str:
        """The session cookie's value, or ``""``. No cookie library needed."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value.strip()
        return ""

    def _authorised(self, query: Dict[str, list]) -> bool:
        """Three ways in, all comparing the same secret in constant time.

        The header and the URL parameter are the bootstrap; the cookie is what
        makes a page reload work. Any one of them is enough, none of them is
        guessable, and a request with none of them is refused.
        """
        for supplied in (
            self.headers.get(TOKEN_HEADER) or "",
            (query.get("token") or [""])[0],
            self._session_cookie(),
        ):
            if supplied and hmac.compare_digest(supplied, self.token):
                return True
        return False

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if not _host_is_loopback(self.headers.get("Host")):
            self._json(403, {"error": "this server answers on loopback only"})
            return

        if path in STATIC_FILES:
            content_type, name = STATIC_FILES[path]
            self._serve_static(name, content_type)
            return

        if path in ("/", "/index.html"):
            if not self._authorised(query):
                self._send(
                    403,
                    UNAUTHORISED_TEXT.encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
                return
            # Hand the page a session so a reload does not lock the user out.
            self._serve_static(
                "index.html", "text/html; charset=utf-8", set_session=True
            )
            return

        if not path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorised(query):
            self._json(401, {"error": UNAUTHORISED_TEXT})
            return

        # Every branch below is wrapped, for one specific reason: an exception
        # escaping do_GET drops the connection without a response, the page
        # sees a bare "Failed to fetch", and - because these calls happen while
        # the editor is booting - the user gets a window that looks finished
        # and does nothing. A 500 with the traceback's summary in it is worth a
        # great deal more than a dead page.
        try:
            if path == "/api/schema":
                self._respond(self.api.schema())
            elif path == "/api/config":
                self._respond(self.api.config())
            elif path == "/api/device":
                self._respond(self.api.device())
            elif path == "/api/sessions":
                self._respond(self.api.sessions())
            elif path == "/api/projects":
                self._respond(self.api.projects())
            elif path == "/api/layouts":
                self._respond(self.api.layouts())
            elif path == "/api/starters":
                self._respond(self.api.starters())
            elif path == "/api/apps":
                self._respond(self.api.apps())
            elif path == "/api/capture/events":
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._respond(self.api.capture_events(since))
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - one bad read must not kill it
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not _host_is_loopback(self.headers.get("Host")):
            self._json(403, {"error": "this server answers on loopback only"})
            return
        if not self._authorised(query):
            self._json(401, {"error": UNAUTHORISED_TEXT})
            return
        body = self._read_body()
        routes = {
            "/api/validate": self.api.validate,
            "/api/config": self.api.save,
            "/api/preview": self.api.preview,
            "/api/preview/off": self.api.blank,
            "/api/layouts/save": self.api.layout_save,
            "/api/layouts/delete": self.api.layout_delete,
            "/api/capture/start": self.api.capture_start,
            "/api/capture/stop": self.api.capture_stop,
        }
        handler = routes.get(parsed.path)
        if handler is None:
            self._json(404, {"error": "not found"})
            return
        try:
            self._respond(handler(body))
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill it
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _respond(self, response: Tuple[int, Dict[str, Any]]) -> None:
        status, payload = response
        self._json(status, payload)

    def _serve_static(
        self, name: str, content_type: str, set_session: bool = False
    ) -> None:
        path = STATIC_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._json(404, {"error": f"missing asset {name}"})
            return
        self._send(200, body, content_type, set_session=set_session)


class WebUiServer(ThreadingHTTPServer):
    """A threading server so a slow pad write never blocks the page."""

    daemon_threads = True
    #: SO_REUSEADDR, so restarting the UI after a crash does not fail for a
    #: minute on a lingering TIME_WAIT socket. It does not let a second process
    #: listen on the same port - that would be SO_REUSEPORT, which we do not set.
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], api: Api, token: str) -> None:
        super().__init__(address, _Handler)
        self.api = api
        self.token = token
        self.verbose = False

    @property
    def url(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}/"

    @property
    def entry_url(self) -> str:
        """The URL to actually open: includes the one-time token."""
        return f"{self.url}?token={self.token}"


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    config_path: Optional[Path] = None,
    token: Optional[str] = None,
) -> WebUiServer:
    """Build a server without starting it.

    ``port=0`` asks the OS for a free ephemeral port, which is both the "random
    high port" the design calls for and one less thing to collide with.
    """
    check_bind_host(host)
    return WebUiServer(
        (host, int(port)), Api(config_path), token or secrets.token_urlsafe(32)
    )


def serve(
    open_browser: bool = True,
    host: str = "127.0.0.1",
    port: int = 0,
    config_path: Optional[Path] = None,
) -> None:
    """Run the config UI until interrupted. **The entry point for the CLI.**

    Blocks. Ctrl-C stops it, releases the pad if preview or capture had it, and
    removes the advisory lock.
    """
    server = create_server(host=host, port=port, config_path=config_path)
    api = server.api
    print("FreeMicro web UI")
    print(f"  Open:   {server.entry_url}")
    print(f"  Editing: {api.save_path}")
    print(
        "  This link carries a one-time token and works only from this machine.\n"
        "  Anyone who can read it can change what your pad types - treat it\n"
        "  like a password, and press Ctrl-C when you are done."
    )
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(server.entry_url,)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
        api.close()


__all__ = [
    "BindRefused",
    "COOKIE_NAME",
    "LOOPBACK_HOSTS",
    "TOKEN_HEADER",
    "UNAUTHORISED_TEXT",
    "WebUiServer",
    "check_bind_host",
    "create_server",
    "serve",
]
