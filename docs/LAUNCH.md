# FreeMicro launch kit 🚀

Everything you need for the announcement, **gated so you never claim more than
the hardware proves.**

> **Status, 2026-07-23: the gate is open.** This file used to branch three ways
> on an unknown: could anything outside the ChatGPT app move the Agent Keys? It
> can. Input, LEDs and the RPC channel are verified on a physical unit over USB
> *and* Bluetooth. The two fallback scenarios that hedged against the answer
> being "no" are gone, along with the fallback renderers they advertised (see
> `SPEC.md` §5.3). What is below is the post for what actually happened.

---

## ⛔ The two things that will get you Community-Noted

1. **"First to market" is false, so do not say it.** A paid closed-source clone
   (`codex-micro.com`) already sells Claude-Code agent lighting on a CM2-class
   pad. Your defensible claim is narrower and stronger:

   > **The first *open-source* tool that drives the *actual shipping* OpenAI
   > Codex Micro's Agent-Key LEDs from Claude Code, with no ChatGPT app.**

   Use the clone as your *contrast*, not something to ignore: "a paid clone
   exists; this is the free, open, works-on-the-real-Micro version."

2. **Do not claim a fallback.** FreeMicro has none any more, on purpose. If
   somebody's pad is missing or their machine is not a Mac, the honest answer is
   "then this does nothing for you yet", not "it falls back to a busylight".

---

## ✅ Re-run this before you post

```sh
pipx install git+https://github.com/eliBenven/freemicro
freemicro doctor        # every permission, the transport, a real round-trip write
freemicro selftest      # the hook to state to LED loop, no agent needed
freemicro lights --cycle    # watch the Agent Keys walk all five states
```

If `doctor` is green on your unit and `selftest` passes, every claim in the
thread below is one you can stand behind.

---

## 🟢 The thread

**Post 1 (hook + demo)**
> My OpenAI Codex Micro's Agent Keys now glow with **Claude Code's** state, with
> no ChatGPT desktop app.
>
> One key per project. Blue while that repo's agent is thinking, amber when it
> needs me, green when it is done. Press the key, jump to that terminal.
>
> Open source, MIT. 🧵
>
> [attach the 10-sec recording of three keys in three colours]

**Post 2 (how)**
> How: Claude Code fires lifecycle hooks, a small state engine normalizes them
> to idle/working/waiting/done/error *per session*, and a renderer writes the
> pad's own LEDs over its vendor JSON-RPC channel. No firmware reflash, no
> soldering, and it works over Bluetooth.

**Post 3 (the protocol)**
> The wire protocol is written up in full: methods, key ids, the lighting
> objects, the effect enum, and the per-transport framing trap that made the
> LEDs look USB-only when they were not. As far as I can tell it is the first
> public documentation of it anywhere.
>
> github.com/eliBenven/freemicro/blob/main/docs/PROTOCOL.md

**Post 4 (honesty + moat)**
> Caveats, because I like keeping my mentions calm:
> • A *paid* closed-source clone already does Claude-Code lighting on a clone
>   pad. This is the free, open one, on the shipping Micro.
> • Pad support is macOS-only today. The vendor channel goes through IOKit.
> • One unit, one firmware. If yours behaves differently I want the report.

**Post 5 (CTA)**
> `pipx install git+https://github.com/eliBenven/freemicro && freemicro start`
>
> Then `freemicro config --web` for the visual editor: click a key on a picture
> of your pad, pick what it does.
>
> ⭐ github.com/eliBenven/freemicro

---

## 📌 Before you hit post

- [ ] Repo is **public** and the About/description + topics are set (see below)
- [ ] README renders (mermaid diagram, badges, tables)
- [ ] The demo media shows **more than one project**, in different colours; one
      key changing colour is a notification light and reads as one
- [ ] `freemicro doctor` and `freemicro selftest` are green on the unit you filmed
- [ ] First reply to your own thread = the repo link (algorithm likes it in a reply)

### Repo About + topics to paste into GitHub

**Description:**
> Your Codex Micro shows which of your Claude Code projects needs you, and
> pressing a key takes you to it. Open source, MIT, no ChatGPT app.

**Topics:**
`claude-code` `codex` `codex-micro` `macropad` `hid` `rgb`
`developer-tools` `ambient-computing` `coding-agents` `work-louder`
`status-light` `python`
