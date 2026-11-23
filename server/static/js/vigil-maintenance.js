/* Maintenance windows pane (sprint M7).
   HTML: templates/pages/_settings.html (data-pane="notifications")
   API:  GET/POST /api/v1/alerts/maintenance/, GET/PATCH/DELETE .../<id>/
         GET /api/v1/hosts/  — all admin-only.

   A window is either a one-off stretch between two datetimes, or a weekly
   recurrence with weekdays, a start time, a duration and a time zone. Hosts
   and tags pick which machines it covers: an empty pair covers every host in
   the window's site. Editing sends every field the form holds, so what the
   admin sees in the form is exactly what is saved.
   Rows reuse the relay row styles, like the channels card above. */

const _MAINT_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let _maintenanceById = {};

/* "45 min", "3 h", "2 h 30 min". */
function _maintDuration(minutes) {
  const mins = Number(minutes) || 0;
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/* A one-off's ends to the minute, in the viewer's own locale and zone. */
function _maintWhen(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function _maintSchedule(w) {
  if (w.kind === 'weekly') {
    const days = (w.weekdays || []).slice().sort((a, b) => a - b)
      .map((d) => _MAINT_DAYS[d]).filter(Boolean).join(', ');
    return `${days || 'No days'} at ${w.start_time} for ${_maintDuration(w.duration_minutes)}`
      + ` (${w.timezone})`;
  }
  return `${_maintWhen(w.starts_at)} to ${_maintWhen(w.ends_at)}`;
}

function _maintCovers(w) {
  const hosts = (w.host_ids || []).length;
  const tags = w.tags || [];
  if (!hosts && !tags.length) return w.site_id ? 'Every host in its site' : 'Every host in every site';
  const parts = [];
  if (hosts) parts.push(`${hosts} host${hosts === 1 ? '' : 's'}`);
  if (tags.length) parts.push(`tags: ${tags.join(', ')}`);
  return parts.join(', ');
}

function _maintStatusHtml(w) {
  if (w.active) return '<span class="relay-status ok">Active now</span>';
  if (!w.enabled) return '<span class="relay-status off">Off</span>';
  return '<span class="relay-status off">Scheduled</span>';
}

function _maintRowHtml(w) {
  const id = escAttr(w.id);
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(w.name)}`
      + `<span class="relay-badge">${w.kind === 'weekly' ? 'Weekly' : 'One-off'}</span></div>`
      + `<div class="relay-meta">${escHtml(_maintSchedule(w))}</div>`
      + `<div class="relay-meta">${escHtml(_maintCovers(w))}</div>`
    + '</div>'
    + `<div class="relay-status-cell">${_maintStatusHtml(w)}`
      + '<div class="relay-actions">'
        + `<button class="btn btn-sm btn-outline" data-act="editMaintenance" data-a1="${id}">Edit</button>`
        + `<button class="btn btn-sm btn-outline" data-act="toggleMaintenance" data-a1="${id}">${w.enabled ? 'Turn off' : 'Turn on'}</button>`
        + `<button class="btn btn-sm btn-rose" data-act="deleteMaintenance" data-a1="${id}">Delete</button>`
      + '</div>'
    + '</div>'
  + '</div>';
}

async function loadMaintenance() {
  const list = document.getElementById('maintenance-list');
  if (!list) return;
  let rows;
  try {
    rows = await apiJson('/api/v1/alerts/maintenance/');
  } catch (e) {
    list.innerHTML = `<p class="muted">Could not load maintenance windows: ${escHtml(e.message)}</p>`;
    return;
  }
  _maintenanceById = {};
  rows.forEach((w) => { _maintenanceById[w.id] = w; });
  list.innerHTML = rows.length ? rows.map(_maintRowHtml).join('')
    : '<p class="muted">No maintenance windows. Alerts fire at any time.</p>';
}

/* datetime-local wants local wall-clock text with no offset and no seconds:
   an ISO instant typed into it comes out blank. */
function _maintLocalInput(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* The host picker. The host list is paginated, so every page is read: a
   window naming a host past the first page would otherwise lose it on save.
   A selected host the list did not return (another site's, or deleted) stays
   selected under its id. A failure is not fatal: a window may name no hosts,
   so the select is simply short and the save still works. */
async function _maintHostOptions(selected) {
  const wanted = new Set((selected || []).map(String));
  const rows = await fetchAllHosts();
  const seen = new Set(rows.map((h) => String(h.id)));
  const missing = [...wanted].filter((id) => !seen.has(id));
  return rows.map((h) => `<option value="${escAttr(h.id)}"${wanted.has(String(h.id)) ? ' selected' : ''}>`
    + `${escHtml(h.hostname)}</option>`).join('')
    + missing.map((id) => `<option value="${escAttr(id)}" selected>${escHtml(id)}</option>`).join('');
}

function _maintDayChecks(weekdays) {
  const on = new Set(weekdays || []);
  return '<div class="maint-days">' + _MAINT_DAYS.map((label, day) => '<label class="setting-check">'
    + `<input type="checkbox" data-day="${day}"${on.has(day) ? ' checked' : ''}> ${label}</label>`)
    .join('') + '</div>';
}

function _maintShowScheduleGroups(m) {
  const weekly = m.modal.querySelector('#mwf-kind').value === 'weekly';
  m.modal.querySelector('#mwf-once-group').hidden = weekly;
  m.modal.querySelector('#mwf-once-end-group').hidden = weekly;
  m.modal.querySelector('#mwf-weekly-group').hidden = !weekly;
}

async function openMaintenanceForm(w) {
  const editing = Boolean(w && w.id);
  const hosts = await _maintHostOptions(editing ? w.host_ids : null);
  const tzDefault = (Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC';
  const m = mountModal('maintenance-form', { wide: true });
  m.setBody(`
    <div class="modal-title"><span id="mwf-title"></span>
      <button class="modal-close" id="mwf-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="form-group"><label class="form-label" for="mwf-name">Name</label>
      <input type="text" class="form-control" id="mwf-name" maxlength="200" autocomplete="off"></div>
    <div class="form-group"><label class="form-label" for="mwf-kind">Repeats</label>
      <select class="form-control" id="mwf-kind">
        <option value="once">Once</option>
        <option value="weekly">Every week</option>
      </select></div>
    <div class="form-group" id="mwf-once-group">
      <label class="form-label" for="mwf-start">Starts</label>
      <input type="datetime-local" class="form-control" id="mwf-start">
    </div>
    <div class="form-group" id="mwf-once-end-group">
      <label class="form-label" for="mwf-end">Ends</label>
      <input type="datetime-local" class="form-control" id="mwf-end">
    </div>
    <div id="mwf-weekly-group">
      <div class="form-group"><span class="form-label">Days</span>${_maintDayChecks(editing ? w.weekdays : null)}</div>
      <div class="form-group"><label class="form-label" for="mwf-time">Start time</label>
        <input type="time" class="form-control" id="mwf-time"
               value="${editing && w.start_time ? escAttr(w.start_time) : '22:00'}"></div>
      <div class="form-group"><label class="form-label" for="mwf-dur">Duration in minutes</label>
        <input type="number" class="form-control" id="mwf-dur" min="1" max="1440" step="1"
               value="${editing ? escAttr(w.duration_minutes) : 60}"></div>
      <div class="form-group"><label class="form-label" for="mwf-tz">Time zone</label>
        <input type="text" class="form-control" id="mwf-tz" autocomplete="off"
               value="${editing ? escAttr(w.timezone) : escAttr(tzDefault)}"></div>
    </div>
    <div class="form-group"><label class="form-label" for="mwf-hosts">Hosts</label>
      <select class="form-control" id="mwf-hosts" multiple size="6">${hosts}</select>
      <div class="totp-sub">Leave empty, with no tags, to cover every host in the site.</div></div>
    <div class="form-group"><label class="form-label" for="mwf-tags">Tags</label>
      <input type="text" class="form-control" id="mwf-tags" autocomplete="off"
             placeholder="db, edge" value="${editing ? escAttr((w.tags || []).join(', ')) : ''}"></div>
    <div class="form-group" id="mwf-site-field" hidden><label class="form-label" for="mwf-site">Site</label>
      <select class="form-control" id="mwf-site"></select>
      <div class="totp-sub">A site's window covers only that site's hosts.</div></div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="mwf-cancel">Cancel</button>
      <button class="btn btn-mint btn-sm" id="mwf-save">Save</button>
    </div>`);
  const $ = (sel) => m.modal.querySelector(sel);
  $('#mwf-title').textContent = editing ? `Edit ${w.name}` : 'Add a maintenance window';
  $('#mwf-name').value = editing ? w.name : '';
  $('#mwf-kind').value = editing && w.kind === 'weekly' ? 'weekly' : 'once';
  $('#mwf-start').value = _maintLocalInput(editing ? w.starts_at : null);
  $('#mwf-end').value = _maintLocalInput(editing ? w.ends_at : null);
  const kindSel = $('#mwf-kind');
  kindSel.onchange = () => _maintShowScheduleGroups(m);
  _maintShowScheduleGroups(m);
  sitePickFill($('#mwf-site'), $('#mwf-site-field'), editing ? w.site_id : null, { editing });

  const close = () => m.close();
  $('#mwf-x').onclick = close;
  $('#mwf-cancel').onclick = close;
  $('#mwf-save').onclick = async () => {
    const name = $('#mwf-name').value.trim();
    if (!name) { showToast('Give the window a name', 'error'); return; }
    const weekly = kindSel.value === 'weekly';
    const body = {
      name,
      kind: kindSel.value,
      host_ids: Array.from($('#mwf-hosts').selectedOptions).map((o) => o.value),
      tags: $('#mwf-tags').value.split(',').map((t) => t.trim()).filter(Boolean),
    };
    if (weekly) {
      const days = [];
      // data-day is the weekday, not the document order: never index by it.
      m.modal.querySelectorAll('#mwf-weekly-group [data-day]').forEach((cb) => {
        if (cb.checked) days.push(Number(cb.dataset.day));
      });
      body.weekdays = days;
      body.start_time = $('#mwf-time').value;
      body.duration_minutes = Number($('#mwf-dur').value);
      body.timezone = $('#mwf-tz').value.trim();
    } else {
      const start = $('#mwf-start').value;
      const end = $('#mwf-end').value;
      if (!start || !end) { showToast('Say when the window starts and ends', 'error'); return; }
      body.starts_at = new Date(start).toISOString();
      body.ends_at = new Date(end).toISOString();
    }
    const site = sitePickValue($('#mwf-site'), $('#mwf-site-field'));
    if (site !== undefined) body.site_id = site;
    try {
      await apiJson(editing ? `/api/v1/alerts/maintenance/${w.id}/` : '/api/v1/alerts/maintenance/', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body),
      });
      close();
      showToast('Maintenance window saved', 'success');
      loadMaintenance();
    } catch (e) {
      showToast(e.body && e.body.error ? e.body.error : e.message, 'error');
    }
  };
  requestAnimationFrame(() => { m.open(); $('#mwf-name').focus(); });
}

function addMaintenance() { openMaintenanceForm(null); }

function editMaintenance(id) {
  const w = _maintenanceById[id];
  if (w) openMaintenanceForm(w);
}

async function toggleMaintenance(id) {
  const w = _maintenanceById[id];
  if (!w) return;
  try {
    await apiJson(`/api/v1/alerts/maintenance/${id}/`, {
      method: 'PATCH', body: JSON.stringify({ enabled: !w.enabled }),
    });
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadMaintenance();
}

async function deleteMaintenance(id) {
  const w = _maintenanceById[id];
  if (!w) return;
  const ok = await confirmModal(`Delete ${w.name}? Alerts for its hosts fire again during that time.`,
    { title: 'Delete maintenance window', confirmText: 'Delete', danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/alerts/maintenance/${id}/`, { method: 'DELETE' });
    showToast(`${w.name} deleted`, 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadMaintenance();
}
