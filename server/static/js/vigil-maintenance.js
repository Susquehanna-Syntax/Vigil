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

/* Built on each call, not once at load: t() needs the translations, which may not be in yet. */
function _maintDays() {
  return [t('Mon'), t('Tue'), t('Wed'), t('Thu'), t('Fri'), t('Sat'), t('Sun')];
}
let _maintenanceById = {};

/* "45 min", "3 h", "2 h 30 min". */
function _maintDuration(minutes) {
  const mins = Number(minutes) || 0;
  if (mins < 60) return t('{n} min', { n: mins });
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? t('{h} h {m} min', { h: hours, m: rest }) : t('{h} h', { h: hours });
}

/* A one-off's ends to the minute, in the viewer's own locale and zone. */
function _maintWhen(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function _maintSchedule(w) {
  if (w.kind === 'weekly') {
    const days = (w.weekdays || []).slice().sort((a, b) => a - b)
      .map((d) => _maintDays()[d]).filter(Boolean).join(', ');
    return t('{days} at {time} for {duration} ({zone})', {
      days: days || t('No days'), time: w.start_time,
      duration: _maintDuration(w.duration_minutes), zone: w.timezone,
    });
  }
  return t('{start} to {end}', { start: _maintWhen(w.starts_at), end: _maintWhen(w.ends_at) });
}

function _maintCovers(w) {
  const hosts = (w.host_ids || []).length;
  const tags = w.tags || [];
  if (!hosts && !tags.length) return w.site_id ? t('Every host in its site') : t('Every host in every site');
  const parts = [];
  if (hosts) parts.push(tn('{n} host', '{n} hosts', hosts));
  if (tags.length) parts.push(t('tags: {tags}', { tags: tags.join(', ') }));
  return parts.join(', ');
}

function _maintStatusHtml(w) {
  if (w.active) return `<span class="relay-status ok">${escHtml(t('Active now'))}</span>`;
  if (!w.enabled) return `<span class="relay-status off">${escHtml(t('Off'))}</span>`;
  return `<span class="relay-status off">${escHtml(t('Scheduled'))}</span>`;
}

function _maintRowHtml(w) {
  const id = escAttr(w.id);
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(w.name)}`
      + `<span class="relay-badge">${w.kind === 'weekly' ? escHtml(t('Weekly')) : escHtml(t('One-off'))}</span></div>`
      + `<div class="relay-meta">${escHtml(_maintSchedule(w))}</div>`
      + `<div class="relay-meta">${escHtml(_maintCovers(w))}</div>`
    + '</div>'
    + `<div class="relay-status-cell">${_maintStatusHtml(w)}`
      + '<div class="relay-actions">'
        + `<button class="btn btn-sm btn-outline" data-act="editMaintenance" data-a1="${id}">${escHtml(t('Edit'))}</button>`
        + `<button class="btn btn-sm btn-outline" data-act="toggleMaintenance" data-a1="${id}">${w.enabled ? escHtml(t('Turn off')) : escHtml(t('Turn on'))}</button>`
        + `<button class="btn btn-sm btn-rose" data-act="deleteMaintenance" data-a1="${id}">${escHtml(t('Delete'))}</button>`
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
    list.innerHTML = `<p class="muted">${escHtml(t('Could not load maintenance windows: {error}', { error: e.message }))}</p>`;
    return;
  }
  _maintenanceById = {};
  rows.forEach((w) => { _maintenanceById[w.id] = w; });
  list.innerHTML = rows.length ? rows.map(_maintRowHtml).join('')
    : `<p class="muted">${escHtml(t('No maintenance windows. Alerts fire at any time.'))}</p>`;
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
  return '<div class="maint-days">' + _maintDays().map((label, day) => '<label class="setting-check">'
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
      <button class="modal-close" id="mwf-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="form-group"><label class="form-label" for="mwf-name">${escHtml(t('Name'))}</label>
      <input type="text" class="form-control" id="mwf-name" maxlength="200" autocomplete="off"></div>
    <div class="form-group"><label class="form-label" for="mwf-kind">${escHtml(t('Repeats'))}</label>
      <select class="form-control" id="mwf-kind">
        <option value="once">${escHtml(t('Once'))}</option>
        <option value="weekly">${escHtml(t('Every week'))}</option>
      </select></div>
    <div class="form-group" id="mwf-once-group">
      <label class="form-label" for="mwf-start">${escHtml(t('Starts'))}</label>
      <input type="datetime-local" class="form-control" id="mwf-start">
    </div>
    <div class="form-group" id="mwf-once-end-group">
      <label class="form-label" for="mwf-end">${escHtml(t('Ends'))}</label>
      <input type="datetime-local" class="form-control" id="mwf-end">
    </div>
    <div id="mwf-weekly-group">
      <div class="form-group"><span class="form-label">${escHtml(t('Days'))}</span>${_maintDayChecks(editing ? w.weekdays : null)}</div>
      <div class="form-group"><label class="form-label" for="mwf-time">${escHtml(t('Start time'))}</label>
        <input type="time" class="form-control" id="mwf-time"
               value="${editing && w.start_time ? escAttr(w.start_time) : '22:00'}"></div>
      <div class="form-group"><label class="form-label" for="mwf-dur">${escHtml(t('Duration in minutes'))}</label>
        <input type="number" class="form-control" id="mwf-dur" min="1" max="1440" step="1"
               value="${editing ? escAttr(w.duration_minutes) : 60}"></div>
      <div class="form-group"><label class="form-label" for="mwf-tz">${escHtml(t('Time zone'))}</label>
        <input type="text" class="form-control" id="mwf-tz" autocomplete="off"
               value="${editing ? escAttr(w.timezone) : escAttr(tzDefault)}"></div>
    </div>
    <div class="form-group"><label class="form-label" for="mwf-hosts">${escHtml(t('Hosts'))}</label>
      <select class="form-control" id="mwf-hosts" multiple size="6">${hosts}</select>
      <div class="totp-sub">${escHtml(t('Leave empty, with no tags, to cover every host in the site.'))}</div></div>
    <div class="form-group"><label class="form-label" for="mwf-tags">${escHtml(t('Tags'))}</label>
      <div class="mwf-tag-row"><input type="text" class="form-control" id="mwf-tags" autocomplete="off"
             placeholder="${escAttr(t('db, edge'))}" value="${editing ? escAttr((w.tags || []).join(', ')) : ''}"></div></div>
    <div class="form-group" id="mwf-site-field" hidden><label class="form-label" for="mwf-site">${escHtml(t('Site'))}</label>
      <select class="form-control" id="mwf-site"></select>
      <div class="totp-sub">${escHtml(t("A site's window covers only that site's hosts."))}</div></div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="mwf-cancel">${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" id="mwf-save">${escHtml(t('Save'))}</button>
    </div>`);
  const $ = (sel) => m.modal.querySelector(sel);
  $('#mwf-title').textContent = editing ? t('Edit {name}', { name: w.name }) : t('Add a maintenance window');
  $('#mwf-name').value = editing ? w.name : '';
  $('#mwf-kind').value = editing && w.kind === 'weekly' ? 'weekly' : 'once';
  $('#mwf-start').value = _maintLocalInput(editing ? w.starts_at : null);
  $('#mwf-end').value = _maintLocalInput(editing ? w.ends_at : null);
  const kindSel = $('#mwf-kind');
  kindSel.onchange = () => _maintShowScheduleGroups(m);
  _maintShowScheduleGroups(m);
  sitePickFill($('#mwf-site'), $('#mwf-site-field'), editing ? w.site_id : null, { editing });
  // The same Pick… button and tag list every other tag field has.
  if (typeof attachTagPicker === 'function') {
    attachTagPicker('mwf-tags', { title: t('Hosts in this window are tagged'), includeReserved: true });
  }

  const close = () => m.close();
  $('#mwf-x').onclick = close;
  $('#mwf-cancel').onclick = close;
  $('#mwf-save').onclick = async () => {
    const name = $('#mwf-name').value.trim();
    if (!name) { showToast(t('Give the window a name'), 'error'); return; }
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
      if (!start || !end) { showToast(t('Say when the window starts and ends'), 'error'); return; }
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
      showToast(t('Maintenance window saved'), 'success');
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
  const ok = await confirmModal(t('Delete {name}? Alerts for its hosts fire again during that time.', { name: w.name }),
    { title: t('Delete maintenance window'), confirmText: t('Delete'), danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/alerts/maintenance/${id}/`, { method: 'DELETE' });
    showToast(t('{name} deleted', { name: w.name }), 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadMaintenance();
}
