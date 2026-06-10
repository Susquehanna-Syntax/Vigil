// vigil-about.js
// Owns: About page — version surface, scanner configured-status pills.
// HTML: templates/pages/_about.html
// Depends on: vigil-utils.js (apiJson)
// API: GET /api/v1/about/

async function refreshAbout() {
  const btn = document.getElementById('about-refresh-btn');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }

  try {
    const d = await apiJson('/api/v1/about/');
    _setText('about-server-version', 'v' + (d.vigil_version || 'unknown'));
    _setText('about-agent-version', 'v' + (d.expected_agent_version || 'unknown'));
    _setText('about-python', d.python_version || '—');
    _setText('about-database', d.database || '—');
    _setText('about-timezone', d.timezone || 'UTC');
    _renderScanners(d.scanners || []);
  } catch (e) {
    // 401 only happens if a logged-out client somehow hits this; show a hint.
    const wrap = document.getElementById('about-scanners');
    if (wrap) {
      wrap.replaceChildren();
      const err = document.createElement('div');
      err.style.color = 'var(--text-3)';
      err.textContent = 'Failed to load: ' + e.message;
      wrap.appendChild(err);
    }
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
  }
}

function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _renderScanners(scanners) {
  const wrap = document.getElementById('about-scanners');
  if (!wrap) return;
  wrap.replaceChildren();
  if (!scanners.length) {
    const empty = document.createElement('div');
    empty.style.color = 'var(--text-3)';
    empty.textContent = 'No scanners registered.';
    wrap.appendChild(empty);
    return;
  }
  for (const s of scanners) {
    const row = document.createElement('div');
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.justifyContent = 'space-between';
    row.style.padding = '8px 0';
    row.style.borderBottom = '1px solid var(--s3)';

    const name = document.createElement('span');
    name.className = 'mono';
    name.style.color = 'var(--text-1)';
    name.style.fontWeight = '600';
    name.textContent = s.name;
    row.appendChild(name);

    const pill = document.createElement('span');
    pill.style.fontSize = '11px';
    pill.style.fontWeight = '600';
    pill.style.padding = '3px 10px';
    pill.style.borderRadius = '10px';
    pill.style.letterSpacing = '0.4px';
    pill.style.textTransform = 'uppercase';
    if (s.configured) {
      pill.style.color = 'var(--mint)';
      pill.style.background = 'rgba(126,221,181,0.10)';
      pill.textContent = 'Configured';
    } else {
      pill.style.color = 'var(--text-3)';
      pill.style.background = 'var(--s2)';
      pill.textContent = 'Not configured';
    }
    row.appendChild(pill);
    wrap.appendChild(row);
  }
}

// Wire the sidebar nav click → load when the page becomes active.
document.querySelectorAll('[data-page="about"]').forEach(el => {
  el.addEventListener('click', refreshAbout);
});

// Also load if the About page is the initial active page on cold load.
if (document.getElementById('page-about')?.classList.contains('active')) {
  refreshAbout();
}
