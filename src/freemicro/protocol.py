"""Learned LED protocol profiles — the core of sniff-and-replay (Path B).

A :class:`SniffedProtocol` is what we learn by watching the ChatGPT desktop app
drive the Codex Micro's Agent Keys. It stores, for each agent state, the *exact*
HID report frames the app sent — so FreeMicro can replay them byte-for-byte when
Claude Code enters the equivalent state. That's the "replication" half of the
ask: identical on-pad behavior, triggered by a different agent.

We key captures by **Codex** state names (what the app emits) and map them onto
FreeMicro's :class:`AgentState` vocabulary via :data:`CODEX_TO_AGENT`. Optional
inferred byte offsets (report id / command / rgb positions) let the same profile
also synthesize arbitrary colours parametrically, but literal replay is the
default because it reproduces animations and per-key patterns faithfully.

Nothing here is OpenAI/Work Louder firmware — it is a description of the HID
reports observed on the wire, stored locally for personal interoperability with
hardware you own.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from freemicro.state.engine import AgentState

# The six thread-state events the Codex App Server streams (per OpenAI docs),
# mapped onto FreeMicro's five normalized states.
CODEX_TO_AGENT: dict[str, AgentState] = {
    "idle": AgentState.IDLE,
    "thinking": AgentState.WORKING,
    "running": AgentState.WORKING,
    "awaiting_input": AgentState.WAITING,
    "awaiting": AgentState.WAITING,
    "done": AgentState.DONE,
    "error": AgentState.ERROR,
}


@dataclass
class ByteLayout:
    """Inferred positions inside a set-color report (for parametric replay)."""

    report_id: int = 0
    command: int | None = None
    command_offset: int | None = None
    key_index_offset: int | None = None
    r_offset: int | None = None
    g_offset: int | None = None
    b_offset: int | None = None

    def is_usable(self) -> bool:
        return None not in (self.r_offset, self.g_offset, self.b_offset)


@dataclass
class SniffedProtocol:
    """Everything learned about how to drive one pad's LEDs.

    ``frames_by_state`` maps a FreeMicro :class:`AgentState` value (e.g.
    ``"working"``) to a list of report frames (each a list of ints, exactly the
    bytes to hand to ``hid.device.write``). ``layout`` is optional and only used
    for parametric colour synthesis.
    """

    vid_pid: str = ""
    report_length: int = 33
    frames_by_state: dict[str, list[list[int]]] = field(default_factory=dict)
    layout: ByteLayout = field(default_factory=ByteLayout)
    source: str = "sniffed"

    # -- (de)serialization ----------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        data = asdict(self)
        return json.dumps(data, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "SniffedProtocol":
        layout = ByteLayout(**data.get("layout", {}))
        frames = {
            state: [list(map(int, frame)) for frame in frames]
            for state, frames in data.get("frames_by_state", {}).items()
        }
        return cls(
            vid_pid=str(data.get("vid_pid", "")),
            report_length=int(data.get("report_length", 33)),
            frames_by_state=frames,
            layout=layout,
            source=str(data.get("source", "sniffed")),
        )

    @classmethod
    def load(cls, path: Path) -> "SniffedProtocol":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    # -- replay ----------------------------------------------------------

    def frames_for(self, state: AgentState) -> list[list[int]]:
        """Report frames to replay for a FreeMicro ``state``.

        Prefers literally-captured frames; if none exist for this state but a
        byte layout was inferred, synthesizes a single set-color report from the
        palette instead. Returns ``[]`` if we know nothing for this state.
        """
        literal = self.frames_by_state.get(state.value)
        if literal:
            return [list(f) for f in literal]
        if self.layout.is_usable():
            synth = self._synthesize(state)
            if synth:
                return [synth]
        return []

    def _synthesize(self, state: AgentState) -> list[int] | None:
        from freemicro.renderers.base import PALETTE

        lay = self.layout
        if not lay.is_usable():
            return None
        frame = [0] * self.report_length
        if lay.report_id is not None and self.report_length:
            frame[0] = lay.report_id
        if lay.command is not None and lay.command_offset is not None:
            frame[lay.command_offset] = lay.command
        r, g, b = PALETTE[state]
        frame[lay.r_offset] = r
        frame[lay.g_offset] = g
        frame[lay.b_offset] = b
        return frame

    def known_states(self) -> list[AgentState]:
        out = []
        for value in self.frames_by_state:
            try:
                out.append(AgentState(value))
            except ValueError:
                continue
        return out


def default_profile_path() -> Path:
    from freemicro.config import config_home

    return config_home() / "protocol.json"
