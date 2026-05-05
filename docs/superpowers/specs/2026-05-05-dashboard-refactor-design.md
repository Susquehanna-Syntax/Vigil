# Dashboard Refactor — Design Spec
**Date:** 2026-05-05
**Status:** Approved

## Goal

Break up `server/templates/dashboard.html` (5,576 lines) into maintainable, correctly-typed files. Introduce `base.html` for Django template inheritance, move CSS to a static file, and split JavaScript into feature-scoped modules. No features added or removed — this is purely a structural refactor.

---

## File Structure

```
server/
├── static/
│   ├── css/
│   │   └── vigil.css               ← all CSS, moved verbatim from dashboard.html <style> block
│   └── js/
│       ├── vigil-utils.js          ← getCsrf, apiPost, apiJson, showToast, escHtml (~60 lines)
│       ├── vigil-nav.js            ← navigateTo, tab switching (~40 lines)
│       ├── vigil-host-cards.js     ← refreshHostCards, pins, filterHostCards, RDP (~200 lines)
│       ├── vigil-monitor.js        ← charts, auto-refresh, metric fetching (~400 lines)
│       ├── vigil-alerts.js         ← acknowledgeAlert, tab logic (~60 lines)
│       ├── vigil-tasks.js          ← task library, editor, YAML highlight, community (~500 lines)
│       ├── vigil-deploy.js         ← deploy modal, fleet cache, policy, inputs (~400 lines)
│       ├── vigil-inventory.js      ← inventory table, columns, AD sync, CSV export (~350 lines)
│       ├── vigil-vulns.js          ← refreshVulns, renderVulns (~80 lines)
│       └── vigil-settings.js       ← TOTP setup/disable, settings page (~100 lines)
│
└── templates/
    ├── base.html                   ← <!DOCTYPE>, head, fonts, CDN scripts, sidebar, blocks
    ├── dashboard.html              ← {% extends "base.html" %}, ~30 lines of {% include %} calls
    ├── _host_card.html             ← unchanged (already a partial)
    └── pages/
        ├── _dashboard.html         ← <section id="page-dashboard">
        ├── _alerts.html            ← <section id="page-alerts">
        ├── _tasks.html             ← <section id="page-tasks">
        ├── _community.html         ← <section id="page-community">
        ├── _monitor.html           ← <section id="page-monitor">
        ├── _settings.html          ← <section id="page-settings">
        ├── _vulns.html             ← <section id="page-vulns">
        ├── _inventory.html         ← <section id="page-inventory">
        └── _task_editor.html       ← <section id="page-task-editor">
```

`dashboard.html` reduces from 5,576 lines to ~30 lines.

---

## `base.html` Structure

```html
{% load static tz %}
<!DOCTYPE html>
<html lang="en">
<head>
  <!-- meta, title, fonts, Chart.js CDN -->

  <!-- Styles: static/css/vigil.css — all design tokens, layout, and component styles -->
  <link rel="stylesheet" href="{% static 'css/vigil.css' %}">
</head>
<body>
  <!-- sidebar HTML (unchanged) -->

  {% block content %}{% endblock %}

  {# Bridge: passes server-rendered config to JS. See: vigil/urls.py dashboard() view. #}
  {# Security: only non-secret, already-rendered values. No keys or user-specific data. #}
  {# CSP note: this inline script is the one blocker to a strict script-src policy. #}
  {# When adding CSP, assign a nonce here and add it to all static script tags too.  #}
  {% block config %}
  <script>
    window.VIGIL_CONFIG = {
      timezone: '{{ vigil_timezone|escapejs }}',
      timeFormat: '{{ vigil_time_format|escapejs }}',
    };
  </script>
  {% endblock %}

  <!-- vigil-utils.js — getCsrf, apiPost/apiJson, showToast, escHtml. Load first: all other scripts depend on this. -->
  <script src="{% static 'js/vigil-utils.js' %}"></script>
  <!-- vigil-nav.js — navigateTo(), tab switching -->
  <script src="{% static 'js/vigil-nav.js' %}"></script>
  <!-- vigil-host-cards.js — dashboard host cards, pin bar, RDP, host filtering -->
  <script src="{% static 'js/vigil-host-cards.js' %}"></script>
  <!-- vigil-monitor.js — Monitor page charts, metric fetching, auto-refresh -->
  <script src="{% static 'js/vigil-monitor.js' %}"></script>
  <!-- vigil-alerts.js — alert acknowledge/silence, firing/ack tab logic -->
  <script src="{% static 'js/vigil-alerts.js' %}"></script>
  <!-- vigil-tasks.js — task library, editor, YAML highlighting, community fork -->
  <script src="{% static 'js/vigil-tasks.js' %}"></script>
  <!-- vigil-deploy.js — deploy modal, fleet cache, schedule/retry/success-criteria policy -->
  <script src="{% static 'js/vigil-deploy.js' %}"></script>
  <!-- vigil-inventory.js — inventory table, column editor, AD sync, CSV export -->
  <script src="{% static 'js/vigil-inventory.js' %}"></script>
  <!-- vigil-vulns.js — vulnerability scan refresh and render -->
  <script src="{% static 'js/vigil-vulns.js' %}"></script>
  <!-- vigil-settings.js — TOTP setup/disable, settings page -->
  <script src="{% static 'js/vigil-settings.js' %}"></script>
</body>
</html>
```

---

## Comment Conventions

**Rule: every cross-file reference is annotated at the point of reference.**

### In `base.html`
One-line comment before each `<link>` and `<script>` stating what the file owns.

### In `dashboard.html`
Each `{% include %}` annotated with what JS drives it and where data comes from:
```html
{# Dashboard page — host cards, pins, activity strip #}
{# Logic: static/js/vigil-host-cards.js | Data: active_hosts/inactive_hosts from dashboard() view #}
{% include "pages/_dashboard.html" %}
```

### In each JS file — file header:
```js
// vigil-monitor.js
// Owns: Monitor page (templates/pages/_monitor.html)
// Depends on: vigil-utils.js (apiJson, escHtml)
// API: GET /api/v1/metrics/{host}/{metric}/
```

### In each page partial — mirror comment:
```html
{# Monitor page section #}
{# Logic: static/js/vigil-monitor.js | Styles: vigil.css .monitor-* #}
<section id="page-monitor" class="page">
```

---

## Whitenoise Setup

Whitenoise allows gunicorn to serve static files correctly — no nginx required.

**`requirements.txt`** — add:
```
whitenoise==6.9.0
```

**`settings.py`** — two changes:
```python
# In MIDDLEWARE, insert after SecurityMiddleware:
"whitenoise.middleware.WhiteNoiseMiddleware",

# Add STATICFILES_DIRS so Django finds server/static/:
STATICFILES_DIRS = [BASE_DIR / "static"]
```

**`Dockerfile`** — remove the `|| true` suppressor from collectstatic. Silent failures during builds are a security and reliability risk:
```dockerfile
# Before:
RUN python manage.py collectstatic --noinput || true
# After:
RUN python manage.py collectstatic --noinput
```

---

## Security Constraints

- **`window.VIGIL_CONFIG`** contains only `vigil_timezone` and `vigil_time_format` — non-secret, already rendered to the page by Django. No credentials, no user-specific tokens, no keys.
- **CSRF token** is obtained via `getCsrf()` reading from the DOM cookie (`csrftoken`), not from `VIGIL_CONFIG`. This preserves Django's standard CSRF handling.
- **`|escapejs` filter** applied to every string value injected into a JS context. No raw Django vars in JS.
- **Static files are public** — `server/static/` must contain only CSS, JS, and public assets. No env-derived values, secrets, or per-user content.
- **`collectstatic` failure surfaces** — removing `|| true` means a build with broken static files fails fast rather than deploying silently broken.
- **Future CSP path** — the single inline `<script>` block for `VIGIL_CONFIG` is the only blocker to a strict `script-src` Content Security Policy. When CSP is added: generate a nonce server-side, inject it into the `{% block config %}` script tag and all `<script src>` tags. This is explicitly called out in `base.html` comments so it isn't forgotten.

---

## What Does Not Change

- All HTML structure, class names, IDs, and data attributes — untouched
- All JavaScript logic — moved verbatim, no rewrites
- All CSS — moved verbatim, no rewrites
- The `dashboard()` view in `vigil/urls.py` — no changes
- `_host_card.html` — already a partial, stays as-is
- The SPA tab-switching behavior — `navigateTo()` continues to work identically

---

## Out of Scope

- Rewriting or refactoring any JS logic
- Changing CSS
- Adding new features
- Changing the URL structure or Django views
- Adding a JS module system (ES modules, bundler)
