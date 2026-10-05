# Vigil UI standards

Vigil's interface follows the SQSY design language. Its tokens live at the top
of `server/static/css/vigil.css`, and the class names match the design
language, so markup copies straight across. This page covers what to reach
for, and what not to do, when you add a page, dialog or widget.

## Principles

- **Flat, not boring.** No gradients and no drop shadows on surfaces. Depth
  comes from the surface steps (`--bg`, `--s1` … `--s4`) and from pastel fills.
- **Tint the whole thing.** A card, banner or row that carries a colour is
  tinted all over (`rgba(var(--rgb-mint), var(--tint-a))`). Never put a
  coloured stripe or border on one side of it.
- **Meaning survives themes.** Mint is healthy, rose is critical, lemon is a
  warning, in every theme.
- **Motion is meaning.** Every animation has a reason, and reduced motion is
  respected everywhere.

## Themes

There are four themes, set on `<html data-theme>`: `dark` (the default),
`light` (Paper, a warm #ede8bb ground), `river` (navy) and `ink` (a warm
near-black, still experimental). There is also a System option that switches
between Dark and Paper with the OS. The choice lives in `localStorage`
(`vigil-theme`). It is applied before first paint by the inline script in
`base.html`, and changed through `setTheme()` in `vigil-utils.js`. The wiki
reads the same key.

A theme only redefines tokens. If a component needs a different rule per
theme, it is missing a token: add the token to every theme block instead.

## Tokens to use

| Need | Token |
|---|---|
| Page, card and recess surfaces | `--bg`, `--s1`, `--s2`, `--s3`, `--s4`, `--border` |
| Text | `--text-1` (primary), `--text-2`, `--text-3` (muted) |
| A pastel as a fill (button, chip background, dot) | `--mint`, `--rose`, … |
| A pastel as **text, an icon stroke or a thin line** | `--mint-ink`, `--rose-ink`, … |
| A tinted surface | `rgba(var(--rgb-<pastel>), var(--tint-a))`; `--tint-a-line` for its border, `--tint-a-hover` on hover |
| Text on a filled pastel | `--on-accent` |
| Hover wash, hairline, glass, scrim | `--wash`, `--grid-line`, `--glass` / `--glass-edge`, `--scrim` |
| Run states and compared series in charts | `--st-ok`, `--st-failed`, `--st-pending`, `--st-na`, `--st-skipped`, then `--oc-1` … `--oc-4` |
| Radii | `--r-sm` 10, `--r-md` 14, `--r-lg` 20, `--r-xl` 24 |
| Curves | `--ease-out-expo` for things that move, `--ease-out-back` for things that land, `--spring` for things that are squeezed |

Never hard-code a hex value or an `rgba()` literal for any of these. On Paper,
a pastel used as text is unreadable; that is why the `-ink` tokens exist. On
the dark themes they are the pastel itself.

### Charts

Chart.js draws on a canvas, and a canvas cannot resolve `var(--x)`. Pass
`tok('--s3')` (from `vigil-utils.js`) for grids, ticks and tooltips. It reads
the token on every draw, and every chart redraws on the `vigil:theme` event.
A pastel dataset colour can stay a literal, because pastels are the same in
every theme.

## Dialogs and drawers

Write a dialog as `.modal-overlay` plus `.modal`, both toggled with `.open`
(or build it with `mountModal()`). A side panel is `.detail-panel`.
`vigil-modal.js` watches the `open` class and gives every dialog the same
behaviour, so do not reimplement any of it:

- focus moves into the dialog when it opens, and returns to the opener when it closes;
- Tab stays inside the dialog, and the page behind it is inert;
- Esc closes the top dialog only, through the module's own close function.
  It finds `close<Name>` from the element id (`wave-editor-modal` →
  `closeWaveEditor`), or the scrim's `data-act`, or the close button;
- a dialog opened over another makes the lower one recede, and its scrim only dims.

Pick the variant by use:

| Use | Variant |
|---|---|
| Forms, editors, the deploy dialog, read-mostly detail | Rise — plain `.modal` (the default) |
| Confirm, prompt, small quick decisions | Pop — `.m-pop` |
| A picker; a long list on a phone | `.m-pop .m-sheet-sm` (pops on a desktop, becomes a bottom sheet under 600px) |
| Destructive type-to-confirm | `shakeEl(input)` when the typed text does not match |
| Host detail kept beside the list | `.detail-panel` (drawer) |

Motion timing comes from `--m-dur` (300ms), `--m-ease` and `--m-blur`.
Leaving is 0.7× as long as arriving.

## Components

- **Buttons:** use `.btn` with `.btn-mint` (primary or confirm), `.btn-rose`
  (destructive), `.btn-sky`, `.btn-lav`, `.btn-peach` or `.btn-lemon`; or
  `.btn-outline` / `.btn-ghost`. Sizes are `.btn-sm` and `.btn-xs`.
- **Chips:** use `.chip` (a tinted status tag) or `.chip-muted`.
- **Tabs:** use `.tab-bar` holding `.tab` elements with `data-tab`. The bar
  scrolls sideways on a phone; it never wraps.
- **Tables:** use `.hunt-table` inside `.table-wrap`, with numbers in `.num` cells.
- **Forms:** use `.form-group`, `.form-label` and `.form-control`.
- **Settings rows:** use `.set-row` with a `.toggle` switch
  (`role="switch"`, `aria-checked`).
- **Empty states:** use muted `--text-3` text in the space the content would
  fill, and name the next step.
- **Polling:** use `pollingInterval()` and `clearPollingInterval()`, never
  bare `setInterval`. A test enforces this.
- **Event handlers:** no inline handlers. Use `data-act="fnName"` (with
  `data-a1` … for arguments) or `addEventListener`, so the CSP never needs
  `'unsafe-inline'`.

## Checking a change

Look at the page in all four themes at 1440 and 420 pixels wide: Settings →
Display, or the theme icon in the sidebar. There should be no horizontal
scroll, and every word should be readable on Paper. For a dialog, also check:
focus moves in, Tab wraps, Esc closes only the top one, a scrim click closes
it, and focus returns to the button that opened it.
