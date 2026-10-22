# Vendored third-party assets

Checked in rather than pulled from a CDN at runtime. Vigil has no build step,
and a self-hosted install has to work with no route to the internet — an
air-gapped deployment is a supported deployment, so a CDN `<script src>` is
not an option.

## gridstack.js

| | |
|---|---|
| Version | 12.4.0 |
| Licence | MIT (`gridstack.LICENSE`) |
| Upstream | https://github.com/gridstack/gridstack.js |
| Fetched from | https://cdnjs.cloudflare.com/ajax/libs/gridstack.js/12.4.0/ |
| `gridstack-all.js` | sha256 `6e3032a6906805e43ca7e2dc818989b170bb65b686a6d0e89e6b68ad61f5946f` |
| `../css/vendor/gridstack.min.css` | sha256 `55e9d4ea6d8c6f8f1ea8a449b8af18d8571487c0afc6b433cccf877047cb8457` |

The UMD build, which defines the `GridStack` global. Drives the widget grid on
the dashboard: drag, resize, collision and the serialisable layout the
`/api/v1/dashboards/<id>/layout/` endpoint stores.

To upgrade: fetch the same two files at the new version, replace them, update
the version and both digests above, and re-run the dashboard tests.

## chart.js

| | |
|---|---|
| Version | 4.5.1 (was `@4`, a floating major) |
| Licence | MIT (`chart.js.LICENSE`) |
| Upstream | https://github.com/chartjs/Chart.js |
| `chart.umd.min.js` | sha256 `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| `chartjs-adapter-date-fns.bundle.min.js` | sha256 `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` (adapter 3.0.0, MIT) |

Drives every time-series chart: the monitor page, the host detail drawer, and
the dashboard's metric-chart widget. Was loaded from jsdelivr at a floating
major version, which meant an air-gapped install had no charts at all and a
CDN-side release could change the dashboard with no change here.

## xterm.js

| | |
|---|---|
| Version | 5.5.0 (`@xterm/xterm`), fit addon 0.10.0 (`@xterm/addon-fit`) |
| Licence | MIT (`xterm.LICENSE`) |
| Upstream | https://github.com/xtermjs/xterm.js |
| Fetched from | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/ and `@xterm/addon-fit@0.10.0/` |
| `xterm.js` | sha256 `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| `xterm-addon-fit.js` | sha256 `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089` |
| `../css/vendor/xterm.css` | sha256 `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |

The UMD builds, which define the `Terminal` and `FitAddon` globals. Draw the
web shell and container shell (2026.15.0). Loaded only on the pages that open
a session, not on every page.

## guacamole-common-js

| | |
|---|---|
| Version | 1.5.0 (protocol-compatible with guacd 1.5.x) |
| Licence | Apache-2.0 (`guacamole-common.LICENSE`) |
| Upstream | https://github.com/apache/guacamole-client (`guacamole-common-js`) |
| Fetched from | https://cdn.jsdelivr.net/npm/guacamole-common-js@1.5.0/dist/cjs/guacamole-common.js |
| Packaged by | the npm package is a repackaging of Apache's source by github.com/padarom/guacamole-common-js |
| as fetched | sha256 `ec60dc01a730a9767a0a9b7c22787e23073b9e984c5748a5f6fea6c630f72d5e` |
| `guacamole-common.js` | sha256 `f13e6dc27f25610ca3099a0499837ad261c19ec3f084ef8de6f2b407cb95440b` |

The fetched file ends with `module.exports = Guacamole;`; that one line is
removed so the file defines the `Guacamole` global in a browser. Nothing else
is changed. Draws Web RDP (2026.15.0); loaded on demand, like xterm.js.
