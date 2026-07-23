/*
 * FreeMicro — optional custom QMK keymap for true per-key Agent-Key colours.
 *
 * This is the `micro-qmk` renderer's firmware side (SPEC §5.2, Milestone 3).
 * It is STRICTLY OPT-IN: it requires an open bootloader and a deliberate
 * reflash. If you just want a status light, you do NOT need this — the host
 * `micro-via` / busylight / screen renderers cover that without touching
 * firmware.
 *
 * Protocol (must match src/freemicro/renderers/micro_qmk.py):
 *   raw_hid report: [ 0xF1, state_id, r, g, b, ... ]
 *   state_id: 0=idle 1=working 2=waiting 3=done 4=error
 *
 * Build target and matrix are placeholders pending Milestone 0 confirmation
 * of the shipping pad's QMK support. Treat this file as a reference until the
 * hardware capability report lands.
 */

#include QMK_KEYBOARD_H
#include "raw_hid.h"

#define FREEMICRO_CMD 0xF1
#define AGENT_KEY_COUNT 6  // top-row translucent Agent Keys

// Index of the first Agent Key LED in the RGB matrix. Set from the real
// board's layout once known.
#ifndef FREEMICRO_AGENT_LED_START
#    define FREEMICRO_AGENT_LED_START 0
#endif

static uint8_t agent_r = 40, agent_g = 40, agent_b = 48;  // idle default

void raw_hid_receive(uint8_t *data, uint8_t length) {
    if (length < 5 || data[0] != FREEMICRO_CMD) {
        return;
    }
    // data[1] = state_id (reserved for per-state effects); data[2..4] = RGB.
    agent_r = data[2];
    agent_g = data[3];
    agent_b = data[4];

    // Echo back so the host can confirm receipt.
    raw_hid_send(data, length);
}

#ifdef RGB_MATRIX_ENABLE
bool rgb_matrix_indicators_user(void) {
    for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
        rgb_matrix_set_color(FREEMICRO_AGENT_LED_START + i, agent_r, agent_g, agent_b);
    }
    return false;
}
#endif

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    // Layer 0 kept transparent: FreeMicro drives *lighting*, not remapping.
    // Use Work Louder Input or VIA for key bindings (see presets/).
    [0] = LAYOUT(
        KC_TRNS, KC_TRNS, KC_TRNS, KC_TRNS, KC_TRNS, KC_TRNS
    ),
};
