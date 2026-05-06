// vigil-settings.js
// Owns: Settings page — TOTP enrollment / disable.
//   (AD config lives in vigil-inventory.js since it imports Hosts.)
// HTML: templates/pages/_settings.html
// Depends on: vigil-utils.js (apiJson, showToast)
// API: GET/POST /api/v1/accounts/totp/{,enroll/,enroll/confirm/,disable/}

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

