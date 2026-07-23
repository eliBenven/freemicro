# FreeMicro QMK keymap (optional, Milestone 3)

**You almost certainly don't need this.** A status light works today with the
host-side renderers (`micro-via`, `busylight`, `screen`) and **no reflash**.
Reflash only if you want *true per-key* Agent-Key colours and you have an open
bootloader.

## What it does

Adds a `raw_hid_receive` handler that listens for a 6-byte report from the
`micro-qmk` renderer and paints the top-row Agent Keys that colour:

```
[ 0xF1, state_id, r, g, b ]
state_id: 0=idle 1=working 2=waiting 3=done 4=error
```

## Status

⚠️ **Reference only until Milestone 0.** The build target, matrix, and LED
indices are placeholders. Whether the shipping Codex Micro even accepts custom
QMK is an open question (see [`SPEC.md` §4](../../SPEC.md)). Confirm with
`freemicro detect` and the [hardware capability DB](../../hardware/capabilities.json)
before flashing anything.

## Building (once the board is known)

```sh
# Copy into your QMK tree under keyboards/<vendor>/<board>/keymaps/freemicro/
qmk compile -kb <vendor>/<board> -km freemicro
qmk flash   -kb <vendor>/<board> -km freemicro
```

Never flash firmware you can't recover from — make sure the bootloader is
reachable first.
