# Sniff & Replay Runbook — replicate the Codex Micro's LEDs for Claude Code

This is the hands-on procedure for **Path B** ([`LED-STRATEGY.md`](LED-STRATEGY.md)):
capture the exact HID reports the ChatGPT desktop app sends to light the Agent
Keys, then replay them from Claude Code. The result is the *same* on-pad
behaviour — colours, per-key patterns, animations — triggered by Claude instead
of Codex.

> **You run this on the machine the Micro is plugged into**, not in a cloud
> session — USB bus capture needs the physical device. Budget ~30 minutes the
> first time.

> **Scope:** this reads HID reports on the wire for personal interoperability
> with hardware you own. It does not modify, extract, or redistribute any
> OpenAI or Work Louder firmware.

---

## 0. What you'll produce

One capture file per Codex state, then a single learned profile:

```
thinking.json  running.json  awaiting.json  done.json  error.json  idle.json
        │
        ▼
freemicro learn  →  ~/.freemicro/protocol.json  →  micro-sniffed renderer
```

The Codex → Claude Code state mapping the profile uses:

| Codex state | FreeMicro state |
|---|---|
| `idle` | idle |
| `thinking`, `running` | working |
| `awaiting_input` | waiting |
| `done` | done |
| `error` | error |

---

## 1. Find the pad on the bus

```sh
pip install "freemicro[detect]"
freemicro detect --json    # note the vendor_id:product_id and the interface
```

Record the `vid:pid` (e.g. `574c:1df9`) — you'll pin it in step 4.

## 2. Capture (pick your OS)

The trick in every case: **make the pad change to one known state, capture a
short burst, stop.** Do each state separately so the files stay clean. Keep the
ChatGPT app running for the capture (it's the thing we're observing).

### Windows — USBPcap + Wireshark
1. Install [Wireshark](https://www.wireshark.org/) (includes USBPcap).
2. Start capturing on the USBPcap interface that lists the Micro.
3. Drive the pad to one state — the simplest way is to make Codex actually enter
   it (start a task → `thinking`/`running`; trigger an approval → `awaiting`;
   let it finish → `done`). Capture ~2 s around the transition.
4. Filter to the pad's OUT reports: `usb.dst contains "<your device>"` and
   `usbhid.data`. Then **File → Export Packet Dissections → As JSON**.
5. Save as `thinking.json`, `done.json`, etc.

### macOS — Wireshark USB capture
1. `Wireshark` → enable the USB capture interface (you may need the
   Apple-provided USB capture / to run Wireshark with the right entitlement).
2. Same idea: capture per state, filter `usbhid.data`, export **As JSON**.
   (Alternatively use Apple's **PacketLogger** and convert, but JSON export from
   Wireshark is the easy path.)

### Linux — usbmon + tshark
```sh
sudo modprobe usbmon
# find the bus number for the pad from `lsusb`; say it's bus 1:
sudo tshark -i usbmon1 -Y 'usbhid.data' -T json > thinking.json
#   ...drive the pad to `thinking`, wait ~2s, Ctrl-C. Repeat per state.
```

> **Tip — solid colours help.** If you can also capture the pad showing solid
> **red**, **green**, and **blue** (any means), pass them to `--color` in step 4
> and `learn` will additionally infer the RGB byte offsets, unlocking parametric
> colours beyond the literal captures.

## 3. Sanity-check a capture

```sh
python3 -c "from freemicro.capture import parse_capture; \
print(parse_capture('done.json')[:3])"
```

You should see a few report frames like `[[11, 0, 52, 199, 89], ...]`. If it's
empty, your filter caught the wrong direction/interface — re-capture the OUT
(host→device) reports.

## 4. Learn the profile

```sh
freemicro learn \
  thinking=thinking.json running=running.json awaiting=awaiting.json \
  done=done.json error=error.json idle=idle.json \
  --color red=red.json green=green.json blue=blue.json \
  --vid-pid 574c:1df9
```

Writes `~/.freemicro/protocol.json`. Re-run anytime to refine it.

## 5. Replay it from Claude Code

```sh
# IMPORTANT: quit the ChatGPT desktop app first, or it will keep writing its own
# state to the pad and fight the replay for the HID channel.
freemicro install     # if you haven't wired Claude Code hooks yet
freemicro watch       # micro-sniffed becomes the primary renderer automatically
```

Now use Claude Code. The Agent Keys light exactly as Codex drove them — but on
Claude's state. Confirm the renderer took over:

```sh
freemicro renderers   # micro-sniffed should show [✓]
```

## 6. Share it (optional, high-value)

Your `protocol.json` (with any device-specific IDs removed) plus your
`freemicro detect --json` is the first public capability report for the shipping
Codex Micro. Attach it to a **Hardware Report** issue to seed
`hardware/capabilities.json` for everyone.

---

## Troubleshooting

- **`micro-sniffed` not `[✓]`** → no `protocol.json`, or it has no frames, or the
  device isn't found. Re-check `freemicro detect` and the `--vid-pid`.
- **LEDs flicker / fight** → the ChatGPT app is still running. Quit it fully.
- **Nothing captured** → wrong interface or direction. You want host→device OUT
  reports on the pad's interface. On Linux, confirm the right `usbmonN`.
- **Frames but no colour change on replay** → the app may checksum or sequence
  reports, or use a set-and-flush pair. Capture the *full* burst per state (not
  one packet) — `learn` keeps the whole distinct set and replays it in order.
