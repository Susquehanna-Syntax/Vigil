// @ts-check
/** A row of /api/v1/hosts/devices/, and the fields this file's form writes back. */
/** @typedef {{id: string, name?: string|null, device_type?: string|null, device_type_label?: string|null, ip_address?: string|null, mac_address?: string|null, serial?: string|null, vendor?: string|null, location?: string|null, owner?: string|null, warranty_end?: string|null, notes?: string|null, site_id?: string|number|null, extra?: Record<string, string>|null}} InventoryDevice */
/** What the device form reads: every field is an input or select in the modal. */
/** @typedef {Omit<InventoryDevice, 'id'|'device_type_label'> & {site_id?: string|number|null}} DeviceFormBody */
// vigil-devices.js — Unmanaged device inventory (manual registry on the Inventory page).
// Depends on: vigil-utils.js (apiJson, showToast, escHtml), vigil-nav.js (navigateTo).

/** @type {InventoryDevice[]} */
let _devices = [];

/** @returns {Promise<void>} */
async function loadDevices() {
  try {
    _devices = await apiJson('/api/v1/hosts/devices/');
  } catch (e) {
    _devices = [];
  }
  renderDevices();
}

function renderDevices() {
  const wrap = /** @type {HTMLElement|null} */ (document.getElementById('dev-content'));
  const countEl = /** @type {HTMLElement|null} */ (document.getElementById('dev-count'));
  if (!wrap) return;

  if (!_devices.length) {
    if (countEl) countEl.textContent = '';
    wrap.innerHTML = '<div class="dev-empty">No unmanaged devices yet. Add routers, switches, '
      + 'printers and other gear to keep a full picture of the network.</div>';
    return;
  }
  if (countEl) countEl.textContent = _devices.length === 1 ? '1 device' : `${_devices.length} devices`;

  let html = `<div class="dev-card"><table class="ctr-table dev-table">
    <thead><tr>
      <th>Name</th><th>Type</th><th>IP Address</th><th>MAC</th><th>Serial</th>
      <th>Vendor</th><th>Location</th><th>Owner</th><th class="num"></th>
    </tr></thead><tbody>`;
  for (const d of _devices) {
    html += `<tr>
      <td class="ctr-name">${escHtml(d.name || '')}</td>
      <td>${escHtml(d.device_type_label || '')}</td>
      <td class="dev-mono">${escHtml(d.ip_address || '—')}</td>
      <td class="dev-mono">${escHtml(d.mac_address || '—')}</td>
      <td class="dev-mono">${escHtml(d.serial || '—')}</td>
      <td>${escHtml(d.vendor || '—')}</td>
      <td>${escHtml(d.location || '—')}</td>
      <td>${escHtml(d.owner || '—')}</td>
      <td class="num"><button class="btn btn-ghost btn-sm" data-device-edit="${escAttr(d.id)}">Edit</button></td>
    </tr>`;
  }
  html += `</tbody></table></div>`;
  wrap.innerHTML = html;
}

/** @param {string|number} id @returns {void} */
function openDeviceModal(id) {
  const d = id ? _devices.find(x => x.id === id) : null;
  /** @type {HTMLElement} */ (document.getElementById('device-modal-title')).textContent = d ? 'Edit Device' : 'Add Device';
  /** @type {HTMLInputElement} */ (document.getElementById('device-id')).value = d ? d.id : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-name')).value = d ? (d.name || '') : '';
  sitePickFill(/** @type {HTMLSelectElement} */ (document.getElementById('device-site')),
    /** @type {HTMLElement} */ (document.getElementById('device-site-field')),
    d ? d.site_id : null, { editing: !!d });
  /** @type {HTMLSelectElement} */ (document.getElementById('device-type')).value = d ? (d.device_type || 'other') : 'other';
  /** @type {HTMLInputElement} */ (document.getElementById('device-ip')).value = d ? (d.ip_address || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-mac')).value = d ? (d.mac_address || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-vendor')).value = d ? (d.vendor || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-location')).value = d ? (d.location || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-serial')).value = d ? (d.serial || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-warranty')).value = d ? (d.warranty_end || '') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('device-owner')).value = d ? (d.owner || '') : '';
  /** @type {HTMLTextAreaElement} */ (document.getElementById('device-extra')).value = d ? _extraToLines(d.extra) : '';
  /** @type {HTMLTextAreaElement} */ (document.getElementById('device-notes')).value = d ? (d.notes || '') : '';
  /** @type {HTMLElement} */ (document.getElementById('device-delete-btn')).style.display = d ? 'inline-flex' : 'none';
  /** @type {HTMLElement} */ (document.getElementById('device-overlay')).classList.add('open');
  /** @type {HTMLElement} */ (document.getElementById('device-modal')).classList.add('open');
}

/** @returns {void} */
function closeDeviceModal() {
  /** @type {HTMLElement} */ (document.getElementById('device-overlay')).classList.remove('open');
  /** @type {HTMLElement} */ (document.getElementById('device-modal')).classList.remove('open');
}

/** @returns {DeviceFormBody} */
function _deviceFormBody() {
  const ip = /** @type {HTMLInputElement} */ (document.getElementById('device-ip')).value.trim();
  return {
    name: /** @type {HTMLInputElement} */ (document.getElementById('device-name')).value.trim(),
    device_type: /** @type {HTMLSelectElement} */ (document.getElementById('device-type')).value,
    ip_address: ip || null,
    mac_address: /** @type {HTMLInputElement} */ (document.getElementById('device-mac')).value.trim(),
    vendor: /** @type {HTMLInputElement} */ (document.getElementById('device-vendor')).value.trim(),
    location: /** @type {HTMLInputElement} */ (document.getElementById('device-location')).value.trim(),
    serial: /** @type {HTMLInputElement} */ (document.getElementById('device-serial')).value.trim(),
    warranty_end: /** @type {HTMLInputElement} */ (document.getElementById('device-warranty')).value || null,
    owner: /** @type {HTMLInputElement} */ (document.getElementById('device-owner')).value.trim(),
    extra: _extraFromBody(/** @type {HTMLTextAreaElement} */ (document.getElementById('device-extra')).value),
    notes: /** @type {HTMLTextAreaElement} */ (document.getElementById('device-notes')).value.trim(),
  };
}

/** The extra-facts object as the textarea shows it: "Key: value" per line. */
/** @param {Record<string, string>|null|undefined} extra @returns {string} */
function _extraToLines(extra) {
  return Object.entries(extra || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
}

/** And back: split on the first colon so a value may carry its own colons. */
/** @param {string} text @returns {Record<string, string>} */
function _extraFromBody(text) {
  /** @type {Record<string, string>} */
  const extra = {};
  for (const line of (text || '').split('\n')) {
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    if (!key) continue;
    extra[key] = line.slice(colon + 1).trim();
  }
  return extra;
}

/** @returns {Promise<void>} */
async function saveDevice() {
  const id = /** @type {HTMLInputElement} */ (document.getElementById('device-id')).value;
  const body = _deviceFormBody();
  if (!body.name) { showToast('Name is required', 'error'); return; }
  const siteId = sitePickValue(/** @type {HTMLSelectElement} */ (document.getElementById('device-site')),
    /** @type {HTMLElement} */ (document.getElementById('device-site-field')));
  if (siteId !== undefined) body.site_id = siteId;
  try {
    if (id) {
      await apiJson(`/api/v1/hosts/devices/${id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast('Device updated', 'success');
    } else {
      await apiJson('/api/v1/hosts/devices/', { method: 'POST', body: JSON.stringify(body) });
      showToast('Device added', 'success');
    }
    closeDeviceModal();
    loadDevices();
  } catch (e) {
    showToast(e.message || 'Could not save device — check the IP and MAC format', 'error');
  }
}

/** @returns {Promise<void>} */
async function deleteDeviceFromModal() {
  const id = /** @type {HTMLInputElement} */ (document.getElementById('device-id')).value;
  if (!id) return;
  if (!confirm('Delete this device from the inventory?')) return;
  try {
    await apiJson(`/api/v1/hosts/devices/${id}/`, { method: 'DELETE' });
    showToast('Device deleted', 'success');
    closeDeviceModal();
    loadDevices();
  } catch (e) {
    showToast(e.message || 'Delete failed', 'error');
  }
}

const _origNavForDevices = navigateTo;
window.navigateTo = function(pageName) {
  _origNavForDevices(pageName);
  if (pageName === 'inventory') loadDevices();
};


/* This one applied escHtml inside an onclick attribute — escaping for the
   wrong context, harmless only because the value is a server UUID. */
delegateClick('[data-device-edit]', (el) => openDeviceModal(el.dataset.deviceEdit));
