"""The renderer interface, colour palette, and the auto-select registry."""

from __future__ import annotations

from abc import ABC, abstractmethod

from freemicro.state.engine import AgentState

# Canonical RGB for each state. Renderers that can't do per-key colour still
# use these for a global colour, and the menu bar draws its dot from them.
PALETTE: dict[AgentState, tuple[int, int, int]] = {
    AgentState.IDLE: (40, 40, 48),      # dim slate - "on but quiet"
    AgentState.WORKING: (0, 122, 255),  # blue - thinking
    AgentState.WAITING: (255, 149, 0),  # amber - needs you
    AgentState.DONE: (52, 199, 89),     # green - finished
    AgentState.ERROR: (255, 59, 48),    # red - something broke
}

# A short glyph per state for text/console output.
GLYPH: dict[AgentState, str] = {
    AgentState.IDLE: "○",
    AgentState.WORKING: "◍",
    AgentState.WAITING: "◐",
    AgentState.DONE: "●",
    AgentState.ERROR: "✖",
}


class Renderer(ABC):
    """Base class for every output target.

    A renderer is cheap to construct and lazily binds to its device. Call
    :meth:`available` before :meth:`render`; the registry does this for you.
    """

    #: Stable identifier used in config and the CLI (e.g. ``"micro-leds"``).
    name: str = "renderer"

    #: Rough reliability score. The registry prefers higher numbers when
    #: choosing the *primary* target.
    priority: int = 0

    #: Set on renderers that need a physical device we may not have yet.
    experimental: bool = False

    @abstractmethod
    def available(self) -> bool:
        """Return True if this renderer can actually display state now."""

    @abstractmethod
    def render(self, state: AgentState) -> None:
        """Display ``state``. Must be safe to call repeatedly."""

    def close(self) -> None:
        """Release any resources. Safe to call more than once."""

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, type[Renderer]] = {}

#: Renderers that used to exist, and what to do instead. Kept so a config or a
#: command line written against an older FreeMicro gets a sentence rather than
#: silence: a name that quietly does nothing is how someone spends an evening
#: wondering why their pad is dark.
REMOVED: dict[str, str] = {
    "screen": (
        "the on-screen chip was removed - its window could never open, and "
        "`freemicro run` now prints every state change to the terminal itself"
    ),
    "busylight": (
        "busylight support was removed - VibeSignal drives blink(1), Luxafor "
        "and friends properly: https://github.com/yzhao062/vibesignal"
    ),
    "micro-via": (
        "the VIA renderer was removed - the Codex Micro exposes no 0xFF60 raw "
        "channel, so it never had anything to talk to"
    ),
    "micro-qmk": (
        "the QMK renderer was removed - QMK does not run on this pad's ESP32, "
        "so there was no firmware to reflash"
    ),
}


def register(cls: type[Renderer]) -> type[Renderer]:
    """Class decorator that adds a renderer to the global registry."""
    REGISTRY[cls.name] = cls
    return cls


def available_renderers() -> list[Renderer]:
    """Instantiate every registered renderer that is currently available.

    Returns them sorted by descending priority, so the first element is the
    best primary target.
    """
    live: list[Renderer] = []
    for cls in REGISTRY.values():
        try:
            instance = cls()
            if instance.available():
                live.append(instance)
            else:
                instance.close()
        except Exception:
            # A renderer that blows up while probing must never take the
            # process down - just skip it.
            continue
    live.sort(key=lambda r: r.priority, reverse=True)
    return live


def removed_names(prefer: list[str] | None) -> list[str]:
    """The entries of ``prefer`` that name a renderer FreeMicro used to have."""
    if not prefer:
        return []
    return [name for name in prefer if name.lower() in REMOVED]


def select(prefer: list[str] | None = None) -> list[Renderer]:
    """Choose which renderers to drive.

    The primary is the highest-priority available renderer, optionally
    constrained to ``prefer``. Names in :data:`REMOVED` are ignored here and
    reported by the CLI, so an old config still runs the pad.
    """
    live = available_renderers()
    if prefer:
        wanted = {name.lower() for name in prefer if name.lower() not in REMOVED}
        constrained = [r for r in live if r.name.lower() in wanted]
        if constrained:
            live = constrained

    chosen: list[Renderer] = []
    seen: set[str] = set()
    for renderer in live:
        if renderer.name not in seen:
            chosen.append(renderer)
            seen.add(renderer.name)
    return chosen
