// vigil-inventory.js
// Owns: Inventory page — hardware snapshot table with sortable/filterable
//   columns, drag-to-reorder, custom column support, CSV export.
//   Also the Active Directory import settings (lives in Settings page UI
//   but imports Hosts so it's a Hosts/Inventory concern).
// HTML: templates/pages/_inventory.html, AD section in templates/pages/_settings.html
// Depends on: vigil-utils.js (apiJson, showToast),
//   vigil-host-cards.js (openHostDetail invoked from inventory row click).
// API: GET /api/v1/hosts/inventory/, GET/POST /api/v1/hosts/ad/{,sync/}

const inventoryState = {
  rows: [], customColumns: [],
  sortCol: null, sortDir: 'asc',
  colFilters: {},
  dragSrcColId: null,
};

// ── Column definitions ──────────────────────────────────────────────────────
// fn(row) → <td>   val(row) → string used for sort/filter
const INV_COLUMNS = [
  { id: 'hostname',        label: 'Host',         dv: true,
    fn: r => _hostCell(r),                                              val: r => r.hostname || '' },
  { id: 'ip_address',      label: 'IP',           dv: true,
    fn: r => _cellText(r.ip_address || '', 'mono'),                     val: r => r.ip_address || '' },
  { id: 'os',              label: 'OS (agent)',   dv: true,
    fn: r => _cellText(r.os || ''),                                     val: r => r.os || '' },
  { id: 'os_name',         label: 'OS Name',      dv: false,
    fn: r => _osNameCell(r.os_name || ''),                              val: r => r.os_name || '' },
  { id: 'os_version',      label: 'OS Version',   dv: false,
    fn: r => _cellText(r.os_version || '', 'mono'),                     val: r => r.os_version || '' },
  { id: 'kernel_version',  label: 'Kernel',       dv: false,
    fn: r => _cellText(r.kernel_version || '', 'mono'),                 val: r => r.kernel_version || '' },
  { id: 'architecture',    label: 'Arch',         dv: false,
    fn: r => _cellText(r.architecture || ''),                           val: r => r.architecture || '' },
  { id: 'uptime',          label: 'Uptime',       dv: false,
    fn: r => _cellText(r.uptime_seconds ? _formatUptime(r.uptime_seconds) : ''),
    val: r => r.uptime_seconds != null ? String(r.uptime_seconds) : '' },
  { id: 'last_logged_user',label: 'Last User',    dv: false,
    fn: r => _cellText(r.last_logged_user || ''),                       val: r => r.last_logged_user || '' },
  { id: 'manufacturer',    label: 'Manufacturer', dv: true,
    fn: r => _cellText(r.manufacturer || ''),                           val: r => r.manufacturer || '' },
  { id: 'model_name',      label: 'Model',        dv: true,
    fn: r => _cellText(r.model_name || ''),                             val: r => r.model_name || '' },
  { id: 'service_tag',     label: 'Service Tag',  dv: true,
    fn: r => _cellText(r.service_tag || '', 'mono'),                    val: r => r.service_tag || '' },
  { id: 'cpu_model',       label: 'CPU',          dv: true,
    fn: r => _cellText(r.cpu_model || ''),                              val: r => r.cpu_model || '' },
  { id: 'cpu_cores',       label: 'Cores',        dv: true,
    fn: r => _cellText(r.cpu_cores ? String(r.cpu_cores) : ''),        val: r => r.cpu_cores != null ? String(r.cpu_cores) : '' },
  { id: 'ram',             label: 'RAM',          dv: true,
    fn: r => _cellText(r.ram_total_bytes ? _formatBytes(r.ram_total_bytes) : '', 'mono'),
    val: r => r.ram_total_bytes != null ? String(r.ram_total_bytes) : '' },
  { id: 'mac',             label: 'MAC',          dv: true,
    fn: r => _cellText(_firstMac(r.mac_addresses) || '', 'mono'),       val: r => _firstMac(r.mac_addresses) || '' },
  { id: 'bios_version',    label: 'BIOS',         dv: false,
    fn: r => _cellText(r.bios_version || '', 'mono'),                   val: r => r.bios_version || '' },
  { id: 'bios_date',       label: 'BIOS Date',    dv: false,
    fn: r => _cellText(r.bios_date || ''),                              val: r => r.bios_date || '' },
  { id: 'system_timezone', label: 'Timezone',     dv: false,
    fn: r => _cellText(r.system_timezone || ''),                        val: r => r.system_timezone || '' },
  { id: 'tags',            label: 'Tags',         dv: false,
    fn: r => _cellText((r.tags || []).join(', ')),                      val: r => (r.tags || []).join(', ') },
  { id: 'last_seen',       label: 'Last Seen',    dv: true,
    fn: r => _cellText(r.last_checkin ? new Date(r.last_checkin).toLocaleString() : 'Never', 'mono'),
    val: r => r.last_checkin || '' },
];

function _invVisibleIds() {
  const saved = localStorage.getItem('vigil_inv_cols');
  if (saved) { try { return new Set(JSON.parse(saved)); } catch {} }
  return new Set(INV_COLUMNS.filter(c => c.dv).map(c => c.id));
}
function _invSaveVisible(ids) {
  localStorage.setItem('vigil_inv_cols', JSON.stringify([...ids]));
}

function _invColOrder() {
  const saved = localStorage.getItem('vigil_inv_col_order');
  if (saved) { try { return JSON.parse(saved); } catch {} }
  return INV_COLUMNS.map(c => c.id);
}
function _invSaveOrder(ids) {
  localStorage.setItem('vigil_inv_col_order', JSON.stringify(ids));
}

function _getActiveCols() {
  const visible = _invVisibleIds();
  const order = _invColOrder();
  const byId = Object.fromEntries(INV_COLUMNS.map(c => [c.id, c]));
  const inOrder = new Set(order);
  const orderedStatic = order.filter(id => byId[id] && visible.has(id)).map(id => byId[id]);
  const extras = INV_COLUMNS.filter(c => visible.has(c.id) && !inOrder.has(c.id));
  const customColDefs = (inventoryState.customColumns || []).map(col => ({
    id: 'custom__' + col, label: col,
    fn: r => _cellText(String((r.custom_columns || {})[col] ?? '')),
    val: r => String((r.custom_columns || {})[col] ?? ''),
  }));
  return [...orderedStatic, ...extras, ...customColDefs];
}

function toggleInvColEditor() {
  const panel = document.getElementById('inv-col-editor');
  if (!panel) return;
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }

  const visible = _invVisibleIds();
  panel.innerHTML = '';
  for (const col of INV_COLUMNS) {
    const row = document.createElement('label');
    row.className = 'inv-col-editor-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = visible.has(col.id);
    cb.onchange = () => {
      const cur = _invVisibleIds();
      cb.checked ? cur.add(col.id) : cur.delete(col.id);
      _invSaveVisible(cur);
      renderInventory();
    };
    row.appendChild(cb);
    row.appendChild(document.createTextNode(col.label));
    panel.appendChild(row);
  }
  panel.style.display = 'block';
  setTimeout(() => {
    function _outside(e) {
      if (!document.getElementById('inv-col-editor-wrap')?.contains(e.target)) {
        panel.style.display = 'none';
        document.removeEventListener('click', _outside);
      }
    }
    document.addEventListener('click', _outside);
  }, 0);
}

function _formatBytes(value) {
  if (!value) return '—';
  const units = ['B','KB','MB','GB','TB'];
  let v = value, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) + ' ' + units[i];
}

function _formatUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function _firstMac(macMap) {
  if (!macMap || typeof macMap !== 'object') return '';
  const entries = Object.entries(macMap);
  if (!entries.length) return '';
  entries.sort(([a],[b]) => {
    const score = (n) => (/^en|^eth/.test(n) ? 0 : /wl/.test(n) ? 1 : 2);
    return score(a) - score(b);
  });
  return entries[0][1];
}

async function refreshInventory() {
  try {
    const data = await apiJson('/api/v1/hosts/inventory/');
    inventoryState.rows = data.rows || [];
    inventoryState.customColumns = data.custom_columns || [];
    renderInventory();
  } catch (e) {
    const content = document.getElementById('inv-content');
    const msg = document.createElement('div');
    msg.className = 'inv-empty';
    msg.textContent = 'Failed to load inventory: ' + e.message;
    content.replaceChildren(msg);
  }
}

function renderInventory() {
  const content = document.getElementById('inv-content');
  const q = (document.getElementById('inv-search')?.value || '').trim().toLowerCase();
  let rows = inventoryState.rows;

  // Global search across all text fields
  if (q) {
    rows = rows.filter(r => {
      const hay = [
        r.hostname, r.os, r.os_name, r.os_version, r.kernel_version,
        r.ip_address, r.manufacturer, r.model_name, r.service_tag,
        r.cpu_model, r.architecture, r.last_logged_user, r.system_timezone,
        ...(r.tags || []),
      ].filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }

  // Per-column filters — prefix "=" for exact match, otherwise contains
  const allCols = _getActiveCols();
  rows = rows.filter(r => {
    for (const col of allCols) {
      const f = (inventoryState.colFilters[col.id] || '').trim();
      if (!f) continue;
      const cellVal = (col.val ? col.val(r) : '').toLowerCase();
      if (f.startsWith('=')) {
        if (cellVal !== f.slice(1).toLowerCase()) return false;
      } else {
        if (!cellVal.includes(f.toLowerCase())) return false;
      }
    }
    return true;
  });

  // Sorting
  const { sortCol, sortDir } = inventoryState;
  if (sortCol) {
    const sc = allCols.find(c => c.id === sortCol);
    if (sc && sc.val) {
      rows = [...rows].sort((a, b) => {
        const va = sc.val(a), vb = sc.val(b);
        const na = parseFloat(va), nb = parseFloat(vb);
        const cmp = (!isNaN(na) && !isNaN(nb)) ? na - nb : va.localeCompare(vb);
        return sortDir === 'asc' ? cmp : -cmp;
      });
    }
  }

  const anyFilter = q || Object.values(inventoryState.colFilters).some(v => v.trim());
  if (!rows.length) {
    const msg = document.createElement('div');
    msg.className = 'inv-empty';
    msg.textContent = anyFilter ? 'No matching inventory rows.' : 'No agents have reported inventory yet.';
    content.replaceChildren(msg);
    return;
  }

  const wrap = document.createElement('div');
  wrap.className = 'inv-scroll-wrap';

  const table = document.createElement('table');
  table.className = 'inv-table';
  const thead = document.createElement('thead');

  // ── Row 1: column labels (sortable + draggable) ──
  const labelTr = document.createElement('tr');
  for (const col of allCols) {
    const th = document.createElement('th');
    th.dataset.colId = col.id;
    th.draggable = !col.id.startsWith('custom__');

    const span = document.createElement('span');
    span.textContent = col.label;
    th.appendChild(span);

    if (sortCol === col.id) {
      const arrow = document.createElement('span');
      arrow.className = 'inv-sort-arrow';
      arrow.textContent = sortDir === 'asc' ? '↑' : '↓';
      th.appendChild(arrow);
    }

    // Sort on click
    th.addEventListener('click', () => {
      if (inventoryState.sortCol === col.id) {
        inventoryState.sortDir = inventoryState.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        inventoryState.sortCol = col.id;
        inventoryState.sortDir = 'asc';
      }
      renderInventory();
    });

    // Drag-to-reorder
    th.addEventListener('dragstart', e => {
      inventoryState.dragSrcColId = col.id;
      th.classList.add('inv-th-dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    th.addEventListener('dragend', () => {
      th.classList.remove('inv-th-dragging');
      document.querySelectorAll('.inv-th-drag-over').forEach(el => el.classList.remove('inv-th-drag-over'));
    });
    th.addEventListener('dragover', e => { e.preventDefault(); th.classList.add('inv-th-drag-over'); });
    th.addEventListener('dragleave', () => th.classList.remove('inv-th-drag-over'));
    th.addEventListener('drop', e => {
      e.preventDefault();
      th.classList.remove('inv-th-drag-over');
      const srcId = inventoryState.dragSrcColId;
      const dstId = col.id;
      if (srcId && srcId !== dstId) {
        const order = _invColOrder();
        const si = order.indexOf(srcId), di = order.indexOf(dstId);
        if (si !== -1 && di !== -1) { order.splice(si, 1); order.splice(di, 0, srcId); _invSaveOrder(order); }
        else if (si !== -1) { order.splice(si, 1); order.push(srcId); _invSaveOrder(order); }
        renderInventory();
      }
    });

    labelTr.appendChild(th);
  }
  thead.appendChild(labelTr);

  // ── Row 2: per-column filter inputs ──
  const filterTr = document.createElement('tr');
  filterTr.className = 'inv-filter-row';
  for (const col of allCols) {
    const th = document.createElement('th');
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'inv-col-filter' + (inventoryState.colFilters[col.id] ? ' active' : '');
    inp.placeholder = '…';
    inp.value = inventoryState.colFilters[col.id] || '';
    inp.title = 'Contains match  |  prefix = for exact  (e.g. =Ubuntu)';
    inp.addEventListener('input', e => {
      e.stopPropagation();
      inventoryState.colFilters[col.id] = inp.value;
      inp.classList.toggle('active', !!inp.value);
      renderInventory();
    });
    inp.addEventListener('click', e => e.stopPropagation());
    inp.addEventListener('keydown', e => e.stopPropagation());
    th.appendChild(inp);
    filterTr.appendChild(th);
  }
  thead.appendChild(filterTr);

  table.appendChild(thead);

  // ── Body ──
  const tbody = document.createElement('tbody');
  for (const r of rows) {
    const row = document.createElement('tr');
    row.addEventListener('click', () => openInventoryDetail(r));
    for (const col of allCols) row.appendChild(col.fn(r));
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  content.replaceChildren(wrap);
}

function _hostCell(r) {
  const td = document.createElement('td');
  td.className = 'col-status';
  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.style.background = r.status === 'online' ? 'var(--mint)'
                       : r.status === 'pending' ? 'var(--peach)'
                       : 'var(--rose)';
  td.appendChild(dot);
  const name = document.createElement('span');
  name.textContent = r.hostname || '';
  name.style.fontWeight = '600';
  td.appendChild(name);
  return td;
}

// DOMParser is XSS-safe in SVG mode; osLogo() never interpolates user data into SVG strings.
function _osNameCell(name) {
  const td = document.createElement('td');
  td.style.cssText = 'display:flex;align-items:center;gap:5px;';
  if (name) {
    const doc = new DOMParser().parseFromString(osLogo(name), 'image/svg+xml');
    td.appendChild(document.adoptNode(doc.documentElement));
    td.appendChild(document.createTextNode(name));
  } else {
    const span = document.createElement('span');
    span.className = 'empty-cell';
    span.textContent = '—';
    td.appendChild(span);
  }
  return td;
}

function _cellText(text, extraClass) {
  const td = document.createElement('td');
  if (extraClass) td.classList.add(extraClass);
  if (text === '' || text == null) {
    const span = document.createElement('span');
    span.className = 'empty-cell';
    span.textContent = '—';
    td.appendChild(span);
  } else {
    td.textContent = text;
  }
  return td;
}

function exportInventoryCsv() {
  if (!inventoryState.rows.length) {
    showToast('No inventory data to export', 'info');
    return;
  }
  const customCols = inventoryState.customColumns;
  const headers = [
    'hostname','status','os','os_name','os_version','kernel_version','architecture',
    'ip_address','manufacturer','model','service_tag','cpu_model','cpu_cores',
    'ram_total_bytes','mac_address','bios_version','bios_date','system_timezone',
    'uptime_seconds','last_logged_user','tags',...customCols,'last_checkin'
  ];
  const escape = (v) => {
    const s = (v == null) ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [headers.join(',')];
  for (const r of inventoryState.rows) {
    const row = [
      r.hostname, r.status, r.os, r.os_name || '', r.os_version || '',
      r.kernel_version || '', r.architecture || '',
      r.ip_address || '', r.manufacturer || '', r.model_name || '',
      r.service_tag || '', r.cpu_model || '', r.cpu_cores || '',
      r.ram_total_bytes || '', _firstMac(r.mac_addresses) || '',
      r.bios_version || '', r.bios_date || '', r.system_timezone || '',
      r.uptime_seconds || '', r.last_logged_user || '',
      (r.tags || []).join(';'),
    ];
    for (const col of customCols) row.push((r.custom_columns || {})[col] || '');
    row.push(r.last_checkin || '');
    lines.push(row.map(escape).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'vigil-inventory-' + new Date().toISOString().slice(0,10) + '.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function openInventoryDetail(row) {
  // Reuse the existing host detail panel by synthesizing a card-like dataset.
  const fakeCard = { dataset: {
    id: row.host_id,
    hostname: row.hostname || '',
    os: row.os || 'Unknown OS',
    ip: row.ip_address || '—',
    status: row.status || '',
    mode: row.mode || '',
    modeDisplay: row.mode || '',
    lastCheckin: row.last_checkin || '',
    kernel: '',
    tags: (row.tags || []).join(','),
  }};
  openHostDetail(fakeCard);
}

// Auto-load inventory the first time the page is shown
const _origNavForInv = navigateTo;
navigateTo = function(pageName) {
  _origNavForInv(pageName);
  if (pageName === 'inventory' && !inventoryState.rows.length) {
    refreshInventory();
  }
  if (pageName === 'settings') {
    adLoadConfig();
  }
};

/* ── Active Directory import settings ────────────────────────────────── */
async function adLoadConfig() {
  try {
    const cfg = await apiJson('/api/v1/hosts/ad/');
    document.getElementById('ad-ldap-url').value = cfg.ldap_url || '';
    document.getElementById('ad-bind-dn').value = cfg.bind_dn || '';
    document.getElementById('ad-base-dn').value = cfg.base_dn || '';
    document.getElementById('ad-computer-ou').value = cfg.computer_ou || '';
    document.getElementById('ad-enabled').checked = !!cfg.enabled;
    const status = document.getElementById('ad-status');
    if (cfg.last_sync) {
      status.textContent = `Last sync: ${new Date(cfg.last_sync).toLocaleString()} — ${cfg.last_sync_status || ''}`;
    } else {
      status.textContent = cfg.has_password ? 'Configured but never synced.' : 'Not yet configured.';
    }
  } catch (e) { /* settings page may load before login */ }
}

async function adSaveConfig() {
  const body = {
    ldap_url: document.getElementById('ad-ldap-url').value.trim(),
    bind_dn: document.getElementById('ad-bind-dn').value.trim(),
    base_dn: document.getElementById('ad-base-dn').value.trim(),
    computer_ou: document.getElementById('ad-computer-ou').value.trim(),
    enabled: document.getElementById('ad-enabled').checked,
  };
  const pwd = document.getElementById('ad-bind-password').value;
  if (pwd) body.bind_password = pwd;
  try {
    await apiJson('/api/v1/hosts/ad/', { method: 'POST', body: JSON.stringify(body) });
    document.getElementById('ad-bind-password').value = '';
    showToast('AD settings saved', 'success');
    adLoadConfig();
  } catch (e) {
    showToast('Failed to save: ' + e.message, 'error');
  }
}

async function adSyncNow() {
  const btn = event.target;
  btn.disabled = true; btn.style.opacity = '0.6';
  try {
    const result = await apiJson('/api/v1/hosts/ad/sync/', { method: 'POST' });
    if (result.queued) {
      showToast('AD sync queued', 'success');
    } else if (result.result && result.result.error) {
      showToast('AD sync failed: ' + result.result.error, 'error');
    } else if (result.result) {
      showToast(`AD sync complete: ${result.result.created} created, ${result.result.updated} updated`, 'success');
    }
    adLoadConfig();
  } catch (e) {
    showToast('Failed to start sync: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '';
  }
}
