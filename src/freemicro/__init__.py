"""FreeMicro - turn a macro pad into a live status light for coding agents.

Four decoupled layers:

* :mod:`freemicro.state` - Claude Code hooks → normalized agent state.
* :mod:`freemicro.renderers` - state → the pad's LEDs.
* :mod:`freemicro.detector` - read-only hardware capability probe.
* :mod:`freemicro.config` - user configuration.

The public surface is intentionally tiny; see the CLI (``freemicro --help``)
or the module docstrings for details.
"""

from freemicro.state import AgentState, StateStore, classify

__version__ = "0.1.0"

__all__ = ["AgentState", "StateStore", "classify", "__version__"]
