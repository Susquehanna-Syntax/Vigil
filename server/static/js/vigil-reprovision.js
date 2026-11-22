// @ts-check
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

// A file size in GiB. The unit is part of the sentence: a translation may want
// "2,3 GB" with the number after it or a different unit name entirely.
function _bytesToGiB(n) {
  return t('{size} GiB', { size: (Number(n || 0) / 1024 / 1024 / 1024).toFixed(1) });
}

function _stateClass(state) {
  if (state === 'completed') return 'ok';
  if (TERMINAL.includes(state)) return 'bad';
  return 'pending';
}

// ── Rebuild ceremony ────────────────────────────────────────────────────────

async function openRebuildModal(hostId, hostname) {
  // Mount and open before fetching — a modal that appears only after the
  // network settles reads as lag (see commit 57470af). The loading state goes
  // in its own div below the title, so a failure can replace it by id.
  const m = mountModal('rebuild', { wide: true });
  m.setBody(`<h3>${escHtml(t('Rebuild host'))}</h3><div id="rb-body"></div>`);
  showLoading(document.getElementById('rb-body'), t('Loading images…'));
  m.open();

  let images = [];
  let profiles = [];
  let transport = { acknowledged: false };
  try {
    [images, profiles, transport] = await Promise.all([
      apiJson('/api/v1/reprovision/images/'),
      apiJson('/api/v1/reprovision/profiles/'),
      // A standing acknowledgement made in Settings means this ceremony does
      // not ask again.
      apiJson('/api/v1/hosts/transport-ack/').catch(() => ({ acknowledged: false })),
    ]);
  } catch (e) {
    m.setBody(`<h3>${escHtml(t('Rebuild host'))}</h3><div id="rb-body"></div>` +
      `<div class="modal-actions"><button class="btn btn-outline" id="rb-cancel">${escHtml(t('Close'))}</button></div>`);
    document.getElementById('rb-cancel').onclick = m.close;
    showLoadFailure(document.getElementById('rb-body'), t('the rebuild images'), e, () => openRebuildModal(hostId, hostname));
    return;
  }

  const ready = images.filter((i) => i.status === 'ready');
  if (!ready.length) {
    m.setBody(`<h3>${escHtml(t('Rebuild host'))}</h3>` +
      `<p class="muted">${escHtml(t('No images are ready. Pull or upload one under Reprovision → Add an image.'))}</p>` +
      '<div class="modal-actions"><button class="btn btn-outline" id="rb-cancel">' + escHtml(t('Close')) + '</button></div>');
    document.getElementById('rb-cancel').onclick = m.close;
    return;
  }

  const insecure = window.location.protocol !== 'https:' && !transport.acknowledged;
  const imageOpts = ready.map((i) =>
    `<option value="${escAttr(i.id)}">${escHtml(i.name)} (${escHtml(i.architecture)})</option>`).join('');

  m.setBody(
    `<h3>${escHtml(t('Rebuild {hostname}', { hostname }))}</h3>` +
    `<p class="bad"><strong>${escHtml(t('This erases everything on the target disk.'))}</strong> ` +
    `${escHtml(t('There is no undo once the installer starts.'))}</p>` +
    `<label>${escHtml(t('Image'))}<select class="form-control" id="rb-image">${imageOpts}</select></label>` +
    `<label>${escHtml(t('Profile'))}<select class="form-control" id="rb-profile"></select></label>` +
    `<div id="rb-summary" class="rb-summary muted">${escHtml(t('Select an image and profile…'))}</div>` +
    `<div id="rb-preflight" class="rb-preflight"><span class="muted">${escHtml(t('Checking rebuild readiness…'))}</span></div>` +
    (insecure
      ? `<label class="rb-warn"><input type="checkbox" id="rb-ack-plain"> ` +
        `${escHtml(t('Vigil is served over plain HTTP. The answer file carries the admin password hash, SSH keys, and the enrolment token in clear text. Proceed anyway.'))}</label>`
      : '') +
    `<hr>` +
    `<label>${escHtml(t('Password'))}<input class="form-control" type="password" id="rb-password" autocomplete="current-password"></label>` +
    // placeholder is set as a DOM property below, not interpolated: escHtml
    // does not escape quotes, and the hostname is agent-supplied.
    `<label>${escHtml(t('Type the hostname to confirm'))}<input class="form-control mono" id="rb-hostname" autocomplete="off"></label>` +
    `<div class="modal-actions">` +
      `<button class="btn btn-outline" id="rb-cancel">${escHtml(t('Cancel'))}</button>` +
      `<button class="btn btn-danger" id="rb-go" disabled>${escHtml(t('Rebuild this machine'))}</button>` +
    `</div>`);

  const imageSel = /** @type {HTMLSelectElement} */ (document.getElementById('rb-image'));
  const profileSel = /** @type {HTMLSelectElement} */ (document.getElementById('rb-profile'));
  const summary = document.getElementById('rb-summary');
  const hostnameInput = /** @type {HTMLInputElement} */ (document.getElementById('rb-hostname'));
  hostnameInput.placeholder = hostname;
  const goBtn = /** @type {HTMLButtonElement} */ (document.getElementById('rb-go'));
  const ackBox = /** @type {HTMLInputElement|null} */ (document.getElementById('rb-ack-plain'));

  function currentProfile() {
    return profiles.find((p) => p.id === profileSel.value);
  }

  function refreshSummary() {
    const img = ready.find((i) => i.id === imageSel.value);
    const prof = currentProfile();
    if (!img || !prof) { summary.textContent = t('Select an image and profile…'); return; }
    summary.innerHTML =
      `<div><span class="muted">${escHtml(t('Target disk'))}</span> <strong class="bad">${escHtml(prof.disk_target)}</strong></div>` +
      `<div><span class="muted">${escHtml(t('Image'))}</span> ${escHtml(img.name)}</div>` +
      `<div><span class="muted">${escHtml(t('Checksum'))}</span> <code>${escHtml(img.sha256)}</code></div>` +
      `<div><span class="muted">${escHtml(t('Network'))}</span> ${escHtml(prof.network_mode)}` +
        (prof.network_mode === 'static' ? ` ${escHtml(prof.static_address || '')}` : '') + `</div>`;
  }

  function refreshProfiles() {
    const forImage = profiles.filter((p) => p.image === imageSel.value);
    profileSel.innerHTML = forImage.length
      ? forImage.map((p) => `<option value="${escAttr(p.id)}">${escHtml(p.name)}</option>`).join('')
      : `<option value="">${escHtml(t('No profile for this image'))}</option>`;
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
  // Enter on a hostname that does not match shakes the field (the design
  // language's Destructive variant) instead of silently doing nothing.
  hostnameInput.onkeydown = (e) => {
    if (e.key === 'Enter' && hostnameInput.value !== hostname) shakeEl(hostnameInput);
  };
  if (ackBox) ackBox.onchange = refreshGate;
  document.getElementById('rb-cancel').onclick = m.close;
  refreshProfiles();

  _runPreflight(hostId);

  goBtn.onclick = async () => {
    const totp = await totpPrompt(t('Rebuilding {hostname} erases it and reinstalls the operating system.', { hostname }),
      { title: t('Rebuild this machine'), confirmText: t('Rebuild') });
    if (!totp) return;
    goBtn.disabled = true;
    try {
      const job = await apiJson('/api/v1/reprovision/jobs/', {
        method: 'POST',
        body: JSON.stringify({
          host: hostId,
          image: imageSel.value,
          profile: profileSel.value,
          password: /** @type {HTMLInputElement} */ (document.getElementById('rb-password')).value,
          totp,
          typed_hostname: hostnameInput.value,
          acknowledge_plaintext_transport: !!(ackBox && ackBox.checked),
        }),
      });
      _showJobProgress(m, job, hostname);
    } catch (e) {
      showToast(e.message || t('Rebuild was refused'), 'error');
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
      box.innerHTML = `<span class="muted">${escHtml(t('Readiness check dispatched — results appear after the agent next checks in.'))}</span>`;
      return;
    }
    const rows = result.disks.map((d) => {
      return `<li><code>${escHtml(d.name)}</code> ${escHtml(_bytesToGiB(d.size_bytes))}` +
        (d.mountpoints && d.mountpoints.length
          ? ` <span class="muted">${escHtml(t('mounted at {paths}', { paths: d.mountpoints.join(', ') }))}</span>` : '') +
        `</li>`;
    }).join('');
    box.innerHTML =
      (result.ok
        ? `<p class="ok">${escHtml(t('Preflight passed.'))}</p>`
        : `<p class="bad">${escHtml(t('Preflight failed: {failures}', { failures: (result.failures || []).join('; ') }))}</p>`) +
      `<p class="muted">${escHtml(t('Disks found on this machine — check the target above is the one you mean:'))}</p><ul class="rb-disks">${rows}</ul>`;
  } catch (e) {
    box.innerHTML = `<span class="muted">${escHtml(t('Readiness check unavailable.'))}</span>`;
  }
}

function _showJobProgress(m, job, hostname) {
  let remaining = ABORT_WINDOW_SECONDS;
  let timer = null;
  let poll = null;

  function stop() {
    if (timer) clearPollingInterval(timer);
    if (poll) clearPollingInterval(poll);
  }

  function render(current) {
    const abortable = ABORTABLE.includes(current.state);
    m.setBody(
      `<h3>${escHtml(t('Rebuilding {hostname}', { hostname }))}</h3>` +
      `<p class="rb-state ${_stateClass(current.state)}">${escHtml(current.state)}</p>` +
      (current.failure_reason
        ? `<p class="bad">${escHtml(current.failure_reason)}</p>` : '') +
      (abortable
        ? `<p class="muted">${escHtml(t('Abort is still possible — nothing has been erased yet.'))}` +
          (remaining > 0 ? ` ${escHtml(t('Staging begins in {seconds}s.', { seconds: remaining }))}</p>` : '</p>')
        : (TERMINAL.includes(current.state)
          ? ''
          : `<p class="bad">${escHtml(t('Past the point of no return — the disk is being rewritten and this can no longer be stopped.'))}</p>`)) +
      `<div class="modal-actions">` +
        (abortable
          ? `<button class="btn btn-danger" id="rb-abort">${escHtml(t('Abort rebuild'))}</button>` : '') +
        `<button class="btn btn-outline" id="rb-close">${escHtml(t('Close'))}</button>` +
      `</div>`);
    document.getElementById('rb-close').onclick = () => { stop(); m.close(); };
    const abortBtn = /** @type {HTMLButtonElement|null} */ (document.getElementById('rb-abort'));
    if (abortBtn) {
      abortBtn.onclick = async () => {
        abortBtn.disabled = true;
        try {
          const updated = await apiJson(
            `/api/v1/reprovision/jobs/${job.id}/abort/`,
            { method: 'POST', body: JSON.stringify({}) });
          showToast(t('Rebuild aborted — nothing was erased'), 'success');
          render(updated);
        } catch (e) {
          showToast(e.message || t('Too late to abort'), 'error');
        }
      };
    }
  }

  render(job);
  timer = pollingInterval(() => {
    remaining -= 1;
    if (remaining <= 0) clearPollingInterval(timer);
  }, 1000);
  poll = pollingInterval(async () => {
    try {
      const current = await apiJson(`/api/v1/reprovision/jobs/${job.id}/`);
      render(current);
      if (TERMINAL.includes(current.state)) stop();
    } catch (e) { /* state-ok: transient — keep polling */ }
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
      list.innerHTML = `<p class="muted">${escHtml(t('No OS images registered yet.'))}</p>`;
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
            ? `<span class="bad">${escHtml(img.import_error || t('Import failed'))}</span>` : '') +
        `</div>` +
        `<div class="site-row-meta">` +
          `<span class="${_stateClass(img.status === 'ready' ? 'completed' : img.status)}">` +
            `${escHtml(img.status)}</span>` +
          `<div class="site-row-actions">` +
            `<button class="btn btn-rose btn-sm img-del" data-id="${escAttr(img.id)}">${escHtml(t('Delete'))}</button>` +
          `</div>` +
        `</div>`;
      list.appendChild(row);
    });
    list.querySelectorAll('.img-del').forEach((/** @type {HTMLElement} */ btn) => {
      btn.onclick = async () => {
        if (!await confirmModal(t('Delete this image? Rebuilds using it will fail.'))) return;
        try {
          await apiJson(`/api/v1/reprovision/images/${btn.dataset.id}/`,
                        { method: 'DELETE' });
          loadOSImages();
        } catch (e) { showToast(e.message || t('Could not delete'), 'error'); }
      };
    });
  } catch (e) {
    // Non-admins simply do not see this pane; a 403/404 here is not an error
    // worth shouting about.
    list.innerHTML = `<p class="muted">${escHtml(t('OS image management requires an admin account.'))}</p>`;
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
  if (mins < 1) return t('just now');
  if (mins < 60) return t('{m}m ago', { m: mins });
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return t('{h}h ago', { h: hrs });
  return t('{d}d ago', { d: Math.floor(hrs / 24) });
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
  span.textContent = rawValue || t('unknown'); // server-controlled enum either way
  return span;
}

async function loadReprovisionPage() {
  _reproLoadImages();
  _reproLoadProfiles();
  if (typeof loadAgentProfiles === 'function') loadAgentProfiles();
  _reproLoadAgentProfiles();
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
    p.textContent = t('Image library requires an admin account.');
    list.appendChild(p);
    return;
  }
  _reproImagesById = {};
  for (const img of images) {
    if (img && img.id) _reproImagesById[img.id] = img;
  }
  _reproRenderImageTable(list, images);
  // Catalog cards show what the library already holds, so they follow it —
  // including on the 4s poll, which is how a card flips from "Downloading…"
  // to "In library" without the operator reloading.
  _reproRenderCatalog();
}

function _reproRenderImageTable(list, images) {
  list.replaceChildren();
  if (!images.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = t('No OS images registered yet — pull one below.');
    list.appendChild(p);
    return;
  }

  const table = document.createElement('table');
  table.className = 'vuln-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const label of [t('Name'), t('Family'), t('Version'), t('Size'), t('Status'), '']) {
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
  // The distro mark. osLogo() builds its SVG from hardcoded literals and never
  // interpolates the name it is passed, so parsing it is safe; the name itself
  // still goes in as textContent below, because it is operator-supplied on the
  // custom-URL path.
  try {
    nameTd.appendChild(osLogoNode(img.name || ''));
  } catch (e) { /* state-ok: a missing mark is cosmetic — never block the row on it */ }
  nameTd.appendChild(document.createTextNode(img.name || ''));
  tr.appendChild(nameTd);

  const familyTd = document.createElement('td');
  familyTd.textContent = img.os_family || '';
  tr.appendChild(familyTd);

  const versionTd = document.createElement('td');
  versionTd.textContent = img.version || '';
  tr.appendChild(versionTd);

  const sizeTd = document.createElement('td');
  sizeTd.className = 'mono';
  sizeTd.textContent = img.size_bytes ? formatBytes(img.size_bytes) : t('unknown');
  tr.appendChild(sizeTd);

  const statusTd = document.createElement('td');
  statusTd.appendChild(_reproImageStatusCell(img));
  tr.appendChild(statusTd);

  const actTd = document.createElement('td');
  actTd.style.textAlign = 'right';
  const delBtn = document.createElement('button');
  delBtn.className = 'btn btn-outline btn-sm';
  delBtn.textContent = t('Delete');
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
      ? t('{downloaded} / {total}', { downloaded: formatBytes(downloaded), total: formatBytes(size) })
      : t('{downloaded} downloaded (total size unknown)', { downloaded: formatBytes(downloaded) });
    wrap.appendChild(label);
  }

  if (img.status === 'failed') {
    const err = document.createElement('div');
    err.style.cssText = 'margin-top:8px;font-size:12px;color:var(--rose-ink);max-width:320px;white-space:pre-wrap;word-break:break-word;';
    // import_error is free text produced by a failed import — the whole
    // diagnostic for a multi-gigabyte fetch, and operator-supplied on the
    // custom-URL path. textContent only; see SECURITY note atop this file.
    err.textContent = img.import_error || t('Import failed');
    wrap.appendChild(err);
  }

  return wrap;
}

async function _reproDeleteImage(id, name) {
  // confirmModal sets `message` via textContent internally (vigil-utils.js),
  // so interpolating the operator-supplied name here is safe.
  if (!await confirmModal(t('Delete "{name}"? Rebuilds using it will fail.', { name }))) return;
  try {
    await apiJson(`/api/v1/reprovision/images/${id}/`, { method: 'DELETE' });
    showToast(t('Image deleted'), 'success');
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || t('Could not delete image'), 'error');
  }
}

/* ── Pull controls: catalog, RHEL/Windows presets, custom URL ──────────── */

// The catalog is static server-shipped data (apps/reprovision/catalog.py),
// fetched once. Cards re-render from this cache whenever the library
// changes, so "already in your library" tracks a pull without a refetch.
let _reproCatalogEntries = [];

async function _reproLoadCatalog() {
  try {
    _reproCatalogEntries = await apiJson('/api/v1/reprovision/catalog/');
  } catch (e) {
    _reproCatalogEntries = [];
    const grid = document.getElementById('repro-catalog-grid');
    if (grid) showLoadFailure(grid, t('the app catalog'), e, _reproLoadCatalog);
    return;
  }
  _reproRenderCatalog();
}

// Digest → the library image holding those bytes. The server refuses a
// second pull of the same digest (409), so the card has to say so before
// the operator clicks rather than after.
function _reproLibraryByDigest() {
  const byDigest = {};
  for (const img of Object.values(_reproImagesById)) {
    if (img && img.sha256) byDigest[String(img.sha256).toLowerCase()] = img;
  }
  return byDigest;
}

function _reproRenderCatalog() {
  const grid = document.getElementById('repro-catalog-grid');
  const empty = document.getElementById('repro-catalog-empty');
  if (!grid) return;

  const entries = Array.isArray(_reproCatalogEntries) ? _reproCatalogEntries : [];
  // A failed catalog load leaves its failure in the grid (the filter can call
  // here with an empty list): keep it rather than drawing "no apps" over a
  // server error. The retry button offered there still works.
  if (!entries.length && grid.querySelector('.load-failed')) return;
  const held = _reproLibraryByDigest();
  grid.replaceChildren();
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object' || !entry.id) continue;
    grid.appendChild(_reproCatalogCard(entry, held[String(entry.sha256 || '').toLowerCase()]));
  }
  grid.style.display = entries.length ? '' : 'none';
  if (empty) empty.style.display = entries.length ? 'none' : 'block';
}

// `existing` is the library image with these bytes, or undefined.
function _reproCatalogCard(entry, existing) {
  const card = document.createElement('div');
  card.className = 'repro-cat-card';

  const logo = document.createElement('div');
  logo.className = 'repro-cat-logo';
  // Name only, never the family: derivatives carry their parent's family, so
  // appending it would put Ubuntu's logo on Linux Mint. Every catalog name
  // states its own distro.
  logo.appendChild(osLogoNode(entry.name || ''));
  card.appendChild(logo);

  const name = document.createElement('div');
  name.className = 'repro-cat-name';
  name.textContent = entry.name || entry.id;
  card.appendChild(name);

  const meta = document.createElement('div');
  meta.className = 'repro-cat-meta';
  meta.textContent = [entry.architecture, entry.size_bytes ? formatBytes(entry.size_bytes) : '']
    .filter(Boolean).join(' · ');
  card.appendChild(meta);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm repro-cat-btn';
  if (existing) {
    // Greyed rather than hidden: the operator asked for this image, and a
    // card that vanishes once pulled reads as the catalog having lost it.
    card.classList.add('is-held');
    btn.classList.add('btn-outline');
    btn.disabled = true;
    const status = String(existing.status || '').toLowerCase();
    btn.textContent = status === 'ready' ? t('In library')
      : status === 'failed' ? t('Retry pull')
      : t('Downloading…');
    if (status === 'failed') {
      // A failed pull retries in place on the same row — the one case where
      // pulling again is the right move rather than a duplicate.
      btn.disabled = false;
      btn.classList.add('repro-cat-retry');
    }
  } else {
    btn.classList.add('btn-mint');
    btn.textContent = t('Pull');
  }
  btn.addEventListener('click', () => _reproPullFromCatalog(entry.id, btn));
  card.appendChild(btn);
  return card;
}

async function _reproPullFromCatalog(catalogId, btn) {
  if (!catalogId) return;
  const label = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = t('Queueing…');
  }
  try {
    await apiJson('/api/v1/reprovision/images/pull/', {
      method: 'POST',
      body: JSON.stringify({ catalog_id: catalogId }),
    });
    showToast(t('Download queued'), 'success');
    // Repaints the card as held — _reproLoadImages re-renders the catalog.
    await _reproLoadImages();
  } catch (e) {
    showToast(e.message || t('Could not queue the pull'), 'error');
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
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
  const famSel = /** @type {HTMLSelectElement|null} */ (document.getElementById('repro-custom-family'));
  if (famSel) famSel.value = family;
  const fileInput = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-file'));
  if (fileInput) fileInput.focus();
}

function _reproRefreshCustomGate() {
  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById('repro-custom-submit'));
  if (!btn) return;
  const ack = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-ack'));
  const name = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-name'));
  const url = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-url'));
  const sha = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-sha256'));
  btn.disabled = !(ack && ack.checked &&
    name && name.value.trim() &&
    url && url.value.trim() &&
    sha && sha.value.trim());
}

async function _reproSubmitCustom() {
  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById('repro-custom-submit'));
  if (!btn) return;
  const nameEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-name'));
  const familyEl = /** @type {HTMLSelectElement} */ (document.getElementById('repro-custom-family'));
  const versionEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-version'));
  const urlEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-url'));
  const shaEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-sha256'));
  const ackEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-ack'));

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
    showToast(t('Download queued'), 'success');
    nameEl.value = '';
    versionEl.value = '';
    urlEl.value = '';
    shaEl.value = '';
    ackEl.checked = false;
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || t('Could not queue the pull'), 'error');
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
  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById('repro-upload-submit'));
  if (!btn) return;
  const name = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-name'));
  const sha = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-sha256'));
  const file = /** @type {HTMLInputElement|null} */ (document.getElementById('repro-custom-file'));
  btn.disabled = !(name && name.value.trim() &&
    sha && sha.value.trim() &&
    file && file.files && file.files.length > 0);
}

async function _reproSubmitUpload() {
  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById('repro-upload-submit'));
  if (!btn) return;
  const nameEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-name'));
  const familyEl = /** @type {HTMLSelectElement} */ (document.getElementById('repro-custom-family'));
  const versionEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-version'));
  const shaEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-sha256'));
  const fileEl = /** @type {HTMLInputElement} */ (document.getElementById('repro-custom-file'));
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
  btn.textContent = t('Uploading…');
  try {
    const resp = await fetch('/api/v1/reprovision/images/upload/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': getCsrf() },
      body: formData,
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || t('Upload failed'));
    showToast(t('Image uploaded and imported'), 'success');
    nameEl.value = '';
    versionEl.value = '';
    shaEl.value = '';
    fileEl.value = '';
    _reproLoadImages();
  } catch (e) {
    showToast(e.message || t('Could not upload the image'), 'error');
  } finally {
    btn.textContent = originalLabel;
    _reproRefreshUploadGate();
  }
}

/* ── Install profiles ────────────────────────────────────────────────── */

// Agent profiles for the install-profile form's picker. The Reimage page loads
// it with the rest of its data; the "none" option is what an install profile
// without one means to the API (null).
let _reproAgentProfiles = [];

async function _reproLoadAgentProfiles() {
  try {
    const rows = await apiJson('/api/v1/agent-profiles/');
    _reproAgentProfiles = Array.isArray(rows) ? rows : [];
  } catch (e) {
    // state-ok: a picker is a convenience; non-admin, or the endpoint is off,
    // and leaving it on "none" is a valid choice the form still accepts.
    _reproAgentProfiles = [];
  }
  _reproFillProfileAgentProfiles();
}

function _reproFillProfileAgentProfiles() {
  const sel = document.getElementById('repro-prof-agentprofile');
  if (!sel) return;
  sel.replaceChildren();
  const none = document.createElement('option');
  none.value = '';
  none.textContent = t('none');
  sel.appendChild(none);
  for (const ap of _reproAgentProfiles) {
    const opt = document.createElement('option');
    opt.value = String(ap.id);
    opt.textContent = ap.name;
    sel.appendChild(opt);
  }
}

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
    p.textContent = t('Install profiles require an admin account.');
    list.appendChild(p);
    return;
  }
  list.replaceChildren();
  if (!profiles.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = t('No install profiles yet.');
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
  const imgLabel = img ? img.name : t('image {image}', { image: prof.image || '?' });
  const diskLabel = t('disk {disk}', { disk: prof.disk_target || '?' });
  let descText = t('{image} · {disk} · {mode}', { image: imgLabel, disk: diskLabel, mode: prof.network_mode || '?' });
  if (prof.network_mode === 'static' && prof.static_address) {
    descText += ` ${prof.static_address}`;
  }
  if (Array.isArray(prof.completion_tags) && prof.completion_tags.length) {
    descText += ` · ${t('tags {tags}', { tags: prof.completion_tags.join(', ') })}`;
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

  const edit = document.createElement('button');
  edit.className = 'btn btn-outline btn-sm';
  edit.type = 'button';
  edit.textContent = t('Edit');
  edit.addEventListener('click', () => _reproOpenProfile(prof));
  meta.appendChild(edit);

  const del = document.createElement('button');
  del.className = 'btn btn-outline btn-sm img-del';
  del.type = 'button';
  del.textContent = t('Delete');
  del.addEventListener('click', () => _reproDeleteProfile(prof));
  meta.appendChild(del);

  row.appendChild(meta);

  return row;
}

/* ── Install profile editor ──────────────────────────────────────────────
   Every field the installer needs to run unattended. The API has always
   supported create/edit/delete; until now the page could only list what
   someone had created through it directly. */

// Non-empty means editing that profile; empty means creating one.
function _reproProfileModal() {
  return document.getElementById('repro-profile-modal');
}

function _reproSetProfileOpen(open) {
  const modal = _reproProfileModal();
  const overlay = document.getElementById('repro-profile-overlay');
  if (!modal || !overlay) return;
  modal.classList.toggle('open', open);
  overlay.classList.toggle('open', open);
}

// Static/DHCP: the static fields are meaningless under DHCP, and the
// serializer rejects static without an address and gateway.
function _reproSyncProfileNetwork() {
  const mode = (/** @type {HTMLSelectElement|null} */ (document.getElementById('repro-prof-netmode')))?.value;
  for (const el of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('#repro-profile-modal .repro-prof-static'))) {
    el.style.display = mode === 'static' ? '' : 'none';
  }
}

function _reproFillProfileImages(selectedId) {
  const sel = /** @type {HTMLSelectElement|null} */ (document.getElementById('repro-prof-image'));
  const hint = document.getElementById('repro-prof-image-hint');
  if (!sel) return;
  sel.replaceChildren();
  // Only READY images can be installed from — one still downloading has no
  // extracted tree for the installer to fetch.
  const ready = Object.values(_reproImagesById)
    .filter(img => img && (img.status === 'ready' || img.id === selectedId));
  for (const img of ready) {
    const opt = document.createElement('option');
    opt.value = img.id;
    opt.textContent = img.status === 'ready'
      ? (img.name || img.id)
      : t('{name} (not ready)', { name: img.name || img.id });
    sel.appendChild(opt);
  }
  if (selectedId) sel.value = String(selectedId);
  if (hint) {
    hint.textContent = ready.length ? ''
      : t('No image is ready yet — pull or upload one first.');
  }
}

function _reproOpenProfile(prof) {
  const modal = _reproProfileModal();
  if (!modal) return;
  const editing = prof && prof.id ? String(prof.id) : '';
  modal.dataset.editing = editing;
  document.getElementById('repro-profile-title').textContent =
    editing ? t('Edit install profile') : t('New install profile');

  const set = (/** @type {string} */ id, /** @type {string|number|null|undefined} */ value) => {
    const el = /** @type {HTMLInputElement|null} */ (document.getElementById(id));
    if (el) el.value = /** @type {string} */ (value);
  };
  set('repro-prof-name', prof?.name || '');
  set('repro-prof-disk', prof?.disk_target || '');
  set('repro-prof-partition', prof?.partition_scheme || 'lvm');
  set('repro-prof-fs', prof?.filesystem || 'ext4');
  set('repro-prof-deadline', prof?.deadline_minutes ?? 60);
  set('repro-prof-netmode', prof?.network_mode || 'dhcp');
  set('repro-prof-address', prof?.static_address || '');
  set('repro-prof-gateway', prof?.gateway || '');
  set('repro-prof-dns', prof?.dns || '');
  set('repro-prof-timezone', prof?.timezone || 'UTC');
  set('repro-prof-locale', prof?.locale || 'en_US.UTF-8');
  set('repro-prof-keyboard', prof?.keyboard || 'us');
  set('repro-prof-user', prof?.admin_username || '');
  set('repro-prof-keys', prof?.ssh_authorized_keys || '');
  set('repro-prof-raw', prof?.raw_append || '');
  set('repro-prof-packages',
      Array.isArray(prof?.extra_packages) ? prof.extra_packages.join(', ') : '');
  set('repro-prof-tags',
      Array.isArray(prof?.completion_tags) ? prof.completion_tags.join(', ') : '');

  // The API never returns a stored password, so the box always starts empty
  // and blank means "leave whatever is stored alone" on an edit.
  set('repro-prof-password', '');
  const pwHint = document.getElementById('repro-prof-password-hint');
  if (pwHint) {
    pwHint.textContent = editing
      ? t('Leave blank to keep the stored hash. A crypt hash, never a plaintext password.')
      : t('A crypt hash, never a plaintext password — the answer file carries it verbatim. Generate one with {command}', { command: 'mkpasswd -m sha-512' });
  }

  sitePickFill(/** @type {HTMLSelectElement} */ (document.getElementById('repro-prof-site')), /** @type {HTMLElement} */ (document.getElementById('repro-prof-site-field')),
    prof ? prof.site_id : null, { editing: !!editing });
  _reproFillProfileImages(prof?.image);
  _reproFillProfileAgentProfiles();
  const agentProfile = /** @type {HTMLSelectElement|null} */ (document.getElementById('repro-prof-agentprofile'));
  if (agentProfile) agentProfile.value = prof?.agent_profile ? String(prof.agent_profile) : '';
  _reproSyncProfileNetwork();
  _reproSetProfileOpen(true);
}

async function _reproSaveProfile() {
  const modal = _reproProfileModal();
  if (!modal) return;
  const val = (/** @type {string} */ id) => ((/** @type {HTMLInputElement|null} */ (document.getElementById(id)))?.value || '').trim();
  const editing = modal.dataset.editing;

  const body = {
    name: val('repro-prof-name'),
    image: val('repro-prof-image'),
    disk_target: val('repro-prof-disk'),
    partition_scheme: val('repro-prof-partition'),
    filesystem: val('repro-prof-fs'),
    network_mode: val('repro-prof-netmode'),
    static_address: val('repro-prof-address'),
    gateway: val('repro-prof-gateway'),
    dns: val('repro-prof-dns'),
    timezone: val('repro-prof-timezone'),
    locale: val('repro-prof-locale'),
    keyboard: val('repro-prof-keyboard'),
    admin_username: val('repro-prof-user'),
    ssh_authorized_keys: val('repro-prof-keys'),
    raw_append: val('repro-prof-raw'),
    extra_packages: val('repro-prof-packages').split(',')
      .map(p => p.trim()).filter(Boolean),
    completion_tags: val('repro-prof-tags').split(',')
      .map(t => t.trim()).filter(Boolean),
    deadline_minutes: parseInt(val('repro-prof-deadline'), 10) || 60,
    // null clears it: the serializer's allow_null reads "" as a bad id.
    agent_profile: val('repro-prof-agentprofile') || null,
  };
  const password = val('repro-prof-password');
  // Omitted rather than sent blank on an edit: the serializer reads a blank
  // as "no password given" and would fail its own can-anyone-log-in check
  // for a profile that already has one stored.
  if (password) body.admin_password = password;
  const siteId = sitePickValue(document.getElementById('repro-prof-site'),
                               document.getElementById('repro-prof-site-field'));
  if (siteId !== undefined) body.site_id = siteId;

  if (!body.name) return showToast(t('Give the profile a name'), 'error');
  if (!body.image) return showToast(t('Pick an image'), 'error');
  if (!body.disk_target) return showToast(t('Name the target disk'), 'error');
  if (!body.admin_username) return showToast(t('Set an admin username'), 'error');

  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById('repro-profile-save'));
  if (btn) btn.disabled = true;
  try {
    if (editing) {
      await apiJson(`/api/v1/reprovision/profiles/${editing}/`,
                    { method: 'PATCH', body: JSON.stringify(body) });
    } else {
      await apiJson('/api/v1/reprovision/profiles/',
                    { method: 'POST', body: JSON.stringify(body) });
    }
    showToast(t('Profile saved'), 'success');
    _reproSetProfileOpen(false);
    _reproLoadProfiles();
  } catch (e) {
    showToast(e.message || t('Could not save the profile'), 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _reproDeleteProfile(prof) {
  if (!prof || !prof.id) return;
  const ok = await confirmModal(
    t('Delete install profile “{name}”?', { name: prof.name || prof.id }),
    { confirmText: t('Delete'), danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/reprovision/profiles/${prof.id}/`, { method: 'DELETE' });
    showToast(t('Profile deleted'), 'success');
    _reproLoadProfiles();
  } catch (e) {
    showToast(e.message || t('Could not delete the profile'), 'error');
  }
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
    p.textContent = t('Could not load rebuild jobs.');
    list.appendChild(p);
    return;
  }
  list.replaceChildren();
  if (!jobs.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = t('No rebuild jobs yet.');
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
  desc.textContent = t('{image} · {profile} · requested {ago}', { image: job.image_name || '', profile: job.profile_name || '', ago: _reproAge(job.requested_at) });
  main.appendChild(desc);

  if (job.state === 'failed' && job.failure_reason) {
    const err = document.createElement('span');
    err.style.cssText = 'font-size:12px;color:var(--rose-ink);';
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
  _reproPollTimer = pollingInterval(() => {
    if (_reproPageVisible()) {
      _reproLoadImages();
      _reproLoadJobs();
    }
  }, 4000);
}

/* ── Static control wiring ───────────────────────────────────────────── */

// No catalog-wide pull button: each card owns its own, bound in
// _reproCatalogCard, because the button's label and enabled state depend on
// whether that entry is already in the library.

document.getElementById('repro-profile-new-btn')
  ?.addEventListener('click', () => _reproOpenProfile(null));

// Start a rebuild without going to find the host card first. The ceremony
// itself is unchanged — picking the host here only replaces the drawer as the
// way in, and openRebuildModal still refuses a host that cannot be rebuilt.
document.getElementById('repro-rebuild-btn')?.addEventListener('click', () => {
  openPicker({
    type: 'machine',
    title: t('Which host should be rebuilt?'),
    allowAdd: false,
    onSelect: (item) => {
      closePicker();
      openRebuildModal(item.key, item.name);
    },
  });
});
document.getElementById('repro-profile-save')
  ?.addEventListener('click', _reproSaveProfile);
document.getElementById('repro-prof-netmode')
  ?.addEventListener('change', _reproSyncProfileNetwork);
for (const id of ['repro-profile-close', 'repro-profile-cancel',
                  'repro-profile-overlay']) {
  document.getElementById(id)
    ?.addEventListener('click', () => _reproSetProfileOpen(false));
}
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
window.navigateTo = function (page) {
  _reproNavigateTo(page);
  if (page === 'reprovision') loadReprovisionPage();
};
