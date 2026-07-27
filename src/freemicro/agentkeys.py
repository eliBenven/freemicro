"""Which project each of the six Agent Keys stands for, and what colour it is.

The pad has six individually addressable Agent Keys. Until this module existed
FreeMicro lit all six with the single winning state - six copies of one status
light - which is the largest gap between what the hardware can do and what the
project did with it. On the vendor's pad each Agent Key follows one *chat*. This
is the equivalent for Claude Code.

What is a key bound to?
-----------------------
**The project directory, not the session.** This is the one design decision
everything else follows from.

A Claude Code "session" is a terminal process with a UUID that dies when you
close the tab; the next tab gets a new UUID. Binding ``AG00`` to a session id
would mean re-binding your pad several times a day, and a pad you have to keep
re-teaching is worse than no pad. A *project directory* is stable for as long as
the work is: it survives restarts, `/clear`, a crashed tab, a reboot. It is also
how people actually think - "the API repo is waiting on me", never "session
0f2c…9a is waiting on me".

Hook events already carry ``cwd`` (see :mod:`freemicro.state.hooks`), so this
needs no new plumbing. Several sessions in one directory collapse into one
project, and the project shows the highest-priority state among them - if any
tab in that repo needs you, the key for that repo says so.

The slot-stability rule
-----------------------
An indicator you cannot trust is noise. If the key that meant "the API repo"
silently became "the docs repo" because activity order shifted, you would have
to read the pad instead of glancing at it, and the feature would be worthless.

So slots are **sticky**, in this precedence:

1. **A pinned slot is absolute.** Under ``pinned``/``manual`` a configured slot
   always represents that directory. If nothing is live there the key is dark -
   it is *never* lent to another project. Your pins do not move.
2. **An incumbent keeps its slot.** A project that held slot *n* keeps slot *n*
   for as long as it stays live, no matter how the activity order changes
   around it. Nothing is ever evicted for being less recent.
3. **Only vacated slots are refilled.** When a project goes stale (the store's
   TTL) or ends, its slot frees, and the most-recently-active project without a
   slot moves in - into the lowest free index, so keys fill left to right.
4. **A free slot remembers its last occupant.** If a project goes away and its
   slot has not been reused, coming back puts it on the same key.
5. **First run** (no history) fills slots 0-5 with live projects, most recent
   first. That is factory ``recent`` behaviour, and it means the pad is useful
   with an empty config.

The consequence worth stating plainly: with more than six live projects, the
seventh does **not** appear until a slot frees. Bumping an incumbent for
whoever moved most recently would make the pad unreadable - which is precisely
what rule 2 exists to prevent.

Purity
------
Everything here is a pure function of ``(config, sessions, previous, now)``. No
device, no filesystem, no clock of its own. :class:`SlotResolver` is the only
stateful thing, and all it holds is the previous assignment.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from freemicro.state.engine import (
    DEFAULT_DECAY,
    AgentState,
    DecayPolicy,
    SessionState,
    decay_of,
)
from freemicro.state.engine import effective_state as _engine_effective_state

#: One slot per Agent Key, ``AG00``-``AG05``.
SLOT_COUNT = 6

#: The six most recently active projects fill the keys automatically. Zero
#: config, and what the factory pad does (``codex-micro-agent-source``).
POLICY_RECENT = "recent"
#: Pinned directories keep their key; the rest fill from ``recent``.
POLICY_PINNED = "pinned"
#: Exactly the pinned set. Unpinned keys stay dark.
POLICY_MANUAL = "manual"
#: The pre-slot behaviour: all six keys show the one winning state. Kept for
#: people who liked the old single-colour pad, and for a one-project machine
#: where six copies of one light is arguably the honest picture.
POLICY_MIRROR = "mirror"

POLICIES: Tuple[str, ...] = (
    POLICY_RECENT,
    POLICY_PINNED,
    POLICY_MANUAL,
    POLICY_MIRROR,
)


class AgentKeysError(ValueError):
    """Raised for an ``agent_keys`` section we cannot make sense of."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _clean_owned_keys(raw: Iterable[Any]) -> Tuple[int, ...]:
    """Normalise ``agent_keys.keys`` to a sorted tuple of owned indices.

    Empty means "drive everything" - the same as all six - so both the absent
    case and an explicit ``[]`` collapse to ``(0, 1, 2, 3, 4, 5)``. Anything
    that is not a whole number 0-5, and any repeat, is a hard error: a light
    that silently drives the wrong physical key is worse than one that refuses
    to load.
    """
    seen: List[int] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise AgentKeysError(
                "agent_keys.keys entries must be whole numbers 0-"
                f"{SLOT_COUNT - 1}, got {entry!r}"
            )
        if not 0 <= entry < SLOT_COUNT:
            raise AgentKeysError(
                f"agent_keys.keys has {entry}; Agent Key indices are 0-"
                f"{SLOT_COUNT - 1}"
            )
        if entry in seen:
            raise AgentKeysError(
                f"agent_keys.keys lists key {entry} twice; each Agent Key is "
                "owned once or not at all"
            )
        seen.append(entry)
    if not seen:
        return tuple(range(SLOT_COUNT))
    return tuple(sorted(seen))

def normalise_project(path: Any) -> str:
    """Canonical form of a project directory, for comparison and display.

    Purely textual - ``expanduser`` plus ``normpath``, no ``realpath``. Touching
    the filesystem here would make the resolver impure and would resolve
    symlinks that the user's own ``cwd`` has not resolved, so two names for one
    directory would stop matching rather than start.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    text = os.path.expanduser(text)
    text = os.path.normpath(text)
    # normpath leaves a lone "/" alone but strips other trailing separators.
    return text


def agent_key_index(input_id: Any) -> Optional[int]:
    """The Agent Key index (0-5) an input id names, or ``None``.

    ``"AG03"`` -> ``3``; every other input - an action key, a joystick flick, a
    chord id like ``"AG00+AG03"``, anything malformed - is ``None``. This is the
    inverse of :attr:`AgentSlot.key_id`, and the one place that decides "is this
    input one of the six Agent Keys, and which", so a caller never parses ``AG``
    strings by hand.
    """
    text = str(input_id or "")
    if len(text) == 4 and text[:2] == "AG" and text[2:].isdigit():
        index = int(text[2:])
        if 0 <= index < SLOT_COUNT:
            return index
    return None


@dataclass(frozen=True)
class AgentKeysConfig:
    """The parsed ``agent_keys`` section.

    ``slots`` is always exactly :data:`SLOT_COUNT` long, one entry per Agent Key
    in ``AG00``-``AG05`` order, each a normalised project path or ``""``.

    ``keys`` is the set of physical Agent Key indices (0-5) FreeMicro *owns* -
    the keys it is allowed to light. The default is all six, which is
    byte-identical to the behaviour before this field existed. Setting it to a
    strict subset (e.g. ``[3, 4, 5]``) is how FreeMicro coexists with the
    ChatGPT desktop app driving Codex: the ChatGPT app cannot be told to limit
    itself, but FreeMicro can limit *itself* to a chosen subset and leave the
    rest for Codex. See :meth:`subset` and the ``thstatus`` builder in
    :mod:`freemicro.renderers.micro_leds`.
    """

    policy: str = POLICY_RECENT
    slots: Tuple[str, ...] = ("",) * SLOT_COUNT
    keys: Tuple[int, ...] = tuple(range(SLOT_COUNT))

    def __post_init__(self) -> None:
        if len(self.slots) != SLOT_COUNT:  # pragma: no cover - guarded in parse
            raise AgentKeysError(f"agent_keys.slots must have {SLOT_COUNT} entries")
        # Normalise here too, so constructing a config directly (the tests, the
        # web UI round-trip) gets the same sorted, validated, empty-means-all
        # invariant that :func:`parse_agent_keys` gives the file.
        object.__setattr__(self, "keys", _clean_owned_keys(self.keys))

    @property
    def owns_all(self) -> bool:
        """True when FreeMicro drives every Agent Key - the default."""
        return len(self.keys) == SLOT_COUNT

    @property
    def subset(self) -> bool:
        """True when a strict subset is owned, i.e. we are coexisting.

        This is the switch the whole feature turns on: a partial ``thstatus``,
        a reassert cadence to hold the split, and the reserved-key markings in
        the UI all key off it. When it is ``False`` there is zero behaviour
        change and zero added cost.
        """
        return not self.owns_all

    def owns(self, index: int) -> bool:
        """Whether FreeMicro drives the Agent Key at ``index``."""
        return index in self.keys

    def leaves_to_codex(self, input_id: Any) -> bool:
        """Whether ``input_id`` is an Agent Key FreeMicro does not own.

        The split is not only about lighting: an un-owned Agent Key is the
        vendor app's (Codex's) entirely, so FreeMicro must ignore its *presses*
        too - no focus, no new terminal, no bound action at all. The bridge asks
        this before dispatching a key, so a press of AG00 while owning ``[3, 4,
        5]`` does nothing on FreeMicro's side and Codex's own action stands.

        ``False`` for every input that is not one of the six Agent Keys, and -
        via the :attr:`owns_all` short-circuit - ``False`` for *every* input
        under the default (all six owned), so the key path is byte-identical to
        the behaviour before this option existed.
        """
        if self.owns_all:
            return False
        index = agent_key_index(input_id)
        return index is not None and index not in self.keys

    @property
    def pins(self) -> Tuple[str, ...]:
        """The pinned paths, in slot order, ``""`` where unpinned.

        Only ``pinned`` and ``manual`` honour pins; the other policies ignore
        them so that switching policy back and forth never loses them.
        """
        if self.policy in (POLICY_PINNED, POLICY_MANUAL):
            return self.slots
        return ("",) * SLOT_COUNT

    @property
    def mirrors(self) -> bool:
        """True when the six keys should all show the one winning state."""
        return self.policy == POLICY_MIRROR

    def to_dict(self) -> Dict[str, Any]:
        """Round-trip form - the exact shape the web UI reads and writes.

        ``keys`` is emitted only when it is a strict subset, so the common
        all-six case stays the ``{"policy", "slots"}`` shape it has always been
        and no shipped default grows a field it does not need.
        """
        out: Dict[str, Any] = {"policy": self.policy, "slots": list(self.slots)}
        if self.subset:
            out["keys"] = list(self.keys)
        return out


def parse_agent_keys(raw: Any) -> AgentKeysConfig:
    """Validate an ``agent_keys`` mapping. Raises :class:`AgentKeysError`.

    Deliberately forgiving in one direction: a slot naming a directory that
    does not exist (or is not running right now) is **fine**. Projects come and
    go, and a config that refuses to load because you closed a terminal would
    be intolerable.
    """
    if raw is None:
        return AgentKeysConfig()
    if not isinstance(raw, Mapping):
        raise AgentKeysError('"agent_keys" must be an object')

    unknown = set(raw) - {"policy", "slots", "keys", "comment", "_comment"}
    if unknown:
        raise AgentKeysError(
            "agent_keys has unknown field(s): " + ", ".join(sorted(unknown))
        )

    policy = str(raw.get("policy", POLICY_RECENT)).strip().lower()
    if policy not in POLICIES:
        raise AgentKeysError(
            f"agent_keys.policy must be one of {', '.join(POLICIES)}; "
            f"got {raw.get('policy')!r}"
        )

    slots_raw = raw.get("slots")
    if slots_raw is None:
        slots_raw = []
    if isinstance(slots_raw, (str, bytes)) or not isinstance(slots_raw, Sequence):
        raise AgentKeysError('"agent_keys.slots" must be a list of six entries')
    if len(slots_raw) > SLOT_COUNT:
        raise AgentKeysError(
            f"agent_keys.slots has {len(slots_raw)} entries; the pad has "
            f"{SLOT_COUNT} Agent Keys"
        )

    slots: List[str] = []
    for index, entry in enumerate(slots_raw):
        if entry is None:
            slots.append("")
            continue
        if not isinstance(entry, str):
            raise AgentKeysError(
                f"agent_keys.slots[{index}] must be a project path or null, "
                f"got {type(entry).__name__}"
            )
        slots.append(normalise_project(entry))
    slots.extend([""] * (SLOT_COUNT - len(slots)))

    keys_raw = raw.get("keys")
    if keys_raw is None:
        keys = tuple(range(SLOT_COUNT))
    elif isinstance(keys_raw, (str, bytes)) or not isinstance(keys_raw, Sequence):
        raise AgentKeysError(
            '"agent_keys.keys" must be a list of Agent Key indices (0-5); an '
            "empty list or all six means drive every key"
        )
    else:
        keys = _clean_owned_keys(keys_raw)

    return AgentKeysConfig(policy=policy, slots=tuple(slots), keys=keys)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def project_label(path: str) -> str:
    """The short name to show for a project directory."""
    if not path:
        return ""
    base = os.path.basename(path.rstrip(os.sep)) or path
    return base


@dataclass(frozen=True)
class Project:
    """One project directory and the live sessions inside it."""

    path: str
    state: AgentState
    last_active: float
    sessions: Tuple[SessionState, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", project_label(self.path))

    @property
    def lead(self) -> Optional[SessionState]:
        """The session that decided this project's state.

        This is the session an Agent Key press should jump to: the one that is
        waiting on you, erroring, or most recently working.
        """
        return self.sessions[0] if self.sessions else None

    @property
    def session_count(self) -> int:
        return len(self.sessions)


def effective_state(
    session: SessionState,
    *,
    now: float,
    decay: DecayPolicy = DEFAULT_DECAY,
) -> AgentState:
    """A session's state after its expired claims are retired.

    Two decays, one rule, and the rule itself lives in
    :func:`freemicro.state.engine.effective_state` - the Agent Keys must reach
    the same verdict as ``resolved_state()`` and ``freemicro status`` or the pad
    ends up showing something no other view agrees with.

    * The factory's green is ``unread``, not "completed": it clears once you
      look at the thread (``docs/FACTORY-DEFAULTS.md`` §2). FreeMicro has no
      signal for "the human looked at it", so the decay is a timer - applied
      *per project* here, so one repo going quiet does not clear another's.
    * Blue is a claim that has to keep being renewed. Interrupting an agent
      fires no hook at all, so a ``working`` claim that has gone silent is
      retired rather than believed for the next half hour.
    """
    return _engine_effective_state(session, now=now, decay=decay)


def group_projects(
    sessions: Sequence[SessionState],
    *,
    now: float,
    decay: DecayPolicy = DEFAULT_DECAY,
) -> List[Project]:
    """Collapse sessions into projects, most recently active first.

    A project's state is the highest-priority state among its sessions -
    ``waiting > error > done > working > idle``, the same rule the global
    resolver uses - with recency breaking ties. Sessions with no ``cwd`` cannot
    be attributed to a project and are dropped; there is nothing honest to show
    for them, and inventing a slot would put a light on a key that no press can
    reach.
    """
    grouped: Dict[str, List[Tuple[AgentState, SessionState]]] = {}
    for session in sessions:
        path = normalise_project(session.cwd)
        if not path:
            continue
        state = effective_state(session, now=now, decay=decay)
        grouped.setdefault(path, []).append((state, session))

    projects: List[Project] = []
    for path, entries in grouped.items():
        # Most interesting first: priority, then recency. entries[0] is both
        # the project's state and the session an Agent Key press should reach.
        entries.sort(key=lambda e: (e[0].priority, e[1].updated_at), reverse=True)
        projects.append(
            Project(
                path=path,
                state=entries[0][0],
                last_active=max(session.updated_at for _, session in entries),
                sessions=tuple(session for _, session in entries),
            )
        )
    projects.sort(key=lambda p: p.last_active, reverse=True)
    return _disambiguate(projects)


def _disambiguate(projects: Sequence[Project]) -> List[Project]:
    """Give every project a label unique among the ones on screen.

    Two checkouts both called ``web`` would otherwise show as two identical
    keys, which defeats the point. Parent directories are added one at a time
    until the labels differ (``api/web`` vs ``site/web``).
    """
    result = list(projects)
    labels = [_tail(p.path, 1) for p in result]
    for depth in range(2, 7):
        clashing = {
            label for label, count in Counter(labels).items() if count > 1
        }
        if not clashing:
            break
        labels = [
            _tail(project.path, depth) if label in clashing else label
            for project, label in zip(result, labels)
        ]
    return [
        Project(
            path=p.path,
            state=p.state,
            last_active=p.last_active,
            sessions=p.sessions,
            label=label or project_label(p.path),
        )
        for p, label in zip(result, labels)
    ]


def _tail(path: str, depth: int) -> str:
    """The last ``depth`` components of a path, for display only."""
    parts = [part for part in path.split(os.sep) if part]
    if not parts:
        return path
    return "/".join(parts[-depth:])


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentSlot:
    """What one Agent Key stands for right now.

    A slot may be *empty* (nothing to show - the key is dark), or *reserved*: a
    pinned directory with no live session, which is still dark but is never
    given away to another project.
    """

    index: int
    #: Normalised project directory, or ``""`` when the slot has never been
    #: given one. Non-empty with ``project is None`` means "reserved".
    path: str = ""
    project: Optional[Project] = None
    pinned: bool = False
    #: Whether FreeMicro drives this Agent Key at all. ``False`` for a key the
    #: user left to the ChatGPT app (Codex): it is never allocated a project
    #: and the renderer omits it from ``thstatus`` entirely, so Codex's own
    #: colour on that key persists. See :attr:`AgentKeysConfig.subset`.
    owned: bool = True

    @property
    def empty(self) -> bool:
        """True when this key has nothing live to show and must be dark."""
        return self.project is None

    @property
    def reserved(self) -> bool:
        """Pinned to a directory that has no live session right now."""
        return self.project is None and bool(self.path)

    @property
    def state(self) -> Optional[AgentState]:
        return self.project.state if self.project else None

    @property
    def label(self) -> str:
        if self.project is not None:
            return self.project.label
        return project_label(self.path)

    @property
    def last_active(self) -> float:
        return self.project.last_active if self.project else 0.0

    @property
    def session(self) -> Optional[SessionState]:
        """The session an Agent Key press should jump to, if any."""
        return self.project.lead if self.project else None

    @property
    def key_id(self) -> str:
        """The pad input id for this slot - ``AG00``…``AG05``."""
        return f"AG{self.index:02d}"

    def describe(self) -> str:
        """One line for ``freemicro status`` and the dry-run printer."""
        if self.project is not None:
            state = self.project.state.value
            extra = (
                f" ({self.project.session_count} sessions)"
                if self.project.session_count > 1
                else ""
            )
            return f"{self.label} - {state}{extra}"
        if self.reserved:
            return f"{self.label} - pinned, not running"
        if not self.owned:
            return "(left for the vendor app / Codex)"
        return "(empty)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "key": self.key_id,
            "path": self.path,
            "label": self.label,
            "state": self.state.value if self.state else None,
            "last_active": self.last_active,
            "pinned": self.pinned,
            "empty": self.empty,
            "owned": self.owned,
            "sessions": self.project.session_count if self.project else 0,
        }


def resolve_slots(
    config: Optional[AgentKeysConfig],
    sessions: Sequence[SessionState],
    *,
    previous: Sequence[str] = (),
    now: Optional[float] = None,
    decay: DecayPolicy = DEFAULT_DECAY,
    owned: Optional[Sequence[int]] = None,
) -> List[AgentSlot]:
    """Assign the six Agent Keys. Pure - see the module docstring for the rule.

    ``previous`` is the path each slot held last time, which is what makes the
    assignment sticky; pass ``()`` for a cold start. The caller owns that memory
    (:class:`SlotResolver` does it for you) so this function stays a function.

    ``owned`` overrides which keys FreeMicro drives for this one call. It exists
    for the coexistence feature: the configured subset (``config.keys``) is only
    honoured *while the ChatGPT app is running*; when it is not there is no one to
    share with, so FreeMicro owns all six. That "effective" set is resolved at
    runtime by :class:`AgentKeyOwnership` and threaded in here, leaving the config
    immutable. ``None`` (the default) means "use ``config.keys``", so every caller
    that does not coexist is byte-identical to before.

    ``POLICY_MIRROR`` is resolved exactly like ``recent`` - the renderer is what
    decides to ignore slots and paint all six keys one colour, and ``freemicro
    status`` should still be able to say what *would* be on each key.
    """
    config = config or AgentKeysConfig()
    now = time.time() if now is None else now
    projects = group_projects(sessions, now=now, decay=decay)
    by_path: Dict[str, Project] = {p.path: p for p in projects}

    # The owned set constrains every step below: a project is only ever placed
    # on a key FreeMicro drives, so an un-owned key stays empty (and is left out
    # of ``thstatus`` by the renderer) and no live project vanishes onto a key
    # we never light. ``owns_all`` is the default, in which every index is owned
    # and this is exactly the old behaviour. ``owned`` is the effective set for
    # this call (all six when Codex is absent); ``config.keys`` when unset.
    owned = set(config.keys if owned is None else owned)

    assigned: List[str] = [""] * SLOT_COUNT
    pinned: List[bool] = [False] * SLOT_COUNT

    # 1. Pins are absolute, live or not - but only on a key we own. A pin on an
    #    un-owned key is ignored (and warned about at load time), never honoured
    #    onto a key Codex is driving.
    for index, pin in enumerate(config.pins):
        if pin and index in owned:
            assigned[index] = pin
            pinned[index] = True

    if config.policy != POLICY_MANUAL:
        taken = {path for path in assigned if path}

        # 2. Incumbents keep their key while they stay live.
        for index, previous_path in enumerate(previous[:SLOT_COUNT]):
            if index not in owned:
                continue
            path = normalise_project(previous_path)
            if assigned[index] or not path:
                continue
            if path in by_path and path not in taken:
                assigned[index] = path
                taken.add(path)

        # 3. Vacancies go to the most recently active project without a key,
        #    filling from the left so the pad stays readable - across the owned
        #    keys only.
        waiting = [p.path for p in projects if p.path not in taken]
        for index in range(SLOT_COUNT):
            if index not in owned or assigned[index] or not waiting:
                continue
            assigned[index] = waiting.pop(0)

    return [
        AgentSlot(
            index=index,
            path=assigned[index],
            project=by_path.get(assigned[index]),
            pinned=pinned[index],
            owned=index in owned,
        )
        for index in range(SLOT_COUNT)
    ]


@dataclass
class SlotResolver:
    """A :func:`resolve_slots` that remembers, so slots stay put.

    Holds one thing: the path each key last stood for. A key that goes empty
    *keeps* that memory (rule 4) until another project actually takes the slot,
    so closing a terminal and reopening it lands you back on the same key.

    The decay policy is the store's, whole (:meth:`for_store`). The keys are
    lit from records the store has *already* decayed, and re-deciding here
    under different timers means the stricter of the two wins whatever the user
    configured: raise ``working_ttl_seconds`` and the store honours it while
    the pad quietly undoes it.
    """

    config: AgentKeysConfig = field(default_factory=AgentKeysConfig)
    decay: DecayPolicy = DEFAULT_DECAY
    _previous: Tuple[str, ...] = field(default=("",) * SLOT_COUNT, init=False)

    @classmethod
    def for_store(
        cls, store: Any, *, config: Optional[AgentKeysConfig] = None
    ) -> "SlotResolver":
        """A resolver that decays exactly the way ``store`` does."""
        return cls(config=config or AgentKeysConfig(), decay=decay_of(store))

    @property
    def previous(self) -> Tuple[str, ...]:
        return self._previous

    def resolve(
        self,
        sessions: Sequence[SessionState],
        *,
        now: Optional[float] = None,
        owned: Optional[Sequence[int]] = None,
    ) -> List[AgentSlot]:
        slots = resolve_slots(
            self.config,
            sessions,
            previous=self._previous,
            now=now,
            decay=self.decay,
            owned=owned,
        )
        # An un-owned key remembers and publishes nothing: it is not ours to
        # focus, so pressing it does nothing on FreeMicro's side and Codex keeps
        # it. Owned keys keep the sticky-memory rule (rule 4) as before.
        self._previous = tuple(
            (slot.path or self._previous[slot.index]) if slot.owned else ""
            for slot in slots
        )
        return slots

    def seed(self, previous: Sequence[str]) -> None:
        """Start from a known assignment - e.g. one restored from disk."""
        entries = [normalise_project(entry) for entry in list(previous)[:SLOT_COUNT]]
        entries.extend([""] * (SLOT_COUNT - len(entries)))
        self._previous = tuple(entries)

    def forget(self) -> None:
        """Drop the memory - the next resolve starts from a cold pad."""
        self._previous = ("",) * SLOT_COUNT


def slot_for_project(slots: Sequence[AgentSlot], path: str) -> Optional[AgentSlot]:
    """Find the slot showing ``path``, if any."""
    wanted = normalise_project(path)
    for slot in slots:
        if slot.path == wanted:
            return slot
    return None


# ---------------------------------------------------------------------------
# Effective ownership: the subset only holds while Codex is actually here
# ---------------------------------------------------------------------------

#: How often :class:`AgentKeyOwnership` may re-check whether the ChatGPT app is
#: running, in seconds. The check is a ``pgrep`` (see
#: :func:`freemicro.permissions.chatgpt_running`) - a fork - so it must never run
#: on the render tick (~4 Hz) or the key path, both of which only ever read the
#: cached answer. **3 seconds**: soon enough that the pad expands to all six
#: within a few seconds of Codex quitting (and contracts within a few of it
#: launching), rare enough to cost nothing, and the same cadence the lighting
#: owner already probes the vendor app on (:attr:`ReassertConfig.poll_seconds`).
#: Only ever paid while a strict subset is configured; the all-six default never
#: probes at all.
DEFAULT_CODEX_POLL_SECONDS = 3.0


def _default_codex_probe() -> bool:
    """Whether the ChatGPT desktop app is running. Forks; lazy import.

    Isolated behind a function so :class:`AgentKeyOwnership` can be driven from a
    test with a fake provider and never shell out. See
    :func:`freemicro.permissions.chatgpt_running`.
    """
    from freemicro import permissions

    return permissions.chatgpt_running()


@dataclass
class AgentKeyOwnership:
    """Which Agent Keys FreeMicro drives *right now*, resolved against Codex.

    ``agent_keys.keys`` names a static subset of the six Agent Keys to coexist
    with the ChatGPT desktop app (Codex): FreeMicro lights and acts on its keys
    and leaves the rest for Codex. But that split only makes sense while Codex is
    actually there to share with. **When the ChatGPT app is not running there is
    no one to leave keys for, so FreeMicro owns all six** - it lights all six and
    acts on all six - even though a subset is configured. When the app is
    running, the configured subset is honoured. It is dynamic: if Codex quits
    FreeMicro expands to all six within a few seconds; if Codex launches it
    contracts to its subset and hands the rest back.

    This is the one place that resolves that, so the renderer (lighting) and the
    bridge (press gating) consult a single effective owned set and can never
    disagree about which keys are FreeMicro's. It caches an "is Codex here" flag
    and re-probes only on a slow cadence (:data:`DEFAULT_CODEX_POLL_SECONDS`) via
    :meth:`refresh` - never on the render tick or the key path, which only read
    the cache.

    The config stays immutable: the dynamic part is state, and it lives here.
    With the all-six default (:attr:`AgentKeysConfig.owns_all`) nothing changes,
    the probe is never called, and every read is byte-identical to the config's
    own static answer.

    Both the probe and the clock are injectable, so a test drives the whole thing
    from data with no real ``pgrep`` - the same discipline as
    :class:`freemicro.input.frontmost.FrontmostWatcher` and
    :class:`freemicro.lighting_owner.LightingOwner`.
    """

    config: AgentKeysConfig
    #: The "is the ChatGPT app running?" provider. ``None`` resolves lazily to
    #: :func:`_default_codex_probe` (the real ``pgrep``); a test passes its own.
    probe: Optional[Callable[[], bool]] = None
    #: Monotonic by default: the cadence is a duration, and an NTP step must not
    #: be able to freeze the re-probe.
    clock: Callable[[], float] = time.monotonic
    poll_seconds: float = DEFAULT_CODEX_POLL_SECONDS
    #: Assumed present until the first probe proves otherwise, so a subset config
    #: honours its split from the very first read (before any poll) rather than
    #: briefly grabbing keys it was told to share. The run loop's first tick
    #: re-probes within a tick and corrects this if Codex is in fact absent.
    _codex_here: bool = field(default=True, init=False)
    _checked_at: Optional[float] = field(default=None, init=False)

    @property
    def codex_here(self) -> bool:
        """The cached verdict: is the ChatGPT app running? Never probes."""
        return self._codex_here

    @property
    def coexisting(self) -> bool:
        """True when we are actually sharing: a subset configured AND Codex here."""
        return self.config.subset and self._codex_here

    @property
    def keys(self) -> Tuple[int, ...]:
        """The Agent Key indices FreeMicro effectively drives right now.

        The configured subset while Codex is here; all six when it is not (or
        when all six are configured anyway). A pure read of the cached flag.
        """
        if self.config.owns_all or not self._codex_here:
            return tuple(range(SLOT_COUNT))
        return self.config.keys

    def owns(self, index: int) -> bool:
        """Whether FreeMicro drives the Agent Key at ``index`` right now."""
        return index in self.keys

    def leaves_to_codex(self, input_id: Any) -> bool:
        """Whether ``input_id`` is an Agent Key we currently leave to Codex.

        ``False`` for every input when we own all six - the default, and also
        whenever Codex is not running - so the key path is byte-identical to the
        pre-split behaviour unless a subset is configured *and* the ChatGPT app
        is actually up. Pure: reads the cached flag, never probes.
        """
        if self.config.owns_all or not self._codex_here:
            return False
        index = agent_key_index(input_id)
        return index is not None and index not in self.config.keys

    def refresh(self, now: Optional[float] = None) -> bool:
        """Re-probe whether Codex is here, at most once per :attr:`poll_seconds`.

        Returns whether the effective owned set *changed*, so the caller can
        invalidate the renderer's dedupe cache and repaint the keys that just
        changed hands. Does no work and never probes when all six are owned - the
        split is off, so there is nothing to resolve. **This is the only method
        that forks**; call it from the run loop's tick, never from the key path.
        """
        if self.config.owns_all:
            return False
        now = self.clock() if now is None else now
        if self._checked_at is not None and now - self._checked_at < self.poll_seconds:
            return False
        self._checked_at = now
        was = self._codex_here
        probe = self.probe if self.probe is not None else _default_codex_probe
        self._codex_here = bool(probe())
        return self._codex_here != was

    def set_config(self, config: AgentKeysConfig) -> None:
        """Adopt a reloaded config, and re-probe promptly on the next refresh.

        The config is immutable, but a *reload* can swap all-six for a subset or
        the reverse, so the cached verdict has to be re-resolved. Reset to the
        assume-present default and force the next :meth:`refresh` to probe, so a
        newly-configured subset resolves against reality within a tick.
        """
        self.config = config
        self._codex_here = True
        self._checked_at = None


__all__ = [
    "AgentKeyOwnership",
    "AgentKeysConfig",
    "AgentKeysError",
    "AgentSlot",
    "DEFAULT_CODEX_POLL_SECONDS",
    "POLICIES",
    "POLICY_MANUAL",
    "POLICY_MIRROR",
    "POLICY_PINNED",
    "POLICY_RECENT",
    "Project",
    "SLOT_COUNT",
    "SlotResolver",
    "agent_key_index",
    "effective_state",
    "group_projects",
    "normalise_project",
    "parse_agent_keys",
    "project_label",
    "resolve_slots",
    "slot_for_project",
]
