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
  const code = prompt('Enter a current 6-digit code to disable TOTP:');
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
      ver.style.cssText = 'color:var(--sky);font-size:11px;';
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
  navigator.clipboard.writeText(cmd).then(() => showToast('Copied', 'success'));
}

function copyInstallCmdPs1() {
  const cmd = `irm ${window.location.origin}/agent/install.ps1 | iex`;
  navigator.clipboard.writeText(cmd).then(() => showToast('Copied', 'success'));
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

/* ── Status pages ────────────────────────────────────────────────────── */
async function loadStatusPages() {
  const list = document.getElementById('statuspage-list');
  if (!list) return;
  try {
    const pages = await apiJson('/api/v1/status-pages/');
    if (!pages.length) { list.innerHTML = '<p class="muted">No status pages yet.</p>'; return; }
    list.innerHTML = pages.map(p => {
      const url = window.location.origin + p.url;
      const branded = p.is_primary ? '' :
        `<label class="setting-check"><input type="checkbox" data-sp-brand="${p.id}" ${p.hide_badge ? 'checked' : ''}> Hide "Powered by Vigil" badge (Business)</label>`;
      return `<div class="sp-row">
        <div class="sp-row-head">
          <input type="text" class="sp-title" data-sp-title="${p.id}" value="${escHtml(p.title)}">
          <label class="setting-check"><input type="checkbox" data-sp-enabled="${p.id}" ${p.enabled ? 'checked' : ''}> Public</label>
        </div>
        <div class="sp-url">${p.enabled ? `<a href="${escHtml(url)}" target="_blank">${escHtml(url)}</a>` : '<span class="muted">enable to publish</span>'}${p.is_primary ? ' <span class="tag-chip">primary · free</span>' : ''}</div>
        ${branded}
        <div class="sp-row-actions">
          <button class="btn btn-xs btn-outline" data-sp-save="${p.id}">Save</button>
          <button class="btn btn-xs btn-outline" data-sp-rotate="${p.id}">Rotate URL</button>
          <button class="btn btn-xs btn-outline" style="color:var(--rose);" data-sp-del="${p.id}">Delete</button>
        </div>
      </div>`;
    }).join('');
    _wireStatusPageRows();
  } catch (e) { list.innerHTML = `<p class="muted">Couldn't load: ${escHtml(e.message)}</p>`; }
}

function _wireStatusPageRows() {
  const q = (sel) => document.querySelectorAll(sel);
  q('[data-sp-save]').forEach(b => b.addEventListener('click', async () => {
    const id = b.dataset.spSave;
    const body = {
      title: document.querySelector(`[data-sp-title="${id}"]`).value,
      enabled: document.querySelector(`[data-sp-enabled="${id}"]`).checked,
    };
    const brand = document.querySelector(`[data-sp-brand="${id}"]`);
    if (brand) body.hide_badge = brand.checked;
    try { await apiJson(`/api/v1/status-pages/${id}/`, { method: 'PATCH', body: JSON.stringify(body) }); showToast('Saved', 'success'); loadStatusPages(); }
    catch (e) { showToast(e.message.includes('402') ? 'Branding is a Business feature' : 'Save failed: ' + e.message, 'error'); }
  }));
  q('[data-sp-rotate]').forEach(b => b.addEventListener('click', async () => {
    await apiJson(`/api/v1/status-pages/${b.dataset.spRotate}/`, { method: 'PATCH', body: JSON.stringify({ rotate_token: true }) });
    showToast('New URL issued', 'success'); loadStatusPages();
  }));
  q('[data-sp-del]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('Delete this status page?')) return;
    await fetch(`/api/v1/status-pages/${b.dataset.spDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadStatusPages();
  }));
}

document.addEventListener('DOMContentLoaded', () => {
  const cs = document.getElementById('civil-save-btn');
  if (cs) cs.addEventListener('click', () => saveCivilSettings());
  const ct = document.getElementById('civil-test-btn');
  if (ct) ct.addEventListener('click', () => saveCivilSettings({ test: true, refresh_key: true }));
  const sn = document.getElementById('statuspage-new-btn');
  if (sn) sn.addEventListener('click', async () => {
    try { await apiJson('/api/v1/status-pages/', { method: 'POST', body: '{}' }); loadStatusPages(); }
    catch (e) { showToast(e.message.includes('402') ? 'Additional pages are a Business feature' : e.message, 'error'); }
  });
  loadCivilSettings();
  loadStatusPages();
});
