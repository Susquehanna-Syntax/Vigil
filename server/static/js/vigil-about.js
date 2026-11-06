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
    _setText('about-licensed-to', d.licensed_to
      ? `${d.licensed_to}${d.license_id ? ` (license ${d.license_id})` : ''}`
      : 'Free edition');
    _renderScanners(d.scanners || []);
    _renderHealth();
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
      pill.style.color = 'var(--mint-ink)';
      pill.style.background = 'rgba(var(--rgb-mint), var(--tint-a))';
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

async function _renderHealth() {
  const box = document.getElementById('about-health');
  const body = document.getElementById('about-health-body');
  if (!box || !body) return;
  let h;
  try {
    h = await apiJson('/api/v1/instance-health/');
  } catch (e) {
    box.hidden = true; // not an instance admin
    return;
  }
  const rows = [];
  const hosts = h.hosts || {};
  rows.push(['Hosts', `${hosts.online || 0} online, ${hosts.offline || 0} offline, ${hosts.pending || 0} waiting for approval`]);
  rows.push(['Checked in, last 5 minutes', String((h.checked_in || {})['5m'] || 0)]);
  rows.push(['Remote sessions open', String(h.remote_sessions_open || 0)]);
  const tasks = h.tasks || {};
  rows.push(['Tasks waiting', String((tasks.pending || 0) + (tasks.dispatched || 0))]);
  rows.push(['Job queue', h.celery_broker_up ? `${h.celery_queue_depth || 0} waiting` : 'broker not answering']);
  if (h.database_bytes != null) rows.push(['Database size', `${(h.database_bytes / 1073741824).toFixed(2)} GB`]);
  const sk = h.signing_key || {};
  rows.push(['Signing key', (sk.active || '').slice(0, 16) + '...']);
  if (sk.next) rows.push(['Key rotation', `next key acknowledged by ${sk.acknowledged} of ${sk.hosts} hosts`]);
  body.replaceChildren();
  rows.forEach(([label, value]) => {
    const l = document.createElement('div');
    l.style.color = 'var(--text-3)';
    l.textContent = label;
    const v = document.createElement('div');
    v.textContent = value;
    body.append(l, v);
  });
  box.hidden = false;
}

// Wire the sidebar nav click → load when the page becomes active.
document.querySelectorAll('[data-page="about"]').forEach(el => {
  el.addEventListener('click', refreshAbout);
});


// Also load if the About page is the initial active page on cold load.
if (document.getElementById('page-about')?.classList.contains('active')) {
  refreshAbout();
}
