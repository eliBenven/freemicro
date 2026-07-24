"""State layer: normalize Claude Code activity into agent states and store them.

The state layer has two jobs:

1. Turn raw Claude Code hook events into a small, stable vocabulary of
   :class:`~freemicro.state.engine.AgentState` values.
2. Keep one record per live session and answer the question *"what is the
   single most important thing happening across all my agents right now?"*

Everything downstream (renderers, the CLI) consumes the resolved state and
never has to know about hooks.
"""

from freemicro.state.engine import (
    DEFAULT_DECAY,
    AgentState,
    DecayPolicy,
    ProcessLiveness,
    SessionSignals,
    SessionState,
    StateStore,
    decay_of,
    default_liveness,
    default_store,
    pid_alive,
)
from freemicro.state.hooks import classify, read_signals

__all__ = [
    "DEFAULT_DECAY",
    "AgentState",
    "DecayPolicy",
    "ProcessLiveness",
    "SessionSignals",
    "SessionState",
    "StateStore",
    "classify",
    "decay_of",
    "default_liveness",
    "default_store",
    "pid_alive",
    "read_signals",
]
