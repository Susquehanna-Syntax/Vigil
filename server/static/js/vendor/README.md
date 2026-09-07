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
