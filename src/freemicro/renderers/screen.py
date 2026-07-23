"""The guaranteed fallback renderer: an always-on-top chip, or the console.

If a display is available we draw a small always-on-top window (a coloured
"chip" with the state label). If Tk isn't importable or there's no display
(headless CI, SSH), we degrade to writing a single status line to the
terminal. Either way the signal lands — which is the entire point of having a
fallback that never touches the pad.
"""

from __future__ import annotations

import os
import sys

from freemicro.renderers.base import GLYPH, PALETTE, Renderer, register
from freemicro.state.engine import AgentState

_LABELS = {
    AgentState.IDLE: "idle",
    AgentState.WORKING: "working…",
    AgentState.WAITING: "needs you",
    AgentState.DONE: "done",
    AgentState.ERROR: "error",
}


def _has_display() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    # X11 / Wayland on Linux.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


@register
class ScreenRenderer(Renderer):
    """Always-available renderer. Tk window when possible, console otherwise."""

    name = "screen"
    priority = 1  # lowest: real hardware should win as the primary target.

    def __init__(self) -> None:
        self._tk = None
        self._root = None
        self._label = None
        self._last: AgentState | None = None
        self._use_gui = _has_display()

    def available(self) -> bool:
        # Console mode is always available; GUI mode is a best-effort upgrade.
        return True

    # -- GUI path --------------------------------------------------------

    def _ensure_window(self) -> bool:
        if self._root is not None:
            return True
        try:
            import tkinter as tk
        except Exception:
            self._use_gui = False
            return False
        try:
            root = tk.Tk()
            root.title("FreeMicro")
            root.overrideredirect(True)  # borderless chip
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", 0.95)
            except tk.TclError:
                pass
            label = tk.Label(
                root,
                text="",
                font=("SF Mono", 14, "bold"),
                fg="#ffffff",
                padx=16,
                pady=8,
            )
            label.pack()
            # Bottom-right corner.
            root.update_idletasks()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"+{sw - 180}+{sh - 90}")
            self._tk, self._root, self._label = tk, root, label
            return True
        except Exception:
            self._use_gui = False
            return False

    # -- render ----------------------------------------------------------

    def render(self, state: AgentState) -> None:
        if state == self._last:
            self._pump()
            return
        self._last = state

        if self._use_gui and self._ensure_window():
            color = _hex(PALETTE[state])
            self._label.configure(text=f"{GLYPH[state]}  {_LABELS[state]}", bg=color)
            self._root.configure(bg=color)
            self._pump()
        else:
            self._render_console(state)

    def _render_console(self, state: AgentState) -> None:
        # 8-bit ANSI truecolor status line, carriage-returned in place.
        r, g, b = PALETTE[state]
        line = f"\r\033[38;2;{r};{g};{b}m{GLYPH[state]} freemicro: {_LABELS[state]}\033[0m   "
        sys.stdout.write(line)
        sys.stdout.flush()

    def _pump(self) -> None:
        if self._root is not None:
            try:
                self._root.update()
            except Exception:
                # Window was closed; fall back to console for the rest.
                self._root = None
                self._use_gui = False

    def close(self) -> None:
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None
        if not self._use_gui:
            sys.stdout.write("\n")
            sys.stdout.flush()
