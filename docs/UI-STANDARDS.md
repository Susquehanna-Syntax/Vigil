# Vigil UI standards

Vigil's interface follows the SQSY design language. Its tokens live in
`server/static/css/tokens.css`, the styles in the numbered parts under
`server/static/css/vigil/`, and the class names match the design
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

Admins can open `/ui-kit/` to see every shared component in the current theme,
with a switch for the other three. Check a change to a shared component there in
all four themes before checking it on a page that happens to use it. The page is
admin-only (404 for anyone else), so its screenshots never leave the instance.

Look at the page in all four themes at 1440 and 420 pixels wide: Settings →
Display, or the theme icon in the sidebar. There should be no horizontal
scroll, and every word should be readable on Paper. For a dialog, also check:
focus moves in, Tab wraps, Esc closes only the top one, a scrim click closes
it, and focus returns to the button that opened it.

## Update Log

### Update — 2026-11-17 15:40 (progress)

**Phase 06b started.** Executing as executor: the `/ui-kit/` view and route, the standalone `ui_kit.html`, the new
`vigil-ui-kit.js`, the `15-ui-kit.css` part linked last in `_stylesheets.html`, the paragraph under "Checking a change",
and `server/vigil/test_ui_kit.py`.

### Update — 2026-11-17 16:50 (end-to-end verification)

**Verification, all commands, output captured.**

```
$ env -C server USE_SQLITE=1 DJANGO_SECRET_KEY=test VIGIL_SIGNING_KEY_SEED=<seed> nice -n 19 .venv/bin/python manage.py test vigil.test_ui_kit vigil.test_stylesheets vigil.test_site_isolation apps.hosts.test_template_comments --parallel 1
Ran 24 tests in 2.461s

OK
Destroying test database for alias 'default'...
Found 24 test(s).
System check identified no issues (0 silenced).
```

`systemd-run --user --scope` cannot run on this executor host (`Failed to
connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and
$XDG_RUNTIME_DIR not defined`), so the Django command ran with the same env and
`nice -n 19` but without the memory-scope wrapper.

```
$ env -C server npx --yes -p typescript@5.6.3 tsc -p tsconfig.json
(no output; exit 0)

$ env -C server npx --yes -p stylelint@16.10.0 stylelint "static/css/**/*.css" --formatter compact
(no output; exit 0)

$ env -C server node --test jstests/
# tests 34
# pass 34
# fail 0

$ env -C server USE_SQLITE=1 DJANGO_SECRET_KEY=test VIGIL_SIGNING_KEY_SEED=<seed> .venv/bin/python manage.py check
System check identified no issues (0 silenced).
```

(The `FILE_UPLOAD_TEMP_DIR '/var/lib/vigil/images/tmp-uploads' is not writable`
line that precedes every Django run on this host is pre-existing environment
noise, unrelated to this phase.)

### Spec notes within this phase

- **Task 6 (tests).** `server/vigil/test_ui_kit.py`: admin gets 200 and every
  section id; operator and viewer get 404; anonymous redirects to `/login/`; the
  template has no `<script>` without `src`, no ` style="` attribute, no
  ` on[a-z]+="` attribute; every `data-act` names a function declared in
  `vigil-ui-kit.js`, `vigil-utils.js` or `vigil-modal.js`; and the page includes
  `_stylesheets.html`. The spec named `apps/hosts/test_device_check_snmp.py` for
  the user/profile fixture and that file does not exist here, so it follows
  `apps/accounts/test_capabilities.py` (`create_user` +
  `UserProfile.objects.create(role=...)`).
- **Task 7 (end-to-end).** `test_an_admin_sees_every_section` is a request
  through the project's own URLconf as an admin and asserts against the response
  body, not a fake: all eleven `id="kit-..."`, the four button rows with all
  eight colours in each, both `.toggle` switches (one `aria-checked="true"`, one
  `"false"`), the three dialog ids `kit-rise-modal` / `kit-pop-modal` /
  `kit-sheet-modal`, and the four scripts in dependency order.
- **Two files outside the acceptance list**, both mechanical:
  `server/tsconfig.json` lists `static/js/vigil-ui-kit.js` (CI type-checks only
  the listed scripts, and the spec requires this one checked), and the new
  `ui-kit/` route is allowlisted in `test_site_isolation.py`, which is how that
  file declares a page it must not probe.
