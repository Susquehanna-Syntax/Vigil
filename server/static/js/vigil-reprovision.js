// vigil-reprovision.js
// Owns: host Rebuild ceremony + Settings → OS Images pane + the Reprovision
//   page (image library, pull controls, install profiles, rebuild jobs).
// HTML: templates/pages/_settings.html (data-pane="images"), host detail page,
//   templates/pages/_reprovision.html
// Depends on: vigil-utils.js (apiJson, showToast, escHtml, mountModal,
//   confirmModal, formatBytes)
//
// The ceremony deliberately front-loads what is about to be destroyed: the
// target disk and every other disk found by preflight are shown before the
// confirmation fields, because a wrong disk is caught by a human reading the
// list, not by a predicate. See docs/reprovisioning.md §5.
//
// SECURITY (Reprovision page section, below the ceremony code): every value
// rendered there that came from the API — image name/family/version, and
// especially `import_error` (free text produced by a failed import, entirely
// operator/attacker-influenced on the custom-URL path) — is written via
// textContent / element properties only, never innerHTML or a template
// literal treated as HTML. This project has already shipped one XSS of this
// exact shape (vigil-pickers.js); the ceremony code above this line predates
// that lesson and uses escHtml+innerHTML instead, which is out of scope for
// this change (it is not touched here).

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

// ── Reprovision page (sidebar app) ──────────────────────────────────────────
// HTML: templates/pages/_reprovision.html
// Image library + pull controls (catalog / RHEL / Windows / custom URL) +
// install profiles + rebuild jobs. Every value below that came from the API
// — image name/family/version, `import_error`, hostnames, profile fields —
// is written via textContent / element properties only. See the SECURITY
// note at the top of this file.

let _reproCatalogLoaded = false;
let _reproImagesById = {};
let _reproPollTimer = null;

function _reproAge(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.max(0, Math.floor((Date.now() - then) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// Maps the ok/bad/pending vocabulary from _stateClass() to a plain colored
// label — no pill background, matching how vigil-firewall.js renders
// tri-state values (colored text, not a badge chip).
function _reproStatusSpan(rawValue, classifyAs) {
  const span = document.createElement('span');
  span.style.fontWeight = '600';
  const cls = _stateClass(classifyAs !== undefined ? classifyAs : rawValue);
  const colorMap = { ok: 'var(--mint)', bad: 'var(--rose)', pending: 'var(--peach)' };
  span.style.color = colorMap[cls] || 'var(--text-3)';
  span.textContent = rawValue || 'unknown'; // server-controlled enum either way
  return span;
}

async function loadReprovisionPage() {
  _reproLoadImages();
  _reproLoadProfiles();
  _reproLoadJobs();
  if (!_reproCatalogLoaded) {
    _reproCatalogLoaded = true;
    _reproLoadCatalog();
  }
  _reproStartPolling();
}

/* ── Image library ───────────────────────────────────────────────────── */

async function _reproLoadImages() {
  const list = document.getElementById('repro-images-list');
  if (!list) return;
  let images;
  try {
    images = await apiJson('/api/v1/reprovision/images/');
  } catch (e) {
    list.replaceChildren();
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'Image library requires an admin account.';
    list.appendChild(p);
    return;
  }
  _reproImagesById = {};
  for (const img of images) {
    if (img && img.id) _reproImagesById[img.id] = img;
  }
  _reproRenderImageTable(list, images);
}

function _reproRenderImageTable(list, images) {
  list.replaceChildren();
  if (!images.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'No OS images registered yet — pull one below.';
    list.appendChild(p);
    return;
  }

  const table = document.createElement('table');
  table.className = 'vuln-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const label of ['Name', 'Family', 'Version', 'Size', 'Status', '']) {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const img of images) {
    if (!img || typeof img !== 'object') continue;
    tbody.appendChild(_reproImageRow(img));
  }
  table.appendChild(tbody);
  list.appendChild(table);
}

function _reproImageRow(img) {
  const tr = document.createElement('tr');

  const nameTd = document.createElement('td');
  // Operator-supplied on the custom-URL path — textContent only.
  nameTd.textContent = img.name || '';
  tr.appendChild(nameTd);

  const familyTd = document.createElement('td');
  familyTd.textContent = img.os_family || '';
  tr.appendChild(familyTd);

  const versionTd = document.createElement('td');
  versionTd.textContent = img.version || '';
  tr.appendChild(versionTd);

  const sizeTd = document.createElement('td');
  sizeTd.className = 'mono';
  sizeTd.textContent = img.size_bytes ? formatBytes(img.size_bytes) : 'unknown';
  tr.appendChild(sizeTd);

  const statusTd = document.createElement('td');
  statusTd.appendChild(_reproImageStatusCell(img));
  tr.appendChild(statusTd);

  const actTd = document.createElement('td');
  actTd.style.textAlign = 'right';
  const delBtn = document.createElement('button');
  delBtn.className = 'btn btn-outline btn-sm';
  delBtn.textContent = 'Delete';
  delBtn.addEventListener('click', () => _reproDeleteImage(img.id, img.name));
  actTd.appendChild(delBtn);
  tr.appendChild(actTd);

  return tr;
}

function _reproImageStatusCell(img) {
  const wrap = document.createElement('div');
  wrap.appendChild(_reproStatusSpan(img.status, img.status === 'ready' ? 'completed' : img.status));

  if (img.status === 'downloading' || img.status === 'importing') {
    const size = Number(img.size_bytes) || 0;
    const downloaded = Number(img.bytes_downloaded) || 0;

    const bar = document.createElement('div');
    bar.style.cssText = 'margin-top:8px;height:4px;width:160px;background:var(--s3);border-radius:2px;overflow:hidden;';
    const inner = document.createElement('div');
    const pct = size > 0 ? Math.max(0, Math.min(100, (downloaded / size) * 100)) : 0;
    inner.style.height = '100%';
    inner.style.background = 'var(--sky)';
    inner.style.borderRadius = '2px';
    inner.style.width = (size > 0 ? pct : 4) + '%';
    bar.appendChild(inner);
    wrap.appendChild(bar);

    const label = document.createElement('div');
    label.style.cssText = 'margin-top:4px;font-size:11px;color:var(--text-3);';
    label.textContent = size > 0
      ? `${formatBytes(downloaded)} / ${formatBytes(size)}`
      : `${formatBytes(downloaded)} downloaded (total size unknown)`;
    wrap.appendChild(label);
  }

  if (img.status === 'failed') {
    const err = document.createElement('div');
    err.style.cssText = 'margin-top:8px;font-size:12px;color:var(--rose);max-width:320px;white-space:pre-wrap;word-break:break-word;';
    // import_error is free text produced by a failed import — the whole
    // diagnostic for a multi-gigabyte fetch, and operator-supplied on the
    // custom-URL path. textContent only; see SECURITY note atop this file.
    err.textContent = img.import_error || 'Import failed';
    wrap.appendChild(err);
  }

  return wrap;
}

async function _reproDeleteImage(id, name) {
  // confirmModal sets `message` via textContent internally (vigil-utils.js),
  // so interpolating the operator-supplied name here is safe.
  if (!await confirmModal(`Delete "${name}"? Rebuilds using it will fail.`)) return;
  try {
    await apiJson(`/api/v1/reprovision/images/${id}/`, { method: 'DELETE' });
    showToast('Image deleted', 'success');
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || 'Could not delete image', 'error');
  }
}

/* ── Pull controls: catalog, RHEL/Windows presets, custom URL ──────────── */

async function _reproLoadCatalog() {
  const sel = document.getElementById('repro-catalog-select');
  const empty = document.getElementById('repro-catalog-empty');
  const pullBtn = document.getElementById('repro-catalog-pull-btn');
  if (!sel) return;
  let entries = [];
  try {
    entries = await apiJson('/api/v1/reprovision/catalog/');
  } catch (e) {
    entries = [];
  }
  sel.replaceChildren();
  for (const e of entries) {
    if (!e || typeof e !== 'object' || !e.id) continue;
    const opt = document.createElement('option');
    opt.value = e.id;
    // Catalog entries are static, server-shipped data (apps/reprovision/
    // catalog.py), not operator input — but set via textContent regardless.
    const label = `${e.name || e.id} (${[e.os_family, e.version].filter(Boolean).join(' ')})`;
    opt.textContent = label;
    sel.appendChild(opt);
  }
  const has = sel.options.length > 0;
  sel.style.display = has ? '' : 'none';
  if (pullBtn) pullBtn.style.display = has ? '' : 'none';
  if (empty) empty.style.display = has ? 'none' : 'block';
}

async function _reproPullFromCatalog() {
  const sel = document.getElementById('repro-catalog-select');
  const btn = document.getElementById('repro-catalog-pull-btn');
  if (!sel || !sel.value || !btn) return;
  btn.disabled = true;
  try {
    await apiJson('/api/v1/reprovision/images/pull/', {
      method: 'POST',
      body: JSON.stringify({ catalog_id: sel.value }),
    });
    showToast('Download queued', 'success');
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || 'Could not queue the pull', 'error');
  } finally {
    btn.disabled = false;
  }
}

// The RHEL button pre-sets the shared name/family fields above both the
// upload and custom-URL forms — RHEL has no anonymous URL with a published
// digest (see catalog.py), so it reaches the library through one of these
// two operator-driven paths rather than the catalog. Upload is the better
// fit for an operator who already has the ISO on their laptop; the URL path
// stays for an internal mirror, which is faster than a round trip through
// the browser.
//
// There is no Windows equivalent: OSImage.Family and images.KERNEL_PATHS
// have no Windows entry (it needs a WinPE boot.wim/bootmgr pipeline this
// feature doesn't have yet, not the vmlinuz/initrd pair this extracts), so
// the "Get Windows image" button is left `disabled` in the template rather
// than wired to this — presetting a family the server will always refuse,
// after the operator may have already picked a multi-gigabyte file, would
// be worse than the button doing nothing.
function _reproPresetFamily(family) {
  const famSel = document.getElementById('repro-custom-family');
  if (famSel) famSel.value = family;
  const fileInput = document.getElementById('repro-custom-file');
  if (fileInput) fileInput.focus();
}

function _reproRefreshCustomGate() {
  const btn = document.getElementById('repro-custom-submit');
  if (!btn) return;
  const ack = document.getElementById('repro-custom-ack');
  const name = document.getElementById('repro-custom-name');
  const url = document.getElementById('repro-custom-url');
  const sha = document.getElementById('repro-custom-sha256');
  btn.disabled = !(ack && ack.checked &&
    name && name.value.trim() &&
    url && url.value.trim() &&
    sha && sha.value.trim());
}

async function _reproSubmitCustom() {
  const btn = document.getElementById('repro-custom-submit');
  if (!btn) return;
  const nameEl = document.getElementById('repro-custom-name');
  const familyEl = document.getElementById('repro-custom-family');
  const versionEl = document.getElementById('repro-custom-version');
  const urlEl = document.getElementById('repro-custom-url');
  const shaEl = document.getElementById('repro-custom-sha256');
  const ackEl = document.getElementById('repro-custom-ack');

  btn.disabled = true;
  try {
    await apiJson('/api/v1/reprovision/images/pull/', {
      method: 'POST',
      body: JSON.stringify({
        name: nameEl.value.trim(),
        os_family: familyEl.value,
        version: versionEl.value.trim(),
        url: urlEl.value.trim(),
        sha256: shaEl.value.trim(),
      }),
    });
    showToast('Download queued', 'success');
    nameEl.value = '';
    versionEl.value = '';
    urlEl.value = '';
    shaEl.value = '';
    ackEl.checked = false;
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || 'Could not queue the pull', 'error');
  } finally {
    _reproRefreshCustomGate();
  }
}

// The upload counterpart to _reproSubmitCustom, above. Shares the
// name/family/version/sha256 fields with the URL form (both register the
// same OSImage row, they just differ in where the bytes come from) but
// posts multipart/form-data straight to images/upload/ rather than JSON to
// images/pull/ — apiJson always sends Content-Type: application/json,
// which is wrong for a file body, so this uses fetch directly and lets the
// browser set the multipart boundary itself.
function _reproRefreshUploadGate() {
  const btn = document.getElementById('repro-upload-submit');
  if (!btn) return;
  const name = document.getElementById('repro-custom-name');
  const sha = document.getElementById('repro-custom-sha256');
  const file = document.getElementById('repro-custom-file');
  btn.disabled = !(name && name.value.trim() &&
    sha && sha.value.trim() &&
    file && file.files && file.files.length > 0);
}

async function _reproSubmitUpload() {
  const btn = document.getElementById('repro-upload-submit');
  if (!btn) return;
  const nameEl = document.getElementById('repro-custom-name');
  const familyEl = document.getElementById('repro-custom-family');
  const versionEl = document.getElementById('repro-custom-version');
  const shaEl = document.getElementById('repro-custom-sha256');
  const fileEl = document.getElementById('repro-custom-file');
  const file = fileEl && fileEl.files && fileEl.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('name', nameEl.value.trim());
  formData.append('os_family', familyEl.value);
  formData.append('version', versionEl.value.trim());
  formData.append('sha256', shaEl.value.trim());
  formData.append('iso', file);

  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = 'Uploading…';
  try {
    const resp = await fetch('/api/v1/reprovision/images/upload/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': getCsrf() },
      body: formData,
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || 'Upload failed');
    showToast('Image uploaded and imported', 'success');
    nameEl.value = '';
    versionEl.value = '';
    shaEl.value = '';
    fileEl.value = '';
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || 'Could not upload the image', 'error');
  } finally {
    btn.textContent = originalLabel;
    _reproRefreshUploadGate();
  }
}

/* ── Install profiles ────────────────────────────────────────────────── */

async function _reproLoadProfiles() {
  const list = document.getElementById('repro-profiles-list');
  if (!list) return;
  let profiles;
  try {
    profiles = await apiJson('/api/v1/reprovision/profiles/');
  } catch (e) {
    list.replaceChildren();
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'Install profiles require an admin account.';
    list.appendChild(p);
    return;
  }
  list.replaceChildren();
  if (!profiles.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'No install profiles yet.';
    list.appendChild(p);
    return;
  }
  for (const prof of profiles) {
    if (!prof || typeof prof !== 'object') continue;
    list.appendChild(_reproProfileRow(prof));
  }
}

function _reproProfileRow(prof) {
  const row = document.createElement('div');
  row.className = 'site-row';

  const main = document.createElement('div');
  main.className = 'site-row-main';
  const name = document.createElement('span');
  name.className = 'site-row-name';
  name.textContent = prof.name || ''; // operator-authored — textContent
  main.appendChild(name);

  const desc = document.createElement('span');
  desc.className = 'site-row-desc muted';
  const img = _reproImagesById[prof.image];
  const imgLabel = img ? img.name : `image ${prof.image || '?'}`;
  let descText = `${imgLabel} · disk ${prof.disk_target || '?'} · ${prof.network_mode || '?'}`;
  if (prof.network_mode === 'static' && prof.static_address) {
    descText += ` ${prof.static_address}`;
  }
  desc.textContent = descText; // built from operator-authored fields — textContent
  main.appendChild(desc);
  row.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'site-row-meta';
  const user = document.createElement('span');
  user.className = 'mono';
  user.style.color = 'var(--text-3)';
  user.textContent = prof.admin_username || '';
  meta.appendChild(user);
  row.appendChild(meta);

  return row;
}

/* ── Rebuild jobs ─────────────────────────────────────────────────────── */

async function _reproLoadJobs() {
  const list = document.getElementById('repro-jobs-list');
  if (!list) return;
  let jobs;
  try {
    jobs = await apiJson('/api/v1/reprovision/jobs/');
  } catch (e) {
    list.replaceChildren();
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'Could not load rebuild jobs.';
    list.appendChild(p);
    return;
  }
  list.replaceChildren();
  if (!jobs.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = 'No rebuild jobs yet.';
    list.appendChild(p);
    return;
  }
  for (const job of jobs) {
    if (!job || typeof job !== 'object') continue;
    list.appendChild(_reproJobRow(job));
  }
}

function _reproJobRow(job) {
  const row = document.createElement('div');
  row.className = 'site-row';

  const main = document.createElement('div');
  main.className = 'site-row-main';
  const name = document.createElement('span');
  name.className = 'site-row-name';
  name.textContent = job.hostname || ''; // host-reported — textContent
  main.appendChild(name);

  const desc = document.createElement('span');
  desc.className = 'site-row-desc muted';
  desc.textContent = `${job.image_name || ''} · ${job.profile_name || ''} · ` +
    `requested ${_reproAge(job.requested_at)}`;
  main.appendChild(desc);

  if (job.state === 'failed' && job.failure_reason) {
    const err = document.createElement('span');
    err.style.cssText = 'font-size:12px;color:var(--rose);';
    err.textContent = job.failure_reason; // job-produced text — textContent
    main.appendChild(err);
  }
  row.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'site-row-meta';
  meta.appendChild(_reproStatusSpan(job.state));
  row.appendChild(meta);

  return row;
}

/* ── Polling (mirrors vigil-tasks.js's history poll: on a timer, only while
   the tab is actually visible) ──────────────────────────────────────── */

function _reproPageVisible() {
  const page = document.getElementById('page-reprovision');
  return !!(page && page.classList.contains('active') && !document.hidden);
}

function _reproStartPolling() {
  if (_reproPollTimer) return;
  _reproPollTimer = setInterval(() => {
    if (_reproPageVisible()) {
      _reproLoadImages();
      _reproLoadJobs();
    }
  }, 4000);
}

/* ── Static control wiring ───────────────────────────────────────────── */

document.getElementById('repro-catalog-pull-btn')?.addEventListener('click', _reproPullFromCatalog);
document.getElementById('repro-rhel-btn')?.addEventListener('click', () => _reproPresetFamily('rhel'));
// repro-windows-btn is `disabled` in the template — see the comment on
// _reproPresetFamily above for why it isn't wired to anything.
document.getElementById('repro-custom-submit')?.addEventListener('click', _reproSubmitCustom);
document.getElementById('repro-upload-submit')?.addEventListener('click', _reproSubmitUpload);
for (const id of ['repro-custom-ack', 'repro-custom-name', 'repro-custom-url', 'repro-custom-sha256']) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('input', _reproRefreshCustomGate);
  el.addEventListener('change', _reproRefreshCustomGate);
}
// name and sha256 are shared with the upload form above, so both gates
// need to react to them; the file input only affects the upload gate.
for (const id of ['repro-custom-name', 'repro-custom-sha256', 'repro-custom-file']) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('input', _reproRefreshUploadGate);
  el.addEventListener('change', _reproRefreshUploadGate);
}

// Auto-load the first time the Reprovision tab is opened, following the
// wrap-navigateTo pattern documented atop vigil-nav.js.
const _reproNavigateTo = navigateTo;
navigateTo = function (page) {
  _reproNavigateTo(page);
  if (page === 'reprovision') loadReprovisionPage();
};
