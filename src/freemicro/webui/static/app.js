/* FreeMicro web UI.
 *
 * Vanilla JS on purpose: FreeMicro's core is dependency-free and a settings
 * page is not a good reason to introduce a toolchain. No framework, no build,
 * no CDN - everything on this page came from the Python process that served it.
 *
 * The editing model is deliberately dumb: there is exactly one source of truth,
 * `S.doc`, which is the *raw JSON document* from ~/.freemicro/keymap.json,
 * comments and all. Every control mutates that object; nothing is re-derived
 * from a parsed model. That is what lets an edit round-trip without eating the
 * user's comments or any config key this build has never heard of.
 *
 * The diagram is a mirror, not a picture. The six Agent Keys are drawn in the
 * colour they are actually configured to show for the state you have selected,
 * with the glow scaled by brightness, so what is on screen is what would be on
 * the desk. The action keys are drawn tan and unlit because that is what they
 * are: no per-key LED, only the global backlight washing under them.
 *
 * Three rules this file learned the hard way, from a build where clicking the
 * pad did nothing and nobody could see why:
 *
 * 1. **Listeners are attached once, up front, and never inside a render.**
 *    `wire()` runs synchronously before the first fetch. The pad uses one
 *    delegated click handler on the container, so a redraw cannot detach it and
 *    a render that throws cannot leave a page that looks finished and is inert.
 * 2. **Nothing fails silently.** A script error, a rejected promise or a dead
 *    endpoint puts a message on the screen. The user should never have to open
 *    a console to find out that the page gave up.
 * 3. **Anything that refuses to work says why, where you are looking.** The pad
 *    being held by another process is normal and correct; being unable to tell
 *    is the bug.
 */

'use strict';

/* ----------------------------------------------------------------- token */

const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';
if (params.has('token')) {
  // Keep it out of the address bar, browser history and any copied link.
  history.replaceState(null, '', location.pathname);
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers['X-FreeMicro-Token'] = TOKEN;
  if (opts.body !== undefined) {
    opts.method = opts.method || 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new Error(
      `Could not reach FreeMicro at ${path}. Is it still running in your ` +
      'terminal? (' + (err && err.message ? err.message : err) + ')');
  }
  if (res.status === 401 || res.status === 403) {
    throw new Error('This tab lost its token. Reopen the URL FreeMicro printed.');
  }
  let payload;
  try {
    payload = await res.json();
  } catch (err) {
    throw new Error(`${path} returned something that is not JSON (HTTP ` +
                    `${res.status}).`);
  }
  if (res.status >= 500) {
    throw new Error(payload.error || `${path} failed (HTTP ${res.status}).`);
  }
  return payload;
}

/* ----------------------------------------------------------------- state */

const S = {
  schema: null,
  doc: null,
  // What the page LOADED, kept untouched. Every save sends this alongside the
  // edited document so the server can write only the difference - the page is
  // never again allowed to overwrite a file with its own idea of it.
  base: null,
  fingerprint: '',
  baseline: '',
  paths: { load: '', save: '', backup: '' },
  selected: null,     // input id being edited, or null
  control: null,      // firmware control being explained, or null
  tab: 'setup',
  // `unknown` until the first /api/device answer: the page must not flash
  // "no pad connected" at someone who has one plugged in.
  device: { usable: false, reason: '', present: false, transport: null,
            unknown: true },
  capture: { on: false, since: 0, timer: null },
  live: true,
  showState: 'idle',
  openCard: 'idle',
  projects: [],   // live project directories, for the Agent-Key slots
  starters: [],
  layouts: [],        // named whole pads: built-in starters plus the user's
  layout: '',         // which one is showing, when we know
  comboCapture: null, // the shortcut field currently listening
  captureClaimed: false,
  comboCaptureStop: null,
  dragFrom: null,     // key being dragged on the diagram
  apps: null,         // null = not fetched yet
  preview: null,      // starter diff awaiting confirmation
  undo: null,         // { document, label } - one step, always offered
  applied: null,      // what the last apply actually did, shown afterwards
  dictation: 'wispr',
  validation: { ok: true, error: '' },
  previewBusy: false,
  previewQueued: null,
  // Set only by the "carry on anyway" button on the stale-server panel.
  staleDismissed: false,
};

const $ = (sel) => document.querySelector(sel);

const SVG_NS = 'http://www.w3.org/2000/svg';

/* SVG is a different namespace, and `document.createElement('path')` does not
 * fail - it silently returns an HTMLUnknownElement that the layout engine will
 * never draw. That is the worst shape a bug can have on this page: correct
 * looking code, a node in the DOM, and nothing on the glass. So the tag table
 * lives here and `el()` routes to `createElementNS` on its own; there is no way
 * to build one of these by accident through the wrong door. */
const SVG_TAGS = new Set([
  'svg', 'g', 'path', 'circle', 'ellipse', 'rect', 'line', 'polyline',
  'polygon', 'text', 'tspan', 'defs', 'use', 'symbol', 'title', 'desc',
  'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'pattern',
  'marker', 'foreignObject',
]);

const el = (tag, attrs, ...kids) => {
  // Namespace-aware on purpose: see SVG_TAGS.
  const node = SVG_TAGS.has(tag)
    ? document.createElementNS(SVG_NS, tag)
    : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    // `className` on an SVG element is a read-only SVGAnimatedString, so the
    // assignment does nothing and the glyph loses its styling in silence.
    if (k === 'class') {
      if (SVG_TAGS.has(tag)) node.setAttribute('class', v);
      else node.className = v;
    } else if (k === 'text') node.textContent = v;
    // No innerHTML anywhere in this file: every string that reaches the DOM is
    // set as text, so a label pasted out of someone's config cannot inject.
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    // Set through the CSSOM, not a style="" attribute: the page ships with a
    // strict Content-Security-Policy and `style-src 'self'` blocks inline style
    // attributes outright. Keeping the policy tight is worth this much.
    else if (k === 'style') {
      for (const rule of String(v).split(';')) {
        const at = rule.indexOf(':');
        if (at < 0) continue;
        node.style.setProperty(rule.slice(0, at).trim(), rule.slice(at + 1).trim());
      }
    } else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

/* The only way this file replaces the contents of a node.
 *
 * `Node.replaceChildren()` is a NATIVE DOM method and it does not behave like
 * our own `el()`. It does not flatten arrays and it does not drop nulls: every
 * argument that is not a Node is run through `String()` and inserted as text.
 * So a builder that returns a list renders as
 * "[object HTMLHeadingElement],[object HTMLParagraphElement]", and a
 * `condition ? el(...) : null` renders as the literal word "null". Both of
 * those shipped, and neither throws, which is why they were found by eye.
 *
 * `mount()` takes the same children `el()` takes - nested arrays, nulls,
 * false, plain strings - and cannot produce either failure. Nothing in this
 * file calls `replaceChildren` directly; there is a test that says so. */
function mount(host, ...kids) {
  const nodes = [];
  const add = (kid) => {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(add); return; }
    nodes.push(kid.nodeType ? kid : document.createTextNode(String(kid)));
  };
  kids.forEach(add);
  host.replaceChildren(...nodes);
  return host;
}

/* ------------------------------------------------------------------ icons */

/* The pad ships with a tray of translucent keycaps, each carrying one black
 * line-art glyph, and you swap them by hand. So the diagram draws the glyph on
 * the cap you told it is installed - the picture is only a mirror of the desk
 * if it shows the cap you actually put there.
 *
 * Hand-authored 24-unit paths: no icon font, no CDN, nothing external (the CSP
 * forbids all three), and no emoji. Stroked rather than filled so they stay
 * legible at the ~40px the diagram draws them at, and so they take the ink
 * colour of the cap they sit on.
 */
const GLYPHS = {
  'lightning': ['M13 2 5 13h5l-1 9 9-12h-5l1-8z'],
  'check-circle': ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'm8 12 3 3 5-6'],
  'x-circle': ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'm9 9 6 6', 'm15 9-6 6'],
  'branch': ['M7 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
             'M7 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
             'M17 10a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
             'M7 8v8', 'M17 10v1a5 5 0 0 1-5 5H7'],
  'mic': ['M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z',
          'M6 11a6 6 0 0 0 12 0', 'M12 17v4', 'M9 21h6'],
  'codex': ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'm10 9-3 3 3 3',
            'm14 9 3 3-3 3'],
  'bug': ['M9 7a3 3 0 0 1 6 0', 'M8 10h8v5a4 4 0 0 1-8 0v-5z',
          'M4 11h4M16 11h4M5 17l3-2M19 17l-3-2M6 6l2 2M18 6l-2 2'],
  'openai': ['M12 3.5 19 7.5v9L12 20.5 5 16.5v-9z', 'M12 3.5v8.5l7 4.5',
             'm12 12-7 4.5'],
  'terminal': ['M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
               'm7 9 3 3-3 3', 'M13 15h4'],
  'download': ['M12 4v11', 'm8 11 4 4 4-4', 'M5 19h14'],
  'trash': ['M5 7h14', 'M10 7V5h4v2', 'm7 7 1 12h8l1-12', 'M11 11v5M13 11v5'],
  'compose': ['M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6',
              'm14 3.5 4 4L11 15H7v-4z'],
  'pointer': ['m6 3 12 8-5.2 1.4L10.4 18z'],
  'star': ['m12 3 2.6 5.6 6 .8-4.4 4.3 1.1 6.1-5.3-3-5.3 3 1.1-6.1L3.4 9.4l6-.8z'],
  'diff': ['M12 5v6M9 8h6', 'M9 16h6', 'M9 19h6'],
  'play': ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'm10 8 6 4-6 4z'],
  'git-commit': ['M3 12h6M15 12h6',
                 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z'],
  'pull-request': ['M7 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z', 'M7 8v13',
                   'M17 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
                   'M17 16V8l-3-3', 'M14 8h3V5'],
  'pull-request-draft': ['M7 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z', 'M7 8v13',
                         'M17 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
                         'M17 13v-2M17 8V6'],
  'pull-request-merged': ['M7 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z', 'M7 8v13',
                          'M17 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
                          'M17 16v-2a6 6 0 0 0-6-6H7'],
  'paint': ['m15 3 6 6-9 9H6v-6z', 'M4 21h5', 'm13 5 6 6'],
  'flask': ['M9 3h6', 'M10 3v6l-5 9a1.5 1.5 0 0 0 1.3 2.3h11.4A1.5 1.5 0 0 0 19 18l-5-9V3',
            'M8 15h8'],
  'confetti': ['M4 20 9 8l7 7z', 'M14 4v2M18 6l-1.5 1.5M20 11h-2M16 2l1 1'],
  'clock': ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M12 7v5.5l3.5 2'],
  'brain': ['M12 4.5A3.5 3.5 0 0 0 5.5 6 3 3 0 0 0 4 11.5a3 3 0 0 0 1.5 5A3.5 3.5 0 0 0 12 19.5z',
            'M12 4.5A3.5 3.5 0 0 1 18.5 6 3 3 0 0 1 20 11.5a3 3 0 0 1-1.5 5A3.5 3.5 0 0 1 12 19.5z'],
  'empty': ['M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'],
  'settings': ['M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
               'M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.2 2.2M16.9 16.9l2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2'],
  'folder-plus': ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
                  'M12 11v6M9 14h6'],
  'cloud-upload': ['M7.5 18a3.8 3.8 0 0 1 .6-7.6 5 5 0 0 1 9.6-1A3.6 3.6 0 0 1 17 18',
                   'M12 21v-8', 'm9 16 3-3 3 3'],
  'all-products': ['M4 5.5A1.5 1.5 0 0 1 5.5 4h3A1.5 1.5 0 0 1 10 5.5v3A1.5 1.5 0 0 1 8.5 10h-3A1.5 1.5 0 0 1 4 8.5z',
                   'M14 5.5A1.5 1.5 0 0 1 15.5 4h3A1.5 1.5 0 0 1 20 5.5v3A1.5 1.5 0 0 1 18.5 10h-3A1.5 1.5 0 0 1 14 8.5z',
                   'M4 15.5A1.5 1.5 0 0 1 5.5 14h3A1.5 1.5 0 0 1 10 15.5v3A1.5 1.5 0 0 1 8.5 20h-3A1.5 1.5 0 0 1 4 18.5z',
                   'M14 15.5A1.5 1.5 0 0 1 15.5 14h3A1.5 1.5 0 0 1 20 15.5v3A1.5 1.5 0 0 1 18.5 20h-3A1.5 1.5 0 0 1 14 18.5z'],
};
// The two reasoning caps are the brain plus a sign, so the sign is the only
// thing that differs - exactly as it is on the physical caps.
GLYPHS['brain-plus'] = GLYPHS.brain.concat(['M17 19h5M19.5 16.5v5']);
GLYPHS['brain-minus'] = GLYPHS.brain.concat(['M17 19h5']);

/* Say it out loud when you mean SVG.
 *
 * `el()` already routes every tag in SVG_TAGS through `createElementNS`, so
 * this is not the only safe door - but a glyph builder that spells out the
 * namespace it needs cannot be quietly moved onto `document.createElement` by
 * someone tidying up, and the throw below turns a typo into an error instead
 * of an element that exists and never draws. */
function svgEl(tag, attrs, ...kids) {
  if (!SVG_TAGS.has(tag)) {
    throw new Error(`svgEl: <${tag}> is not an SVG element`);
  }
  return el(tag, attrs, ...kids);
}

/* Built through createElementNS rather than innerHTML - same rule as the rest
 * of this file, and the CSP would not have it any other way.
 *
 * Takes a catalogue entry or a bare cap id, and ALWAYS returns something with
 * ink in it. Drawing nothing is the failure this function exists to prevent:
 * a key with no glyph looks exactly like a key with no cap, so a catalogue
 * this build has never heard of would silently empty the whole diagram. */
function glyphNode(cap) {
  const entry = typeof cap === 'string' ? (capById(cap) || { id: cap }) : cap;
  const svg = svgEl('svg', {
    viewBox: '0 0 24 24', class: 'glyph', 'aria-hidden': 'true',
  });
  if (!entry) return svg;
  const paths = GLYPHS[entry.icon];
  if (!paths || entry.icon === 'text') {
    // No drawing for this cap: set its id in the same ink rather than nothing.
    svg.append(svgEl('text', { x: '12', y: '15', class: 'glyph-text' },
                     entry.id || '?'));
    return svg;
  }
  for (const d of paths) svg.append(svgEl('path', { d }));
  if (entry.icon === 'pull-request-draft') {
    svg.firstChild.setAttribute('stroke-dasharray', '3 2');
  }
  return svg;
}

const capById = (id) =>
  (S.schema && S.schema.keycaps || []).find((c) => c.id === id) || null;

/* --------------------------------------------------------- document access */

/* Two flavours of accessor, and the difference matters:
 *
 *   peek*() - pure reads, used by everything that only draws.
 *   lighting(), joystick(), agents(), stateLight() - materialise the section if
 *              it is missing. Only ever called from a change handler.
 *
 * Mixing them up is how a settings page ends up marked "unsaved" the instant it
 * loads, which trains people to ignore the indicator.
 */

function bindings() {
  // Required by the schema, so it always exists in anything that parsed.
  if (!S.doc.bindings || typeof S.doc.bindings !== 'object') S.doc.bindings = {};
  return S.doc.bindings;
}
function binding(id) {
  const raw = bindings()[id];
  return raw && typeof raw === 'object' ? raw : null;
}

/* Every id a single keycap fires. The wide MIC cap sits over two switches and
 * reports ACT10 *and* ACT11 on every press, so one edit has to write both. */
function pairOf(id) {
  const paired = (S.schema && S.schema.paired_inputs) || {};
  const group = paired[id];
  return Array.isArray(group) && group.length ? group.slice() : [id];
}

/* Which physical cap is installed on a key. A top-level section, because
 * padconfig hands every unrecognised *binding* field to the action validator
 * and would refuse to load a config with a "keycap" in one - see
 * freemicro/webui/keycaps.py. Purely presentational either way. */
function peekCaps() {
  const section = S.doc[S.schema.keycap_section];
  return (section && typeof section === 'object') ? section : {};
}
function capOf(id) {
  const chosen = peekCaps()[id];
  // A cap id this build does not know still gets drawn - as its own name, in
  // the same ink. Returning null would blank the key, which reads as "the
  // icons are broken" rather than "that cap came from a newer catalogue".
  if (chosen) return capById(chosen) || { id: chosen, icon: '', label: chosen };
  const cell = (S.schema.layout.cells || []).find((c) => c.id === id);
  if (!cell || !cell.factory_cap) return null;
  return capById(cell.factory_cap)
      || { id: cell.factory_cap, icon: '', label: cell.factory_cap };
}
/* Has the user told us what is actually fitted here, or are we still guessing
 * from the box? Worth distinguishing: the guess is drawn faintly. */
function capIsAssumed(id) {
  return !peekCaps()[id];
}
function setCap(id, capId) {
  const key = S.schema.keycap_section;
  if (!S.doc[key] || typeof S.doc[key] !== 'object') S.doc[key] = {};
  for (const member of pairOf(id)) {
    if (capId) S.doc[key][member] = capId;
    else delete S.doc[key][member];
  }
  if (!Object.keys(S.doc[key]).length) delete S.doc[key];
}

/* The cap we would offer for a binding. Same rule table the server evaluates,
 * shipped in the schema so both sides cannot disagree - and a *suggestion*
 * only: the cap in your hand is the truth, and you may not own the one we
 * would have picked. */
function suggestCap(bound) {
  if (!bound || typeof bound !== 'object') return '';
  const kind = String(bound.action || '');
  for (const rule of S.schema.keycap_rules || []) {
    if (rule.action && rule.action !== kind) continue;
    if (rule.field) {
      const value = bound[rule.field];
      if (typeof value !== 'string') continue;
      if (!value.toLowerCase().includes(rule.contains)) continue;
    }
    return rule.keycap;
  }
  return '';
}

function peekLighting() {
  const l = S.doc.lighting;
  return l && typeof l === 'object' ? l : {};
}
function peekZones() {
  const zones = peekLighting().zones;
  return Array.isArray(zones) && zones.length ? zones : ['agent_keys'];
}
function peekLight(name) {
  const states = peekLighting().states;
  const light = states && states[name];
  if (light && typeof light === 'object') return light;
  // Not configured: show the factory colour, which is roughly what the
  // renderer's fallback palette does anyway.
  const preset = (S.schema.presets || []).find((p) => p.state === name);
  return { color: preset ? preset.hex : '#FFFFFF', effect: 'solid',
           brightness: 1, speed: 0, _missing: true };
}
function peekAgents() {
  const a = S.doc[S.schema.agent_section];
  return (a && typeof a === 'object') ? a : {};
}
function peekSlots() {
  const slots = peekAgents().slots;
  const out = Array.isArray(slots) ? slots.slice() : [];
  while (out.length < S.schema.layout.agent_slots) out.push('');
  return out;
}

function lighting() {
  if (!S.doc.lighting || typeof S.doc.lighting !== 'object') S.doc.lighting = {};
  const l = S.doc.lighting;
  if (!Array.isArray(l.zones) || !l.zones.length) l.zones = ['agent_keys'];
  if (!l.states || typeof l.states !== 'object') l.states = {};
  return l;
}
function stateLight(name) {
  const states = lighting().states;
  if (!states[name] || typeof states[name] !== 'object') {
    const seed = peekLight(name);
    states[name] = { color: seed.color, effect: seed.effect,
                     brightness: seed.brightness, speed: seed.speed };
  }
  return states[name];
}
function joystick() {
  if (!S.doc.joystick || typeof S.doc.joystick !== 'object') S.doc.joystick = {};
  return S.doc.joystick;
}
function agents() {
  const key = S.schema.agent_section;
  if (!S.doc[key] || typeof S.doc[key] !== 'object') S.doc[key] = {};
  const a = S.doc[key];
  if (typeof a.policy !== 'string') a.policy = 'recent';
  if (!Array.isArray(a.slots)) a.slots = peekSlots();
  return a;
}

const dirty = () => JSON.stringify(S.doc) !== S.baseline;
const clone = (value) => JSON.parse(JSON.stringify(value));

/* ------------------------------------------------------------ conversions */

function toHex(value) {
  if (typeof value === 'number') {
    return '#' + value.toString(16).toUpperCase().padStart(6, '0');
  }
  if (Array.isArray(value) && value.length === 3) {
    return '#' + value.map((c) => Number(c).toString(16).padStart(2, '0'))
      .join('').toUpperCase();
  }
  if (typeof value === 'string') {
    let t = value.trim().replace(/^#/, '').replace(/^0x/i, '');
    if (t.length === 3) t = t.split('').map((c) => c + c).join('');
    if (/^[0-9a-f]{6}$/i.test(t)) return '#' + t.toUpperCase();
  }
  return '#FFFFFF';
}
function rgbOf(hex) {
  const h = toHex(hex).slice(1);
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16),
          parseInt(h.slice(4, 6), 16)];
}
const rgba = (hex, alpha) => `rgba(${rgbOf(hex).join(',')},${alpha.toFixed(3)})`;

function effectName(value) {
  const effects = S.schema.effects;
  if (typeof value === 'number') {
    const hit = effects.find((e) => e.value === value);
    return hit ? hit.name : 'solid';
  }
  const text = String(value || '').trim().toLowerCase().replace(/[ _]/g, '-');
  const hit = effects.find((e) => e.name === text);
  return hit ? hit.name : 'solid';
}
function effectValue(name) {
  const hit = S.schema.effects.find((e) => e.name === effectName(name));
  return hit ? hit.value : 1;
}
const unit = (value, fallback) => {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : fallback;
};

/* ----------------------------------------------------------------- picker */

/* One searchable dropdown, used everywhere a value comes from a knowable set.
 *
 * The house rule, and it was learned the expensive way: **if the valid values
 * can be enumerated, the user picks from them.** Free text has already cost
 * this project a config silently rejected for saying "app" instead of "name",
 * an owner who could not work out how to open an application, and every
 * mistyped key combo that fails at save time or - worse - at press time.
 *
 * Behaviour, shared by every instance: type to filter from the first
 * keystroke; arrows/Enter/Escape; the current value visible when closed;
 * options that exist but are unavailable shown disabled *with the reason*
 * rather than hidden; and an empty list that explains itself.
 */
function picker(spec) {
  const options = spec.options || [];
  const current = options.find((o) => o.value === spec.value);
  const node = el('div', { class: 'picker' });
  const button = el('button', {
    class: 'picker-value' + (current ? '' : ' is-empty'),
    type: 'button',
    'aria-haspopup': 'listbox',
    'aria-expanded': 'false',
    title: spec.title || '',
  },
    current && current.icon ? current.icon() : null,
    el('span', { class: 'picker-label',
                 text: current ? current.label : (spec.placeholder || 'Choose…') }),
    current && current.hint
      ? el('span', { class: 'picker-hint', text: current.hint }) : null,
    el('span', { class: 'picker-caret', 'aria-hidden': 'true', text: '▾' }));
  node.append(button);

  let pop = null;
  let active = 0;
  let shown = options;

  const close = () => {
    if (!pop) return;
    pop.remove();
    pop = null;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', onOutside, true);
  };
  const onOutside = (e) => { if (!node.contains(e.target)) close(); };

  const pick = (option) => {
    if (option.disabled) return;
    close();
    spec.onpick(option.value, option);
  };

  const paint = (list) => {
    const rows = list.host;
    mount(rows, shown.map((option, index) => el('div', {
      class: 'picker-option' + (index === active ? ' is-active' : '') +
             (option.value === spec.value ? ' is-current' : '') +
             (option.disabled ? ' is-disabled' : ''),
      role: 'option',
      'aria-selected': option.value === spec.value ? 'true' : 'false',
      'aria-disabled': option.disabled ? 'true' : 'false',
      title: option.reason || '',
      onmousedown: (e) => { e.preventDefault(); pick(option); },
      onmousemove: () => {
        if (active === index) return;
        active = index;
        paint(list);
      },
    },
      option.icon ? option.icon() : null,
      el('span', { class: 'picker-option-label', text: option.label }),
      option.hint ? el('span', { class: 'picker-hint', text: option.hint }) : null,
      option.disabled && option.reason
        ? el('span', { class: 'picker-why', text: option.reason }) : null)));
    if (!shown.length) {
      mount(rows, el('p', { class: 'picker-empty',
        text: spec.empty || 'Nothing matches.' }));
    }
    const activeNode = rows.children[active];
    if (activeNode && activeNode.scrollIntoView) {
      activeNode.scrollIntoView({ block: 'nearest' });
    }
  };

  const open = () => {
    if (pop) { close(); return; }
    shown = options;
    active = Math.max(0, options.findIndex((o) => o.value === spec.value));
    const host = el('div', { class: 'picker-list', role: 'listbox' });
    const list = { host };
    const search = el('input', {
      type: 'text', class: 'picker-search', placeholder: spec.search || 'Search…',
      oninput: (e) => {
        const q = e.target.value.trim().toLowerCase();
        shown = !q ? options : options.filter((o) =>
          (o.terms || o.label || '').toLowerCase().includes(q) ||
          String(o.value).toLowerCase().includes(q));
        active = 0;
        paint(list);
      },
      onkeydown: (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          if (!shown.length) return;
          active = (active + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
          paint(list);
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (shown[active]) pick(shown[active]);
        } else if (e.key === 'Escape') {
          e.preventDefault();
          close();
          button.focus();
        }
      },
    });
    pop = el('div', { class: 'picker-pop' + (spec.grid ? ' is-grid' : '') },
      search, host);
    node.append(pop);
    button.setAttribute('aria-expanded', 'true');
    paint(list);
    search.focus();
    document.addEventListener('mousedown', onOutside, true);
  };

  button.addEventListener('click', open);
  return node;
}

/* ------------------------------------------------------------------- chrome */

function toast(message, bad) {
  const node = $('#toast');
  node.textContent = message;
  node.className = 'toast' + (bad ? ' bad' : '');
  node.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { node.hidden = true; }, 3600);
}

function showBanner(text, kind) {
  const node = $('#banner');
  if (!text) { node.hidden = true; return; }
  node.textContent = text;
  node.className = 'banner' + (kind === 'warn' ? ' warn' : '');
  node.hidden = false;
}

/* The last line of defence. If anything at all throws, the user is told - 
 * a page that has quietly stopped working must not look like a working page. */
function fatal(what, err) {
  const node = $('#fatal');
  if (!node) return;
  mount(node,
    el('strong', { text: 'Something in this page broke.' }),
    el('p', { class: 'fatal-why', text: `${what}: ${err && err.message ? err.message : err}` }),
    el('p', { class: 'fatal-why' },
      'Your config on disk has not been touched. Reload the page from the URL ' +
      'FreeMicro printed, and if it happens again please report it with this ' +
      'message.'));
  node.hidden = false;
}

function renderChrome() {
  $('#dirty-dot').hidden = !dirty();
  $('#save').disabled = !dirty() || !S.validation.ok;
  $('#save').title = S.validation.ok
    ? (dirty() ? 'Write these changes to your config file' : 'No changes to save')
    : 'Fix the highlighted problem first: ' + S.validation.error;
  $('#revert').disabled = !dirty();
  const path = $('#config-path');
  if (path) path.textContent = S.paths.save;

  // Save and Revert appear only when there is something to save or revert:
  // two fewer things on screen for the ninety per cent of the time that the
  // page is simply showing you your pad.
  $('#save').hidden = !dirty();
  $('#revert').hidden = !dirty() && !S.undo;
}

/* ------------------------------------------------- why the pad is unusable */

/* This banner is the whole point of BUG 2, so it is deliberately impossible to
 * miss: it sits across the top of the diagram, it is there on page load with no
 * interaction, it says what is wrong *and* what to do, and it clears itself
 * within a few seconds of the user fixing it - no reload required.
 *
 * Editing bindings works perfectly well with no pad attached, so this must not
 * read as "the app is broken": it says which two features are unavailable and
 * leaves everything else alone.
 */
function renderAvailability() {
  const d = S.device;
  renderStatus();
  $('#pad').classList.toggle('is-blocked', !(d.usable || d.unknown));

  // Anything that needs the hardware says so on itself rather than sitting
  // there greyed out with no explanation. Lighting and input are asked about
  // separately: the vendor app being open stops neither, it only means a
  // preview may be overwritten.
  for (const control of document.querySelectorAll('[data-needs-pad]')) {
    const lighting = control.dataset.needsPad === 'lighting';
    const ok = lighting ? lightingOk() : inputOk();
    control.disabled = !ok;
    control.title = ok
      ? (lighting && d.lighting_warning ? d.lighting_warning : '')
      : (d.reason || 'No pad connected.');
  }
  const identify = $('#identify');
  identify.disabled = !inputOk() && !S.capture.on;
  identify.title = inputOk()
    ? 'Press any key on the pad and this page opens that key'
    : (d.unknown ? 'Checking whether the pad is free…'
                 : (d.reason || 'No pad connected.'));
}

/* Reading keys and writing LEDs contend differently - macOS opens this device
 * non-exclusively, so two processes read it happily, and only lighting writes
 * fight. Asking one "is the pad usable" question for both is what disabled
 * identify mode whenever the ChatGPT app was open. */
const inputOk = () => !!S.device.input_ok;
const lightingOk = () => !!S.device.lighting_ok;

/* ---------------------------------------------------------------- the pad */

/* What the Agent Keys are showing right now: the configured look for the state
 * the user has selected, dimmed by its brightness, blanked by effect "off". */
function shownLook() {
  const light = peekLight(S.showState);
  const hex = toHex(light.color);
  const off = effectName(light.effect) === 'off';
  const bright = off ? 0 : unit(light.brightness, 1);
  return { hex, bright, off, light };
}

function capLabel(cell) {
  // A paired cap reads from its first id; the editor keeps the others in step.
  const bound = binding(cell.id);
  if (!bound) return 'unbound';
  return bound.label || bound.action;
}

function keyNode(cell) {
  const bound = binding(cell.id);
  const look = shownLook();
  const classes = ['key', cell.cell];
  if (cell.round) classes.push('round');
  if (S.selected === cell.id) classes.push('sel');
  if (!bound) classes.push('unbound');
  if (mismatched(cell)) classes.push('mismatch');

  let style = `--span:${cell.span || 1}`;
  if (cell.lit && peekZones().includes('agent_keys')) {
    // Frosted cap lit from underneath: tint the face, glow around it.
    style += `;--face:${rgba(look.hex, 0.12 + 0.5 * look.bright)}`;
    style += `;--glow:${rgba(look.hex, 0.55 * look.bright)}`;
    if (look.bright > 0) classes.push('glow');
  } else if (!cell.lit && peekZones().includes('backlight')) {
    // The backlight is one global colour under every keycap at once - there
    // is no per-key colour on these, and pretending otherwise would be a lie.
    style += `;--face:${rgba(look.hex, 0.10 + 0.3 * look.bright)}`;
    classes.push('backlit');
  }
  if (cell.lit) style += `;--dot:${rgba(look.hex, 0.35 + 0.65 * look.bright)}`;

  const ids = cell.ids && cell.ids.length ? cell.ids : [cell.id];
  const cap = capOf(cell.id);
  if (cap && capIsAssumed(cell.id)) classes.push('assumed');
  // The glyph is the picture on the physical cap; the words are what a screen
  // reader, a tooltip and `keys --list` need. Both, always.
  const spoken = `${cap ? cap.id + ' keycap' : cell.keycap} - ` +
                 `${ids.join(' and ')} - ${capLabel(cell)}`;
  return el('button', {
    class: classes.join(' '),
    type: 'button',
    'data-key': cell.id,
    'aria-pressed': S.selected === cell.id ? 'true' : 'false',
    'aria-label': spoken,
    draggable: 'true',
    style,
    title: spoken + (capIsAssumed(cell.id) && cap
      ? ' (assuming the cap it shipped with - click to set yours)'
      : '') + '. Click to edit.',
  },
    cap ? glyphNode(cap) : null,
    el('span', { class: 'key-cap', text: cap ? cap.id : cell.keycap }),
    el('span', { class: 'key-label', text: capLabel(cell) }),
    el('span', { class: 'key-id', text: ids.join('+') }),
    cell.slot !== undefined && el('span', {
      class: 'slot-tag', text: `slot ${cell.slot + 1}`,
    }),
    mismatched(cell) && el('span', { class: 'key-warn', text: '!',
                                     title: 'The two halves disagree' }));
}

/* A cap over two switches where the second half still does something. Worth
 * shouting about: one press fires both ids, so the key does two things. */
function mismatched(cell) {
  const ids = cell.ids || [];
  if (ids.length < 2) return false;
  return ids.slice(1).some((id) => {
    const bound = bindings()[id];
    return !!bound && bound.action !== 'none';
  });
}

function roundControl(cell) {
  const positions = { n: 'pos-n', e: 'pos-e', s: 'pos-s', w: 'pos-w', c: 'pos-c' };
  return el('div', {
    class: 'round-ctl ' + cell.cell,
    style: `--span:${cell.span || 1}`,
    title: cell.cell === 'dial'
      ? 'Rotary dial - turn it either way, or press it'
      : 'Analogue thumbstick - flick it in a direction',
  },
    cell.inputs.map((input) => el('button', {
      class: 'nub ' + (positions[input.position] || 'pos-c') +
             (S.selected === input.id ? ' sel' : ''),
      type: 'button',
      'data-key': input.id,
      'aria-pressed': S.selected === input.id ? 'true' : 'false',
      title: `${input.id} - ${input.keycap}. Click to edit what it does.`,
    }, input.keycap)));
}

/* The haptic Bluetooth-profile pad. Drawn because it is on the object; not
 * offered an editor because the firmware owns it and never tells us about it.
 * Leaving it off the diagram is how someone concludes the UI is broken. */
function controlNode(cell) {
  return el('button', {
    class: 'key control round' + (S.control === cell.control ? ' sel' : ''),
    type: 'button',
    'data-control': cell.control,
    style: `--span:${cell.span || 1}`,
    title: cell.label + ' - firmware-owned, FreeMicro cannot bind it. ' +
           'Click for what it does.',
  },
    el('span', { class: 'leds', 'aria-hidden': 'true' },
      [0, 1, 2].map(() => el('i', {}))),
    el('span', { class: 'key-cap', text: cell.keycap }),
    el('span', { class: 'key-id', text: 'not bindable' }));
}

function renderPad() {
  const layout = S.schema.layout;
  const grid = el('div', { class: 'pad-grid' },
    layout.cells.map((cell) => el('div', {
      class: 'cell', style: `--span:${cell.span || 1}`,
    },
      cell.inputs ? roundControl(cell)
        : (cell.bindable === false ? controlNode(cell) : keyNode(cell)))));

  const pad = $('#pad');
  const look = shownLook();
  pad.className = 'pad' +
    (S.device.usable || S.device.unknown ? '' : ' is-blocked');
  pad.style.removeProperty('--under');
  if (peekZones().includes('underglow')) {
    pad.classList.add('underglow');
    pad.style.setProperty('--under', rgba(look.hex, 0.5 * look.bright));
  }
  pad.style.removeProperty('--wash');
  if (peekZones().includes('backlight')) {
    pad.classList.add('backlit');
    pad.style.setProperty('--wash', rgba(look.hex, 0.7 * look.bright));
  }
  mount(pad, grid);
  renderChips();
}

/* Five swatches under the pad. Click one to see the pad in that state; click
 * the one already showing to edit its colour. That is the whole lighting UI on
 * the front page - the five-row table it replaces is under Advanced. */
function renderChips() {
  const host = $('#state-chips');
  mount(host, S.schema.states.map((name) => {
    const hex = toHex(peekLight(name).color);
    const showing = S.showState === name;
    return el('button', {
      class: 'chip' + (showing ? ' is-on' : ''),
      type: 'button',
      title: showing
        ? `Edit the colour for “${name}”`
        : `Show the pad as it looks when a project is ${name}`,
      onclick: () => {
        if (showing) { openColourModal(name); return; }
        S.showState = name;
        renderPad();
      },
    }, el('i', { style: `--sw:${hex}` }), name);
  }));
}

/* --------------------------------------------------------- pad hit testing */

/* One delegated listener for the whole diagram, attached once in wire() and
 * never removed. Two things this buys, both of which were previously broken:
 *
 *   * Redrawing the pad cannot orphan a handler, and a render that throws
 *     halfway cannot leave clickable-looking keys that are not clickable.
 *   * There are **no dead zones**. The dial and the stick are round controls
 *     whose small nubs used to be the only live targets - a click on the rest
 *     of the dial (over half its area) hit the container and did nothing at
 *     all. Now a click anywhere inside a control resolves to its nearest
 *     input, and a click in the gutter beside a round cap resolves to that
 *     cap. If you can see it, you can click it.
 */
function targetFor(event) {
  const keyed = event.target.closest('[data-key]');
  if (keyed) return { key: keyed.dataset.key };
  const control = event.target.closest('[data-control]');
  if (control) return { control: control.dataset.control };

  const box = event.target.closest('.round-ctl, .cell');
  if (!box) return null;
  const nubs = Array.from(box.querySelectorAll('[data-key]'));
  if (!nubs.length) {
    const dead = box.querySelector('[data-control]');
    return dead ? { control: dead.dataset.control } : null;
  }
  let best = null;
  let bestDistance = Infinity;
  for (const nub of nubs) {
    const rect = nub.getBoundingClientRect();
    const dx = event.clientX - (rect.left + rect.width / 2);
    const dy = event.clientY - (rect.top + rect.height / 2);
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) { bestDistance = distance; best = nub; }
  }
  return best ? { key: best.dataset.key } : null;
}

function onPadClick(event) {
  const hit = targetFor(event);
  if (!hit) return;
  if (hit.control) selectControl(hit.control);
  else select(hit.key);
}

function select(id) {
  S.selected = id;
  S.control = null;
  renderPad();
  openKeyModal(id);
}

function selectControl(name) {
  S.control = name;
  S.selected = null;
  renderPad();
  openModal('Bluetooth profile switch', controlPanel(name));
}

function flash(id) {
  const node = document.querySelector(`[data-key="${CSS.escape(id)}"]`);
  if (!node) return;
  node.classList.remove('hit');
  void node.offsetWidth;
  node.classList.add('hit');
}

function allInputs() {
  const out = [];
  for (const cell of S.schema.layout.cells) {
    if (cell.inputs) out.push(...cell.inputs);
    else if (cell.bindable !== false) out.push(cell);
  }
  return out;
}

function inputEntry(id) {
  return allInputs().find((e) => e.id === id)
      || { id, ids: [id], keycap: id, note: '' };
}

/* ------------------------------------------------------------ binding tab */

/* Writing a binding always goes through here, and it always settles *every* id
 * the cap fires - because the wide cap fires two.
 *
 * The pad reports ACT10 and ACT11 on a single press of the double-width cap.
 * The factory addresses the pair as ACT10_ACT11 and discards the second half,
 * and so do we: the action goes on the first id and the second is bound to
 * `none`, explicitly. Binding both fires the key twice per press - on a
 * push-to-talk hold that means the combo goes down and straight back up under
 * your finger, which is exactly the sort of "it just doesn't work" this page
 * exists to stop. */
function writeBinding(id, value) {
  const map = bindings();
  const group = pairOf(id);
  const primary = group[0];
  if (value === null) {
    for (const member of group) delete map[member];
    return;
  }
  map[primary] = value;
  for (const member of group.slice(1)) {
    map[member] = {
      action: 'none',
      label: 'second half of the wide cap',
      comment: 'Silenced on purpose: this cap spans two switches and the pad ' +
               'reports both, so acting on both would fire the key twice.',
    };
  }
}

function fieldControl(spec, bound, id) {
  const name = spec.name;
  const set = (value) => {
    if (value === '' || value === undefined) delete bound[name];
    else bound[name] = value;
    syncPair(id);
    changed();
  };
  if (spec.widget === 'boolean') {
    return el('label', { class: 'check' },
      el('input', {
        type: 'checkbox', checked: !!bound[name],
        onchange: (e) => { bound[name] = e.target.checked; syncPair(id); changed(); },
      }),
      spec.help || name);
  }
  if (spec.widget === 'app') return appField(spec, bound, id, set);
  if (spec.widget === 'textarea') {
    return el('textarea', {
      placeholder: spec.placeholder || '',
      oninput: (e) => set(e.target.value),
    }, bound[name] === undefined ? '' : String(bound[name]));
  }
  if (spec.widget === 'number') {
    return el('input', {
      type: 'number', step: spec.step || 1,
      value: bound[name] === undefined ? '' : bound[name],
      oninput: (e) => set(e.target.value === '' ? '' : Number(e.target.value)),
    });
  }
  if (spec.widget === 'choice') {
    return el('select', { onchange: (e) => set(e.target.value) },
      spec.choices.map((c) => el('option', {
        value: c, selected: String(bound[name] === undefined ? '' : bound[name]) === c,
      }, c || 'none')));
  }
  if (spec.widget === 'combo') return comboField(spec, bound, set);
  return el('input', {
    type: 'text',
    placeholder: spec.placeholder || '',
    value: bound[name] === undefined ? '' : String(bound[name]),
    oninput: (e) => set(e.target.value),
  });
}

/* The app picker. "Open an app" used to be a free-text box whose one job was to
 * hold an exact application name - get it wrong and the key silently does
 * nothing, which is the worst failure this config has. So: a searchable list of
 * what is actually installed, with a typed name still allowed as an escape
 * hatch and checked against the disk as you type. */
function appField(spec, bound, id, set) {
  const current = bound[spec.name] === undefined ? '' : String(bound[spec.name]);
  const apps = S.apps || [];
  const known = apps.some((a) => a.name.toLowerCase() === current.toLowerCase());
  const options = apps.map((a) => ({
    value: a.name, label: a.name, hint: a.where.replace('/System', ''),
    terms: a.name + ' ' + a.where,
  }));
  if (current && !known) {
    // A name that is not installed is *shown*, disabled, with the reason - 
    // hiding it would leave the user staring at an empty box wondering where
    // their setting went.
    options.unshift({
      value: current, label: current, disabled: true,
      reason: 'not installed on this Mac', terms: current,
    });
  }
  const verdict = el('p', { class: 'fieldnote' });
  const typed = el('input', {
    type: 'text', class: 'mono', autocomplete: 'off',
    placeholder: spec.placeholder || 'Terminal',
    value: current,
    oninput: (e) => { set(e.target.value); judgeApp(e.target.value, verdict, set); },
  });
  judgeApp(current, verdict, set);
  return el('div', {},
    picker({
      value: current,
      options,
      placeholder: S.apps === null ? 'Loading installed apps…' : 'Choose an app',
      search: 'Search installed apps',
      empty: 'No app here matches that. Type the name below instead.',
      onpick: (value) => { set(value); reopenIfKeyModal(); },
    }),
    verdict,
    el('details', { class: 'escape' },
      el('summary', { text: 'Type a name instead' }),
      typed,
      el('p', { class: 'hint' },
        'For an app that is not in /Applications - the exact name macOS knows ' +
        'it by, without “.app”.')));
}

function judgeApp(name, node, set) {
  mount(node);
  node.className = 'fieldnote';
  if (S.apps === null || !String(name || '').trim()) return;
  const wanted = String(name).trim().toLowerCase().replace(/\.app$/, '');
  const exact = S.apps.find((a) => a.name.toLowerCase() === wanted);
  if (exact) {
    node.className = 'fieldnote ok';
    mount(node, el('span', { text: `✓ ${exact.name} - ${exact.where}` }));
    return;
  }
  const near = S.apps.filter((a) => a.name.toLowerCase().includes(wanted)).slice(0, 5);
  node.className = 'fieldnote bad';
  mount(node,
    el('span', { text: `No app called “${name}” is installed here. The key ` +
                       'would do nothing when pressed.' }),
    near.length
      ? el('span', { class: 'did-you-mean' }, 'Did you mean: ',
          near.map((a) => el('button', {
            class: 'btn tiny', type: 'button',
            onclick: () => { set(a.name); reopenIfKeyModal(); },
          }, a.name)))
      : null);
}

/* A keystroke field. Three ways to set it, in the order they should be
 * reached for: press the actual keys, pick the base key from the list the
 * parser accepts, or type it. Never a bare text box - a mistyped combo is
 * refused at save time at best and dead at press time at worst. */

/* A shortcut field you PRESS.
 *
 * Typing "ctrl+cmd+o" is the fallback, not the path. This control is already
 * listening when it appears: press the keys, see them, confirm. Two rules it
 * must never break, both learned expensively:
 *
 *   * **Capture on commit.** The combo is held aside until "Use this". An
 *     earlier version wrote the first key it saw straight into the document,
 *     which is how a user's working dictation shortcut silently became a
 *     combo that appears nowhere in this project's source.
 *   * **Escape cancels, and is never captured.** Nor is Tab, nor ⌘W. A
 *     capture field that traps the user is worse than a text box.
 */
function comboField(spec, bound, set) {
  const value = bound[spec.name] === undefined ? '' : String(bound[spec.name]);
  const MODS = [['cmd', '⌘'], ['ctrl', '⌃'], ['option', '⌥'], ['shift', '⇧']];

  const shown = el('span', { class: 'combo-shown', text: value || '' });
  const hint = el('p', { class: 'fieldnote' });
  const accept = el('button', {
    class: 'btn tiny primary', type: 'button', hidden: true,
  }, 'Use this');
  const box = el('div', {
    class: 'capture', tabindex: '0',
    role: 'textbox',
    'aria-label': 'Press the keys you want, then confirm',
  });

  let pending = '';
  const paint = () => {
    const showing = pending || value;
    box.classList.toggle('is-armed', S.comboCapture === box);
    box.classList.toggle('is-empty', !showing);
    shown.textContent = showing || (S.comboCapture === box
      ? 'Press the keys you want…'
      : 'Click here, then press the keys you want…');
    accept.hidden = !pending;
    const problem = pending ? dictationProblem(pending) : '';
    hint.className = 'fieldnote' + (problem ? ' bad' : '');
    hint.textContent = problem || (pending ? 'Press “Use this” to keep it.' : '');
  };

  const onKey = (e) => {
    // Typing in a field is not a shortcut. An earlier version captured on a
    // window listener regardless of where the keystroke was going, so a search
    // box or a text field could quietly feed the recorder - which is how a
    // working dictation combo turned into one that appears nowhere in this
    // project's source. Stand down instead of swallowing.
    const into = e.target;
    if (into && into !== box && (into.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(into.tagName))) {
      stopComboCapture();
      return;
    }
    // Never swallow the ways out.
    if (e.key === 'Escape' || e.key === 'Tab' ||
        (e.metaKey && (e.key === 'w' || e.key === 'r' || e.key === 'q'))) {
      if (e.key === 'Escape') {
        // Cancel the capture, and *only* the capture: this listener runs in
        // the capture phase, so stopping propagation here keeps the same
        // keystroke from also closing the modal behind it.
        e.preventDefault();
        e.stopPropagation();
        stopComboCapture();
        paint();
      }
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    const parts = [];
    if (e.metaKey) parts.push('cmd');
    if (e.ctrlKey) parts.push('ctrl');
    if (e.altKey) parts.push('option');
    if (e.shiftKey) parts.push('shift');
    const base = comboBase(e);
    if (!base) { pending = parts.join('+'); paint(); return; }
    parts.push(base);
    pending = parts.join('+');   // held aside - nothing is written yet
    paint();
  };

  const arm = () => {
    stopComboCapture();
    S.comboCapture = box;
    S.comboCaptureStop = () => {
      window.removeEventListener('keydown', onKey, true);
      S.comboCapture = null;
      S.comboCaptureStop = null;
      paint();
    };
    window.addEventListener('keydown', onKey, true);
    paint();
  };

  accept.addEventListener('click', () => {
    if (!pending) return;
    set(pending);
    pending = '';
    stopComboCapture();
    renderPad();
    reopenIfKeyModal();
  });
  box.addEventListener('click', arm);
  box.addEventListener('focus', arm);

  const typed = el('input', {
    type: 'text', class: 'mono', placeholder: 'ctrl+cmd+o', value,
    oninput: (e) => set(e.target.value),
  });

  box.append(shown);
  // Armed the moment it is built, not on a timer and not on a click: a field
  // you must first click is a field people type into instead.
  //
  // Only the FIRST such field in a modal arms itself, though. The Advanced
  // disclosure renders the same parameter again, and a hidden field quietly
  // stealing the keyboard from the visible one is precisely the kind of "why
  // is nothing happening" this page keeps being punished for.
  if (!S.captureClaimed) {
    S.captureClaimed = true;
    arm();
  }
  return el('div', { class: 'combo' },
    el('div', { class: 'row' }, box, accept),
    hint,
    el('div', { class: 'mods hint-mods' },
      MODS.map(([mod, sign]) => el('span', { class: 'mod-legend', text: sign })),
      el('span', { class: 'hint', text: 'modifiers show as you hold them' })),
    el('details', { class: 'escape' },
      el('summary', { text: 'Type it instead' }),
      typed,
      el('p', { class: 'hint' },
        'For a combo you cannot press here - over a remote session, or with ' +
        'an assistive device.')));
}

function stopComboCapture() {
  if (S.comboCaptureStop) S.comboCaptureStop();
}

function reopenIfKeyModal() {
  if (modalOpen() && S.selected) reopenKey(S.selected);
}

/* The Wispr Flow limit, checked while you press rather than at save. */
function dictationProblem(combo) {
  const choices = S.schema.dictation || [];
  const chosen = choices.find((c) => c.id === S.dictation);
  if (!chosen) return '';
  return comboProblem(chosen, combo);
}

function comboBase(event) {
  const key = event.key;
  if (['Meta', 'Control', 'Alt', 'Shift'].includes(key)) return '';
  const named = {
    ' ': 'space', 'Escape': 'escape', 'Enter': 'return', 'Tab': 'tab',
    'Backspace': 'delete', 'ArrowUp': 'up', 'ArrowDown': 'down',
    'ArrowLeft': 'left', 'ArrowRight': 'right',
  };
  if (named[key]) return named[key];
  if (/^F\d{1,2}$/.test(key)) return key.toLowerCase();
  if (key.length === 1) return key.toLowerCase();
  return '';
}

/* After any edit to a paired cap, make sure the other halves stay silent. */
function syncPair(id) {
  const group = pairOf(id);
  if (group.length < 2) return;
  const source = binding(group[0]);
  if (source === null) return;
  writeBinding(group[0], source);
}


/* ------------------------------------------------------- the key editor */

/* Outcomes, not action kinds.
 *
 * `focus_session`, `hold`, `applescript` are our vocabulary, and making the
 * user learn it was the single biggest source of "a million ways to do
 * everything". Each entry below is one sentence about what the key will DO,
 * mapped to the kind and the one field that answer needs. Everything else - 
 * shell, applescript, mouse, raw parameters - is still here, one disclosure
 * away, because burying capability is not the same as removing it. */
const OUTCOMES = [
  {
    id: 'focus', kind: 'focus_session', field: null,
    label: 'Jump to this project’s terminal',
    hint: 'The key lights up with what that project is doing.',
  },
  {
    id: 'type', kind: 'text', field: 'text',
    label: 'Type something',
    hint: 'A prompt or a command, typed into whatever is in front of you.',
  },
  {
    id: 'shortcut', kind: 'key', field: 'key',
    label: 'Press a shortcut',
    hint: 'Send a keystroke, like ⌘⇧K.',
  },
  {
    id: 'dictate', kind: 'hold', field: 'key',
    label: 'Hold to talk',
    hint: 'Holds a dictation shortcut down while you hold the key.',
  },
  {
    id: 'app', kind: 'app', field: 'name',
    label: 'Open an app',
    hint: 'Bring it to the front, and cycle its windows if it is already there.',
  },
  {
    id: 'nothing', kind: 'none', field: null,
    label: 'Do nothing',
    hint: 'Leave this key unbound.',
  },
];

const outcomeFor = (bound) => {
  if (!bound) return null;
  return OUTCOMES.find((o) => o.kind === bound.action) || null;
};

function openKeyModal(id) {
  S.selected = id;
  S.control = null;
  renderPad();
  const entry = inputEntry(id);
  const cap = capOf(id);
  openModal(cap ? `${cap.id} key` : entry.keycap, keyModalBody(id));
}

function keyModalBody(id) {
  // One capture field per modal gets the keyboard; see comboField.
  S.captureClaimed = false;
  const bound = binding(id);
  const chosen = outcomeFor(bound);
  const group = pairOf(id);
  const parts = [];

  if (group.length > 1) {
    parts.push(el('p', { class: 'modal-note' },
      'One wide cap over two switches. What you set here goes on ' + group[0] +
      '; ' + group[1] + ' stays silent so the key cannot fire twice.'));
  }

  // 1. What it does.
  parts.push(el('div', { class: 'outcomes' },
    OUTCOMES.map((outcome) => el('button', {
      class: 'outcome' + (chosen && chosen.id === outcome.id ? ' is-on' : ''),
      type: 'button',
      onclick: () => { chooseOutcome(id, outcome); },
    },
      el('strong', { text: outcome.label }),
      el('span', { text: outcome.hint })))));

  // 2. The one field that answer needs.
  if (chosen && chosen.field && bound) {
    const spec = (S.schema.actions.find((a) => a.kind === chosen.kind).fields
      .find((f) => f.name === chosen.field)) || { name: chosen.field, widget: 'text' };
    parts.push(el('div', { class: 'field' },
      el('label', { text: fieldLabel(chosen) }),
      fieldControl(spec, bound, id)));
    if (chosen.kind === 'text') {
      parts.push(el('label', { class: 'check' },
        el('input', {
          type: 'checkbox', checked: bound.submit === true,
          onchange: (e) => {
            if (e.target.checked) bound.submit = true; else delete bound.submit;
            syncPair(id);
            changed();
          },
        }),
        'Press Return afterwards'));
    }
    if (chosen.kind === 'hold') {
      parts.push(el('p', { class: 'hint' },
        'Set this same shortcut in your dictation app as its push-to-talk ' +
        '(hold) shortcut - not its toggle.'));
    }
  }

  // 2b. What the pad shows while it is held. On the front of the modal, not
  //     behind Advanced: "the pad changes colour while the mic is live" is a
  //     thing people come here to turn on, and it is one switch.
  parts.push(activityLightField(id, bound));

  // 3. Which cap is on it. Always on screen, never behind a disclosure: the
  //    cap and the outcome are the two things people open this modal to
  //    change, so both are one click away from the diagram.
  parts.push(keycapField(id, bound));

  // 4. Everything else, one disclosure away.
  parts.push(el('details', { class: 'escape' },
    el('summary', { text: 'Advanced' }),
    advancedKeyBody(id, bound)));

  parts.push(el('div', { class: 'modal-actions' },
    el('button', {
      class: 'btn primary', type: 'button', onclick: () => closeModal(),
    }, 'Done')));
  return parts;
}

/* ------------------------------------- the light while a key is held down */

/* Human names for the three lighting surfaces. The config's own words are
 * fine in a config; "the strip under the pad" is what somebody choosing needs. */
const ZONE_LABELS = {
  underglow: 'Underglow (the strip under the pad)',
  backlight: 'Backlight (under the keycaps)',
  agent_keys: 'The six Agent Keys',
};

/* A binding's `light` is a LAYER over the agent-state colours: it claims the
 * zones it names for exactly as long as the key is down and gives them straight
 * back, so the Agent Keys go on carrying six projects while the underglow says
 * you are talking. That is why the default is the underglow and why turning
 * this on never costs you the status display.
 *
 * The honesty rule this control exists to keep: FreeMicro can only see a key
 * that is HELD. A toggle shortcut starts dictation on a tap and stops it on
 * another tap that looks identical, so a light on one would go out while the
 * mic was still live. The sentence under the switch therefore changes with the
 * action kind, instead of being one reassuring line that is true for only half
 * of them. */
function activityLightField(id, bound) {
  if (!bound || bound.action === 'none') return null;
  const meta = (S.schema && S.schema.activity_light) || {};
  const fallback = { color: '#2E8B57', effect: 'snake', speed: 0.4,
                     brightness: 1, zones: ['underglow'] };
  const tracked = (meta.tracked_kinds || []).includes(bound.action);
  const light = (bound.light && typeof bound.light === 'object')
    ? bound.light : null;
  const touch = () => { syncPair(id); changed(); renderPad(); };

  const rows = [
    el('h3', { text: 'While this key is held' }),
    toggle(!!light, 'Change the pad’s colour', (want) => {
      if (want) bound.light = JSON.parse(JSON.stringify(meta.default || fallback));
      else delete bound.light;
      touch();
      reopenKey(id);
    }),
  ];

  if (!light) {
    rows.push(el('p', { class: 'hint' }, tracked
      ? 'Off. The pad keeps showing your projects the whole time.'
      : 'Off. See the note below before turning it on for dictation.'));
    if (!tracked) rows.push(untrackedWarning(bound));
    return el('div', { class: 'field' }, rows);
  }

  rows.push(el('div', { class: 'row' },
    el('input', {
      type: 'color', value: toHex(light.color || fallback.color),
      oninput: (e) => { light.color = e.target.value.toUpperCase(); touch(); },
    }),
    el('span', { class: 'hint' },
      'The vendor’s own colour while it is listening to you. Not red - red ' +
      'is “error”, and one colour cannot mean two things.')));

  const zones = Array.isArray(light.zones) ? light.zones : ['underglow'];
  rows.push(el('div', { class: 'field' },
    el('span', { class: 'field-label', text: 'Which lights' }),
    (S.schema.zones || []).map((zone) => el('label', { class: 'check' },
      el('input', {
        type: 'checkbox', checked: zones.includes(zone),
        onchange: (e) => {
          const next = zones.filter((z) => z !== zone);
          if (e.target.checked) next.push(zone);
          // Never none: a light with nowhere to show is a setting that does
          // nothing, and the config layer would refuse it anyway.
          light.zones = next.length ? next : [zone];
          touch();
          reopenKey(id);
        },
      }),
      ZONE_LABELS[zone] || zone))));

  rows.push(el('p', { class: 'hint' }, tracked
    ? 'On from the moment the key goes down until it comes back up. If that ' +
      'key-up is never reported - a Bluetooth drop mid-hold - the light is ' +
      'taken down after ' + (Number(light.timeout_seconds) ||
        meta.timeout_default || 120) + 's from the clock, and the pad ' +
      'disconnecting takes it down at once. It never sticks.'
    : ''));
  if (!tracked) rows.push(untrackedWarning(bound));

  rows.push(el('details', { class: 'escape' },
    el('summary', { text: 'Advanced' }),
    el('div', { class: 'field' },
      el('span', { class: 'field-label', text: 'Effect' }),
      picker({
        value: effectName(light.effect),
        options: S.schema.effects.map((eff) => ({
          value: eff.name, label: eff.name, hint: EFFECT_HELP[eff.name] || '',
          terms: eff.name + ' ' + (EFFECT_HELP[eff.name] || ''),
        })),
        search: 'Search effects',
        onpick: (value) => { light.effect = value; touch(); reopenKey(id); },
      })),
    slider('Speed', unit(light.speed, 0), (v) => { light.speed = v; touch(); },
      animated(light) ? '' :
        `“${effectName(light.effect)}” does not animate, so speed does nothing.`),
    slider('Brightness', unit(light.brightness, 1),
      (v) => { light.brightness = v; touch(); }),
    el('div', { class: 'field' },
      el('span', { class: 'field-label', text: 'Give up after (seconds)' }),
      el('input', {
        type: 'number', min: '1', max: String(meta.timeout_max || 600),
        value: String(Number(light.timeout_seconds) ||
                      meta.timeout_default || 120),
        oninput: (e) => {
          const next = Number(e.target.value);
          if (next > 0) { light.timeout_seconds = next; touch(); }
        },
      }),
      el('p', { class: 'hint' },
        'There is no “never”, on purpose: a release can be lost, and a light ' +
        'with nothing to end it would claim the key was still down until you ' +
        'restarted FreeMicro.'))));

  return el('div', { class: 'field' }, rows);
}

function untrackedWarning(bound) {
  return el('p', { class: 'hint' },
    '“' + bound.action + '” fires and returns, so this lasts about as long ' +
    'as a tap. FreeMicro sees the press that starts a toggle and never ' +
    'learns that it stopped, so it will not pretend to: for dictation, ' +
    'choose “Hold to talk” above and set your dictation app’s push-to-talk ' +
    '(hold) shortcut to the same combo.');
}

const fieldLabel = (outcome) => ({
  text: 'What should it type?',
  key: 'Which shortcut?',
  name: 'Which app?',
}[outcome.field] || outcome.field);

function chooseOutcome(id, outcome) {
  if (outcome.kind === 'none') writeBinding(id, { action: 'none' });
  else setKindQuiet(id, outcome.kind);
  changed();
  renderPad();
  openModal($('#modal-title').textContent, keyModalBody(id));
}

/* The full editor: every action kind, every parameter, and the move/swap
 * control. Reached only by opening Advanced inside a key's modal. */
function advancedKeyBody(id, bound) {
  const kinds = S.schema.actions;
  const parts = [
    el('div', { class: 'field' },
      el('label', { text: 'Action kind (the config’s own word for it)' }),
      picker({
        value: bound ? bound.action : '',
        options: [{ value: '', label: 'not bound' }].concat(
          kinds.map((a) => ({ value: a.kind, label: a.kind, hint: a.summary,
                              terms: a.kind + ' ' + a.summary }))),
        search: 'Search actions',
        onpick: (value) => { setKind(id, value); reopenKey(id); },
      })),
  ];
  if (bound) {
    const spec = kinds.find((a) => a.kind === bound.action);
    if (spec) {
      for (const field of spec.fields) {
        parts.push(el('div', { class: 'field' },
          el('span', { class: 'field-label', text: field.name }),
          fieldControl(field, bound, id)));
      }
    }
    parts.push(el('div', { class: 'field' },
      el('label', { text: 'Label' }),
      el('input', {
        type: 'text', value: bound.label || '', placeholder: id,
        oninput: (e) => {
          if (e.target.value) bound.label = e.target.value;
          else delete bound.label;
          syncPair(id);
          changed();
          renderPad();
        },
      })));
  }
  parts.push(moveField(id));
  parts.push(el('p', { class: 'hint', text: 'Input id: ' + pairOf(id).join(' + ') }));
  return parts;
}

/* The keyboard-accessible half of drag-to-swap. Same operation, no pointer. */
function moveField(id) {
  const others = allInputs().filter((entry) => entry.id !== id);
  return el('div', { class: 'field' },
    el('label', { text: 'Swap this key with…' }),
    picker({
      value: '',
      options: others.map((entry) => {
        const cap = capOf(entry.id);
        return {
          value: entry.id,
          label: cap ? cap.id : entry.keycap,
          hint: describeBinding(binding(entry.id)),
          terms: entry.id + ' ' + entry.keycap + ' ' +
                 describeBinding(binding(entry.id)),
        };
      }),
      placeholder: 'Choose a key to swap with',
      search: 'Search keys',
      onpick: (other) => { swapKeys(id, other); reopenKey(id); },
    }));
}

function reopenKey(id) {
  renderPad();
  openModal($('#modal-title').textContent, keyModalBody(id));
}

/* Like setKind, but does not redraw a panel that no longer exists. */
function setKindQuiet(id, kind) {
  const previous = binding(id) || {};
  const spec = S.schema.actions.find((a) => a.kind === kind);
  const next = { action: kind };
  for (const field of spec.fields) {
    if (previous[field.name] !== undefined) next[field.name] = previous[field.name];
  }
  if (previous.label) next.label = previous.label;
  if (previous.comment) next.comment = previous.comment;
  if (previous.light) next.light = previous.light;
  writeBinding(id, next);
}

/* Added and removed *in place*, never by re-rendering the panel: this fires
 * while you are typing, and a redraw would take the caret with it. */
function paintValidation() {
  const host = $('#modal-body');
  if (!host) return;
  const existing = host.querySelector('.field-error');
  if (S.validation.ok || !S.selected || !modalOpen()) {
    if (existing) existing.remove();
    return;
  }
  const node = existing || el('div', { class: 'field-error' },
    el('strong', { text: 'This cannot be saved yet:' }), el('span', {}));
  node.lastChild.textContent = ' ' + S.validation.error;
  if (!existing) host.append(node);
}

/* The keycap grid: all 37 caps, on screen, one click each.
 *
 * This used to be a dropdown. The owner's verdict was "we can't click a key and
 * immediately replace it with what's available", and he was right: swapping a
 * cap is one of the two things people come to this modal for, and it was three
 * clicks and a popover away. Both of those things - what the key DOES and which
 * cap is ON it - are now on screen the moment the modal opens, and each is one
 * click to change.
 *
 * The grid repaints ITSELF rather than rebuilding the modal, so the search box
 * keeps its text and its caret, and a shortcut capture elsewhere in the modal
 * is not torn down under the user's fingers.
 */
function keycapField(id, bound) {
  const wide = pairOf(id).length > 1;
  const all = S.schema.keycaps || [];
  const fits = (c) => !wide || c.size === 'double';

  const cells = el('div', {
    class: 'capgrid-cells', role: 'listbox', 'aria-label': 'Keycap catalogue',
  });
  const foot = el('div', { class: 'capgrid-foot' });
  let query = '';

  const search = el('input', {
    type: 'search', class: 'capgrid-search',
    placeholder: `Search ${all.length} keycaps`,
    'aria-label': 'Search keycaps',
    // A shortcut capture arms itself on a window keydown listener, so it would
    // otherwise eat every letter typed here. Typing in a field is exactly the
    // moment that capture must stand down - that was the data-loss bug.
    onfocus: () => stopComboCapture(),
    oninput: (e) => { query = e.target.value; paint(); },
  });

  const choose = (capId) => {
    setCap(id, capId);
    changed();
    renderPad();
    paint();
  };

  function paint() {
    const cap = capOf(id);
    const assumed = capIsAssumed(id);
    const q = query.trim().toLowerCase();
    const shown = !q ? all : all.filter((c) =>
      (c.terms || '').toLowerCase().includes(q) ||
      c.id.toLowerCase().includes(q) ||
      (c.label || '').toLowerCase().includes(q));

    mount(cells, shown.length
      ? shown.map((c) => {
          const ok = fits(c);
          const here = !!cap && cap.id === c.id;
          const on = here && !assumed;
          return el('button', {
            class: 'capcell' + (on ? ' is-on' : '') +
                   (here && assumed ? ' is-assumed' : '') +
                   (ok ? '' : ' is-off'),
            type: 'button',
            role: 'option',
            'aria-selected': on ? 'true' : 'false',
            disabled: !ok,
            title: !ok ? 'Only a double-width cap fits this slot.'
              : (here && assumed
                  ? `${c.id} - what this position shipped with. Click to ` +
                    'confirm it is the one you fitted.'
                  : `${c.id} - ${c.label}` +
                    (c.factory ? `. On a stock pad: ${c.factory}.` : '')),
            onclick: () => choose(c.id),
          },
            glyphNode(c),
            el('span', { class: 'capcell-id', text: c.id }));
        })
      : el('p', { class: 'picker-empty',
                  text: `No keycap matches “${query.trim()}”.` }));

    const suggested = suggestCap(bound);
    const capBinding = cap ? (S.schema.keycap_bindings || {})[cap.id] : null;
    const bindingDiffers =
      !!capBinding && (!bound || bound.action !== capBinding.action);

    mount(foot,
      el('p', { class: 'hint', text: assumed
        ? 'Nothing chosen yet, so the diagram is assuming the cap this ' +
          'position shipped with. Click the one you actually fitted.'
        : (cap ? `Fitted: ${cap.id}. On a stock pad this cap runs ` +
                 `${cap.factory || 'nothing'}.` : '') }),
      !assumed && cap
        ? el('button', {
            class: 'btn tiny ghost', type: 'button',
            title: 'Forget which cap is on this key and go back to assuming ' +
                   'the one it shipped with',
            onclick: () => choose(''),
          }, 'Forget this one')
        : null,
      suggested && (!cap || cap.id !== suggested)
        ? el('div', { class: 'suggest' },
            glyphNode(capById(suggested) || suggested),
            el('span', { text: `The ${suggested} cap suits this binding.` }),
            el('button', {
              class: 'btn tiny', type: 'button',
              onclick: () => choose(suggested),
            }, 'Use it'))
        : null,
      bindingDiffers
        ? el('div', { class: 'suggest' },
            el('span', { text: `${capBinding.why} Bind this key that way?` }),
            el('button', {
              class: 'btn tiny', type: 'button',
              onclick: () => {
                const next = Object.assign({}, capBinding);
                delete next.why;
                writeBinding(id, next);
                changed();
                renderPad();
                // This one changes what the key DOES, so the outcome buttons
                // and the field above them have to be redrawn too.
                reopenIfKeyModal();
              },
            }, 'Apply'))
        : null);
  }

  paint();
  return el('div', { class: 'capgrid' },
    el('div', { class: 'capgrid-head' },
      el('label', { text: 'Keycap fitted here' }),
      search),
    cells,
    foot);
}

function controlPanel(name) {
  const cell = (S.schema.controls || []).find((c) => c.control === name)
            || { label: name, note: '' };
  return [
    el('div', { class: 'sel-head' },
      el('h1', { text: cell.label }),
      el('span', { class: 'sel-id', text: 'not an input' })),
    el('div', { class: 'info' },
      'FreeMicro cannot bind this control, and neither can anything else on ' +
      'your Mac - it is handled entirely inside the pad.'),
    el('p', { class: 'sel-note', text: cell.note }),
    el('p', { class: 'hint' },
      'The three small LEDs beside it are firmware-owned too: they show which ' +
      'Bluetooth profile is live and FreeMicro never writes to them.'),
  ];
}

function setKind(id, kind) {
  if (!kind) {
    writeBinding(id, null);
  } else {
    const previous = binding(id) || {};
    const spec = S.schema.actions.find((a) => a.kind === kind);
    const next = { action: kind };
    // Carry over anything the new kind also accepts, so switching text -> hold
    // does not silently lose the key you already typed.
    for (const field of spec.fields) {
      if (previous[field.name] !== undefined) next[field.name] = previous[field.name];
    }
    if (previous.label) next.label = previous.label;
    if (previous.comment) next.comment = previous.comment;
    // `light` belongs to the binding, not to the kind - see _META_FIELDS in
    // padconfig - so it survives a change of kind like the label does.
    if (previous.light) next.light = previous.light;
    writeBinding(id, next);
  }
  changed();
  renderPad();
}

/* -------------------------------------------------------------- setup tab */

/* The tab that means most people never have to touch the other four. */

/* ---------------------------------------------------------------- layouts */

/* A layout is a whole pad under a name. The starters are read-only built-ins;
 * anything you save is yours. Switching is one click and goes through exactly
 * the same delta save as any other edit, so it can never clobber a concurrent
 * change. */
function openLayoutModal(layoutId) {
  const layout = S.layouts.find((x) => x.id === layoutId);
  if (!layout) return;
  const diff = buildPreview(layout);
  const body = [
    el('p', { class: 'modal-note', text: layout.who || layout.tagline || '' }),
    (layout.requires || []).length
      ? el('div', { class: 'note', text: 'Needs: ' + layout.requires.join(' ') })
      : null,
    el('p', { class: 'diff-summary', text: diff.rows.length
      ? `${diff.changed} keys change, ${diff.added} newly bound, ` +
        `${diff.removed} unbound.`
      : 'Your pad already matches this exactly - nothing would change.' }),
  ];
  if (diff.rows.length) {
    body.push(el('div', { class: 'difftable' },
      diff.rows.map((row) => el('div', { class: 'diffrow ' + row.kind },
        el('span', { class: 'diff-id', text: row.id }),
        el('span', { class: 'diff-from', text: row.from }),
        el('span', { class: 'diff-arrow', 'aria-hidden': 'true', text: '→' }),
        el('span', { class: 'diff-to', text: row.to })))));
    body.push(el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn primary', type: 'button',
        onclick: (e) => {
          e.target.disabled = true;
          e.target.textContent = 'Switching…';
          applyLayout(layout).catch((err) => fatal('Switching layout', err));
        },
      }, 'Switch to ' + layout.name),
      !layout.builtin ? el('button', {
        class: 'btn danger', type: 'button',
        onclick: () => deleteLayout(layout),
      }, 'Delete') : null,
      el('button', {
        class: 'btn ghost', type: 'button', onclick: () => closeModal(),
      }, 'Cancel')));
    body.push(el('p', { class: 'hint' },
      'Only what the pad does changes. Your colours, stick settings and ' +
      'comments stay exactly as they are, the previous file is kept as .bak, ' +
      'and Revert is one click.'));
  } else {
    body.push(el('div', { class: 'modal-actions' },
      el('button', { class: 'btn', type: 'button', onclick: () => closeModal() },
        'Close')));
  }
  openModal(layout.name, body);
}

function openSaveLayoutModal() {
  const name = el('input', {
    type: 'text', placeholder: 'work', maxlength: '40',
  });
  openModal('Save this pad as a layout',
    el('p', { class: 'modal-note' },
      'Keeps what every key does, under a name you can switch back to. Your ' +
      'colours and other settings are not part of a layout.'),
    el('div', { class: 'field' }, el('label', { text: 'Name' }), name),
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn primary', type: 'button',
        onclick: async () => {
          const res = await api('/api/layouts/save',
            { body: { name: name.value, document: S.doc } });
          if (!res.ok) { toast(res.error, true); return; }
          S.layouts = res.layouts || S.layouts;
          S.layout = res.id;
          closeModal();
          renderHome();
          toast(`Saved as “${res.name}”.`);
        },
      }, 'Save layout'),
      el('button', { class: 'btn ghost', type: 'button',
                     onclick: () => closeModal() }, 'Cancel')));
}

async function deleteLayout(layout) {
  const res = await api('/api/layouts/delete', { body: { id: layout.id } });
  S.layouts = res.layouts || S.layouts;
  if (S.layout === layout.id) S.layout = '';
  closeModal();
  renderHome();
  toast(res.ok ? `Deleted “${layout.name}”.` : (res.error || 'Not deleted.'), !res.ok);
}

/* Undo has to reach the disk too, or "revert" would leave the file saying one
 * thing and the page another. */
async function revertStarter() {
  const snapshot = S.undo && S.undo.document;
  if (!snapshot) return;
  S.doc = snapshot;
  try {
    const res = await api('/api/config', {
      body: { document: S.doc, base: S.base, fingerprint: S.fingerprint },
    });
    if (res.conflict) { showConflict(res); return; }
    if (!res.ok) { showBanner(res.error); toast('Could not revert.', true); return; }
  } catch (err) {
    showBanner(String(err.message || err));
    toast('Could not revert.', true);
    return;
  }
  await loadConfig();
  S.undo = null;
  S.applied = null;
  renderAll();
  toast('Put back the way it was.');
}



function describeBinding(raw) {
  if (!raw || typeof raw !== 'object') return 'unbound';
  // The label alone is not enough for a diff: two bindings can share a label
  // and do completely different things, and a row reading "agent 1 → agent 1"
  // tells the user nothing about what they are about to change.
  if (raw.label) return `${raw.label} (${raw.action})`;
  const bits = [raw.action];
  for (const field of ['text', 'key', 'command', 'name', 'script']) {
    if (raw[field] !== undefined) {
      const value = String(raw[field]);
      bits.push(value.length > 28 ? value.slice(0, 27) + '…' : value);
      break;
    }
  }
  return bits.join(' ');
}


function buildPreview(layout) {
  const current = bindings();
  const next = layout.bindings || {};
  const ids = Array.from(new Set([...Object.keys(current), ...Object.keys(next)]));
  const order = allInputs().map((i) => i.id);
  ids.sort((a, b) => {
    const ai = order.indexOf(a), bi = order.indexOf(b);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });
  const rows = [];
  let added = 0, removed = 0, changed = 0;
  for (const id of ids) {
    const before = current[id], after = next[id];
    if (JSON.stringify(before || null) === JSON.stringify(after || null)) continue;
    let kind = 'changed';
    if (!before) { kind = 'added'; added += 1; }
    else if (!after) { kind = 'removed'; removed += 1; }
    else changed += 1;
    rows.push({ id, kind, from: describeBinding(before), to: describeBinding(after) });
  }
  return { id: layout.id, rows, added, removed, changed };
}

/* ------------------------------------------------------------ swap & move */

/* Moving what a key does used to mean opening two editors and retyping both.
 * Now it is one gesture on the picture of the pad - which is also how the
 * hardware works: when a keycap is physically moved to another switch, its
 * binding should follow in one motion. */
function swapKeys(a, b, copy) {
  const map = bindings();
  const fromA = binding(a) ? clone(binding(a)) : null;
  const fromB = binding(b) ? clone(binding(b)) : null;
  if (fromA === null) delete map[b]; else writeBinding(b, fromA);
  if (!copy) {
    if (fromB === null) delete map[a]; else writeBinding(a, fromB);
  }
  // The cap follows what the key does, unless the user has set caps by hand.
  const caps = peekCaps();
  const capA = caps[a], capB = caps[b];
  if (capA || capB) {
    if (capA) setCap(b, capA); else setCap(b, '');
    if (!copy) { if (capB) setCap(a, capB); else setCap(a, ''); }
  }
  changed();
  renderPad();
  toast(copy ? `Copied onto ${b}.` : `Swapped ${a} and ${b}.`);
}

/* Apply, save, then redraw from what is *on disk*.
 *
 * Not from the preset definition: the thing on screen has to be provably the
 * config that exists, not an optimistic guess about it. A toast on its own is
 * not enough - if the diagram still shows the old bindings, the user has every
 * reason to believe nothing happened. */

/* Switching layout writes bindings and keycaps only, through the same
 * delta+fingerprint save as every other edit, then redraws from disk. */
async function applyLayout(layout) {
  const before = clone(S.doc);
  const diff = buildPreview(layout);
  S.undo = { document: before, label: layout.name };
  S.doc.bindings = clone(layout.bindings);
  if (layout.keycaps && Object.keys(layout.keycaps).length) {
    S.doc[S.schema.keycap_section] = clone(layout.keycaps);
  }

  let res;
  try {
    res = await api('/api/config', {
      body: { document: S.doc, base: S.base, fingerprint: S.fingerprint },
    });
  } catch (err) {
    S.doc = before;
    S.undo = null;
    renderAll();
    showBanner(String(err.message || err));
    toast('Not switched - nothing was written.', true);
    return;
  }
  if (res.conflict) {
    S.doc = before;
    S.undo = null;
    renderAll();
    showConflict(res);
    return;
  }
  if (!res.ok) {
    S.doc = before;
    S.undo = null;
    renderAll();
    showBanner(res.error);
    toast('Not switched - the config layer rejected it.', true);
    return;
  }
  await loadConfig();
  S.layout = layout.id;
  S.applied = { label: layout.name, changed: diff.changed, added: diff.added,
                removed: diff.removed, backup: res.backup };
  closeModal();
  renderAll();
  toast(`Now using “${layout.name}”. Revert is in the header.`);
}

/* A limit that is only discovered months later - when the mic key has quietly
 * never worked - is not a limit the software should let you cross. Wispr Flow
 * refuses any shortcut longer than three keys, which is exactly why the
 * four-key combo this project shipped with could never fire. */
function comboProblem(choice, combo) {
  const limit = Number(choice.max_keys || 0);
  if (!limit) return '';
  const keys = String(combo || '').replace(/-/g, '+').split('+').filter(Boolean);
  if (keys.length <= limit) return '';
  return `${choice.label} shortcuts are limited to ${limit} keys, and ` +
         `“${combo}” is ${keys.length}. It cannot be registered there, so the ` +
         'key would do nothing.';
}

function paintComboLimit(choice, combo) {
  const node = $('#dictation-limit');
  if (!node) return;
  const problem = comboProblem(choice, combo);
  node.className = 'fieldnote' + (problem ? ' bad' : ' ok');
  node.textContent = problem || (combo ? `✓ ${combo} fits.` : '');
}

function dictationSection() {
  const choices = S.schema.dictation || [];
  const chosen = choices.find((c) => c.id === S.dictation) || choices[0];
  if (!chosen) return [];
  const micIds = pairOf('ACT10');
  const current = binding(micIds[0]);
  return [
    el('h2', { text: 'Dictation - the MIC key' }),
    el('p', { class: 'hint' },
      'The wide MIC cap fires ' + micIds.join(' and ') +
      ' together, so both get whatever you pick here.'),
    el('div', { class: 'choices' },
      choices.map((choice) => el('button', {
        class: 'choice' + (choice.id === chosen.id ? ' is-on' : ''),
        type: 'button',
        onclick: () => { S.dictation = choice.id; renderAdvanced(); },
      },
        el('strong', { text: choice.label }),
        el('span', { text: choice.summary })))),
    el('p', { class: 'setup-note', text: chosen.setup }),
    chosen.warning ? el('div', { class: 'note', text: chosen.warning }) : null,
    el('div', { class: 'field' },
      el('span', { class: 'field-label', text: 'Shortcut the pad will send' }),
      el('input', {
        type: 'text', class: 'mono', id: 'dictation-combo',
        value: chosen.key,
        oninput: (e) => paintComboLimit(chosen, e.target.value),
      }),
      el('p', { class: 'fieldnote', id: 'dictation-limit' }),
      chosen.max_keys
        ? el('p', { class: 'hint',
                    text: `${chosen.label} will not register a shortcut longer ` +
                          `than ${chosen.max_keys} keys.` })
        : null),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', type: 'button',
        onclick: () => {
          const combo = $('#dictation-combo').value.trim();
          if (!combo) { toast('Type the shortcut first.', true); return; }
          const problem = comboProblem(chosen, combo);
          if (problem) { toast(problem, true); return; }
          S.undo = { document: clone(S.doc), label: 'dictation on MIC' };
          writeBinding(micIds[0], {
            action: chosen.action,
            key: combo,
            label: chosen.action === 'hold' ? 'mic - push to talk' : 'mic - dictation',
            comment: 'The wide MIC cap sits over two switches and reports both ' +
                     'ids on every press, so both carry the same binding.',
          });
          changed();
          renderPad();
          renderAdvanced();
          toast('MIC set to ' + chosen.label + '.');
        },
      }, 'Set MIC to ' + chosen.label),
      el('span', { class: 'hint', text: current
        ? 'Currently: ' + describeBinding(current)
        : 'MIC is currently unbound.' })),
  ].filter(Boolean);
}

/* ----------------------------------------------------------- lighting tab */


/* -------------------------------------------------------------- colours */

/* Click a swatch under the pad -> colour and effect for that state. Speed and
 * the undocumented `magic` live under Advanced, where they cannot compete for
 * attention with the two controls anybody actually wants. */
function openColourModal(name) {
  const light = stateLight(name);
  const touch = () => {
    changed();
    renderPad();
    if (S.live && lightingOk()) sendPreview(light);
  };
  openModal(`Colour when a project is ${name}`,
    el('div', { class: 'field' },
      el('span', { class: 'field-label', text: 'Colour' }),
      el('div', { class: 'row' },
        el('input', {
          type: 'color', value: toHex(light.color),
          oninput: (e) => { light.color = e.target.value.toUpperCase(); touch(); },
        }),
        el('div', { class: 'presets' },
          S.schema.presets.map((p) => el('button', {
            class: 'preset', type: 'button', title: p.vendor,
            onclick: () => { light.color = p.hex; touch(); openColourModal(name); },
          }, el('i', { style: `background:${p.hex}` }), p.label))))),
    el('div', { class: 'field' },
      el('span', { class: 'field-label', text: 'Effect' }),
      picker({
        value: effectName(light.effect),
        options: S.schema.effects.map((eff) => ({
          value: eff.name, label: eff.name, hint: EFFECT_HELP[eff.name] || '',
          terms: eff.name + ' ' + (EFFECT_HELP[eff.name] || ''),
        })),
        search: 'Search effects',
        onpick: (value) => { light.effect = value; touch(); openColourModal(name); },
      })),
    el('details', { class: 'escape' },
      el('summary', { text: 'Advanced' }),
      slider('Speed', unit(light.speed, 0), (v) => { light.speed = v; touch(); },
        animated(light) ? '' :
          `“${effectName(light.effect)}” does not animate, so speed does nothing.`),
      slider('Magic', unit(light.magic, 0), (v) => { light.magic = v; touch(); }),
      el('p', { class: 'hint' },
        'The vendor documents “magic” only as an effect-specific parameter and ' +
        'we have not seen what it changes on this firmware.')),
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn', type: 'button', 'data-needs-pad': 'lighting',
        disabled: !lightingOk(),
        onclick: () => sendPreview(light, true),
      }, 'Show it on the pad'),
      el('button', {
        class: 'btn primary', type: 'button', onclick: () => closeModal(),
      }, 'Done')));
}

/* ------------------------------------------------------------- advanced */

/* Everything that used to be on the front page. Present, findable, and closed
 * by default - which is the whole difference between "capable" and
 * "overwhelming". */
function renderAdvanced() {
  const host = $('#advanced-body');
  if (!$('#advanced').open) { mount(host); return; }
  const l = peekLighting();
  const j = (S.doc.joystick && typeof S.doc.joystick === 'object')
    ? S.doc.joystick : {};

  mount(host,
    el('h3', { text: 'Sharing the pad' }),
    toggle(peekZones().includes('backlight') && !peekZones().includes('agent_keys'),
      'Leave the Agent Keys to the ChatGPT app',
      (on) => {
        lighting().zones = on ? ['backlight'] : ['agent_keys'];
        changed();
        renderPad();
        renderAdvanced();
      }),
    el('p', { class: 'hint' },
      'Both programs drive the same LEDs. With this on, FreeMicro only lights ' +
      'under the keycaps and leaves the six Agent Keys to the vendor app.'),

    el('h3', { text: 'When FreeMicro stops' }),
    picker({
      value: l.on_exit || 'off',
      options: S.schema.exit_modes.map((m) => ({ value: m, label: m })),
      onpick: (value) => { lighting().on_exit = value; changed(); renderAdvanced(); },
    }),

    el('h3', { text: 'Which project each Agent Key follows' }),
    agentSlots(),

    el('h3', { text: 'Thumbstick' }),
    slider('Deadzone', unit(j.deadzone, 0.6),
      (v) => { joystick().deadzone = v; changed(); }),
    el('p', { class: 'hint' },
      'How far you have to push before a flick registers. The default suits ' +
      'the stick on this unit; there is rarely a reason to change it.'),

    el('h3', { text: 'Dictation' }),
    // `replaceChildren` is a native DOM method: unlike our own `el()` it does
    // NOT flatten arrays, it stringifies them. `dictationSection()` returns a
    // list, so it must be spread - passing it whole renders the literal text
    // "[object HTMLHeadingElement],[object HTMLParagraphElement],…".
    ...dictationSection());
}

function agentSlots() {
  const policy = typeof peekAgents().policy === 'string'
    ? peekAgents().policy : 'recent';
  const pinning = policy === 'pinned' || policy === 'manual';
  const slots = peekSlots();
  if (!pinning) {
    return el('p', { class: 'hint' },
      'The “' + policy + '” policy fills the keys by itself. Choose Pinned or ' +
      'Manual on the front page to nail a project to a key.');
  }
  const rows = S.schema.layout.cells.filter((c) => c.slot !== undefined)
    .map((cell) => {
      const index = cell.slot;
      const current = slots[index] || '';
      const project = S.projects.find((p) => p.path === current);
      return el('div', { class: 'slot' },
        el('div', { class: 'slot-key', text: cell.keycap }),
        el('div', {},
          picker({
            value: current,
            options: [{ value: '', label: 'empty (key stays dark)' }].concat(
              S.projects.map((p) => ({
                value: p.path, label: p.label, hint: p.state + ' · ' + p.path,
                terms: p.path + ' ' + p.label,
              }))),
            placeholder: current || 'Choose a project',
            search: 'Search projects',
            empty: 'No projects are live right now.',
            onpick: (value) => {
              const next = peekSlots();
              next[index] = value;
              agents().slots = next;
              changed();
              renderAdvanced();
            },
          }),
          el('div', { class: 'slot-meta', text: project
            ? `${project.state} · ${project.sessions} live`
            : (current ? 'pinned - nothing live there now' : 'empty') })));
    });
  return el('div', {}, rows);
}



/* What each effect actually does on this hardware, in the picker, in plain
 * words - an integer in a dropdown tells nobody anything. */
const EFFECT_HELP = {
  'off': 'dark',
  'solid': 'one steady colour',
  'snake': 'a lit segment travels along the strip',
  'rainbow': 'cycles the whole spectrum',
  'breath': 'fades all the way in and out',
  'gradient': 'blends across the strip',
  'shallow-breath': 'a gentle pulse, half to full brightness',
};
const animated = (light) =>
  !['off', 'solid'].includes(effectName(light.effect));

function slider(label, value, apply, disabledWhy) {
  const readout = el('span', { class: 'range-val', text: value.toFixed(2) });
  return el('div', { class: 'field' },
    el('span', { class: 'field-label', text: label }),
    el('div', { class: 'range-row' },
      el('input', {
        type: 'range', min: '0', max: '1', step: '0.01', value: String(value),
        disabled: !!disabledWhy, title: disabledWhy || '',
        oninput: (e) => {
          const next = Number(e.target.value);
          readout.textContent = next.toFixed(2);
          apply(next);
        },
      }),
      readout),
    disabledWhy ? el('p', { class: 'hint', text: disabledWhy }) : null);
}

/* ------------------------------------------------------------- agents tab */



/* -------------------------------------------------------------- stick tab */



/* ------------------------------------------------------------------ tabs */

const TABS = ['setup', 'binding', 'lighting', 'agents', 'stick'];


/* ------------------------------------------------------------------ modal */

/* One modal, reused. Everything that is not the pad, the status line or the
 * three global settings lives behind a click on the thing it belongs to. */
function openModal(title, ...body) {
  const host = $('#modal');
  $('#modal-title').textContent = title;
  mount($('#modal-body'), body);
  host.hidden = false;
  // Land on the answer that is already true, not on the first button in the
  // DOM: a focus ring on "Jump to this project's terminal" while "Press a
  // shortcut" is the selected one reads as two selected answers.
  const focusable = $('#modal-body').querySelector('.outcome.is-on') ||
    $('#modal-body').querySelector('input, button, select, textarea, [tabindex]');
  (focusable || $('#modal-close')).focus();
}

function closeModal() {
  const host = $('#modal');
  if (host.hidden) return;
  host.hidden = true;
  mount($('#modal-body'));
  S.selected = null;
  S.control = null;
  stopComboCapture();
  renderPad();
}

const modalOpen = () => !$('#modal').hidden;

/* ------------------------------------------------------------- front page */

/* What is on this page, in full: the pad, one status line, one layout
 * chooser, three global settings, and a disclosure. That is the whole design.
 * If you are adding a sixth control here, it probably belongs in a modal or
 * under Advanced. */
function renderHome() {
  renderStatus();
  renderSettings();
  renderAdvanced();
  renderChrome();
}

function renderStatus() {
  const node = $('#status');
  const d = S.device;
  const parts = [];
  let tone = 'ok';

  if (d.unknown) {
    node.className = 'status';
    mount(node, 'Looking for your pad…');
    return;
  }
  if (!d.present) {
    tone = 'off';
    parts.push(el('strong', { text: 'No pad connected.' }),
               el('span', { text: ' ' + (d.reason || '') }));
  } else if (d.reason) {
    tone = 'warn';
    parts.push(el('strong', { text: 'The pad is busy.' }),
               el('span', { text: ' ' + d.reason }));
  } else {
    const bits = ['Pad connected over ' + (d.transport || 'USB')];
    if (d.battery) bits.push(d.battery);
    parts.push(el('strong', { text: bits.join(' · ') }));
    if (S.capture.on) {
      // The single most reassuring thing this page can say: we are hearing you.
      parts.push(el('span', { class: 'listening' },
        el('span', { class: 'dot-live', 'aria-hidden': 'true' }),
        'Press any key on the pad to set it up - it will not do what it is ' +
        'bound to while this page is listening.'));
    } else if (d.lighting_warning) {
      parts.push(el('span', { text: ' ' + d.lighting_warning }));
    }
  }
  node.className = 'status status-' + tone;
  mount(node, parts);
}

function renderSettings() {
  const host = $('#settings');
  const l = peekLighting();
  const policy = typeof peekAgents().policy === 'string'
    ? peekAgents().policy : 'recent';
  const current = S.layouts.find((x) => x.id === S.layout);

  mount(host,
    // 1. Layouts - the whole pad, in one control.
    el('div', { class: 'setting' },
      el('label', { text: 'Layout' }),
      picker({
        value: S.layout,
        options: S.layouts.map((x) => ({
          value: x.id,
          label: x.name,
          hint: x.builtin ? x.tagline : 'saved',
          terms: x.name + ' ' + (x.tagline || ''),
        })),
        placeholder: current ? current.name : 'Choose a layout',
        search: 'Search layouts',
        empty: 'No layouts yet.',
        onpick: (value) => openLayoutModal(value),
      }),
      el('button', {
        class: 'btn tiny ghost', type: 'button',
        onclick: () => openSaveLayoutModal(),
      }, 'Save current as…')),

    // 2. Lights on or off.
    el('div', { class: 'setting' },
      el('label', { text: 'Lights' }),
      toggle(l.enabled === true, 'FreeMicro drives the LEDs',
        (on) => { lighting().enabled = on; changed(); renderPad(); renderHome(); })),

    // 3. Brightness, one slider for the whole pad, like the vendor's.
    el('div', { class: 'setting' },
      el('label', { text: 'Brightness' }),
      slider('', globalBrightness(), (v) => {
        setGlobalBrightness(v);
        changed();
        renderPad();
        if (S.live && lightingOk()) sendPreview(stateLight(S.showState));
      })),

    // 4. What the six Agent Keys follow.
    el('div', { class: 'setting' },
      el('label', { text: 'Agent keys follow' }),
      picker({
        value: policy,
        options: (S.schema.agent_policies || []).map((p) => ({
          value: p.value, label: p.label, hint: p.help, terms: p.label + p.help,
        })),
        search: 'Search',
        onpick: (value) => { agents().policy = value; changed(); renderHome(); },
      })));
}

function toggle(on, label, apply) {
  return el('label', { class: 'switch' },
    el('input', {
      type: 'checkbox', checked: on,
      onchange: (e) => apply(e.target.checked),
    }),
    el('span', { text: label }));
}

/* One brightness for the pad, as the vendor's settings has. It writes through
 * to every state, because the config's brightness is per state - the user
 * should not have to know that to make the lights dimmer. */
function globalBrightness() {
  const states = peekLighting().states || {};
  const values = Object.values(states)
    .map((light) => unit(light && light.brightness, 1));
  if (!values.length) return 1;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function setGlobalBrightness(value) {
  for (const name of S.schema.states) stateLight(name).brightness = value;
}

/* The server is a long-lived process holding imported modules in memory. If
 * the code on disk has moved on, say so - that mismatch produced an error
 * about an action that plainly existed. */
function renderRestartNotice() {
  const node = $('#restart');
  if (!node) return;
  // While the blocking panel is up it owns this message; showing it twice is
  // noise. Once the user has waved the panel away, this strip stays for as
  // long as the mismatch does, so "carry on anyway" is not the same as
  // "forget about it".
  // The save confirmation and the conflict panel borrow this same node, and
  // the device poll runs every three seconds - so without this, a panel asking
  // "write these 12 changes?" could vanish mid-read.
  if (node.dataset.owner) return;
  if (S.device.restart && S.staleDismissed) {
    node.hidden = false;
    node.className = 'banner warn';
    mount(node, S.device.restart);
  } else {
    node.hidden = true;
  }
}

/* Claim the shared banner node for a panel that must not be polled away, and
 * hand it back when that panel is done with it. */
function claimBanner(owner) {
  const node = $('#restart');
  node.dataset.owner = owner;
  node.hidden = false;
  node.className = 'banner warn';
  return node;
}

function releaseBanner() {
  const node = $('#restart');
  delete node.dataset.owner;
  node.hidden = true;
  mount(node);
}

function renderAll() {
  renderPad();
  renderHome();
}

/* -------------------------------------------------------------- preview */

async function sendPreview(light, announce) {
  if (!lightingOk()) {
    toast(S.device.reason || 'The pad is not available.', true);
    return;
  }
  const payload = {
    color: toHex(light.color),
    effect: effectValue(light.effect),
    brightness: unit(light.brightness, 1),
    speed: unit(light.speed, 0),
    zones: peekZones(),
    method: peekLighting().method || 'rgbcfg',
  };
  if (S.previewBusy) { S.previewQueued = payload; return; }
  S.previewBusy = true;
  try {
    const res = await api('/api/preview', { body: payload });
    if (!res.ok) {
      toast(res.error || 'Preview failed.', true);
      if (res.unavailable) refreshDevice();
    } else if (announce) {
      toast('Sent to the pad: ' + res.described);
    }
  } catch (err) {
    toast(String(err.message || err), true);
  } finally {
    S.previewBusy = false;
    const queued = S.previewQueued;
    S.previewQueued = null;
    // Each lighting call replaces the last, so only the newest frame matters.
    if (queued) {
      sendPreview({
        color: queued.color, effect: queued.effect,
        brightness: queued.brightness, speed: queued.speed,
      });
    }
  }
}

/* -------------------------------------------------------------- capture */

/* Listening is the default when the pad is free.
 *
 * The most natural way to say "configure THIS key" is to press it, and it also
 * answers the question that was invisible all evening: is FreeMicro receiving
 * this pad at all? Capture reads v.oai.hid and nothing else - it never builds
 * a Bridge - so a press while this page is listening cannot type into your
 * terminal, switch apps or run a shell command. The status line says so. */
async function autoListen() {
  if (S.capture.on || !inputOk() || S.capture.declined) return;
  await startCapture(true);
}

async function startCapture(quiet) {
  if (S.capture.on) return;
  let res;
  try {
    res = await api('/api/capture/start', { body: {} });
  } catch (err) {
    toast(String(err.message || err), true);
    return;
  }
  if (!res.ok) {
    // Auto-listening must never nag; an explicit press of the button should.
    S.capture.declined = true;
    if (!quiet) {
      toast(res.error || 'Could not listen to the pad.', true);
      showBanner(res.error || '', 'warn');
    }
    refreshDevice();
    return;
  }
  S.capture.on = true;
  $('#capture-strip').hidden = false;
  $('#identify').classList.add('on');
  $('#identify').textContent = 'Listening…';
  $('#capture-text').textContent =
    'Listening. Press any key on the pad to set it up - it will not do what ' +
    'it is bound to while this is on.';
  renderStatus();
  pollCapture();
}

async function stopCapture() {
  S.capture.on = false;
  clearTimeout(S.capture.timer);
  $('#capture-strip').hidden = true;
  S.capture.declined = true;   // stopped on purpose; do not re-arm behind them
  $('#identify').classList.remove('on');
  $('#identify').textContent = 'Press a key to find it';
  renderStatus();
  try {
    await api('/api/capture/stop', { body: {} });
  } catch (err) { /* the page may be going away; nothing useful to do */ }
}

async function pollCapture() {
  if (!S.capture.on) return;
  let res;
  try {
    res = await api('/api/capture/events?since=' + S.capture.since);
  } catch (err) {
    stopCapture();
    return;
  }
  S.capture.since = res.seq;
  for (const event of res.events) {
    if (event.kind === 'key') {
      flash(event.input);
      if (event.pressed) {
        const cap = capOf(event.input);
        $('#capture-text').textContent =
          (cap ? cap.id : event.input) + ' - pressed';
        // Press a key -> that key's editor. No id to know, nothing to hunt for.
        select(event.input);
      }
    } else if (event.kind === 'joystick') {
      $('#joy-readout').textContent =
        `angle ${event.angle.toFixed(3)}  distance ${event.distance.toFixed(3)}`;
      // A flick past the deadzone selects that direction, same as a key press.
      if (event.distance > 0.6 && !modalOpen()) {
        const which = joystickInputFor(event.angle);
        if (which) { flash(which); select(which); }
      }
    }
  }
  if (!res.capturing) {
    $('#capture-text').textContent = res.error
      ? 'Stopped: ' + res.error
      : 'Listening stopped (it times out so the pad is not held forever).';
    S.capture.on = false;
    $('#identify').classList.remove('on');
    $('#identify').textContent = 'Press a key to find it';
    renderStatus();
    setTimeout(() => { $('#capture-strip').hidden = true; }, 2500);
    return;
  }
  S.capture.timer = setTimeout(pollCapture, 250);
}

/* Which flick a stick angle is, using the config's own wheel so the UI and the
 * bridge cannot disagree about which way is "up". */
function joystickInputFor(angle) {
  const j = (S.doc.joystick && typeof S.doc.joystick === 'object') ? S.doc.joystick : {};
  const directions = Array.isArray(j.directions) && j.directions.length
    ? j.directions : S.schema.joystick_inputs;
  const origin = Number(j.origin) || 0;
  const steps = directions.length;
  const shifted = ((Number(angle) - origin) % 1 + 1) % 1;
  return directions[Math.round(shifted * steps) % steps];
}

/* ------------------------------------------------------------ validation */

let validateTimer = null;
function changed() {
  renderChrome();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    try {
      const res = await api('/api/validate', { body: { document: S.doc } });
      S.validation = { ok: !!res.ok, error: res.error || '' };
      showBanner(res.ok ? '' : res.error);
      renderChrome();
      paintValidation();
    } catch (err) {
      S.validation = { ok: false, error: String(err.message || err) };
      showBanner(String(err.message || err));
      renderChrome();
      paintValidation();
    }
  }, 300);
}

/* ----------------------------------------------------------- stale server */

/* The most expensive class of bug report this project has had.
 *
 * Python holds imported modules in memory, so `freemicro config --web` left
 * running through an update goes on answering with last hour's code while the
 * browser loads this minute's `app.js` straight off disk. The page then looks
 * perfect and is wrong. It has cost, so far: `/api/layouts/save` returning 404
 * for a route that plainly exists, "unknown action" for an action that plainly
 * exists, and - the one that cost a whole evening - `/api/schema` answering
 * without a keycap catalogue, so `capById()` matched nothing, every key drew no
 * glyph, and the pad diagram came up blank with no error anywhere.
 *
 * Two detectors, because they fail at different ages:
 *
 *   1. the server's own mtime comparison (`/api/device` -> `restart`), which is
 *      precise, but only exists in a server new enough to have it;
 *   2. this contract, which is simply what THIS page needs `/api/schema` to
 *      contain. It runs in the browser, so it works against a server of any
 *      age - including one that predates the mtime check itself.
 *
 * And the answer is a blocking panel. A notice you can scroll past is a notice
 * that generates the bug report anyway.
 */
const SCHEMA_CONTRACT = [
  ['keycaps', 'the keycap catalogue every glyph on the diagram is drawn from'],
  ['keycap_section', 'where the fitted keycaps are stored'],
  ['keycap_rules', 'the keycap suggestions'],
  ['layout', 'the shape of the pad'],
  ['actions', 'what a key can be bound to'],
  ['states', 'the lighting states'],
  ['agent_policies', 'what the six Agent Keys follow'],
];

/* Which parts of the contract this server did not answer with. */
function schemaGaps() {
  const schema = S.schema || {};
  const missing = [];
  for (const [key, what] of SCHEMA_CONTRACT) {
    const value = schema[key];
    const empty = value === undefined || value === null ||
                  (Array.isArray(value) && !value.length);
    if (empty) missing.push(`${key} (${what})`);
  }
  return missing;
}

function renderStale() {
  const node = $('#stale');
  if (!node) return;
  const gaps = schemaGaps();
  const told = S.device.restart || '';
  if (S.staleDismissed || (!gaps.length && !told)) { node.hidden = true; return; }
  mount(node, el('div', { class: 'stale-card' },
    el('h2', { text: 'The FreeMicro running this page is older than the ' +
                     'FreeMicro on disk.' }),
    el('p', { text: told ||
      'This page asked the server for things it does not know about, which ' +
      'only happens when the process started before the code it is serving.' }),
    gaps.length
      ? el('p', { class: 'stale-why',
                  text: 'Not in its answer: ' + gaps.join('; ') + '.' })
      : null,
    el('p', { class: 'stale-fix', text: 'In the terminal running it, press ' +
                                        'Ctrl-C and start it again:' }),
    el('code', { class: 'stale-cmd', text: 'freemicro config --web' }),
    el('p', { class: 'hint', text: 'Nothing here is safe to trust until you ' +
      'do. Your config file on disk has not been touched.' }),
    // Loud is right; a trap is not. Someone editing FreeMicro's own source has
    // a legitimate reason to be looking at a server that is one save behind,
    // and a panel with no way past it becomes its own bug report.
    el('button', {
      class: 'btn tiny ghost stale-ignore', type: 'button',
      onclick: () => { S.staleDismissed = true; renderStale(); },
    }, 'Carry on anyway, and expect things to be wrong')));
  node.hidden = false;
}

/* ------------------------------------------------------------------- boot */

async function refreshDevice() {
  try {
    S.device = await api('/api/device');
  } catch (err) {
    S.device = { usable: false, present: false, reason: String(err.message || err) };
  }
  renderChrome();
  renderAvailability();
  renderRestartNotice();
  renderStale();
}

async function refreshProjects() {
  try {
    const res = await api('/api/projects');
    S.projects = res.projects || [];
  } catch (err) {
    S.projects = [];
  }
  const list = $('#live-projects');
  mount(list, S.projects.map((p) =>
    el('option', { value: p.path }, `${p.label} - ${p.state}`)));
}

async function loadLayouts() {
  try {
    const res = await api('/api/layouts');
    S.layouts = res.layouts || [];
  } catch (err) {
    S.layouts = [];
  }
  renderHome();
}

async function loadApps() {
  try {
    const res = await api('/api/apps');
    S.apps = res.apps || [];
  } catch (err) {
    S.apps = [];
  }
  const list = $('#installed-apps');
  mount(list, S.apps.map((a) => el('option', { value: a.name })));
  reopenIfKeyModal();
}

async function loadConfig() {
  const res = await api('/api/config');
  if (!res.document) {
    showBanner(res.error || 'Could not read your config.');
    return false;
  }
  S.doc = res.document;
  // `"AG00": "/resume"` is legal shorthand for a text action. Every editor
  // control needs the object form, so expand it once here - before the
  // baseline is taken, so an equivalent rewrite never shows up as "unsaved".
  const map = S.doc.bindings;
  if (map && typeof map === 'object') {
    for (const [id, raw] of Object.entries(map)) {
      if (typeof raw === 'string') map[id] = { action: 'text', text: raw };
    }
  }
  S.baseline = JSON.stringify(S.doc);
  S.base = clone(S.doc);
  S.fingerprint = res.fingerprint || '';
  S.paths = { load: res.load_path, save: res.save_path, backup: res.backup_path };
  S.validation = { ok: !res.error, error: res.error || '' };
  showBanner(res.error || '');
  if (res.is_default) {
    showBanner(
      'You are editing the built-in defaults. Saving writes your own copy to ' +
      res.save_path + ' - nothing inside the installed package is touched.',
      'warn');
  }
  return true;
}

/* The same leaf walk the server does, so the page can show what a save would
 * write *before* it writes it. Deliberately duplicated rather than fetched:
 * a confirmation that needs a round trip is a confirmation people skip. */
function deltaPaths(base, next, prefix) {
  const at = prefix || [];
  const out = [];
  const keys = Array.from(new Set([...Object.keys(base || {}),
                                   ...Object.keys(next || {})])).sort();
  for (const key of keys) {
    const was = base ? base[key] : undefined;
    const now = next ? next[key] : undefined;
    if (was !== undefined && now !== undefined &&
        JSON.stringify(was) === JSON.stringify(now)) continue;
    const path = at.concat([key]);
    const bothObjects = was && now && typeof was === 'object' &&
                        typeof now === 'object' &&
                        !Array.isArray(was) && !Array.isArray(now);
    if (bothObjects) out.push(...deltaPaths(was, now, path));
    else out.push({ path: path.join('.'), from: was, to: now });
  }
  return out;
}

const shortly = (value) => {
  if (value === undefined) return 'removed';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 40 ? text.slice(0, 39) + '…' : text;
};

/* Save shows its work whenever it would write more than the one thing you just
 * edited. This is the check that would have caught the bug it exists for: a
 * value that had drifted in the page, about to be written over the file, with
 * nothing on screen to say so. */
async function confirmSave() {
  const changes = deltaPaths(S.base || {}, S.doc || {});
  if (changes.length <= 1) return save();
  const node = claimBanner('save');
  mount(node,
    el('strong', { text: `Save will change ${changes.length} settings.` }),
    el('p', { class: 'fatal-why' },
      'Everything below will be written to ',
      el('code', { text: S.paths.save }),
      '. Anything not listed is left exactly as the file has it.'),
    el('div', { class: 'difftable' },
      changes.slice(0, 40).map((row) => el('div', { class: 'diffrow' },
        el('span', { class: 'diff-id', text: row.path }),
        el('span', { class: 'diff-from', text: shortly(row.from) }),
        el('span', { class: 'diff-arrow', 'aria-hidden': 'true', text: '→' }),
        el('span', { class: 'diff-to', text: shortly(row.to) })))),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', type: 'button',
        onclick: () => { releaseBanner(); save(); },
      }, `Write these ${changes.length} changes`),
      el('button', {
        class: 'btn ghost', type: 'button',
        onclick: () => releaseBanner(),
      }, 'Cancel')));
}

async function save() {
  let res;
  try {
    res = await api('/api/config', {
      body: { document: S.doc, base: S.base, fingerprint: S.fingerprint },
    });
  } catch (err) {
    showBanner(String(err.message || err));
    toast('Not saved.', true);
    return;
  }
  if (res.conflict) { showConflict(res); return; }
  if (!res.ok) {
    showBanner(res.error);
    toast('Not saved - the config layer rejected it.', true);
    return;
  }
  S.lastWrite = { changed: res.changed || [], backup: res.backup,
                  merged: !!res.merged_from_disk };
  // Re-read and redraw: after a write, the page shows the file, not our idea
  // of it. Anything the config layer normalised on the way in shows up here.
  await loadConfig();
  showBanner((res.warnings || []).join('\n'), 'warn');
  renderAll();
  const count = (res.changed || []).length;
  toast(count === 1
    ? `Saved - 1 setting changed (${res.changed[0]}).`
    : `Saved - ${count} settings changed.`);
}

/* Somebody else moved the file while this page was open. Never resolved for
 * the user: both answers lose somebody's work, so both are offered by name. */
function showConflict(res) {
  const node = claimBanner('conflict');
  mount(node,
    el('strong', { text: 'Your config file changed while this page was open.' }),
    el('p', { class: 'fatal-why', text: res.error }),
    (res.conflicts || []).length
      ? el('p', { class: 'fatal-why',
                  text: 'Changed in both places: ' + res.conflicts.join(', ') })
      : null,
    el('div', { class: 'row' },
      el('button', {
        class: 'btn', type: 'button',
        onclick: async () => {
          await loadConfig();
          releaseBanner();
          renderAll();
          toast('Reloaded - your unsaved edits were discarded.');
        },
      }, 'Reload the file (lose my edits)'),
      el('button', {
        class: 'btn danger', type: 'button',
        onclick: async () => {
          // An explicit, named overwrite. The .bak still holds the file we are
          // about to replace, and the button says what it does.
          const forced = await api('/api/config', { body: { document: S.doc } });
          releaseBanner();
          if (!forced.ok) { showBanner(forced.error); return; }
          await loadConfig();
          renderAll();
          toast('Overwrote the file with what was on screen.');
        },
      }, 'Keep my edits (overwrite the file)')));
  toast('Not saved - the file changed underneath this page.', true);
}

/* ------------------------------------------------------------------ drag */

/* Drag one key onto another to swap what they do; hold Option to copy instead.
 * Costs no screen space, which is exactly right for a page we are shrinking - 
 * and it matches the physical act it mirrors, moving a keycap to another
 * switch. The same operation is in every key's Advanced panel as "Swap this
 * key with…", so it is never the only way to do it. */
function wireDrag() {
  const pad = $('#pad');
  pad.addEventListener('dragstart', (e) => {
    const key = e.target.closest('[data-key]');
    if (!key) return;
    S.dragFrom = key.dataset.key;
    key.classList.add('is-dragging');
    e.dataTransfer.effectAllowed = 'copyMove';
    e.dataTransfer.setData('text/plain', S.dragFrom);
  });
  pad.addEventListener('dragover', (e) => {
    const key = e.target.closest('[data-key]');
    if (!key || !S.dragFrom || key.dataset.key === S.dragFrom) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = e.altKey ? 'copy' : 'move';
    for (const other of pad.querySelectorAll('.is-target')) {
      other.classList.remove('is-target');
    }
    key.classList.add('is-target');
    key.dataset.dropHint = e.altKey ? 'copy' : 'swap';
  });
  pad.addEventListener('drop', (e) => {
    const key = e.target.closest('[data-key]');
    if (!key || !S.dragFrom) return;
    e.preventDefault();
    const from = S.dragFrom;
    S.dragFrom = null;
    if (key.dataset.key !== from) swapKeys(from, key.dataset.key, e.altKey);
  });
  pad.addEventListener('dragend', () => {
    S.dragFrom = null;
    for (const node of pad.querySelectorAll('.is-dragging, .is-target')) {
      node.classList.remove('is-dragging', 'is-target');
    }
  });
}

/* Everything that must survive a broken render lives here, and this runs
 * before the first fetch. Attaching listeners at the end of an async boot is
 * how a page ends up looking finished and doing nothing. */
function wire() {
  window.addEventListener('error', (e) => fatal('Script error', e.error || e.message));
  window.addEventListener('unhandledrejection',
    (e) => fatal('Request failed', e.reason));

  $('#pad').addEventListener('click', onPadClick);
  wireDrag();
  $('#save').addEventListener('click',
    () => { confirmSave().catch((e) => fatal('Save', e)); });
  $('#revert').addEventListener('click', async () => {
    await loadConfig();
    S.undo = null;
    S.applied = null;
    renderAll();
    toast('Reloaded from disk.');
  });
  $('#identify').addEventListener('click', () => {
    if (S.capture.on) stopCapture(); else startCapture();
  });
  $('#identify-stop').addEventListener('click', stopCapture);
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (e) => {
    if (e.target.dataset.close) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modalOpen() && !S.comboCapture) closeModal();
  });
  $('#advanced').addEventListener('toggle', renderAdvanced);
  window.addEventListener('beforeunload', (e) => {
    if (dirty()) { e.preventDefault(); e.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    // Hand the device back even if the tab is closed mid-capture.
    if (S.capture.on && navigator.sendBeacon) {
      navigator.sendBeacon('/api/capture/stop?token=' + encodeURIComponent(TOKEN));
    }
  });
}

async function boot() {
  try {
    S.schema = await api('/api/schema');
  } catch (err) {
    fatal('Loading the pad description', err);
    return;
  }
  // Before the first paint, and before anything is believed: does this server
  // still speak the same language as this page? See SCHEMA_CONTRACT.
  renderStale();
  try {
    if (!await loadConfig()) return;
  } catch (err) {
    fatal('Reading your config', err);
    return;
  }

  // Draw first, ask the hardware afterwards. The device probe shells out to
  // pgrep and walks IOKit; gating the first paint on it is how the page came
  // to look present and inert while it waited.
  renderAll();

  refreshDevice();
  refreshProjects().then(renderAdvanced);
  loadLayouts();
  loadApps();
  setInterval(() => { refreshDevice().then(autoListen); }, 3000);
  autoListen();
}

wire();
boot().catch((err) => fatal('Starting up', err));
