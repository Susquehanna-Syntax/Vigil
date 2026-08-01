// vigil-reprovision.js
// Owns: host Rebuild ceremony + Settings → OS Images pane.
// HTML: templates/pages/_settings.html (data-pane="images"), host detail page
// Depends on: vigil-utils.js (apiJson, showToast, escHtml, mountModal)
//
// The ceremony deliberately front-loads what is about to be destroyed: the
// target disk and every other disk found by preflight are shown before the
// confirmation fields, because a wrong disk is caught by a human reading the
// list, not by a predicate. See docs/reprovisioning.md §5.

const REBUILD_POLL_MS = 5000;
const ABORT_WINDOW_SECONDS = 60;

// States before the point of no return. Abort is offered only here; past it
// the honest answer is that it is out of our hands (§3.3).
const ABORTABLE = ['pending', 'staging', 'staged'];
const TERMINAL = ['completed', 'failed', 'aborted', 'timed_out'];

function _bytesToGiB(n) {
  return (Number(n || 0) / 1024 / 1024 / 1024).toFixed(1) + ' GiB';
}

function _stateClass(state) {
  if (state === 'completed') return 'ok';
  if (TERMINAL.includes(state)) return 'bad';
  return 'pending';
}

// ── Rebuild ceremony ────────────────────────────────────────────────────────

async function openRebuildModal(hostId, hostname) {
  // Mount and open before fetching — a modal that appears only after the
  // network settles reads as lag (see commit 57470af).
  const m = mountModal('rebuild', { wide: true });
  m.setBody('<h3>Rebuild host</h3><p class="muted">Loading images…</p>');
  m.open();

  let images = [];
  let profiles = [];
  let baselines = [];
  try {
    [images, profiles, baselines] = await Promise.all([
      apiJson('/api/v1/reprovision/images/'),
      apiJson('/api/v1/reprovision/profiles/'),
      apiJson('/api/v1/baselines/').catch(() => []),
    ]);
  } catch (e) {
    m.setBody('<h3>Rebuild host</h3>' +
      '<p class="bad">Could not load the image catalog.</p>' +
      '<div class="modal-actions"><button class="btn btn-outline" id="rb-cancel">Close</button></div>');
    document.getElementById('rb-cancel').onclick = m.close;
    return;
  }

  const ready = images.filter((i) => i.status === 'ready');
  if (!ready.length) {
    m.setBody('<h3>Rebuild host</h3>' +
      '<p class="muted">No images are ready. Add one under Settings → OS Images.</p>' +
      '<div class="modal-actions"><button class="btn btn-outline" id="rb-cancel">Close</button></div>');
    document.getElementById('rb-cancel').onclick = m.close;
    return;
  }

  const insecure = window.location.protocol !== 'https:';
  const imageOpts = ready.map((i) =>
    `<option value="${escHtml(i.id)}">${escHtml(i.name)} (${escHtml(i.architecture)})</option>`).join('');
  const baselineOpts = ['<option value="">None</option>'].concat(
    baselines.map((b) => `<option value="${escHtml(b.id)}">${escHtml(b.name)}</option>`)).join('');

  m.setBody(
    `<h3>Rebuild ${escHtml(hostname)}</h3>` +
    `<p class="bad"><strong>This erases everything on the target disk.</strong> ` +
    `There is no undo once the installer starts.</p>` +
    `<label>Image<select id="rb-image">${imageOpts}</select></label>` +
    `<label>Profile<select id="rb-profile"></select></label>` +
    `<div id="rb-summary" class="rb-summary muted">Select an image and profile…</div>` +
    `<div id="rb-preflight" class="rb-preflight"><span class="muted">Checking rebuild readiness…</span></div>` +
    `<label>Tag on completion<input id="rb-tag" placeholder="rebuilt:need config"></label>` +
    `<label>Run baseline afterwards<select id="rb-baseline">${baselineOpts}</select></label>` +
    (insecure
      ? `<label class="rb-warn"><input type="checkbox" id="rb-ack-plain"> ` +
        `Vigil is served over plain HTTP. The answer file carries the admin ` +
        `password hash, SSH keys, and the enrolment token in clear text. ` +
        `Proceed anyway.</label>`
      : '') +
    `<hr>` +
    `<label>Password<input type="password" id="rb-password" autocomplete="current-password"></label>` +
    `<label>Authenticator code<input id="rb-totp" inputmode="numeric" autocomplete="one-time-code"></label>` +
    // placeholder is set as a DOM property below, not interpolated: escHtml
    // does not escape quotes, and the hostname is agent-supplied.
    `<label>Type the hostname to confirm<input id="rb-hostname" autocomplete="off"></label>` +
    `<div class="modal-actions">` +
      `<button class="btn btn-outline" id="rb-cancel">Cancel</button>` +
      `<button class="btn btn-danger" id="rb-go" disabled>Rebuild this machine</button>` +
    `</div>`);

  const imageSel = document.getElementById('rb-image');
  const profileSel = document.getElementById('rb-profile');
  const summary = document.getElementById('rb-summary');
  const hostnameInput = document.getElementById('rb-hostname');
  hostnameInput.placeholder = hostname;
  const goBtn = document.getElementById('rb-go');
  const ackBox = document.getElementById('rb-ack-plain');

  function currentProfile() {
    return profiles.find((p) => p.id === profileSel.value);
  }

  function refreshSummary() {
    const img = ready.find((i) => i.id === imageSel.value);
    const prof = currentProfile();
    if (!img || !prof) { summary.textContent = 'Select an image and profile…'; return; }
    summary.innerHTML =
      `<div><span class="muted">Target disk</span> <strong class="bad">${escHtml(prof.disk_target)}</strong></div>` +
      `<div><span class="muted">Image</span> ${escHtml(img.name)}</div>` +
      `<div><span class="muted">Checksum</span> <code>${escHtml(img.sha256)}</code></div>` +
      `<div><span class="muted">Network</span> ${escHtml(prof.network_mode)}` +
        (prof.network_mode === 'static' ? ` ${escHtml(prof.static_address || '')}` : '') + `</div>`;
  }

  function refreshProfiles() {
    const forImage = profiles.filter((p) => p.image === imageSel.value);
    profileSel.innerHTML = forImage.length
      ? forImage.map((p) => `<option value="${escHtml(p.id)}">${escHtml(p.name)}</option>`).join('')
      : '<option value="">No profile for this image</option>';
    refreshSummary();
  }

  // The submit button stays disabled until the typed hostname matches
  // exactly — the same rule the server enforces, surfaced early.
  function refreshGate() {
    const typed = hostnameInput.value === hostname;
    const acked = !insecure || (ackBox && ackBox.checked);
    goBtn.disabled = !(typed && acked && profileSel.value);
  }

  imageSel.onchange = () => { refreshProfiles(); refreshGate(); };
  profileSel.onchange = () => { refreshSummary(); refreshGate(); };
  hostnameInput.oninput = refreshGate;
  if (ackBox) ackBox.onchange = refreshGate;
  document.getElementById('rb-cancel').onclick = m.close;
  refreshProfiles();

  _runPreflight(hostId);

  goBtn.onclick = async () => {
    goBtn.disabled = true;
    try {
      const job = await apiJson('/api/v1/reprovision/jobs/', {
        method: 'POST',
        body: JSON.stringify({
          host: hostId,
          image: imageSel.value,
          profile: profileSel.value,
          completion_tag: document.getElementById('rb-tag').value.trim(),
          post_baseline: document.getElementById('rb-baseline').value || null,
          password: document.getElementById('rb-password').value,
          totp: document.getElementById('rb-totp').value.trim(),
          typed_hostname: hostnameInput.value,
          acknowledge_plaintext_transport: !!(ackBox && ackBox.checked),
        }),
      });
      _showJobProgress(m, job, hostname);
    } catch (e) {
      showToast(e.message || 'Rebuild was refused', 'error');
      goBtn.disabled = false;
    }
  };
}

async function _runPreflight(hostId) {
  const box = document.getElementById('rb-preflight');
  if (!box) return;
  try {
    const result = await apiJson('/api/v1/reprovision/preflight/', {
      method: 'POST', body: JSON.stringify({ host: hostId }),
    });
    if (!result || !result.disks) {
      box.innerHTML = '<span class="muted">Readiness check dispatched — ' +
        'results appear after the agent next checks in.</span>';
      return;
    }
    const rows = result.disks.map((d) =>
      `<li><code>${escHtml(d.name)}</code> ${_bytesToGiB(d.size_bytes)}` +
      (d.mountpoints && d.mountpoints.length
        ? ` <span class="muted">mounted at ${escHtml(d.mountpoints.join(', '))}</span>` : '') +
      `</li>`).join('');
    box.innerHTML =
      (result.ok
        ? '<p class="ok">Preflight passed.</p>'
        : '<p class="bad">Preflight failed: ' +
          escHtml((result.failures || []).join('; ')) + '</p>') +
      `<p class="muted">Disks found on this machine — check the target above ` +
      `is the one you mean:</p><ul class="rb-disks">${rows}</ul>`;
  } catch (e) {
    box.innerHTML = '<span class="muted">Readiness check unavailable.</span>';
  }
}

function _showJobProgress(m, job, hostname) {
  let remaining = ABORT_WINDOW_SECONDS;
  let timer = null;
  let poll = null;

  function stop() {
    if (timer) clearInterval(timer);
    if (poll) clearInterval(poll);
  }

  function render(current) {
    const abortable = ABORTABLE.includes(current.state);
    m.setBody(
      `<h3>Rebuilding ${escHtml(hostname)}</h3>` +
      `<p class="rb-state ${_stateClass(current.state)}">${escHtml(current.state)}</p>` +
      (current.failure_reason
        ? `<p class="bad">${escHtml(current.failure_reason)}</p>` : '') +
      (abortable
        ? `<p class="muted">Abort is still possible — nothing has been erased yet.` +
          (remaining > 0 ? ` Staging begins in ${remaining}s.` : '') + `</p>`
        : (TERMINAL.includes(current.state)
          ? ''
          : `<p class="bad">Past the point of no return — the disk is being ` +
            `rewritten and this can no longer be stopped.</p>`)) +
      `<div class="modal-actions">` +
        (abortable
          ? `<button class="btn btn-danger" id="rb-abort">Abort rebuild</button>` : '') +
        `<button class="btn btn-outline" id="rb-close">Close</button>` +
      `</div>`);
    document.getElementById('rb-close').onclick = () => { stop(); m.close(); };
    const abortBtn = document.getElementById('rb-abort');
    if (abortBtn) {
      abortBtn.onclick = async () => {
        abortBtn.disabled = true;
        try {
          const updated = await apiJson(
            `/api/v1/reprovision/jobs/${job.id}/abort/`,
            { method: 'POST', body: JSON.stringify({}) });
          showToast('Rebuild aborted — nothing was erased', 'success');
          render(updated);
        } catch (e) {
          showToast(e.message || 'Too late to abort', 'error');
        }
      };
    }
  }

  render(job);
  timer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) clearInterval(timer);
  }, 1000);
  poll = setInterval(async () => {
    try {
      const current = await apiJson(`/api/v1/reprovision/jobs/${job.id}/`);
      render(current);
      if (TERMINAL.includes(current.state)) stop();
    } catch (e) { /* transient — keep polling */ }
  }, REBUILD_POLL_MS);
}

// ── Settings → OS Images (admin only) ───────────────────────────────────────

async function loadOSImages() {
  const list = document.getElementById('images-list');
  if (!list) return;
  try {
    const images = await apiJson('/api/v1/reprovision/images/');
    list.innerHTML = '';
    if (!images.length) {
      list.innerHTML = '<p class="muted">No OS images registered yet.</p>';
      return;
    }
    images.forEach((img) => {
      const row = document.createElement('div');
      row.className = 'site-row';
      row.innerHTML =
        `<div class="site-row-main">` +
          `<span class="site-row-name">${escHtml(img.name)}</span>` +
          `<span class="site-row-desc muted">${escHtml(img.os_family)} ` +
            `${escHtml(img.version)} · ${escHtml(img.architecture)}</span>` +
          (img.status === 'failed'
            ? `<span class="bad">${escHtml(img.import_error || 'Import failed')}</span>` : '') +
        `</div>` +
        `<div class="site-row-meta">` +
          `<span class="${_stateClass(img.status === 'ready' ? 'completed' : img.status)}">` +
            `${escHtml(img.status)}</span>` +
          `<div class="site-row-actions">` +
            `<button class="btn btn-outline btn-sm img-del" data-id="${escHtml(img.id)}">Delete</button>` +
          `</div>` +
        `</div>`;
      list.appendChild(row);
    });
    list.querySelectorAll('.img-del').forEach((btn) => {
      btn.onclick = async () => {
        if (!await confirmModal('Delete this image? Rebuilds using it will fail.')) return;
        try {
          await apiJson(`/api/v1/reprovision/images/${btn.dataset.id}/`,
                        { method: 'DELETE' });
          loadOSImages();
        } catch (e) { showToast(e.message || 'Could not delete', 'error'); }
      };
    });
  } catch (e) {
    // Non-admins simply do not see this pane; a 403/404 here is not an error
    // worth shouting about.
    list.innerHTML = '<p class="muted">OS image management requires an admin account.</p>';
  }
}
