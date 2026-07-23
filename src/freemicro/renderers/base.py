"""The renderer interface, colour palette, and the auto-select registry."""

from __future__ import annotations

from abc import ABC, abstractmethod

from freemicro.state.engine import AgentState

# Canonical RGB for each state. Renderers that can't do per-key colour still
# use these for a global colour; the screen renderer uses them verbatim.
PALETTE: dict[AgentState, tuple[int, int, int]] = {
    AgentState.IDLE: (40, 40, 48),      # dim slate — "on but quiet"
    AgentState.WORKING: (0, 122, 255),  # blue — thinking
    AgentState.WAITING: (255, 149, 0),  # amber — needs you
    AgentState.DONE: (52, 199, 89),     # green — finished
    AgentState.ERROR: (255, 59, 48),    # red — something broke
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

    #: Stable identifier used in config and the CLI (e.g. ``"screen"``).
    name: str = "renderer"

    #: Rough reliability score. The registry prefers higher numbers when
    #: choosing the *primary* target. Screen is intentionally low so real
    #: hardware wins when present, but it is always kept as a fallback.
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
            # process down — just skip it.
            continue
    live.sort(key=lambda r: r.priority, reverse=True)
    return live


def select(prefer: list[str] | None = None) -> list[Renderer]:
    """Choose which renderers to drive.

    The primary is the highest-priority available renderer (optionally
    constrained to ``prefer``). The screen renderer is always appended as a
    guaranteed fallback if it isn't already the primary — this is the
    load-bearing guarantee of the whole project.
    """
    live = available_renderers()
    if prefer:
        wanted = {name.lower() for name in prefer}
        constrained = [r for r in live if r.name.lower() in wanted]
        if constrained:
            live = constrained

    chosen: list[Renderer] = []
    seen: set[str] = set()
    for renderer in live:
        if renderer.name not in seen:
            chosen.append(renderer)
            seen.add(renderer.name)

    if "screen" not in seen and "screen" in REGISTRY:
        fallback = REGISTRY["screen"]()
        if fallback.available():
            chosen.append(fallback)

    return chosen
