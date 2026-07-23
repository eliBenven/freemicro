# FreeMicro — Launch Kit 🚀

Everything you need for the X post — **gated so you never claim more than the
hardware proves.** Do the go/no-go check first, then fire the matching thread.

---

## ⛔ The two things that will get you Community-Noted

1. **"First to market" is false — don't say it.** A paid closed-source clone
   (`codex-micro.com`) already sells Claude-Code agent lighting on a CM2-class
   pad. Your defensible claim is narrower and stronger:

   > **The first *open-source* tool that drives the *actual shipping* OpenAI
   > Codex Micro's Agent-Key LEDs from Claude Code — no ChatGPT app.**

   Use the clone as your *contrast*, not something to ignore: "a paid clone
   exists; this is the free, open, works-on-the-real-Micro version."

2. **Don't claim "it works" before the probe.** The LED path is unverified
   until `freemicro detect` + a real write test on the physical unit. Post the
   scenario that matches what actually happened — see below.

---

## ✅ Go / No-Go checklist (run when the Micro arrives)

```sh
pip install "freemicro[detect]"

# 1. Read-only probe — what does the pad expose?
freemicro detect --json | tee hardware/report-codex-micro.json

# 2. Is there a writable raw-HID channel?  (look for "has_raw_channel": true)
#    Also try https://usevia.app — does it detect the pad?

# 3. THE money test — does a write actually move the Agent Keys?
freemicro watch &                 # in one pane
freemicro emit working            # then waiting / done / error
#   ...or one-shot:
freemicro render done --hold 5
#   Watch the top-row LEDs. Did they change colour? Per-key or all-one-colour?
#   Did you have to quit the ChatGPT desktop app first? (note it either way)
```

Record the answers in `hardware/capabilities.json` and open a Hardware Report —
that report is itself a community first, win or lose.

**Then pick your scenario:**

| Probe result | Scenario | Post |
|---|---|---|
| Agent Keys change colour from `freemicro render` | 🟢 **A — Full win** | Thread A |
| Raw channel writable, but Agent Keys won't move (they're app-owned) | 🟡 **B — Partial** | Thread B |
| No writable channel / firmware locked | 🔴 **C — Fallback** | Thread C |

---

## 🟢 Thread A — Full win (LEDs on the real Micro, driven by Claude Code)

**Post 1 (hook + demo)**
> My OpenAI Codex Micro's Agent Keys now glow with **Claude Code's** state — no
> ChatGPT desktop app.
>
> Idle → blue while it thinks → amber when it needs me → green when it's done.
>
> Open source, MIT. I think this is the first time anyone's openly driven the
> *real* Micro's LEDs from another agent. 🧵
>
> [attach the 10-sec screen recording of the top row changing colour]

**Post 2 (how)**
> How: Claude Code fires lifecycle hooks → a tiny state engine normalizes them
> to idle/working/waiting/done/error → a renderer writes the pad's LEDs over the
> VIA raw-HID channel. No firmware reflash.
>
> The Micro's own Agent Keys, finally following the agent I actually use.

**Post 3 (honesty + moat)**
> Caveats, because I like keeping my mentions calm:
> • A *paid* closed-source clone already does Claude-Code lighting on a clone
>   pad. This is the free, open one — on the shipping Micro.
> • If your pad's firmware is locked, it falls back to a busylight or an
>   on-screen chip. The alert never depends on the pad.

**Post 4 (CTA)**
> `pip install freemicro && freemicro install && freemicro watch`
>
> Works with any VIA/QMK RGB pad, not just the Micro. Got different hardware?
> `freemicro detect --json` and open a Hardware Report — I'm building a
> crowdsourced capability DB.
>
> ⭐ github.com/eliBenven/freemicro

---

## 🟡 Thread B — Partial (writable pad, but Agent Keys are app-owned)

**Post 1**
> Probed the OpenAI Codex Micro so you don't have to. It *does* expose a
> writable VIA channel — but the six Agent Keys are driven on a separate path
> the ChatGPT app owns.
>
> So I did the next best thing: FreeMicro lights the pad's *other* RGB from
> **Claude Code's** live state. 🧵

**Post 2**
> The full read-only capability report is public (VID/PID, interfaces, the
> 0xFF60 raw channel, what moved and what didn't). Nobody had posted one for
> the shipping Micro. That alone was worth the afternoon.

**Post 3 (CTA)**
> Open source, MIT. Falls back to a busylight or on-screen chip if your pad is
> locked. Any VIA/QMK pad works.
>
> `pip install freemicro`
> ⭐ github.com/eliBenven/freemicro — and send me your `freemicro detect --json`.

---

## 🔴 Thread C — Fallback (pad locked; fallback carries the signal)

**Post 1**
> Bought the OpenAI Codex Micro, opened it up (figuratively), and confirmed:
> the Agent Keys are locked to the ChatGPT app — no writable channel for the
> rest of us. Now you don't have to wonder.
>
> Full read-only capability report 👇 (a community first)

**Post 2**
> So FreeMicro gives Claude Code the same *signal* a different way: a busylight
> or an always-on-top on-screen chip that goes blue → amber → green → red with
> your agent's state. The alert never depended on the pad anyway.
>
> Open source, MIT.

**Post 3 (CTA)**
> If OpenAI/Work Louder ever open the channel, the `micro-via` renderer is
> already written and waiting.
>
> `pip install freemicro`
> ⭐ github.com/eliBenven/freemicro

---

## 📌 Before you hit post

- [ ] Repo is **public** and the About/description + topics are set (see below)
- [ ] README renders (mermaid diagram, badges, tables)
- [ ] The demo media matches the scenario you're claiming
- [ ] You ran the probe and saved the report to `hardware/`
- [ ] You picked the thread that matches reality
- [ ] First reply to your own thread = the repo link (algorithm likes it in a reply)

### Repo About + topics to paste into GitHub

**Description:**
> Turn any macro pad into a live status light for Claude Code and other coding
> agents — drives the OpenAI Codex Micro's Agent-Key LEDs, VIA/QMK RGB pads,
> busylights, or your screen. No ChatGPT app. MIT.

**Topics:**
`claude-code` `codex` `codex-micro` `macropad` `qmk` `via` `hid` `rgb`
`developer-tools` `ambient-computing` `coding-agents` `work-louder`
`busylight` `status-light` `python`
