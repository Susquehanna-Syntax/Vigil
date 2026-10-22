// vigil-enroll.js
// Owns: Enrollment wizard modal — 4-step flow for adding a new host.
// HTML: templates/base.html #enroll-modal
// Depends on: vigil-utils.js (apiPost, apiJson, getCsrf, showToast, escHtml),
//   vigil-agentprofile-form.js (the step-1 settings form)
// API: POST /api/v1/hosts/check-pending/  POST /api/v1/hosts/<id>/approve/
//   POST /api/v1/agent-profiles/enroll-config/

/* ── state ── */
let _enrollToken = null;
let _enrollPollTimer = null;
let _enrollDetectedHost = null;
let _enrollOs = 'unix';  // 'unix' (Linux/macOS, any arch) or 'windows'

/* The one-time config token QA-62 mints, and the address its agent.yml points
   at. Both only exist once "Create install command" has run: until then there
   is no command to show, because nothing about the agent is decided yet. */
let _enrollConfigToken = null;
let _enrollServer = window.location.origin;
let _enrollProfiles = [];
let _enrollProfileId = '';

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

/* The two commands are plain single-quoted strings, each alone on its line.
   NOT template literals: a backtick string fills in ${_enrollToken} when this
   file loads, while the token is still null, and every command then reads
   VIGIL_TOKEN=null. The placeholders are literal text, substituted by the
   builders below, and the one-time config token is spliced in after that.
   test_enroll_command lifts both strings out of this file as text and runs
   the Linux one under bash, so keep each on one line with no ' inside. */

const _ENROLL_WIN_CMD = '$env:VIGIL_TOKEN = "${_enrollToken}"; irm ${origin}/agent/install.ps1 | iex';

function _enrollWindowsCmd() {
  const command = _ENROLL_WIN_CMD.replace('${_enrollToken}', _enrollToken).replace('${origin}', _enrollServer);
  // Its own statement before irm, in the same session, so install.ps1 sees it.
  return _enrollConfigToken
    ? command.replace('; irm', `; $env:VIGIL_CONFIG_TOKEN = "${_enrollConfigToken}"; irm`)
    : command;
}

const _ENROLL_LINUX_CMD = 'curl -fsSL ${origin}/agent/install.sh | sudo env VIGIL_TOKEN=${_enrollToken} bash';

function _enrollLinuxCmd() {
  const command = _ENROLL_LINUX_CMD.replace('${_enrollToken}', _enrollToken).replace('${origin}', _enrollServer);
  // Before ` bash`, inside the `sudo env` list: anywhere else neither sudo nor
  // install.sh ever sees it.
  return _enrollConfigToken
    ? command.replace(' bash', ` VIGIL_CONFIG_TOKEN=${_enrollConfigToken} bash`)
    : command;
}

function _enrollForm() {
  return document.getElementById('enroll-config');
}

function _enrollProfile(id) {
  return _enrollProfiles.find(p => String(p.id) === String(id)) || null;
}

/* Every field the form holds that the profile does not — sent as overrides, so
   "Create install command" mints exactly what is on screen and nothing else. */
function _enrollDiff(profile, values) {
  if (!profile) return values;
  const out = {};
  for (const key of Object.keys(values)) {
    const mine = values[key];
    const theirs = profile[key];
    if (Array.isArray(mine)) {
      if (mine.join('\u0000') !== (theirs || []).join('\u0000')) out[key] = mine;
    } else if (String(mine ?? '') !== String(theirs ?? '')) {
      out[key] = mine;
    }
  }
  return out;
}

/* Step 1 has no command until the settings are turned into one: the block is
   hidden, and this both fills it and reveals it. */
function _enrollRenderCmd() {
  const block = document.getElementById('enroll-cmd-block');
  if (!_enrollConfigToken) {
    if (block) block.hidden = true;
    return;
  }
  const cmd = _enrollOs === 'windows' ? _enrollWindowsCmd() : _enrollLinuxCmd();
  if (block) block.hidden = false;
  document.getElementById('enroll-cmd').textContent = cmd;
  const hint = document.getElementById('enroll-cmd-hint');
  if (hint) {
    hint.textContent = _enrollOs === 'windows'
      ? 'Run this in an elevated (Administrator) PowerShell on the machine you want to add. The agent installs as a Windows service and will appear in the queue below.'
      : 'Run this on the machine you want to add — x86-64 and ARM64 are detected automatically. Requires root / sudo. The agent starts automatically and will appear in the queue below.';
  }
}

/* The form changed after the command was minted: that command installs the
   OLD settings, which is the one outcome an admin must not be able to miss. */
function _enrollInvalidateCmd() {
  if (!_enrollConfigToken) return;
  _enrollConfigToken = null;
  const cmd = document.getElementById('enroll-cmd');
  if (cmd) cmd.textContent = '';
  const block = document.getElementById('enroll-cmd-block');
  if (block) block.hidden = true;
  const note = document.getElementById('enroll-stale');
  if (note) {
    note.textContent = 'Settings changed — create the command again.';
    note.hidden = false;
  }
  const next = document.querySelector('#enroll-s1 [data-act="enrollGoStep2"]');
  if (next) next.disabled = true;
}

function enrollSetOs(os) {
  _enrollOs = os;
  document.getElementById('enroll-os-unix').classList.toggle('active', os === 'unix');
  document.getElementById('enroll-os-windows').classList.toggle('active', os === 'windows');
  _enrollRenderCmd();
}

async function openEnrollWizard() {
  _enrollToken = _uuidv4();
  _enrollDetectedHost = null;
  _enrollConfigToken = null;
  _enrollServer = window.location.origin;
  _stopEnrollPoll();

  const stale = document.getElementById('enroll-stale');
  if (stale) stale.hidden = true;
  const next = document.querySelector('#enroll-s1 [data-act="enrollGoStep2"]');
  if (next) next.disabled = true;
  _enrollRenderCmd();
  _enrollSetStep(1);

  document.getElementById('enroll-overlay').classList.add('open');
  document.getElementById('enroll-modal').classList.add('open');

  const form = _enrollForm();
  if (!form) return;
  const skeleton = document.createElement('div');
  skeleton.className = 'modal-skeleton';
  for (const width of ['sk-w60', '', 'sk-w80']) {
    const line = document.createElement('div');
    line.className = ('sk-line ' + width).trim();
    skeleton.appendChild(line);
  }
  form.replaceChildren(skeleton);
  try {
    const [profiles, ctx] = await Promise.all([
      apiJson('/api/v1/agent-profiles/'),
      apfLoadContext(),
    ]);
    _enrollProfiles = Array.isArray(profiles) ? profiles : [];
    _enrollFillProfiles();
    if (!_enrollProfile(_enrollProfileId)) {
      const monitor = _enrollProfiles.find(p => p.name === 'Monitor only') || _enrollProfiles[0];
      _enrollProfileId = monitor ? String(monitor.id) : '';
    }
    const sel = document.getElementById('enroll-profile');
    if (sel) sel.value = _enrollProfileId;
    apfRender(form, _enrollProfile(_enrollProfileId), ctx);
    _enrollWatchForm(form);
  } catch (e) {
    const p = document.createElement('p');
    p.className = 'confirm-hint';
    p.textContent = e.message;
    form.replaceChildren(p);
  }
}

/* Any edit after a command exists makes that command install the old
   settings, so it is withdrawn. Bound once: the mount outlives re-renders. */
function _enrollWatchForm(form) {
  if (form._enrollWatched) return;
  form._enrollWatched = true;
  for (const type of ['input', 'change', 'apfchange']) {
    form.addEventListener(type, _enrollInvalidateCmd);
  }
}

function _enrollFillProfiles() {
  const sel = document.getElementById('enroll-profile');
  if (!sel) return;
  sel.replaceChildren();
  // The API orders builtins first, so the wizard's three starting points are
  // the first three options.
  for (const p of _enrollProfiles) {
    const opt = document.createElement('option');
    opt.value = String(p.id);
    opt.textContent = p.name;
    sel.appendChild(opt);
  }
}

async function enrollUseProfile() {
  const sel = document.getElementById('enroll-profile');
  const form = _enrollForm();
  if (!sel || !form) return;
  _enrollProfileId = sel.value;
  try {
    apfRender(form, _enrollProfile(_enrollProfileId), await apfLoadContext());
  } catch (e) {
    showToast(e.message, 'error');
  }
  _enrollInvalidateCmd();
}

async function enrollCreateCommand() {
  const form = _enrollForm();
  if (!form) return;
  const values = apfRead(form);
  const note = document.getElementById('enroll-stale');
  if (note) note.hidden = true;
  apfShowErrors(form, null);

  const btn = document.querySelector('#enroll-s1 [data-act="enrollCreateCommand"]');
  if (btn) btn.disabled = true;
  try {
    const res = await apiJson('/api/v1/agent-profiles/enroll-config/', {
      method: 'POST',
      body: JSON.stringify({
        profile: _enrollProfileId || null,
        overrides: _enrollDiff(_enrollProfile(_enrollProfileId), values),
      }),
    });
    _enrollConfigToken = res.token;
    // Where the agent will check in. When its settings name a relay the
    // download comes from the relay too — a host that cannot see this
    // dashboard can still install.
    _enrollServer = res.server_url || window.location.origin;
    const notes = document.getElementById('enroll-notes');
    if (notes) {
      notes.textContent = (res.notes || []).join(' ');
      notes.hidden = !(res.notes || []).length;
    }
    _enrollRenderCmd();
    const next = document.querySelector('#enroll-s1 [data-act="enrollGoStep2"]');
    if (next) next.disabled = false;
  } catch (e) {
    apfShowErrors(form, _enrollErrorFields(e));
    showToast(e.message || 'Could not create the install command', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* QA-62 answers a bad setting with {errors: {field: message}} and no detail,
   so apiJson's message alone would say "Request failed" over eight fields. */
function _enrollErrorFields(err) {
  const body = err && err.body;
  return (body && typeof body.errors === 'object' && body.errors) || null;
}

async function enrollSaveProfile() {
  const form = _enrollForm();
  if (!form) return;
  const name = await promptModal('Name for this profile');
  if (!name) return;
  apfShowErrors(form, null);
  try {
    const created = await apiJson('/api/v1/agent-profiles/', {
      method: 'POST',
      body: JSON.stringify({ ...apfRead(form), name }),
    });
    _enrollProfiles = await apiJson('/api/v1/agent-profiles/');
    _enrollFillProfiles();
    _enrollProfileId = String(created.id);
    const sel = document.getElementById('enroll-profile');
    if (sel) sel.value = _enrollProfileId;
    showToast(`Saved profile ${created.name}`, 'success');
  } catch (e) {
    apfShowErrors(form, _enrollErrorFields(e));
    showToast(e.message || 'Could not save the profile', 'error');
  }
}

function closeEnrollWizard() {
  _stopEnrollPoll();
  document.getElementById('enroll-overlay').classList.remove('open');
  document.getElementById('enroll-modal').classList.remove('open');
}

function copyEnrollCmd() {
  const cmd = document.getElementById('enroll-cmd').textContent;
  copyText(cmd).then((ok) => showToast(
    ok ? 'Copied to clipboard' : 'Copy failed', ok ? 'success' : 'error'));
}

function enrollGoStep2() {
  _enrollSetStep(2);
  _startEnrollPoll();
}

function _startEnrollPoll() {
  _stopEnrollPoll();
  _enrollPollTimer = pollingInterval(_pollForHost, 3000);
}

function _stopEnrollPoll() {
  if (_enrollPollTimer) {
    clearPollingInterval(_enrollPollTimer);
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
      const rep = document.getElementById('enroll-replaces');
      if (rep) {
        if (data.replaces) {
          rep.textContent = `Replaces ${data.replaces.hostname} — its history, tags and site are kept.`;
          rep.style.display = '';
        } else {
          rep.style.display = 'none';
        }
      }
      if (data.status === 'approved') {
        document.getElementById('enroll-approve-btn').disabled = true;
        document.getElementById('enroll-approve-btn').textContent = 'Already approved';
      }
    }
  } catch (_) { /* silently retry */ }
}

async function enrollApproveHost() {
  if (!_enrollDetectedHost) return;
  const totp = await totpPrompt(`Approving ${_enrollDetectedHost.hostname || 'this host'} lets it receive signed tasks.`,
    { title: 'Approve host', confirmText: 'Approve' });
  if (!totp) return;
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


/* The wizard's Done button ran two statements from one attribute. A name for
   the pair, so the attribute is just a name. */
function closeEnrollWizardAndReload() {
  closeEnrollWizard();
  location.reload();
}
