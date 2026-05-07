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

