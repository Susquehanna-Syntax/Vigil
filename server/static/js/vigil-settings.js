// @ts-check
// vigil-settings.js
// Owns: Settings page — TOTP enrollment / disable, agent binary upload.
//   (AD config lives in vigil-inventory.js since it imports Hosts.)
// HTML: templates/pages/_settings.html
// Depends on: vigil-utils.js (apiJson, showToast, getCsrf)
// API: GET/POST /api/v1/accounts/totp/{,enroll/,enroll/confirm/,disable/}
//      GET /agent/info/  POST /agent/upload/<platform>/

/* ── TOTP enrollment ── */
async function refreshTotpStatus() {
  const status = /** @type {HTMLElement} */ (document.getElementById('totp-status-line'));
  if (!status) return;
  // The security keys card sits right under this one and opens with it.
  loadSecurityKeys();
  try {
    const data = await apiJson('/api/v1/accounts/totp/');
    /** @type {HTMLElement} */
    const sub = document.getElementById('totp-sub-line');
    /** @type {HTMLElement} */
    const startBtn = document.getElementById('totp-start-btn');
    /** @type {HTMLElement} */
    const disableBtn = document.getElementById('totp-disable-btn');
    /** @type {HTMLElement} */
    const enrollView = document.getElementById('totp-enroll-view');
    if (data.enrolled) {
      status.textContent = t('TOTP enabled');
      status.className = 'totp-status enabled';
      sub.textContent = t('Your account is protected. Task deploys require a 6-digit code.');
      startBtn.style.display = 'none';
      disableBtn.style.display = '';
      enrollView.style.display = 'none';
    } else if (data.pending) {
      status.textContent = t('Enrollment pending');
      status.className = 'totp-status pending';
      sub.textContent = t('Scan the secret into an authenticator app, then enter a code to finish.');
      startBtn.style.display = 'none';
      disableBtn.style.display = 'none';
    } else {
      status.textContent = t('TOTP not enabled');
      status.className = 'totp-status disabled';
      sub.textContent = t('Password fallback is test-only and will be removed. Enroll TOTP to execute tasks.');
      startBtn.style.display = '';
      disableBtn.style.display = 'none';
      enrollView.style.display = 'none';
    }
  } catch (e) { /* state-ok: a status line on the settings card, not a list; it keeps what it showed */ }
}

async function totpStart() {
  try {
    const data = await apiJson('/api/v1/accounts/totp/enroll/', { method: 'POST' });
    /** @type {HTMLElement} */ (document.getElementById('totp-secret-text')).textContent = data.secret;
    /** @type {HTMLElement} */ (document.getElementById('totp-uri-text')).textContent = data.otpauth_uri;
    /** @type {HTMLElement} */ (document.getElementById('totp-enroll-view')).style.display = 'block';
    /** @type {HTMLInputElement} */ (document.getElementById('totp-verify-code')).value = '';
    /** @type {HTMLInputElement} */ (document.getElementById('totp-verify-code')).focus();
    refreshTotpStatus();
  } catch (e) {
    showToast(t('TOTP enroll failed: {message}', { message: e.message }), 'error');
  }
}

async function totpConfirm() {
  const code = /** @type {HTMLInputElement} */ (document.getElementById('totp-verify-code')).value.trim();
  if (!/^\d{6}$/.test(code)) {
    showToast(t('Enter a 6-digit code'), 'error');
    return;
  }
  try {
    await apiJson('/api/v1/accounts/totp/enroll/confirm/', {
      method: 'POST', body: JSON.stringify({ code }),
    });
    showToast(t('TOTP enabled'), 'success');
    refreshTotpStatus();
  } catch (e) {
    showToast(t('Verify failed: {message}', { message: e.message }), 'error');
  }
}

async function totpDisablePrompt() {
  const code = await totpPrompt(t('Turning off two-factor authentication. Enter a current code to confirm.'),
    { title: t('Disable TOTP'), confirmText: t('Disable') });
  if (!code) return;
  try {
    await apiJson('/api/v1/accounts/totp/disable/', {
      method: 'POST', body: JSON.stringify({ code: code.trim() }),
    });
    showToast(t('TOTP disabled'), 'success');
    refreshTotpStatus();
  } catch (e) {
    showToast(t('Disable failed: {message}', { message: e.message }), 'error');
  }
}

/* ── Agent binary distribution ── */

/* A row of GET /agent/info/. */
/** @typedef {{platform_label: string, version?: string, sha256: string}} AgentBinary */
async function loadAgentInfo() {
  const el = /** @type {HTMLElement} */ (document.getElementById('agent-versions'));
  if (!el) return;
  try {
    const data = await apiJson('/agent/info/');
    el.textContent = '';
    if (!data.length) { el.textContent = t('No binaries uploaded yet.'); return; }
    /** @param {AgentBinary} b @param {number} i @returns {void} */
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
      ver.textContent = `v${b.version || '?'}`; // i18n-ignore
      el.appendChild(ver);
      el.appendChild(document.createTextNode(' '));
      const hash = document.createElement('span');
      hash.style.cssText = 'color:var(--text-3);font-size:11px;';
      hash.textContent = t('(sha256: {digest}…)', { digest: b.sha256.slice(0, 12) });
      el.appendChild(hash);
    });
  } catch (e) { showLoadFailure(el, t('the agent binaries'), e, loadAgentInfo); return; }
}

function fleetVersionLabel(version) {
  return version ? `v${version}` : t('Unknown');
}

// Fleet versions under the Agent Binary pane. A viewer without access gets a
// failed request, and then the block stays empty.
async function loadFleetVersions() {
  const el = document.getElementById('fleet-versions');
  if (!el) return;
  el.textContent = '';
  let data;
  try {
    data = await apiJson('/api/v1/hosts/agent-versions/');
  } catch (_) {
    return; // state-ok: the fleet table is an extra; a viewer without access sees the pane without it
  }
  const total = data.total || 0;
  const expected = data.expected || '';
  const outdated = data.outdated_hosts || 0;

  const label = document.createElement('div');
  label.className = 'fleet-versions-label';
  label.textContent = t('Fleet versions');
  el.appendChild(label);

  const summary = document.createElement('div');
  summary.className = 'fleet-versions-summary';
  el.appendChild(summary);
  if (!total) {
    summary.textContent = t('No approved hosts yet.');
    return;
  }
  if (!expected) {
    summary.textContent = tn('{n} host.', '{n} hosts.', total);
  } else if (outdated > 0) {
    summary.textContent = tn('{n} host.', '{n} hosts.', total) + ' '
      + t('{outdated} on an older agent than v{expected}.', { outdated, expected });
  } else {
    summary.textContent = total === 1
      ? t('The only host runs v{expected}.', { expected })
      : t('All {total} hosts run v{expected}.', { total, expected });
  }

  const table = document.createElement('table');
  table.className = 'fleet-versions-table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Version', 'Hosts', 'Status'].forEach((text) => {
    const th = document.createElement('th');
    th.textContent = text;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  (data.versions || []).forEach((v) => {
    const tr = document.createElement('tr');
    const verCell = document.createElement('td');
    verCell.className = 'mono';
    verCell.textContent = fleetVersionLabel(v.version);
    const hostsCell = document.createElement('td');
    hostsCell.textContent = String(v.hosts);
    const statusCell = document.createElement('td');
    if (v.current) {
      statusCell.className = 'fleet-status-current';
      statusCell.textContent = t('Current');
    } else if (v.outdated) {
      statusCell.className = 'fleet-status-outdated';
      statusCell.textContent = t('Outdated');
    } else if (v.version && expected) {
      // Ahead of the build this server ships: updated from elsewhere, or a
      // server that has not been upgraded yet. Neither is a fault here.
      statusCell.className = 'fleet-status-newer';
      statusCell.textContent = t('Newer than this server');
    }
    tr.appendChild(verCell);
    tr.appendChild(hostsCell);
    tr.appendChild(statusCell);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  el.appendChild(table);

  const rolled = data.rolled_back || [];
  if (rolled.length) {
    const title = document.createElement('div');
    title.className = 'fleet-rollback-title';
    title.textContent = t('Rolled back in the last 30 days');
    el.appendChild(title);
    const list = document.createElement('ul');
    list.className = 'fleet-rollback-list';
    rolled.forEach((r) => {
      const li = document.createElement('li');
      li.textContent = t('{host}: {to} back to {from}', { host: r.hostname, to: fleetVersionLabel(r.to_version), from: fleetVersionLabel(r.from_version) });
      if (r.reason) {
        const reason = document.createElement('span');
        reason.className = 'fleet-rollback-reason';
        reason.textContent = r.reason;
        li.appendChild(reason);
      }
      list.appendChild(li);
    });
    el.appendChild(list);
  }
}

function copyInstallCmd() {
  const cmd = `curl -fsSL ${window.location.origin}/agent/install.sh | sudo bash`; // i18n-ignore
  copyText(cmd).then((ok) => showToast(ok ? t('Copied') : t('Copy failed'),
                                       ok ? 'success' : 'error'));
}

function copyInstallCmdPs1() {
  const cmd = `irm ${window.location.origin}/agent/install.ps1 | iex`; // i18n-ignore
  copyText(cmd).then((ok) => showToast(ok ? t('Copied') : t('Copy failed'),
                                       ok ? 'success' : 'error'));
}

async function uploadAgentBinary() {
  const platform = /** @type {HTMLSelectElement} */ (document.getElementById('upload-platform')).value;
  const version = (/** @type {HTMLInputElement} */ (document.getElementById('upload-version')).value || '').trim();
  const fileInput = /** @type {HTMLInputElement} */ (document.getElementById('upload-binary'));
  const file = fileInput.files[0];
  if (!file) { showToast(t('Select a binary file first'), 'error'); return; }

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
    if (!resp.ok) throw new Error(data.error || t('Upload failed'));
    showToast(t('Uploaded {platform}{version}', { platform, version: version ? ' v' + version : '' }), 'success');
    fileInput.value = '';
    loadAgentInfo();
    loadFleetVersions();
  } catch (e) {
    showToast(t('Upload failed: {message}', { message: e.message }), 'error');
  }
}


/* ── Civil SSO settings ──────────────────────────────────────────────── */
async function loadCivilSettings() {
  const card = /** @type {HTMLElement} */ (document.getElementById('civil-settings-card'));
  if (!card) return;
  try {
    const d = await apiJson('/api/v1/civil/settings/');
    /** @type {HTMLInputElement} */ (document.getElementById('civil-url')).value = d.url || '';
    /** @type {HTMLInputElement} */ (document.getElementById('civil-slug')).value = d.app_slug || '';
    /** @type {HTMLInputElement} */ (document.getElementById('civil-enabled')).checked = !!d.enabled;
    document.getElementById('civil-env-note').hidden = !d.env_override;
    _renderCivilState(d);
  } catch (e) { /* state-ok: a settings form with its own inputs, not a list; it stays blank and save still works */ }
}

function _renderCivilState(d) {
  const el = document.getElementById('civil-state');
  if (!el) return;
  if (!d.active) { el.textContent = t('Civil sign-in is off — using local accounts only.'); return; }
  el.textContent = t('Active against {url} · verification key {state}.',
    { url: d.effective_url, state: d.key_cached ? t('cached') : t('not yet fetched') });
}

async function saveCivilSettings(opts) {
  const body = {
    url: /** @type {HTMLInputElement} */ (document.getElementById('civil-url')).value.trim(),
    app_slug: /** @type {HTMLInputElement} */ (document.getElementById('civil-slug')).value.trim(),
    enabled: /** @type {HTMLInputElement} */ (document.getElementById('civil-enabled')).checked,
    ...(opts || {}),
  };
  try {
    const d = await apiJson('/api/v1/civil/settings/', { method: 'POST', body: JSON.stringify(body) });
    _renderCivilState(d);
    document.getElementById('civil-env-note').hidden = !d.env_override;
    if (opts && opts.test) showToast(d.test_ok ? t('Reached Civil and fetched its key') : t('Could not reach Civil'), d.test_ok ? 'success' : 'error');
    else showToast(t('Civil settings saved'), 'success');
  } catch (e) { showToast(t('Save failed: {message}', { message: e.message }), 'error'); }
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
      status.textContent = t('Served over HTTPS');
      status.className = 'totp-status enabled';
      sub.textContent = t('Secrets are encrypted in transit. No acknowledgement is needed.');
      ackBtn.style.display = 'none';
      // Still offer to withdraw a stale one — an instance that moved to TLS
      // should not keep an acknowledgement that would apply if it moved back.
      revokeBtn.style.display = data.acknowledged ? '' : 'none';
      if (data.acknowledged) {
        sub.textContent += ' ' + t('A plain-text acknowledgement is still on record{from}.',
          { from: data.acknowledged_by ? ' ' + t('from {who}', { who: data.acknowledged_by }) : '' });
      }
      return;
    }
    if (data.acknowledged) {
      status.textContent = t('Plain-text transport acknowledged');
      status.className = 'totp-status pending';
      sub.textContent = t('Rebuilds proceed without asking again. Acknowledged{by}{on}. Put Vigil behind TLS and withdraw this.', {
        by: data.acknowledged_by ? ' ' + t('by {who}', { who: data.acknowledged_by }) : '',
        on: data.acknowledged_at
          ? ' ' + t('on {when}', { when: new Date(data.acknowledged_at).toLocaleString() }) : '',
      });
      ackBtn.style.display = 'none';
      revokeBtn.style.display = '';
    } else {
      status.textContent = t('Served over plain HTTP');
      status.className = 'totp-status disabled';
      sub.textContent = t('A rebuild answer file carries an admin password hash, SSH keys and an enrolment token, and they cross the network in the clear. Rebuilds ask for an acknowledgement every time until one is recorded here.');
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
      t('Vigil will stop asking during rebuilds. The answer file\u2019s admin password hash, SSH keys and enrolment token will keep crossing the network unencrypted until Vigil is served over HTTPS. This is recorded against your account.'),
      { title: t('Acknowledge plain-text transport'),
        confirmText: t('I understand'), danger: true });
    if (!ok) return;
  }
  try {
    await apiJson('/api/v1/hosts/transport-ack/', {
      method: 'POST',
      body: JSON.stringify({ acknowledged: !!acknowledged }),
    });
    showToast(acknowledged ? t('Plain-text transport acknowledged')
                           : t('Acknowledgement withdrawn'), 'success');
  } catch (e) {
    showToast(e.message || t('Could not save that'), 'error');
  }
  refreshTransportAck();
}

document.addEventListener('DOMContentLoaded', () => {
  const cs = document.getElementById('civil-save-btn');
  if (cs) cs.addEventListener('click', () => saveCivilSettings());
  const ct = document.getElementById('civil-test-btn');
  if (ct) ct.addEventListener('click', () => saveCivilSettings({ test: true, refresh_key: true }));

  // Settings sub-nav: switch which pane is visible.
  document.querySelectorAll('#settings-nav .settings-nav-item').forEach((/** @type {HTMLElement} */ item) => {
    item.addEventListener('click', () => {
      document.querySelectorAll('#settings-nav .settings-nav-item').forEach((/** @type {HTMLElement} */ i) => i.classList.remove('active'));
      document.querySelectorAll('#page-settings .settings-pane').forEach((/** @type {HTMLElement} */ p) => p.classList.remove('active'));
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
      if (item.dataset.pane === 'notifications' && typeof loadChannels === 'function') loadChannels();
      if (item.dataset.pane === 'notifications' && typeof loadMaintenance === 'function') loadMaintenance();
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

/** @type {ReturnType<typeof pollingInterval>|null} */
let _relayPoll = null;
/** @type {Record<string, {id: string|number, name?: string, location?: string, url?: string, priority?: number|null}>} */
let _relaysById = {};

/* The installer URLs, as text with braces rather than a template literal: the
   token only exists once the admin opens the dialog, and a backtick string
   would substitute it at load time (see vigil-enroll.js for the same trap). */
// {token} is the new agent's own token, made here as the Add Host wizard makes
// one; {config} is the one-time config token, whose agent.yml names this relay
// as the place to check in. Keep them apart: the config token is redeemed once
// and is never an agent credential.
const _RELAY_URL_SAMPLE = 'https://relay.example.com'; // i18n-ignore
const _RELAY_LINUX_CMD = 'curl -fsSL {origin}/agent/install.sh | sudo env VIGIL_TOKEN={token} VIGIL_CONFIG_TOKEN={config} bash'; // i18n-ignore
const _RELAY_WIN_CMD = '$env:VIGIL_TOKEN = "{token}"; $env:VIGIL_CONFIG_TOKEN = "{config}"; irm {origin}/agent/install.ps1 | iex'; // i18n-ignore

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
  return el ? /** @type {HTMLInputElement} */ (el).value.trim() : '';
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
    return r.enabled ? `<span class="relay-status ok">${escHtml(t('Direct'))}</span>`
                     : `<span class="relay-status off">${escHtml(t('Off'))}</span>`;
  }
  if (!r.enabled) return `<span class="relay-status off">${escHtml(t('Off'))}</span>`;
  // Enabled, but past what the license covers: the dashboard neither links to
  // it nor accepts its traffic, whatever it reported last.
  if (r.over_limit) {
    const pill = r.connected ? t('Linked, not in use') : t('Over your limit');
    return `<div class="relay-status-col"><span class="relay-status off">${escHtml(pill)}</span>`
      + `<div class="relay-err">${escHtml(t('Past your relay limit, so agents cannot use it. Turn the built-in relay or another relay off, or add seats.'))}</div></div>`;
  }
  if (r.connected) return `<span class="relay-status ok">${escHtml(t('Connected'))}</span>`;
  // The last failure is the whole reason the row exists — a relay that has
  // never linked is otherwise indistinguishable from one still booting.
  const err = r.last_error ? `<div class="relay-err">${escHtml(r.last_error)}</div>` : '';
  return `<div class="relay-status-col"><span class="relay-status bad">${escHtml(t('Not connected'))}</span>${err}</div>`;
}

function _relayRowHtml(r) {
  const builtin = _relayIsBuiltin(r);
  const url = builtin ? t('This dashboard') : (r.url ? escHtml(r.url) : '—');
  const seen = timeAgo(r.last_seen_at);
  const meta = [];
  if (seen) meta.push(`<div class="relay-meta">${escHtml(t('Last seen {when}', { when: seen }))}</div>`);
  if (!builtin) {
    meta.push(`<div class="relay-meta">${escHtml(t('Fallback order {order}', { order: String(r.priority == null ? 100 : r.priority) }))}</div>`);
  }
  const actions = [`<button class="btn btn-sm ${r.enabled ? 'btn-outline' : 'btn-mint'}"`
    + ` data-act="toggleRelay" data-a1="${escAttr(r.id)}" data-a2="${r.enabled ? 'off' : 'on'}">`
    + `${r.enabled ? escHtml(t('Turn off')) : escHtml(t('Turn on'))}</button>`];
  if (!builtin) {
    actions.push('<button class="btn btn-sm btn-outline" data-act="editRelay"'
      + ` data-a1="${escAttr(r.id)}">${escHtml(t('Edit'))}</button>`);
    if (r.url) {
      actions.push('<button class="btn btn-sm btn-sky" data-act="relayInstall"'
        + ` data-a1="${escAttr(r.id)}">${escHtml(t('Install commands'))}</button>`);
    }
    actions.push('<button class="btn btn-sm btn-outline" data-act="rotateRelayKey"'
      + ` data-a1="${escAttr(r.id)}">${escHtml(t('New key'))}</button>`);
    actions.push('<button class="btn btn-sm btn-rose" data-act="deleteRelay"'
      + ` data-a1="${escAttr(r.id)}">${escHtml(t('Delete'))}</button>`);
  }
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(r.name)}`
        + (builtin ? `<span class="relay-badge">${escHtml(t('Built-in'))}</span>` : '') + '</div>'
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
    list.innerHTML = `<p class="muted">${escHtml(t('Could not load relays: {message}', { message: e.message }))}</p>`;
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
    const line = t('{used} of {allowed} relays in use.', { used: data.used, allowed: data.allowed });
    if (data.used >= data.allowed) {
      usage.innerHTML = escHtml(line) + ' '
        + t('Business allows your seat count plus two. {link}.', {
            link: `<a href="#license-card">${escHtml(t('See Settings → License'))}</a>`,
          });
    } else {
      usage.textContent = line;
    }
  }

  const rows = relays.filter(_relayIsBuiltin).map(_relayRowHtml)
    .concat(_remoteRelays(relays).map(_relayRowHtml));
  list.innerHTML = rows.length ? rows.join('') : `<p class="muted">${escHtml(t('No relays yet.'))}</p>`;
}

async function addRelay() {
  const name = _relayField('relay-name');
  const url = _relayField('relay-url');
  if (!name) { showToast(t('Give the relay a name'), 'error'); return; }
  if (!url.startsWith('https://')) {
    showToast(t('The address must start with https://'), 'error');
    return;
  }
  const code = await totpPrompt(t('Adding a relay creates a key that lets it carry agent traffic.'),
    { title: t('Add relay'), confirmText: t('Add') });
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
      if (el) /** @type {HTMLInputElement} */ (el).value = '';
    });
    const prio = document.getElementById('relay-priority');
    if (prio) /** @type {HTMLInputElement} */ (prio).value = '100';
    _openRelayKeyDialog(created.name, created.key, created.key_sha256);
    loadRelays();
  } catch (e) {
    showToast(e.status === 402 ? t('{message} See Settings → License to allow more relays.', { message: e.message })
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
  return `<button class="btn btn-sm btn-outline" id="${id}">${escHtml(t('Copy'))}</button>`;
}

function _wireRelayCopy(m, id, text) {
  const btn = /** @type {HTMLElement} */ (m.modal.querySelector('#' + id));
  if (btn) btn.onclick = () => copyText(text).then((ok) => showToast(
    ok ? t('Copied to clipboard') : t('Copy failed'), ok ? 'success' : 'error'));
}

/* The key exists in this response and nowhere else, and the relay is
   configured with its hash — so the dialog leads with the compose snippet. */
function _openRelayKeyDialog(name, key, keySha) {
  const m = mountModal('relay-key', { wide: true });
  const snippet = _relayComposeSnippet(keySha || '');
  m.setBody(`
    <div class="modal-title"><span></span>
      <button class="modal-close" id="rk-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <p class="muted">${escHtml(t("This key is shown once. The relay only needs its hash; keep the key itself only if you'll set up the link by hand."))}</p>
    <div class="deploy-input-label" id="rk-compose-label"></div>
    <div class="relay-code mono" id="rk-compose"></div>
    ${_relayCopyBtn('rk-copy-compose')}
    <div class="deploy-input-label relay-key-sub">${escHtml(t('Key'))}</div>
    <div class="relay-code mono" id="rk-key"></div>
    ${_relayCopyBtn('rk-copy-key')}
    <div class="confirm-actions"><button class="btn btn-accent btn-sm" id="rk-done">${escHtml(t('Done'))}</button></div>`);
  m.modal.querySelector('.modal-title span').textContent = t('Relay added \u2014 copy this now');
  m.modal.querySelector('#rk-compose-label').textContent =
    t('Compose snippet for {name}', { name: name || t('the relay') });
  m.modal.querySelector('#rk-compose').textContent = snippet;
  m.modal.querySelector('#rk-key').textContent = key || '';
  _wireRelayCopy(m, 'rk-copy-compose', snippet);
  _wireRelayCopy(m, 'rk-copy-key', key || '');
  const close = () => m.close();
  /** @type {HTMLElement} */ (m.modal.querySelector('#rk-x')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#rk-done')).onclick = close;
  requestAnimationFrame(m.open);
}

async function toggleRelay(id, onoff) {
  const relay = _relaysById[id];
  if (!relay) return;
  const want = onoff !== 'off';
  const code = await totpPrompt(
    want ? t('Turning on {name} lets agents reach this dashboard through it.', { name: relay.name })
         : t('Turning off {name} moves its agents to another connection point.', { name: relay.name }),
    { title: want ? t('Turn on relay') : t('Turn off relay'),
      confirmText: want ? t('Turn on') : t('Turn off') });
  if (!code) return;
  try {
    await apiJson(`/api/v1/relays/${id}/`, {
      method: 'PATCH', body: JSON.stringify({ enabled: want, totp: code }),
    });
    showToast(t('{relayName} {state}', { relayName: relay.name, state: want ? t('enabled') : t('disabled') }), 'success');
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
    <div class="modal-title"><span>${escHtml(t('Edit relay'))}</span>
      <button class="modal-close" id="re-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="setting-row"><label>${escHtml(t('Name'))}</label><input type="text" class="form-control" id="re-name" maxlength="80" autocomplete="off"></div>
    <div class="setting-row"><label>${escHtml(t('Location'))}</label><input type="text" class="form-control" id="re-location" maxlength="120" autocomplete="off"></div>
    <div class="setting-row"><label>${escHtml(t('Address agents use'))}</label><input type="url" class="form-control" id="re-url" autocomplete="off" placeholder="${_RELAY_URL_SAMPLE}"></div>
    <div class="setting-row"><label>${escHtml(t('Order'))}</label>
      <input type="number" class="form-control relay-order-field" id="re-priority" min="0" max="1000">
      <div class="totp-sub">${escHtml(t('Agents try lower numbers first when their own address stops answering.'))}</div>
    </div>
    <p class="muted" id="re-note"></p>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="re-cancel">${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" id="re-save">${escHtml(t('Save'))}</button>
    </div>`);
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-name')).value = relay.name || '';
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-location')).value = relay.location || '';
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-url')).value = relay.url || '';
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-priority')).value = String(relay.priority == null ? 100 : relay.priority);
  const note = () => m.modal.querySelector('#re-note');

  /** @type {HTMLElement} */ (m.modal.querySelector('#re-save')).onclick = async () => {
    const name = /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-name')).value.trim();
    const location = /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-location')).value.trim();
    const url = /** @type {HTMLInputElement} */ (m.modal.querySelector('#re-url')).value.trim();
    const priority = Number(/** @type {HTMLInputElement} */ (m.modal.querySelector('#re-priority')).value || 0);
    if (!name) { note().textContent = t('Name is required.'); return; }
    if (!url.startsWith('https://')) {
      note().textContent = t('The address must start with https://');
      return;
    }
    const body = { name, location, priority };
    // A new address moves every agent that falls back to this relay \u2014 the same
    // weight as creating it, so a second factor for that field alone.
    if (url !== (relay.url || '')) {
      const code = await totpPrompt(t('Changing the address sends agents to a different machine.'),
        { title: t('Change relay address'), confirmText: t('Change') });
      if (!code) return;
      body.url = url;
      body.totp = code;
    }
    try {
      await apiJson(`/api/v1/relays/${id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast(t('Relay updated'), 'success');
      m.close();
      loadRelays();
    } catch (e) {
      note().textContent = e.message;
    }
  };
  const close = () => m.close();
  /** @type {HTMLElement} */ (m.modal.querySelector('#re-x')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#re-cancel')).onclick = close;
  requestAnimationFrame(m.open);
}

async function rotateRelayKey(id) {
  const relay = _relaysById[id];
  if (!relay || _relayIsBuiltin(relay)) return;
  const ok = await confirmModal(
    t('Agents keep working, but this relay disconnects until you update its RELAY_KEY_SHA256.'),
    { title: t('Generate a new key'), confirmText: t('Generate'), danger: true });
  if (!ok) return;
  const code = await totpPrompt(t('A new key replaces the one this relay connects with.'),
    { title: t('New relay key'), confirmText: t('New key') });
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
    t('Delete {name}? Agents using it stop checking in until you add another.', { name: relay.name }),
    { title: t('Delete relay'), confirmText: t('Delete'), danger: true });
  if (!ok) return;
  const code = await totpPrompt(t('Deleting a relay takes away a connection point agents may be using.'),
    { title: t('Delete relay'), confirmText: t('Delete') });
  if (!code) return;
  try {
    await apiJson(`/api/v1/relays/${id}/`, { method: 'DELETE', body: JSON.stringify({ totp: code }) });
    showToast(t('Deleted {name}', { name: relay.name }), 'success');
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
  const title = t('Install through {relayName}', { relayName: relay.name });
  m.setBody(`
    <div class="modal-title"><span></span>
      <button class="modal-close" id="ri-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w60"></div>
      <div class="sk-line"></div><div class="sk-line"></div><div class="sk-line sk-w80"></div>
    </div>`);
  m.modal.querySelector('.modal-title span').textContent = title;
  /** @type {HTMLElement} */ (m.modal.querySelector('#ri-x')).onclick = () => m.close();
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
      <button class="modal-close" id="ri-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="deploy-input-label">${escHtml(t('Linux / macOS'))}</div>
    <div class="relay-code mono" id="ri-linux"></div>
    ${_relayCopyBtn('ri-copy-linux')}
    <div class="deploy-input-label relay-key-sub">${escHtml(t('Windows (PowerShell, run as Administrator)'))}</div>
    <div class="relay-code mono" id="ri-windows"></div>
    ${_relayCopyBtn('ri-copy-windows')}
    <p class="muted relay-key-sub">${escHtml(t('New hosts installed with these commands check in through this relay and appear under Pending for approval. The commands work once, for 24 hours.'))}</p>
    <div class="confirm-actions"><button class="btn btn-accent btn-sm" id="ri-close">${escHtml(t('Done'))}</button></div>`);
  m.modal.querySelector('.modal-title span').textContent = title;
  m.modal.querySelector('#ri-linux').textContent = linux;
  m.modal.querySelector('#ri-windows').textContent = windows;
  _wireRelayCopy(m, 'ri-copy-linux', linux);
  _wireRelayCopy(m, 'ri-copy-windows', windows);
  const close = () => m.close();
  /** @type {HTMLElement} */ (m.modal.querySelector('#ri-x')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#ri-close')).onclick = close;
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
    list.textContent = t('Could not load your security keys.');
    return;
  }
  const note = document.getElementById('security-keys-note');
  const addBtn = /** @type {HTMLButtonElement} */ (document.getElementById('security-keys-add-btn'));
  const insecure = !!status && !status.keys_possible;
  if (note) {
    note.textContent = insecure ? (status.keys_reason || t('Security keys need HTTPS.')) : '';
    note.style.display = insecure ? '' : 'none';
  }
  if (addBtn) addBtn.disabled = insecure;

  list.textContent = '';
  if (!keys.length) {
    const empty = document.createElement('div');
    empty.className = 'totp-sub';
    empty.textContent = t('No security keys enrolled yet.');
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
  const lastUsed = key.last_used_at ? t('last used {ago}', { ago: timeAgo(key.last_used_at) }) : t('never used');
  const row = document.createElement('div');
  row.className = 'user-row';
  row.innerHTML = `<div>
      <div class="user-name">${escHtml(key.name)}</div>
      <div class="user-meta">${escHtml(t('added {date}', { date: added }))} · ${escHtml(lastUsed)}</div>
    </div>
    <div class="key-row-actions">
      <button class="btn btn-outline btn-sm" data-act="renameSecurityKey" data-a1="${key.id}"
              data-a2="${escAttr(key.name)}">${escHtml(t('Rename'))}</button>
      <button class="btn btn-rose btn-sm" data-act="removeSecurityKey" data-a1="${key.id}"
              data-a2="${escAttr(key.name)}">${escHtml(t('Remove'))}</button>
    </div>`;
  return row;
}
async function renameSecurityKey(id, current) {
  const name = await promptModal(t('Rename this key'), { title: t('Rename key'), value: current });
  if (name === null || !name.trim() || name.trim() === current) return;
  try {
    await apiJson(`${KEYS_API}${id}/`, { method: 'PATCH', body: JSON.stringify({ name: name.trim() }) });
    showToast(t('Key renamed'), 'success');
    loadSecurityKeys();
  } catch (e) {
    showToast(t('Rename failed: {message}', { message: e.message }), 'error');
  }
}
async function removeSecurityKey(id, name) {
  if (!await confirmModal(
        t('Remove the security key "{name}"? You will not be able to tap it at a confirmation prompt any more.', { name }),
        { title: t('Remove security key'), confirmText: t('Remove'), danger: true })) return;
  const totp = await totpPrompt(t('Removing a second factor needs the one you still have.'),
                                { title: t('Confirm removal'), confirmText: t('Remove') });
  if (!totp) return;
  try {
    await apiJson(`${KEYS_API}${id}/`, { method: 'DELETE', body: JSON.stringify({ totp }) });
    showToast(t('Security key removed'), 'success');
    loadSecurityKeys();
  } catch (e) {
    showToast(t('Remove failed: {message}', { message: e.message }), 'error');
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
    rp: { name: (opts.rp && opts.rp.name) || 'Vigil', id: opts.rpId }, // i18n-ignore — the product name
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
  const cred = /** @type {PublicKeyCredential} */ (await navigator.credentials.create({ publicKey }));
  const r = /** @type {AuthenticatorAttestationResponse} */ (cred.response);
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
  } catch (e) { /* state-ok: a pre-flight check, not a list; the server still enforces both rules */ }
  if (status.keys_possible === false) {
    showToast(status.keys_reason || t('Security keys need HTTPS.'), 'error');
    return;
  }
  // The server enforces this gate too; the prompt is here so the user has a
  // way to answer it at all.
  let totp = '';
  if (status.enrolled || (status.keys || 0) > 0) {
    totp = await totpPrompt(t('Confirm to add a security key'),
                            { title: t('Add a security key'), confirmText: t('Confirm') });
    if (!totp) return;
  }
  const name = await promptModal(t('Name this key'), { title: t('Add a security key'), placeholder: t('YubiKey 5') });
  if (name === null || !name.trim()) return;
  try {
    await _createSecurityKey(name.trim(), totp);
    showToast(t('Security key added'), 'success');
    loadSecurityKeys();
  } catch (e) {
    // A cancelled ceremony, or a browser that refused the origin, is ordinary.
    showToast(t('Add failed: {message}', {
      message: (e && e.name === 'NotAllowedError') ? t('cancelled') : e.message,
    }), 'error');
  }
}


/* ── Profile picture (design branch) ─────────────────────────────────────
   Shrunk in the browser to a 256 px square before it is sent, so the stored
   picture stays small whatever was picked. The server checks the bytes. */

/** @param {string} dataUri */
function _paintAvatar(dataUri) {
  const preview = document.getElementById('avatar-preview');
  const side = /** @type {HTMLElement|null} */ (document.querySelector('.sidebar-avatar'));
  const remove = /** @type {HTMLElement|null} */ (document.getElementById('avatar-remove'));
  const initial = (side && side.dataset.initial) || (side ? (side.textContent || '').trim().charAt(0) : '');
  if (side && !side.dataset.initial && !side.querySelector('img')) side.dataset.initial = initial;
  for (const el of [preview, side]) {
    if (!el) continue;
    el.replaceChildren();
    if (dataUri) {
      const img = document.createElement('img');
      img.src = dataUri;
      img.alt = '';
      el.appendChild(img);
    } else {
      el.textContent = (side && side.dataset.initial) || initial || '?';
    }
  }
  if (remove) remove.hidden = !dataUri;
}

async function loadAvatarCard() {
  if (!document.getElementById('avatar-card')) return;
  try {
    const r = await apiJson('/api/v1/accounts/me/avatar/');
    _paintAvatar(r.avatar || '');
  } catch (e) { _paintAvatar(''); /* state-ok: the card still offers an upload */ }
}

/** @param {File} file @returns {Promise<string>} */
function _shrinkImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(t('That file could not be read.')));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error(t('That file is not an image.')));
      img.onload = () => {
        const size = 256;
        const canvas = document.createElement('canvas');
        canvas.width = size; canvas.height = size;
        const ctx = canvas.getContext('2d');
        // Centre-crop to a square, then scale.
        const s = Math.min(img.width, img.height);
        ctx.drawImage(img, (img.width - s) / 2, (img.height - s) / 2, s, s, 0, 0, size, size);
        resolve(canvas.toDataURL('image/png'));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}

document.addEventListener('change', async (ev) => {
  const input = /** @type {HTMLInputElement} */ (ev.target);
  if (input.id !== 'avatar-file' || !input.files || !input.files[0]) return;
  try {
    const dataUri = await _shrinkImage(input.files[0]);
    const r = await apiJson('/api/v1/accounts/me/avatar/', { method: 'PUT', body: JSON.stringify({ avatar: dataUri }) });
    _paintAvatar(r.avatar);
    showToast(t('Profile picture saved'), 'success');
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    input.value = '';
  }
});

delegateClick('#avatar-remove', async () => {
  try {
    await apiJson('/api/v1/accounts/me/avatar/', { method: 'DELETE' });
    _paintAvatar('');
  } catch (e) { showToast(e.message, 'error'); }
});

/** The sidebar avatar: your account settings, scrolled to your picture. */
function openMyAccount() {
  navigateTo('settings');
  /** @type {HTMLElement|null} */ (document.querySelector('.settings-nav-item[data-pane="account"]'))?.click();
  loadAvatarCard();
  setTimeout(() => document.getElementById('avatar-card')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 50);
}

document.addEventListener('DOMContentLoaded', loadAvatarCard);
