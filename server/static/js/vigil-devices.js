// vigil-devices.js — Unmanaged device inventory (manual registry on the Inventory page).
// Depends on: vigil-utils.js (apiJson, showToast, escHtml), vigil-nav.js (navigateTo).

let _devices = [];

async function loadDevices() {
  try {
    _devices = await apiJson('/api/v1/hosts/devices/');
  } catch (e) {
    _devices = [];
  }
  renderDevices();
}

function renderDevices() {
  const wrap = document.getElementById('dev-content');
  const countEl = document.getElementById('dev-count');
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
      <th>Name</th><th>Type</th><th>IP Address</th><th>MAC</th>
      <th>Vendor</th><th>Location</th><th class="num"></th>
    </tr></thead><tbody>`;
  for (const d of _devices) {
    html += `<tr>
      <td class="ctr-name">${escHtml(d.name || '')}</td>
      <td>${escHtml(d.device_type_label || '')}</td>
      <td class="dev-mono">${escHtml(d.ip_address || '—')}</td>
      <td class="dev-mono">${escHtml(d.mac_address || '—')}</td>
      <td>${escHtml(d.vendor || '—')}</td>
      <td>${escHtml(d.location || '—')}</td>
      <td class="num"><button class="btn btn-ghost btn-sm" onclick="openDeviceModal('${escHtml(d.id)}')">Edit</button></td>
    </tr>`;
  }
  html += `</tbody></table></div>`;
  wrap.innerHTML = html;
}

function openDeviceModal(id) {
  const d = id ? _devices.find(x => x.id === id) : null;
  document.getElementById('device-modal-title').textContent = d ? 'Edit Device' : 'Add Device';
  document.getElementById('device-id').value = d ? d.id : '';
  document.getElementById('device-name').value = d ? (d.name || '') : '';
  document.getElementById('device-type').value = d ? (d.device_type || 'other') : 'other';
  document.getElementById('device-ip').value = d ? (d.ip_address || '') : '';
  document.getElementById('device-mac').value = d ? (d.mac_address || '') : '';
  document.getElementById('device-vendor').value = d ? (d.vendor || '') : '';
  document.getElementById('device-location').value = d ? (d.location || '') : '';
  document.getElementById('device-notes').value = d ? (d.notes || '') : '';
  document.getElementById('device-delete-btn').style.display = d ? 'inline-flex' : 'none';
  document.getElementById('device-overlay').classList.add('open');
  document.getElementById('device-modal').classList.add('open');
}

function closeDeviceModal() {
  document.getElementById('device-overlay').classList.remove('open');
  document.getElementById('device-modal').classList.remove('open');
}

function _deviceFormBody() {
  const ip = document.getElementById('device-ip').value.trim();
  return {
    name: document.getElementById('device-name').value.trim(),
    device_type: document.getElementById('device-type').value,
    ip_address: ip || null,
    mac_address: document.getElementById('device-mac').value.trim(),
    vendor: document.getElementById('device-vendor').value.trim(),
    location: document.getElementById('device-location').value.trim(),
    notes: document.getElementById('device-notes').value.trim(),
  };
}

async function saveDevice() {
  const id = document.getElementById('device-id').value;
  const body = _deviceFormBody();
  if (!body.name) { showToast('Name is required', 'error'); return; }
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

async function deleteDeviceFromModal() {
  const id = document.getElementById('device-id').value;
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
navigateTo = function(pageName) {
  _origNavForDevices(pageName);
  if (pageName === 'inventory') loadDevices();
};
