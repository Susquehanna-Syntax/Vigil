// vigil-statuspages.js
// Owns: the Status Pages section — create/edit public status pages, choose
// which machines appear, and give each a custom public display name.
// Depends on: vigil-utils.js (apiJson, confirmModal, showToast, escHtml).

let _spHosts = [];   // all selectable hosts, cached
let _spDevices = [];  // all selectable devices, reloaded with the hosts

async function loadStatusPages() {
  const list = document.getElementById('sp-list');
  if (!list) return;
  try {
    const [pages, hosts] = await Promise.all([
      apiJson('/api/v1/status-pages/'),
      apiJson('/api/v1/status-pages/hosts/'),
    ]);
    _spHosts = hosts;
    // Devices are optional on a page: a failed load leaves the picker empty
    // rather than failing the whole list.
    try {
      _spDevices = await apiJson('/api/v1/status-pages/devices/');
    } catch { _spDevices = []; }
    if (!pages.length) {
      list.innerHTML = `<div class="empty-block">
        <h4>No status pages yet</h4>
        <p>Create a public page to share the health of your hosts — a link you can hand to a client or pin somewhere visible.</p></div>`;
      return;
    }
    list.innerHTML = pages.map(_spCard).join('');
    pages.forEach(p => _wireCard(p));
  } catch (e) {
    list.innerHTML = `<div class="empty-block"><h4>Couldn't load</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

function _spCard(p) {
  const url = window.location.origin + p.url;
  const selected = new Set((p.host_ids || []).map(String));
  const hostRows = _spHosts.map(h => {
    const on = selected.has(String(h.id));
    const label = (p.host_labels || {})[String(h.id)] || '';
    return `<div class="sp-host">
      <input type="checkbox" data-sp-host="${escAttr(String(h.id))}" ${on ? 'checked' : ''}>
      <input type="text" class="sp-host-label" data-sp-hlabel="${escAttr(String(h.id))}"
             placeholder="${escAttr(h.hostname)}" value="${escAttr(label)}">
      <span class="sp-host-status ${h.up ? 'up' : 'down'}">${h.up ? 'up' : 'down'}</span>
    </div>`;
  }).join('') || '<p class="muted-note">No hosts available yet.</p>';

  const selectedDevices = new Set((p.device_ids || []).map(String));
  // A device's state dot: grey when none of its checks has ever reported.
  const deviceRows = (_spDevices || []).map(d => {
    const on = selectedDevices.has(String(d.id));
    const label = (p.device_labels || {})[String(d.id)] || '';
    return `<div class="sp-host">
      <input type="checkbox" data-sp-device="${escAttr(String(d.id))}" ${on ? 'checked' : ''}>
      <input type="text" class="sp-host-label" data-sp-dlabel="${escAttr(String(d.id))}"
             placeholder="${escAttr(d.name)}" value="${escAttr(label)}">
      <span class="sp-host-status ${d.state === 'up' ? 'up' : d.state === 'down' ? 'down' : 'unknown'}">
        <span class="sp-device-dot ${d.state === 'up' || d.state === 'down' ? d.state : 'none'}"></span>${d.state || 'no checks'}</span>
    </div>`;
  }).join('') || '<p class="muted-note">No devices yet. Add them on the Inventory page.</p>';

  const branding = p.is_primary ? '' : `
    <label class="setting-check" style="margin-bottom:14px;">
      <input type="checkbox" data-sp-brand="${p.id}" ${p.hide_badge ? 'checked' : ''}>
      Hide the "Powered by Vigil" badge <span class="chip chip-muted">Business</span>
    </label>`;

  return `<div class="sp-card" data-sp-id="${p.id}">
    <div class="sp-card-head">
      <input type="text" class="form-control sp-title-input" data-sp-title="${p.id}" value="${escAttr(p.title)}">
      <label class="setting-check"><input type="checkbox" data-sp-enabled="${p.id}" ${p.enabled ? 'checked' : ''}> Public</label>
      ${p.is_primary ? '<span class="chip">primary · free</span>' : '<span class="chip chip-muted">extra page</span>'}
    </div>
    <div class="sp-url">${p.enabled
        ? `Live at <a href="${escAttr(url)}" target="_blank" rel="noopener">${escHtml(url)}</a>`
        : '<span class="muted-note">Enable to publish. Nobody can see it while off.</span>'}</div>
    <div class="section-label" style="margin-top:0;">Machines on this page <span class="muted-note" style="text-transform:none;letter-spacing:0;font-weight:400;">— none checked = all hosts</span></div>
    <div class="sp-hosts">${hostRows}</div>
    <div class="section-label">Devices <span class="muted-note" style="text-transform:none;letter-spacing:0;font-weight:400;">— only the ones checked</span></div>
    <div class="sp-hosts">${deviceRows}</div>
    ${branding}
    <div class="sp-card-actions">
      <button class="btn btn-mint btn-sm" data-sp-save="${p.id}">Save</button>
      <button class="btn btn-peach btn-sm" data-sp-rotate="${p.id}">Rotate URL</button>
      <button class="btn btn-rose btn-sm" data-sp-del="${p.id}">Delete</button>
    </div>
  </div>`;
}

function _collect(card) {
  const host_ids = [...card.querySelectorAll('[data-sp-host]')].filter(c => c.checked)
    .map(c => c.dataset.spHost);
  const host_labels = {};
  card.querySelectorAll('[data-sp-hlabel]').forEach(inp => {
    if (inp.value.trim()) host_labels[inp.dataset.spHlabel] = inp.value.trim();
  });
  // An unchecked device publishes nothing, so its public name is dropped too.
  const device_ids = [...card.querySelectorAll('[data-sp-device]')].filter(c => c.checked)
    .map(c => c.dataset.spDevice);
  const device_labels = {};
  card.querySelectorAll('[data-sp-dlabel]').forEach(inp => {
    const cb = card.querySelector(`[data-sp-device="${inp.dataset.spDlabel}"]`);
    if (cb && cb.checked && inp.value.trim()) device_labels[inp.dataset.spDlabel] = inp.value.trim();
  });
  const brand = card.querySelector('[data-sp-brand]');
  const body = {
    title: card.querySelector('[data-sp-title]').value,
    enabled: card.querySelector('[data-sp-enabled]').checked,
    host_ids, host_labels, device_ids, device_labels,
  };
  if (brand) body.hide_badge = brand.checked;
  return body;
}

function _wireCard(p) {
  const card = document.querySelector(`.sp-card[data-sp-id="${p.id}"]`);
  card.querySelector(`[data-sp-save="${p.id}"]`).addEventListener('click', async () => {
    try {
      await apiJson(`/api/v1/status-pages/${p.id}/`, { method: 'PATCH', body: JSON.stringify(_collect(card)) });
      showToast('Status page saved', 'success');
      loadStatusPages();
    } catch (e) {
      showToast(e.message.includes('402') ? 'Branding is a Business feature' : 'Save failed: ' + e.message, 'error');
    }
  });
  card.querySelector(`[data-sp-rotate="${p.id}"]`).addEventListener('click', async () => {
    if (!(await confirmModal('Rotate this page\'s URL? The old link stops working immediately.', { confirmText: 'Rotate' }))) return;
    await apiJson(`/api/v1/status-pages/${p.id}/`, { method: 'PATCH', body: JSON.stringify({ rotate_token: true }) });
    showToast('New URL issued', 'success'); loadStatusPages();
  });
  card.querySelector(`[data-sp-del="${p.id}"]`).addEventListener('click', async () => {
    if (!(await confirmModal('Delete this status page?', { danger: true, confirmText: 'Delete' }))) return;
    await fetch(`/api/v1/status-pages/${p.id}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadStatusPages();
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const nb = document.getElementById('sp-new-btn');
  if (nb) nb.addEventListener('click', async () => {
    const site = await sitePickChoose('New status page');
    if (site === false) return;
    const body = site === undefined ? {} : { site_id: site };
    try { await apiJson('/api/v1/status-pages/', { method: 'POST', body: JSON.stringify(body) }); loadStatusPages(); }
    catch (e) { showToast(e.message.includes('402') ? 'Additional pages are a Business feature' : e.message, 'error'); }
  });
});

if (typeof navigateTo === 'function') {
  const _origNavSp = navigateTo;
  navigateTo = function (p) { _origNavSp(p); if (p === 'statuspages') loadStatusPages(); };
}
