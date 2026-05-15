// vigil-enroll.js
// Owns: Enrollment wizard modal — 4-step flow for adding a new host.
// HTML: templates/base.html #enroll-modal
// Depends on: vigil-utils.js (apiPost, apiJson, getCsrf, showToast, escHtml)
// API: POST /api/v1/hosts/check-pending/  POST /api/v1/hosts/<id>/approve/

/* ── state ── */
let _enrollToken = null;
let _enrollPollTimer = null;
let _enrollDetectedHost = null;

/* ── helpers ── */

function _uuidv4() {
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
  );
}

function _enrollSetStep(n) {
  [1, 2, 3, 4].forEach(i => {
    document.getElementById('enroll-s' + i).style.display = i === n ? 'block' : 'none';
    document.getElementById('enroll-pip-' + i).classList.toggle('active', i === n);
  });
  const labels = ['Generate token', 'Waiting for agent', 'Approve host', 'Done'];
  document.getElementById('enroll-step-label').textContent = labels[n - 1];
}

/* ── public API ── */

function openEnrollWizard() {
  _enrollToken = _uuidv4();
  _enrollDetectedHost = null;
  _stopEnrollPoll();

  const cmd = `VIGIL_TOKEN=${_enrollToken} curl -fsSL ${window.location.origin}/agent/install.sh | sudo bash`;
  document.getElementById('enroll-cmd').textContent = cmd;
  _enrollSetStep(1);

  document.getElementById('enroll-overlay').classList.add('open');
  document.getElementById('enroll-modal').classList.add('open');
}

function closeEnrollWizard() {
  _stopEnrollPoll();
  document.getElementById('enroll-overlay').classList.remove('open');
  document.getElementById('enroll-modal').classList.remove('open');
}

function copyEnrollCmd() {
  const cmd = document.getElementById('enroll-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => showToast('Copied to clipboard', 'success'));
}

function enrollGoStep2() {
  _enrollSetStep(2);
  _startEnrollPoll();
}

function _startEnrollPoll() {
  _stopEnrollPoll();
  _enrollPollTimer = setInterval(_pollForHost, 3000);
}

function _stopEnrollPoll() {
  if (_enrollPollTimer) {
    clearInterval(_enrollPollTimer);
    _enrollPollTimer = null;
  }
}

async function _pollForHost() {
  if (!_enrollToken) return;
  try {
    const data = await apiJson('/api/v1/hosts/check-pending/', {
      method: 'POST',
      body: JSON.stringify({ token: _enrollToken }),
    });
    if (data.status === 'pending' || data.status === 'approved') {
      _stopEnrollPoll();
      _enrollDetectedHost = data.host;
      _enrollSetStep(3);
      const hostname = data.host.hostname || '—';
      const os = data.host.os || 'Unknown OS';
      const ip = data.host.ip_address || 'Unknown IP';
      document.getElementById('enroll-detected-hostname').textContent = hostname;
      document.getElementById('enroll-detected-meta').textContent = `${os} · ${ip}`;
      if (data.status === 'approved') {
        document.getElementById('enroll-approve-btn').disabled = true;
        document.getElementById('enroll-approve-btn').textContent = 'Already approved';
      }
    }
  } catch (_) { /* silently retry */ }
}

async function enrollApproveHost() {
  if (!_enrollDetectedHost) return;
  const totp = (document.getElementById('enroll-approve-totp').value || '').trim();
  if (!totp) { showToast('Enter your TOTP code to approve', 'error'); return; }
  const btn = document.getElementById('enroll-approve-btn');
  btn.disabled = true;
  try {
    await apiJson(`/api/v1/hosts/${_enrollDetectedHost.id}/approve/`, {
      method: 'POST',
      body: JSON.stringify({ totp }),
    });
    const hostname = _enrollDetectedHost.hostname || 'Host';
    document.getElementById('enroll-done-msg').textContent = `${hostname} is now online and monitoring.`;
    _enrollSetStep(4);
  } catch (e) {
    btn.disabled = false;
    showToast('Approval failed: ' + e.message, 'error');
  }
}
