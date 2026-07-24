"""Which physical keycap is on each key - the vendor's glyph catalogue.

The pad ships with a tray of translucent white keycaps, each carrying one black
line-art glyph, and **you swap them by hand** to match what a key does. That
makes the keycap a real property of the object in front of you, not a label the
software invented: the picture on screen is only a mirror of the desk if it
shows the cap you actually installed.

So the editor does not ask you to type a label. It asks *which cap is on this
key*, offers the vendor's own set, and draws that glyph on the diagram.

Where it is stored, and why not on the binding
----------------------------------------------
As a **top-level ``keycaps`` object**, ``{"ACT06": "LAB", ...}``:

.. code-block:: json

    {"keycaps": {"ACT06": "LAB", "ACT10": "MIC", "ACT12": "TERM"}}

Not inside the binding, which is where it reads most naturally, because
:func:`freemicro.padconfig.parse` passes every field of a binding that is not
``action``/``label``/``comment`` to ``validate_params`` - so a stray ``keycap``
key would make the whole config **fail to load**. Unknown *top-level* keys are
preserved and ignored, which is exactly the contract a piece of presentation
metadata wants. If ``padconfig`` ever adopts the field, this moves; until then
this shape cannot break anybody's pad.

Nothing here ever changes behaviour. A keycap is a picture. The binding decides
what the key does, and the two are deliberately allowed to disagree - you might
own no PR cap and use a blank, and the UI must not "fix" that for you.

The catalogue itself is transcribed from ``docs/FACTORY-DEFAULTS.md`` §7 (all 37
ids, **Confirmed** against the vendor's own picker), including each glyph's
factory command so the picker can say what the cap means on a stock pad.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: ``(id, icon, label, factory command, search terms)``.
#:
#: ``icon`` is the drawing the page uses, keyed into the SVG table in
#: ``static/app.js``. One deliberate divergence from the doc: it lists ``diff``
#: for both ``DIFF`` and ``GIT``; two identical glyphs side by side on a diagram
#: help nobody, so ``GIT`` draws a commit marker here.
_CATALOGUE = [
    ("FAST", "lightning", "Fast mode", "composer.toggleFastMode",
     "fast lightning bolt speed turbo"),
    ("APPR", "check-circle", "Approve", "approval.approve",
     "approve accept yes ok tick check"),
    ("REJ", "x-circle", "Reject", "approval.decline",
     "reject decline no cancel deny x"),
    ("SPLIT", "branch", "Fork the thread", "forkThread",
     "split fork branch divide"),
    ("MIC", "mic", "Push to talk", "push-to-talk",
     "mic microphone voice dictate dictation talk speech"),
    ("CODEX", "codex", "Send", "composer.submit",
     "codex send submit enter go"),
    ("BUG", "bug", "Feedback", "feedback", "bug issue report feedback defect"),
    ("OAI", "openai", "OpenAI docs", "open developers.openai.com",
     "openai oai docs developers"),
    ("TERM", "terminal", "Terminal", "toggleTerminal",
     "term terminal shell console prompt iterm"),
    ("DWN", "download", "Copy the conversation", "copyConversationMarkdown",
     "down download save export copy"),
    ("DEL", "trash", "Archive", "archiveThread",
     "delete trash bin remove archive"),
    ("NEW", "compose", "New task", "newTask",
     "new compose write create task pencil"),
    ("NAV", "pointer", "Open a browser tab", "openBrowserTab",
     "nav navigate browser cursor pointer web chrome safari"),
    ("MAGIC", "star", "Pin the thread", "toggleThreadPin",
     "magic star pin favourite favorite"),
    ("DIFF", "diff", "Review tab", "toggleReviewTab",
     "diff changes review compare"),
    ("PLAY", "play", "Environment action", "environmentAction1",
     "play run go start continue resume"),
    ("GIT", "git-commit", "Commit", "git.commit",
     "git commit vcs version"),
    ("BRCH", "pull-request-draft", "Review tab", "toggleReviewTab",
     "branch brch draft pull request"),
    ("MRG", "pull-request-merged", "Review tab", "toggleReviewTab",
     "merge merged pull request"),
    ("PR", "pull-request", "Create a pull request", "git.createPullRequest",
     "pr pull request review merge github"),
    ("PAINT", "paint", "Add photos", "composer.addPhotos",
     "paint brush image photo picture draw"),
    ("LAB", "flask", "Settings", "settings",
     "lab flask experiment science review deep"),
    ("PARTY", "confetti", "Side chat", "openSideChat",
     "party confetti celebrate fun"),
    ("TIME", "clock", "Manage tasks", "manageTasks",
     "time clock schedule history tasks"),
    ("MIND+", "brain-plus", "More reasoning", "composer.increaseReasoningEffort",
     "mind brain think harder more effort reasoning"),
    ("MIND-", "brain-minus", "Less reasoning", "composer.decreaseReasoningEffort",
     "mind brain think less faster effort reasoning"),
    ("EMPT1", "empty", "Blank cap", "", "empty blank plain 1"),
    ("EMPT2", "empty", "Blank cap", "", "empty blank plain 2"),
    ("EMPT3", "empty", "Blank cap", "", "empty blank plain 3"),
    ("EMPT4", "empty", "Blank cap", "", "empty blank plain 4"),
    ("SETUP", "settings", "Settings", "settings",
     "setup settings gear preferences config"),
    ("FOLD", "folder-plus", "Open a folder", "openFolder",
     "fold folder directory open project"),
    ("UPL", "cloud-upload", "Add files", "composer.addFiles",
     "upl upload cloud files attach"),
    ("APPS", "all-products", "Skills", "openSkills",
     "apps grid products skills all"),
    ("YOLO", "text", "Types :yolo:", "types :yolo:", "yolo text word"),
    ("YEET", "text", "Types :yeet:", "types :yeet:", "yeet text word"),
    ("EMPT5", "empty", "Blank cap (double width)", "", "empty blank wide double"),
]

#: Caps that span two key units. Only these two can sit on the MIC slot.
DOUBLE_WIDTH = ("MIC", "EMPT5")


def catalogue() -> List[Dict[str, Any]]:
    """Every keycap the vendor ships, in the order its picker lists them."""
    return [
        {
            "id": cap_id,
            "icon": icon,
            "label": label,
            "factory": factory,
            "terms": f"{cap_id.lower()} {terms}",
            "size": "double" if cap_id in DOUBLE_WIDTH else "single",
        }
        for cap_id, icon, label, factory, terms in _CATALOGUE
    ]


def ids() -> List[str]:
    """Just the ids, for validation."""
    return [cap_id for cap_id, *_ in _CATALOGUE]


#: Which cap to *offer* when a binding changes. Suggestions only: the cap in
#: your hand is the truth, you may not own the one we would pick, and a UI that
#: silently rewrote the picture to match the binding would be lying about the
#: object. Rules are tried in order; the first match wins.
#:
#: Each rule is ``{"action": kind or "", "field": name, "contains": text}``.
#: An empty ``action`` matches any kind; an absent ``field`` matches on the kind
#: alone. Evaluated identically here and in the browser, from this one table.
SUGGESTIONS: List[Dict[str, str]] = [
    # The two the vendor already printed a glyph for. ``answer_permission``
    # answers a Claude Code permission prompt in the session that is asking,
    # which is exactly what APPR (approval.approve) and REJ (approval.decline)
    # mean on a stock pad - the closest a FreeMicro binding ever gets to a
    # factory command. Reject is matched first because the fallback for this
    # kind (no answer named, or "approve"/"always") is APPR.
    {
        "action": "answer_permission",
        "field": "answer",
        "contains": "reject",
        "keycap": "REJ",
    },
    {"action": "answer_permission", "keycap": "APPR"},
    {"action": "hold", "keycap": "MIC"},
    {"action": "app", "field": "name", "contains": "terminal", "keycap": "TERM"},
    {"action": "app", "field": "name", "contains": "iterm", "keycap": "TERM"},
    {"action": "app", "field": "name", "contains": "ghostty", "keycap": "TERM"},
    {"action": "app", "field": "name", "contains": "warp", "keycap": "TERM"},
    {"action": "app", "field": "name", "contains": "chrome", "keycap": "NAV"},
    {"action": "app", "field": "name", "contains": "safari", "keycap": "NAV"},
    {"action": "app", "field": "name", "contains": "firefox", "keycap": "NAV"},
    {"action": "app", "field": "name", "contains": "arc", "keycap": "NAV"},
    {"action": "app", "keycap": "APPS"},
    {"action": "shell", "field": "command", "contains": "pr create", "keycap": "PR"},
    {"action": "shell", "field": "command", "contains": "pr view", "keycap": "PR"},
    {"action": "shell", "field": "command", "contains": "commit", "keycap": "GIT"},
    {"action": "shell", "field": "command", "contains": "diff", "keycap": "DIFF"},
    {"action": "shell", "field": "command", "contains": "merge", "keycap": "MRG"},
    {"action": "shell", "field": "command", "contains": "branch", "keycap": "BRCH"},
    {"action": "shell", "field": "command", "contains": "switch -c", "keycap": "BRCH"},
    {"action": "shell", "field": "command", "contains": "git", "keycap": "GIT"},
    {"action": "text", "field": "text", "contains": "pull request", "keycap": "PR"},
    {"action": "text", "field": "text", "contains": "review", "keycap": "LAB"},
    {"action": "text", "field": "text", "contains": "continue", "keycap": "PLAY"},
    {"action": "text", "field": "text", "contains": "/resume", "keycap": "PLAY"},
    {"action": "text", "field": "text", "contains": "/clear", "keycap": "NEW"},
    {"action": "text", "field": "text", "contains": "/compact", "keycap": "FAST"},
    {"action": "text", "field": "text", "contains": "/model", "keycap": "MIND+"},
    {"action": "text", "field": "text", "contains": "/status", "keycap": "TIME"},
    {"action": "text", "field": "text", "contains": "git status", "keycap": "GIT"},
    {"action": "text", "field": "text", "contains": "git diff", "keycap": "DIFF"},
    {"action": "text", "field": "text", "contains": "git", "keycap": "GIT"},
    {"action": "key", "field": "key", "contains": "shift-tab", "keycap": "FAST"},
    {"action": "key", "field": "key", "contains": "cmd+t", "keycap": "SPLIT"},
    {"action": "key", "field": "key", "contains": "escape", "keycap": "REJ"},
    {"action": "key", "field": "key", "contains": "return", "keycap": "CODEX"},
    {"action": "focus_session", "keycap": "CODEX"},
    {"action": "applescript", "keycap": "MAGIC"},
    {"action": "mouse", "keycap": "NAV"},
    {"action": "none", "keycap": "EMPT1"},
]


#: The other direction: you fitted a cap, so what should the key *do*?
#:
#: The factory caps carry factory commands (``docs/FACTORY-DEFAULTS.md`` §7),
#: and a few of them have an obvious FreeMicro equivalent that the vendor app
#: cannot offer for Claude Code at all - APPR and REJ answering a permission
#: prompt being the best example. Offered as a one-click suggestion, never
#: applied on its own: the cap is a picture, and it is not allowed to rewrite
#: what your key does behind your back.
BINDING_FOR_CAP: Dict[str, Dict[str, Any]] = {
    "APPR": {
        "action": "answer_permission",
        "answer": "approve",
        "long_press": "always",
        "label": "approve",
        "why": (
            "Answers the permission prompt in the session that is actually "
            "asking: it raises that exact terminal tab and answers there. Hold "
            "it for half a second for 'yes, and stop asking'. Does nothing at "
            "all when nothing is waiting, which is why it is not a plain "
            "keystroke - a blind Return would land in whatever window happened "
            "to be in front."
        ),
    },
    "REJ": {
        "action": "answer_permission",
        "answer": "reject",
        "label": "reject",
        "why": (
            "Declines the permission prompt in the session that is asking, "
            "after raising its tab. Silent when nothing is waiting."
        ),
    },
    "MIC": {
        "action": "hold",
        "key": "ctrl+cmd+o",
        "label": "mic - push to talk",
        "why": (
            "Holds a dictation shortcut down for as long as you hold the key. "
            "Three keys, because Wispr Flow refuses anything longer - a "
            "four-key combo cannot be registered there and the mic just "
            "appears dead. Set the same combo in your dictation app as "
            "push-to-talk, not toggle."
        ),
    },
    "PLAY": {
        "action": "text",
        "text": "continue",
        "submit": True,
        "label": "play - continue",
        "why": "Unsticks an agent that has stalled.",
    },
    "TERM": {
        "action": "app",
        "name": "Terminal",
        "cycle": True,
        "label": "term - terminal",
        "why": "Brings your terminal to the front.",
    },
    "PR": {
        "action": "shell",
        "command": "gh pr create --fill",
        "label": "pr - open a pull request",
        "why": "The factory command for this cap is git.createPullRequest.",
    },
    "GIT": {
        "action": "text",
        "text": "git status",
        "submit": True,
        "label": "git status",
        "why": "The factory command for this cap is git.commit.",
    },
    "NEW": {
        "action": "text",
        "text": "/clear",
        "submit": True,
        "label": "new task",
        "why": "The factory command for this cap is newTask.",
    },
    "TIME": {
        "action": "text",
        "text": "/status",
        "submit": True,
        "label": "status",
        "why": "The factory command for this cap is manageTasks.",
    },
    "LAB": {
        "action": "text",
        "text": "/review this session's work in depth: what changed, what is "
                "risky, what is untested",
        "submit": True,
        "label": "lab - deep review",
        "why": "A thorough review pass over the current session.",
    },
    "FAST": {
        "action": "text",
        "text": "/compact",
        "submit": True,
        "label": "compact",
        "why": "The factory command for this cap is composer.toggleFastMode.",
    },
    "SPLIT": {
        "action": "text",
        "text": "/resume",
        "submit": True,
        "label": "resume",
        "why": "The factory command for this cap forks the thread.",
    },
    "CODEX": {
        "action": "key",
        "key": "return",
        "label": "send",
        "why": "The factory command for this cap is composer.submit.",
    },
    "NAV": {
        "action": "app",
        "name": "Google Chrome",
        "cycle": True,
        "label": "nav - browser",
        "why": "The factory command for this cap opens a browser tab.",
    },
}


def binding_for(cap_id: str) -> Optional[Dict[str, Any]]:
    """A suggested binding for a cap, or ``None``. Never applied by itself."""
    found = BINDING_FOR_CAP.get(cap_id)
    return dict(found) if found else None


def suggest(binding: Optional[Dict[str, Any]]) -> str:
    """The keycap to offer for a binding, or ``""`` if nothing fits.

    Pure and table-driven so the browser can run the same rules on every
    keystroke without a round trip, and so a rule can be tested here.
    """
    if not isinstance(binding, dict):
        return ""
    kind = str(binding.get("action") or "")
    for rule in SUGGESTIONS:
        if rule.get("action") and rule["action"] != kind:
            continue
        field = rule.get("field")
        if field:
            value = binding.get(field)
            if not isinstance(value, str):
                continue
            if rule["contains"] not in value.lower():
                continue
        return rule["keycap"]
    return ""


def clean(raw: Any) -> Dict[str, str]:
    """A ``keycaps`` section with only ids this build knows, as strings."""
    known = set(ids())
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(value, str) and value in known
    }


__all__ = [
    "BINDING_FOR_CAP",
    "DOUBLE_WIDTH",
    "SUGGESTIONS",
    "binding_for",
    "catalogue",
    "clean",
    "ids",
    "suggest",
]
