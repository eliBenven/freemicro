# Path B - USB capture playbook (RETIRED, 2026-07-23)

> **This plan was never executed, and it is no longer needed.** The Codex Micro's
> LED channel turned out to speak **JSON-RPC**, not an opaque binary RGB format,
> so the protocol was recovered by enumerating the device's own method table
> rather than by capturing anyone's USB traffic.
>
> **→ The real, verified protocol is in [`PROTOCOL.md`](PROTOCOL.md).**
> **→ The decision record is in [`LED-STRATEGY.md`](LED-STRATEGY.md).**

Kept as a historical record, and because the reasoning that made it obsolete is
worth passing on.

## What this document used to say

Capture the ChatGPT desktop app driving the pad (Linux `usbmon` / Windows
USBPcap), diff the OUT reports between solid-red and solid-green, guess at the
command/index/RGB layout, and replay the bytes from a `micro-sniffed` renderer.
It assumed:

1. the `0xFF00` payload was an opaque 63-byte binary RGB report;
2. macOS userland could not open the channel, so capture had to happen on
   Linux or Windows;
3. therefore the ChatGPT app was the only available source of truth.

## Why all three were wrong

**1. The payload was text.** Each 63-byte report is framed `[0x02][len][UTF-8
JSON…]`, CRLF-terminated, carrying JSON-RPC. An earlier write test concluded the
firmware "accepts writes but ignores every payload" - accurate, and badly
misleading. The writes were accepted because the *framing* was right; nothing
happened because the *payload* was not JSON the firmware recognised. Brute
forcing 63 opaque bytes was never going to work, because the bytes were never
opaque.

**2. macOS was fine all along.** `hidapi` fails on this device because it models
each HID top-level collection as its own openable path, while macOS vends a
single `IOHIDDevice` whose primary usage is the keyboard collection. Going
straight to IOKit works from plain userland with only the user's **Input
Monitoring** grant - no entitlement, no kext, no second machine.

**3. The device documents itself.** Unknown methods return
`{"error":{"code":404,"message":"Method not found"}}`, and the firmware exposes a
conventional method table (`sys.*`, `device.*`, `lights.*`, `ui.*`, `fs.*`,
`v.oai.*`). Asking the device was faster, cleaner and more precise than watching
someone else ask it.

## The transferable lesson

Before reverse-engineering a vendor HID protocol by packet capture, spend ten
minutes checking whether the payload is **text**, and whether the device answers
**introspection**. A surprising number of modern USB peripherals - especially
ESP32-class ones with a companion app - carry JSON-RPC over a vendor collection.
If it does, you skip the capture rig entirely and you never touch anyone else's
traffic.

## Guardrails (unchanged, and still in force)

- **Interop only, on hardware we own.** We describe the device's observable
  behaviour so other software can talk to it.
- **Never redistribute** OpenAI or Work Louder firmware, app binaries, or source.
  Nothing of theirs is copied or vendored into this repository; FreeMicro's
  client is written independently from documented interface facts.
