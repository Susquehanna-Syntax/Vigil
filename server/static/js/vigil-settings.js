// vigil-settings.js
// Owns: Settings page — TOTP enrollment / disable, agent binary upload.
//   (AD config lives in vigil-inventory.js since it imports Hosts.)
// HTML: templates/pages/_settings.html
// Depends on: vigil-utils.js (apiJson, showToast, getCsrf)
// API: GET/POST /api/v1/accounts/totp/{,enroll/,enroll/confirm/,disable/}
//      GET /agent/info/  POST /agent/upload/<platform>/

/* ── TOTP enrollment ── */
async function refreshTotpStatus() {
  const status = document.getElementById('totp-status-line');
  if (!status) return;
  // The security keys card sits right under this one and opens with it.
  loadSecurityKeys();
  try {
    const data = await apiJson('/api/v1/accounts/totp/');
    const sub = document.getElementById('totp-sub-line');
    const startBtn = document.getElementById('totp-start-btn');
    const disableBtn = document.getElementById('totp-disable-btn');
    const enrollView = document.getElementById('totp-enroll-view');
    if (data.enrolled) {
      status.textContent = 'TOTP enabled';
      status.className = 'totp-status enabled';
      sub.textContent = 'Your account is protected. Task deploys require a 6-digit code.';
      startBtn.style.display = 'none';
      disableBtn.style.display = '';
      enrollView.style.display = 'none';
    } else if (data.pending) {
      status.textContent = 'Enrollment pending';
      status.className = 'totp-status pending';
      sub.textContent = 'Scan the secret into an authenticator app, then enter a code to finish.';
      startBtn.style.display = 'none';
      disableBtn.style.display = 'none';
    } else {
      status.textContent = 'TOTP not enabled';
      status.className = 'totp-status disabled';
      sub.textContent = 'Password fallback is test-only and will be removed. Enroll TOTP to execute tasks.';
      startBtn.style.display = '';
      disableBtn.style.display = 'none';
      enrollView.style.display = 'none';
    }
  } catch (e) { /* settings may not be open */ }
}

async function totpStart() {
  try {
    const data = await apiJson('/api/v1/accounts/totp/enroll/', { method: 'POST' });
    document.getElementById('totp-secret-text').textContent = data.secret;
    document.getElementById('totp-uri-text').textContent = data.otpauth_uri;
    document.getElementById('totp-enroll-view').style.display = 'block';
    document.getElementById('totp-verify-code').value = '';
    document.getElementById('totp-verify-code').focus();
    refreshTotpStatus();
  } catch (e) {
    showToast('TOTP enroll failed: ' + e.message, 'error');
  }
}

async function totpConfirm() {
  const code = document.getElementById('totp-verify-code').value.trim();
  if (!/^\d{6}$/.test(code)) {
    showToast('Enter a 6-digit code', 'error');
    return;
  }
  try {
    await apiJson('/api/v1/accounts/totp/enroll/confirm/', {
      method: 'POST', body: JSON.stringify({ code }),
    });
    showToast('TOTP enabled', 'success');
    refreshTotpStatus();
  } catch (e) {
    showToast('Verify failed: ' + e.message, 'error');
  }
}

async function totpDisablePrompt() {
  const code = await totpPrompt('Turning off two-factor authentication. Enter a current code to confirm.',
    { title: 'Disable TOTP', confirmText: 'Disable' });
  if (!code) return;
  try {
    await apiJson('/api/v1/accounts/totp/disable/', {
      method: 'POST', body: JSON.stringify({ code: code.trim() }),
    });
    showToast('TOTP disabled', 'success');
    refreshTotpStatus();
  } catch (e) {
    showToast('Disable failed: ' + e.message, 'error');
  }
}

/* ── Agent binary distribution ── */

async function loadAgentInfo() {
  const el = document.getElementById('agent-versions');
  if (!el) return;
  try {
    const data = await apiJson('/agent/info/');
    el.textContent = '';
    if (!data.length) { el.textContent = 'No binaries uploaded yet.'; return; }
    data.forEach((b, i) => {
      if (i > 0) el.appendChild(document.createTextNode('  ·  '));
      const label = document.createElement('span');
      label.style.color = 'var(--text-2)';
      label.textContent = b.platform_label;
      el.appendChild(label);
      el.appendChild(document.createTextNode(' '));
      const ver = document.createElement('span');
      ver.className = 'mono';
      ver.style.cssText = 'color:var(--sky-ink);font-size:11px;';
      ver.textContent = `v${b.version || '?'}`;
      el.appendChild(ver);
      el.appendChild(document.createTextNode(' '));
      const hash = document.createElement('span');
      hash.style.cssText = 'color:var(--text-3);font-size:11px;';
      hash.textContent = `(sha256: ${b.sha256.slice(0, 12)}…)`;
      el.appendChild(hash);
    });
  } catch (_) {}
}

function copyInstallCmd() {
  const cmd = `curl -fsSL ${window.location.origin}/agent/install.sh | sudo bash`;
  copyText(cmd).then((ok) => showToast(ok ? 'Copied' : 'Copy failed',
                                       ok ? 'success' : 'error'));
}

function copyInstallCmdPs1() {
  const cmd = `irm ${window.location.origin}/agent/install.ps1 | iex`;
  copyText(cmd).then((ok) => showToast(ok ? 'Copied' : 'Copy failed',
                                       ok ? 'success' : 'error'));
}

async function uploadAgentBinary() {
  const platform = document.getElementById('upload-platform').value;
  const version = (document.getElementById('upload-version').value || '').trim();
  const fileInput = document.getElementById('upload-binary');
  const file = fileInput.files[0];
  if (!file) { showToast('Select a binary file first', 'error'); return; }

  const formData = new FormData();
  formData.append('binary', file);
  if (version) formData.append('version', version);

  try {
    const resp = await fetch(`/agent/upload/${encodeURIComponent(platform)}/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': getCsrf() },
      body: formData,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Upload failed');
    showToast(`Uploaded ${platform}${version ? ' v' + version : ''}`, 'success');
    fileInput.value = '';
    loadAgentInfo();
  } catch (e) {
    showToast('Upload failed: ' + e.message, 'error');
  }
}



/* ── Civil SSO settings ──────────────────────────────────────────────── */
async function loadCivilSettings() {
  const card = document.getElementById('civil-settings-card');
  if (!card) return;
  try {
    const d = await apiJson('/api/v1/civil/settings/');
    document.getElementById('civil-url').value = d.url || '';
    document.getElementById('civil-slug').value = d.app_slug || '';
    document.getElementById('civil-enabled').checked = !!d.enabled;
    document.getElementById('civil-env-note').hidden = !d.env_override;
    _renderCivilState(d);
  } catch (e) { /* leave blank */ }
}

function _renderCivilState(d) {
  const el = document.getElementById('civil-state');
  if (!el) return;
  if (!d.active) { el.textContent = 'Civil sign-in is off — using local accounts only.'; return; }
  el.textContent = `Active against ${d.effective_url} · verification key ${d.key_cached ? 'cached' : 'not yet fetched'}.`;
}

async function saveCivilSettings(opts) {
  const body = {
    url: document.getElementById('civil-url').value.trim(),
    app_slug: document.getElementById('civil-slug').value.trim(),
    enabled: document.getElementById('civil-enabled').checked,
    ...(opts || {}),
  };
  try {
    const d = await apiJson('/api/v1/civil/settings/', { method: 'POST', body: JSON.stringify(body) });
    _renderCivilState(d);
    document.getElementById('civil-env-note').hidden = !d.env_override;
    if (opts && opts.test) showToast(d.test_ok ? 'Reached Civil and fetched its key' : 'Could not reach Civil', d.test_ok ? 'success' : 'error');
    else showToast('Civil settings saved', 'success');
  } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
}


/* ── Transport security ────────────────────────────────────────────────────
   A standing acknowledgement that this instance is served over plain HTTP.
   Made once here by a named admin rather than ticked inside every rebuild,
   where it read as another box to clear rather than a decision. */
async function refreshTransportAck() {
  const status = document.getElementById('transport-status-line');
  if (!status) return;
  const sub = document.getElementById('transport-sub-line');
  const ackBtn = document.getElementById('transport-ack-btn');
  const revokeBtn = document.getElementById('transport-revoke-btn');
  try {
    const data = await apiJson('/api/v1/hosts/transport-ack/');
    const secure = data.request_is_secure;
    if (secure) {
      status.textContent = 'Served over HTTPS';
      status.className = 'totp-status enabled';
      sub.textContent = 'Secrets are encrypted in transit. No acknowledgement is needed.';
      ackBtn.style.display = 'none';
      // Still offer to withdraw a stale one — an instance that moved to TLS
      // should not keep an acknowledgement that would apply if it moved back.
      revokeBtn.style.display = data.acknowledged ? '' : 'none';
      if (data.acknowledged) {
        sub.textContent += ' A plain-text acknowledgement is still on record'
          + (data.acknowledged_by ? ` from ${data.acknowledged_by}` : '') + '.';
      }
      return;
    }
    if (data.acknowledged) {
      status.textContent = 'Plain-text transport acknowledged';
      status.className = 'totp-status pending';
      sub.textContent = 'Rebuilds proceed without asking again. Acknowledged'
        + (data.acknowledged_by ? ` by ${data.acknowledged_by}` : '')
        + (data.acknowledged_at
            ? ` on ${new Date(data.acknowledged_at).toLocaleString()}` : '')
        + '. Put Vigil behind TLS and withdraw this.';
      ackBtn.style.display = 'none';
      revokeBtn.style.display = '';
    } else {
      status.textContent = 'Served over plain HTTP';
      status.className = 'totp-status disabled';
      sub.textContent = 'A rebuild answer file carries an admin password hash, '
        + 'SSH keys and an enrolment token, and they cross the network in the '
        + 'clear. Rebuilds ask for an acknowledgement every time until one is '
        + 'recorded here.';
      ackBtn.style.display = '';
      revokeBtn.style.display = 'none';
    }
  } catch (e) {
    // Not an admin, or the pane is not open. Say nothing rather than alarm.
    status.textContent = '';
    if (sub) sub.textContent = '';
    if (ackBtn) ackBtn.style.display = 'none';
    if (revokeBtn) revokeBtn.style.display = 'none';
  }
}

async function transportAckSet(acknowledged) {
  if (acknowledged && typeof confirmModal === 'function') {
    const ok = await confirmModal(
      'Vigil will stop asking during rebuilds. The answer file\u2019s admin '
      + 'password hash, SSH keys and enrolment token will keep crossing the '
      + 'network unencrypted until Vigil is served over HTTPS. This is '
      + 'recorded against your account.',
      { title: 'Acknowledge plain-text transport',
        confirmText: 'I understand', danger: true });
    if (!ok) return;
  }
  try {
    await apiJson('/api/v1/hosts/transport-ack/', {
      method: 'POST',
      body: JSON.stringify({ acknowledged: !!acknowledged }),
    });
    showToast(acknowledged ? 'Plain-text transport acknowledged'
                           : 'Acknowledgement withdrawn', 'success');
  } catch (e) {
    showToast(e.message || 'Could not save that', 'error');
  }
  refreshTransportAck();
}

document.addEventListener('DOMContentLoaded', () => {
  const cs = document.getElementById('civil-save-btn');
  if (cs) cs.addEventListener('click', () => saveCivilSettings());
  const ct = document.getElementById('civil-test-btn');
  if (ct) ct.addEventListener('click', () => saveCivilSettings({ test: true, refresh_key: true }));

  // Settings sub-nav: switch which pane is visible.
  document.querySelectorAll('#settings-nav .settings-nav-item').forEach(item => {
    item.addEventListener('click', () => {
      document.querySelectorAll('#settings-nav .settings-nav-item').forEach(i => i.classList.remove('active'));
      document.querySelectorAll('#page-settings .settings-pane').forEach(p => p.classList.remove('active'));
      item.classList.add('active');
      const pane = document.querySelector(`#page-settings .settings-pane[data-pane="${item.dataset.pane}"]`);
      if (pane) pane.classList.add('active');
      // Load on reveal rather than on page load: the image catalog is an
      // admin-only fetch most sessions never need.
      if (item.dataset.pane === 'images' && typeof loadOSImages === 'function') {
        loadOSImages();
      }
      // Same reasoning for the instance-settings panes: an admin-only fetch
      // that also tells the caller which scanner credentials exist.
      if (['scanners', 'jackil', 'email', 'retention', 'display'].includes(item.dataset.pane)
          && typeof loadInstanceSettings === 'function') {
        loadInstanceSettings();
      }
      if (item.dataset.pane === 'jackil' && typeof loadJackilTickets === 'function') {
        loadJackilTickets();
      }
      if (item.dataset.pane === 'jackil' && typeof loadJackilKeys === 'function') {
        loadJackilKeys();
      }
      if (item.dataset.pane === 'api' && typeof loadApiPane === 'function') {
        loadApiPane();
      }
      // Relays too: the list is admin-only, and it polls while you look at it.
      if (item.dataset.pane === 'relays' && typeof loadRelays === 'function') loadRelays();
      // The account pane holds the security-keys card (QA-65); it is also what
      // refreshTotpStatus() loads, and that runs on page load from the monitor.
      if (item.dataset.pane === 'identity') loadSecurityKeys();
    });
  });

  loadCivilSettings();
  refreshTransportAck();
});



/* ══ SETTINGS → RELAYS ───────────────────────────────────────────────────
   HTML: templates/pages/_settings.html (data-pane="relays")
   API:  GET/POST /api/v1/relays/, PATCH/DELETE /api/v1/relays/<id>/ and
         POST /api/v1/relays/<id>/rotate-key/ — all admin-only. Writes that
         move agent traffic carry a TOTP, and passing the edition's relay
         allowance answers 402 with an upgrade body rather than data.
   ═══════════════════════════════════════════════════════════════════════ */

let _relayPoll = null;
let _relaysById = {};

/* The installer URLs, as text with braces rather than a template literal: the
   token only exists once the admin opens the dialog, and a backtick string
   would substitute it at load time (see vigil-enroll.js for the same trap). */
// {token} is the new agent's own token, made here as the Add Host wizard makes
// one; {config} is the one-time config token, whose agent.yml names this relay
// as the place to check in. Keep them apart: the config token is redeemed once
// and is never an agent credential.
const _RELAY_LINUX_CMD = 'curl -fsSL {origin}/agent/install.sh | sudo env VIGIL_TOKEN={token} VIGIL_CONFIG_TOKEN={config} bash';
const _RELAY_WIN_CMD = '$env:VIGIL_TOKEN = "{token}"; $env:VIGIL_CONFIG_TOKEN = "{config}"; irm {origin}/agent/install.ps1 | iex';

function _relayLinuxCmd(origin, token, config) {
  return _RELAY_LINUX_CMD.replace('{origin}', origin).replace('{token}', token)
    .replace('{config}', config);
}

function _relayWindowsCmd(origin, token, config) {
  return _RELAY_WIN_CMD.replace('{token}', token).replace('{config}', config)
    .replace('{origin}', origin);
}

function _relayField(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function _relayIsBuiltin(r) {
  return r.kind === 'builtin';
}

/* Agents try the lowest order number first once their own address stops
   answering, which is the order this list draws in. The built-in row comes back
   first from the API and stays above the remotes. */
function _remoteRelays(relays) {
  return relays.filter((r) => !_relayIsBuiltin(r)).sort(
    (a, b) => ((a.priority || 0) - (b.priority || 0))
      || String(a.name).localeCompare(String(b.name)));
}

function _relayStatusPill(r) {
  if (_relayIsBuiltin(r)) {
    return r.enabled ? '<span class="relay-status ok">Direct</span>'
                     : '<span class="relay-status off">Off</span>';
  }
  if (!r.enabled) return '<span class="relay-status off">Off</span>';
  // Enabled, but past what the license covers: the dashboard neither links to
  // it nor accepts its traffic, whatever it reported last.
  if (r.over_limit) {
    const pill = r.connected ? 'Linked, not in use' : 'Over your limit';
    return `<div class="relay-status-col"><span class="relay-status off">${pill}</span>`
      + '<div class="relay-err">Past your relay limit, so agents cannot use it. Turn the built-in relay or another relay off, or add seats.</div></div>';
  }
  if (r.connected) return '<span class="relay-status ok">Connected</span>';
  // The last failure is the whole reason the row exists — a relay that has
  // never linked is otherwise indistinguishable from one still booting.
  const err = r.last_error ? `<div class="relay-err">${escHtml(r.last_error)}</div>` : '';
  return `<div class="relay-status-col"><span class="relay-status bad">Not connected</span>${err}</div>`;
}

function _relayRowHtml(r) {
  const builtin = _relayIsBuiltin(r);
  const url = builtin ? 'This dashboard' : (r.url ? escHtml(r.url) : '—');
  const seen = timeAgo(r.last_seen_at);
  const meta = [];
  if (seen) meta.push(`<div class="relay-meta">Last seen ${escHtml(seen)}</div>`);
  if (!builtin) {
    meta.push(`<div class="relay-meta">Fallback order `
      + `${escHtml(String(r.priority == null ? 100 : r.priority))}</div>`);
  }
  const actions = [`<button class="btn btn-sm ${r.enabled ? 'btn-outline' : 'btn-mint'}"`
    + ` data-act="toggleRelay" data-a1="${escAttr(r.id)}" data-a2="${r.enabled ? 'off' : 'on'}">`
    + `${r.enabled ? 'Turn off' : 'Turn on'}</button>`];
  if (!builtin) {
    actions.push('<button class="btn btn-sm btn-outline" data-act="editRelay"'
      + ` data-a1="${escAttr(r.id)}">Edit</button>`);
    if (r.url) {
      actions.push('<button class="btn btn-sm btn-sky" data-act="relayInstall"'
        + ` data-a1="${escAttr(r.id)}">Install commands</button>`);
    }
    actions.push('<button class="btn btn-sm btn-outline" data-act="rotateRelayKey"'
      + ` data-a1="${escAttr(r.id)}">New key</button>`);
    actions.push('<button class="btn btn-sm btn-rose" data-act="deleteRelay"'
      + ` data-a1="${escAttr(r.id)}">Delete</button>`);
  }
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(r.name)}`
        + (builtin ? '<span class="relay-badge">Built-in</span>' : '') + '</div>'
      + (r.location ? `<div class="relay-meta">${escHtml(r.location)}</div>` : '')
      + `<div class="relay-url mono">${url}</div>`
      + meta.join('')
    + '</div>'
    + `<div class="relay-status-cell">${_relayStatusPill(r)}</div>`
    + `<div class="relay-actions">${actions.join('')}</div>`
    + '</div>';
}

function _relayStopPolling() {
  if (_relayPoll) {
    clearPollingInterval(_relayPoll);
    _relayPoll = null;
  }
}

/* The nav handler calls this when the pane is revealed, and it redraws every
   10s so "Not connected" becomes "Connected" without a reload. Choosing another
   pane stops it — see the delegateClick at the bottom of this section. */
async function loadRelays() {
  const list = document.getElementById('relays-list');
  if (!list) { _relayStopPolling(); return; }
  let data;
  try {
    data = await apiJson('/api/v1/relays/');
  } catch (e) {
    _relayStopPolling();
    list.innerHTML = `<p class="muted">Could not load relays: ${escHtml(e.message)}</p>`;
    return;
  }
  const pane = document.querySelector('#page-settings .settings-pane[data-pane="relays"]');
  if (!pane || !pane.classList.contains('active')) { _relayStopPolling(); return; }
  if (!_relayPoll) _relayPoll = pollingInterval(loadRelays, 10000);

  const relays = data.relays || [];
  _relaysById = {};
  relays.forEach((r) => { _relaysById[r.id] = r; });

  const usage = document.getElementById('relays-usage');
  if (usage) {
    const line = `${data.used} of ${data.allowed} relays in use.`;
    if (data.used >= data.allowed) {
      usage.innerHTML = escHtml(line)
        + ' Business allows your seat count plus two.'
        + ' <a href="#license-card">See Settings \u2192 License</a>.';
    } else {
      usage.textContent = line;
    }
  }

  const rows = relays.filter(_relayIsBuiltin).map(_relayRowHtml)
    .concat(_remoteRelays(relays).map(_relayRowHtml));
  list.innerHTML = rows.length ? rows.join('') : '<p class="muted">No relays yet.</p>';
}

async function addRelay() {
  const name = _relayField('relay-name');
  const url = _relayField('relay-url');
  if (!name) { showToast('Give the relay a name', 'error'); return; }
  if (!url.startsWith('https://')) {
    showToast('The address must start with https://', 'error');
    return;
  }
  const code = await totpPrompt('Adding a relay creates a key that lets it carry agent traffic.',
    { title: 'Add relay', confirmText: 'Add' });
  if (!code) return;
  const body = {
    name, url,
    location: _relayField('relay-location'),
    priority: Number(_relayField('relay-priority') || 100),
    totp: code,
  };
  try {
    const created = await apiJson('/api/v1/relays/', { method: 'POST', body: JSON.stringify(body) });
    ['relay-name', 'relay-location', 'relay-url'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const prio = document.getElementById('relay-priority');
    if (prio) prio.value = '100';
    _openRelayKeyDialog(created.name, created.key, created.key_sha256);
    loadRelays();
  } catch (e) {
    showToast(e.status === 402 ? `${e.message} See Settings \u2192 License to allow more relays.`
                               : e.message, 'error');
  }
}

/* Concatenated rather than a template literal: the snippet is YAML full of
   quotes and comments, and only the hash is interpolated. */
function _relayComposeSnippet(keySha) {
  return [
    'services:',
    '  relay:',
    '    image: susquehannasyntax/vigil-relay:latest',
    '    restart: unless-stopped',
    '    environment:',
    `      RELAY_KEY_SHA256: "${keySha}"`,
    '      # RELAY_TLS_CERT: /certs/fullchain.pem   # or put it behind Caddy/nginx',
    '      # RELAY_TLS_KEY: /certs/privkey.pem',
    '    ports:',
    '      - "8443:8443"',
    '    volumes:',
    '      - relay-queue:/var/lib/vigil-relay',
    'volumes:',
    '  relay-queue:',
    '',
  ].join('\n');
}

function _relayCopyBtn(id) {
  return `<button class="btn btn-sm btn-outline" id="${id}">Copy</button>`;
}

function _wireRelayCopy(m, id, text) {
  const btn = m.modal.querySelector('#' + id);
  if (btn) btn.onclick = () => copyText(text).then((ok) => showToast(
    ok ? 'Copied to clipboard' : 'Copy failed', ok ? 'success' : 'error'));
}

/* The key exists in this response and nowhere else, and the relay is
   configured with its hash — so the dialog leads with the compose snippet. */
function _openRelayKeyDialog(name, key, keySha) {
  const m = mountModal('relay-key', { wide: true });
  const snippet = _relayComposeSnippet(keySha || '');
  m.setBody(`
    <div class="modal-title"><span></span>
      <button class="modal-close" id="rk-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <p class="muted">This key is shown once. The relay only needs its hash; keep the key itself only if you'll set up the link by hand.</p>
    <div class="deploy-input-label" id="rk-compose-label"></div>
    <div class="relay-code mono" id="rk-compose"></div>
    ${_relayCopyBtn('rk-copy-compose')}
    <div class="deploy-input-label relay-key-sub">Key</div>
    <div class="relay-code mono" id="rk-key"></div>
    ${_relayCopyBtn('rk-copy-key')}
    <div class="confirm-actions"><button class="btn btn-mint btn-sm" id="rk-done">Done</button></div>`);
  m.modal.querySelector('.modal-title span').textContent = 'Relay added \u2014 copy this now';
  m.modal.querySelector('#rk-compose-label').textContent =
    `Compose snippet for ${name || 'the relay'}`;
  m.modal.querySelector('#rk-compose').textContent = snippet;
  m.modal.querySelector('#rk-key').textContent = key || '';
  _wireRelayCopy(m, 'rk-copy-compose', snippet);
  _wireRelayCopy(m, 'rk-copy-key', key || '');
  const close = () => m.close();
  m.modal.querySelector('#rk-x').onclick = close;
  m.modal.querySelector('#rk-done').onclick = close;
  requestAnimationFrame(m.open);
}

async function toggleRelay(id, onoff) {
  const relay = _relaysById[id];
  if (!relay) return;
  const want = onoff !== 'off';
  const code = await totpPrompt(
    want ? `Turning on ${relay.name} lets agents reach this dashboard through it.`
         : `Turning off ${relay.name} moves its agents to another connection point.`,
    { title: want ? 'Turn on relay' : 'Turn off relay', confirmText: want ? 'Turn on' : 'Turn off' });
  if (!code) return;
  try {
    await apiJson(`/api/v1/relays/${id}/`, {
      method: 'PATCH', body: JSON.stringify({ enabled: want, totp: code }),
    });
    showToast(`${relay.name} ${want ? 'enabled' : 'disabled'}`, 'success');
  } catch (e) {
    // "Enable a remote relay that has connected at least once before turning
    // off direct connections." surfaces here, as its own toast.
    showToast(e.message, 'error');
  }
  loadRelays();
}

function editRelay(id) {
  const relay = _relaysById[id];
  if (!relay || _relayIsBuiltin(relay)) return;
  const m = mountModal('relay-edit');
  m.setBody(`
    <div class="modal-title"><span>Edit relay</span>
      <button class="modal-close" id="re-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="setting-row"><label>Name</label><input type="text" class="form-control" id="re-name" maxlength="80" autocomplete="off"></div>
    <div class="setting-row"><label>Location</label><input type="text" class="form-control" id="re-location" maxlength="120" autocomplete="off"></div>
    <div class="setting-row"><label>Address agents use</label><input type="url" class="form-control" id="re-url" autocomplete="off" placeholder="https://relay.example.com"></div>
    <div class="setting-row"><label>Order</label>
      <input type="number" class="form-control" id="re-priority" min="0" max="1000" style="max-width:120px;">
      <div class="totp-sub">Agents try lower numbers first when their own address stops answering.</div>
    </div>
    <p class="muted" id="re-note"></p>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="re-cancel">Cancel</button>
      <button class="btn btn-mint btn-sm" id="re-save">Save</button>
    </div>`);
  m.modal.querySelector('#re-name').value = relay.name || '';
  m.modal.querySelector('#re-location').value = relay.location || '';
  m.modal.querySelector('#re-url').value = relay.url || '';
  m.modal.querySelector('#re-priority').value = String(relay.priority == null ? 100 : relay.priority);
  const note = () => m.modal.querySelector('#re-note');

  m.modal.querySelector('#re-save').onclick = async () => {
    const name = m.modal.querySelector('#re-name').value.trim();
    const location = m.modal.querySelector('#re-location').value.trim();
    const url = m.modal.querySelector('#re-url').value.trim();
    const priority = Number(m.modal.querySelector('#re-priority').value || 0);
    if (!name) { note().textContent = 'Name is required.'; return; }
    if (!url.startsWith('https://')) {
      note().textContent = 'The address must start with https://';
      return;
    }
    const body = { name, location, priority };
    // A new address moves every agent that falls back to this relay \u2014 the same
    // weight as creating it, so a second factor for that field alone.
    if (url !== (relay.url || '')) {
      const code = await totpPrompt('Changing the address sends agents to a different machine.',
        { title: 'Change relay address', confirmText: 'Change' });
      if (!code) return;
      body.url = url;
      body.totp = code;
    }
    try {
      await apiJson(`/api/v1/relays/${id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast('Relay updated', 'success');
      m.close();
      loadRelays();
    } catch (e) {
      note().textContent = e.message;
    }
  };
  const close = () => m.close();
  m.modal.querySelector('#re-x').onclick = close;
  m.modal.querySelector('#re-cancel').onclick = close;
  requestAnimationFrame(m.open);
}

async function rotateRelayKey(id) {
  const relay = _relaysById[id];
  if (!relay || _relayIsBuiltin(relay)) return;
  const ok = await confirmModal(
    'Agents keep working, but this relay disconnects until you update its RELAY_KEY_SHA256.',
    { title: 'Generate a new key', confirmText: 'Generate', danger: true });
  if (!ok) return;
  const code = await totpPrompt('A new key replaces the one this relay connects with.',
    { title: 'New relay key', confirmText: 'New key' });
  if (!code) return;
  try {
    const res = await apiJson(`/api/v1/relays/${id}/rotate-key/`, {
      method: 'POST', body: JSON.stringify({ totp: code }),
    });
    _openRelayKeyDialog(relay.name, res.key, res.key_sha256);
    loadRelays();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function deleteRelay(id) {
  const relay = _relaysById[id];
  if (!relay || _relayIsBuiltin(relay)) return;
  const ok = await confirmModal(
    `Delete ${relay.name}? Agents using it stop checking in until you add another.`,
    { title: 'Delete relay', confirmText: 'Delete', danger: true });
  if (!ok) return;
  const code = await totpPrompt('Deleting a relay takes away a connection point agents may be using.',
    { title: 'Delete relay', confirmText: 'Delete' });
  if (!code) return;
  try {
    await apiJson(`/api/v1/relays/${id}/`, { method: 'DELETE', body: JSON.stringify({ totp: code }) });
    showToast(`Deleted ${relay.name}`, 'success');
    loadRelays();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

/* The same one-liners the enrollment wizard builds, token from the same
   endpoint it uses, with origin set to this relay. A host that cannot see the
   dashboard can still install: the installer comes back through the relay and
   already names it (agent_dist._base_url), so enrolling there is consistent. */
async function relayInstall(id) {
  const relay = _relaysById[id];
  if (!relay || !relay.url || _relayIsBuiltin(relay)) return;
  const m = mountModal('relay-install', { wide: true });
  const title = `Install through ${relay.name}`;
  m.setBody(`
    <div class="modal-title"><span></span>
      <button class="modal-close" id="ri-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w60"></div>
      <div class="sk-line"></div><div class="sk-line"></div><div class="sk-line sk-w80"></div>
    </div>`);
  m.modal.querySelector('.modal-title span').textContent = title;
  m.modal.querySelector('#ri-x').onclick = () => m.close();
  requestAnimationFrame(m.open);

  let config;
  try {
    // The relay is the primary, so the installed agent.yml checks in through it
    // (with this dashboard as the last fallback), not at this page's address.
    const cfg = await apiJson('/api/v1/agent-profiles/enroll-config/', {
      method: 'POST',
      body: JSON.stringify({ profile: null, overrides: { primary_relay: String(relay.id) } }),
    });
    config = cfg.token;
  } catch (e) {
    m.close();
    showToast(e.message, 'error');
    return;
  }

  const origin = relay.url.replace(/\/+$/, '');
  const token = typeof _uuidv4 === 'function' ? _uuidv4() : crypto.randomUUID();
  const linux = _relayLinuxCmd(origin, token, config);
  const windows = _relayWindowsCmd(origin, token, config);
  m.setBody(`
    <div class="modal-title"><span></span>
      <button class="modal-close" id="ri-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="deploy-input-label">Linux / macOS</div>
    <div class="relay-code mono" id="ri-linux"></div>
    ${_relayCopyBtn('ri-copy-linux')}
    <div class="deploy-input-label relay-key-sub">Windows (PowerShell, run as Administrator)</div>
    <div class="relay-code mono" id="ri-windows"></div>
    ${_relayCopyBtn('ri-copy-windows')}
    <p class="muted relay-key-sub">New hosts installed with these commands check in through this relay and appear under Pending for approval. The commands work once, for 24 hours.</p>
    <div class="confirm-actions"><button class="btn btn-mint btn-sm" id="ri-close">Done</button></div>`);
  m.modal.querySelector('.modal-title span').textContent = title;
  m.modal.querySelector('#ri-linux').textContent = linux;
  m.modal.querySelector('#ri-windows').textContent = windows;
  _wireRelayCopy(m, 'ri-copy-linux', linux);
  _wireRelayCopy(m, 'ri-copy-windows', windows);
  const close = () => m.close();
  m.modal.querySelector('#ri-x').onclick = close;
  m.modal.querySelector('#ri-close').onclick = close;
}

/* Choosing any other pane means the relays list is off-screen. */
delegateClick('#settings-nav .settings-nav-item', () => _relayStopPolling());

/* ── Security keys and passkeys (QA-65) ───────────────────────────────────
   Enrol, name and remove the authenticators that can answer a confirmation
   prompt with a tap. The ceremony itself lives in vigil-utils.js (totpPrompt);
   this card only manages the list. HTML: templates/pages/_settings.html.
   API: GET/POST/PATCH/DELETE /api/v1/accounts/security-keys/{,…} */

//: Whether this browser will run WebAuthn at all — refreshed by every load of
//: the card, so a cert arriving between page loads stops being invisible.
let _keysPossible = true;

const KEYS_API = '/api/v1/accounts/security-keys/';

async function loadSecurityKeys() {
  // The confirmation prompt caches whether to offer a key; the list just
  // changed (or is being shown fresh), so let it ask again next time.
  if (typeof _keyFactor !== 'undefined') _keyFactor = null;
  const list = document.getElementById('security-keys-list');
  if (!list) return;
  let status = null;
  let keys = [];
  try {
    [status, keys] = await Promise.all([apiJson('/api/v1/accounts/totp/'), apiJson(KEYS_API)]);
  } catch (e) {
    list.textContent = 'Could not load your security keys.';
    return;
  }
  const note = document.getElementById('security-keys-note');
  const addBtn = document.getElementById('security-keys-add-btn');
  const insecure = !!status && !status.keys_possible;
  if (note) {
    note.textContent = insecure ? (status.keys_reason || 'Security keys need HTTPS.') : '';
    note.style.display = insecure ? '' : 'none';
  }
  if (addBtn) addBtn.disabled = insecure;

  list.textContent = '';
  if (!keys.length) {
    const empty = document.createElement('div');
    empty.className = 'totp-sub';
    empty.textContent = 'No security keys enrolled yet.';
    list.appendChild(empty);
    return;
  }
  keys.forEach((key) => list.appendChild(_securityKeyRow(key)));
}

//: A key row is a Settings row, so it takes the class the settings rows take —
//: .user-row (display:grid) with its two children being the text block and the
//: buttons; the markup mirrors renderUsers() in vigil-users.js. The name is
//: escaped here because it lands in markup (data-a2 too).
function _securityKeyRow(key) {
  const added = key.created_at ? new Date(key.created_at).toLocaleDateString() : '?';
  const lastUsed = key.last_used_at ? `last used ${timeAgo(key.last_used_at)}` : 'never used';
  const row = document.createElement('div');
  row.className = 'user-row';
  row.innerHTML = `<div>
      <div class="user-name">${escHtml(key.name)}</div>
      <div class="user-meta">added ${escHtml(added)} &middot; ${escHtml(lastUsed)}</div>
    </div>
    <div style="display:flex;gap:6px;">
      <button class="btn btn-outline btn-sm" data-act="renameSecurityKey" data-a1="${key.id}"
              data-a2="${escAttr(key.name)}">Rename</button>
      <button class="btn btn-rose btn-sm" data-act="removeSecurityKey" data-a1="${key.id}"
              data-a2="${escAttr(key.name)}">Remove</button>
    </div>`;
  return row;
}
async function renameSecurityKey(id, current) {
  const name = await promptModal('Rename this key', { title: 'Rename key', value: current });
  if (name === null || !name.trim() || name.trim() === current) return;
  try {
    await apiJson(`${KEYS_API}${id}/`, { method: 'PATCH', body: JSON.stringify({ name: name.trim() }) });
    showToast('Key renamed', 'success');
    loadSecurityKeys();
  } catch (e) {
    showToast(`Rename failed: ${e.message}`, 'error');
  }
}
async function removeSecurityKey(id, name) {
  if (!await confirmModal(`Remove the security key "${name}"? You will not be able to tap it at a confirmation prompt any more.`,
                          { title: 'Remove security key', confirmText: 'Remove', danger: true })) return;
  const totp = await totpPrompt('Removing a second factor needs the one you still have.',
                                { title: 'Confirm removal', confirmText: 'Remove' });
  if (!totp) return;
  try {
    await apiJson(`${KEYS_API}${id}/`, { method: 'DELETE', body: JSON.stringify({ totp }) });
    showToast('Security key removed', 'success');
    loadSecurityKeys();
  } catch (e) {
    showToast(`Remove failed: ${e.message}`, 'error');
  }
}

//: Registration needs the two values the browser will not tell us itself.
async function _createSecurityKey(name, totp) {
  // The second factor the user already has, which the server requires before
  // it hands out creation options (empty only for a user with none yet).
  const opts = await apiJson(`${KEYS_API}register/options/`,
                             { method: 'POST', body: JSON.stringify({ totp: totp || '' }) });
  const publicKey = {
    challenge: b64urlToBytes(opts.challenge).buffer,
    timeout: opts.timeout || 60000,
    rp: { name: (opts.rp && opts.rp.name) || 'Vigil', id: opts.rpId },
    user: {
      id: b64urlToBytes(opts.user.id).buffer,
      name: opts.user.name,
      displayName: opts.user.displayName || opts.user.name,
    },
    pubKeyCredParams: opts.pubKeyCredParams || [],
    excludeCredentials: (opts.excludeCredentials || []).map((c) => ({
      type: c.type || 'public-key', id: b64urlToBytes(c.id).buffer,
    })),
    authenticatorSelection: opts.authenticatorSelection || { userVerification: 'preferred' },
    attestation: opts.attestation || 'none',
  };
  const cred = await navigator.credentials.create({ publicKey });
  const r = cred.response;
  const credential = {
    id: cred.id || bytesToB64url(cred.rawId),
    rawId: bytesToB64url(cred.rawId),
    type: cred.type || 'public-key',
    response: {
      clientDataJSON: bytesToB64url(r.clientDataJSON),
      attestationObject: bytesToB64url(r.attestationObject),
      transports: (typeof r.getTransports === 'function' && r.getTransports().length)
        ? r.getTransports() : undefined,
    },
  };
  return apiJson(`${KEYS_API}register/`, { method: 'POST', body: JSON.stringify({ name, credential }) });
}

async function addSecurityKey() {
  // Adding a factor costs the factor you already have, so that a stolen
  // session cannot enrol the attacker's key and pass every gate after it.
  let status = {};
  try {
    status = await apiJson('/api/v1/accounts/totp/');   // current, not cached
  } catch (e) { /* the server still checks both rules */ }
  if (status.keys_possible === false) {
    showToast(status.keys_reason || 'Security keys need HTTPS.', 'error');
    return;
  }
  // The server enforces this gate too; the prompt is here so the user has a
  // way to answer it at all.
  let totp = '';
  if (status.enrolled || (status.keys || 0) > 0) {
    totp = await totpPrompt('Confirm to add a security key',
                            { title: 'Add a security key', confirmText: 'Confirm' });
    if (!totp) return;
  }
  const name = await promptModal('Name this key', { title: 'Add a security key', placeholder: 'YubiKey 5' });
  if (name === null || !name.trim()) return;
  try {
    await _createSecurityKey(name.trim(), totp);
    showToast('Security key added', 'success');
    loadSecurityKeys();
  } catch (e) {
    // A cancelled ceremony, or a browser that refused the origin, is ordinary.
    showToast(`Add failed: ${(e && e.name === 'NotAllowedError') ? 'cancelled' : e.message}`, 'error');
  }
}
