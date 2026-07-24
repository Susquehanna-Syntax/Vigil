// vigil-users.js
// Owns: Settings → Users & Roles pane — list users + seats, create, change
//   role, delete. Backend: GET/POST /api/v1/accounts/users/,
//   PATCH .../users/<id>/role/, DELETE .../users/<id>/.
// HTML: templates/pages/_settings.html (data-pane="users")
// Depends on: vigil-utils.js (apiJson, showToast, confirmModal, escHtml)

const ROLE_LABELS = { admin: 'Admin', operator: 'Operator', viewer: 'Viewer' };

async function loadUsers() {
  const list = document.getElementById('users-list');
  if (!list) return;
  try {
    const data = await apiJson('/api/v1/accounts/users/');
    const seats = document.getElementById('users-seats');
    if (seats) seats.textContent = `Seats: ${data.seats_used} / ${data.seats_allowed}`;
    const me = (window.VIGIL_CONFIG && window.VIGIL_CONFIG.username) || '';
    const byId = {};
    list.innerHTML = '';
    data.users.forEach((u) => {
      byId[String(u.id)] = u;
      const isMe = u.username === me;
      const row = document.createElement('div');
      row.className = 'user-row';
      const opts = ['viewer', 'operator', 'admin'].map((r) =>
        `<option value="${r}"${u.role === r ? ' selected' : ''}>${ROLE_LABELS[r]}</option>`
      ).join('');
      // Interpolated username/email are display text (escHtml escapes <>&);
      // ids are integers. No free text goes into an attribute value.
      row.innerHTML =
        `<div class="user-row-main">` +
          `<span class="user-row-name">${escHtml(u.username)}${isMe ? ' <span class="muted">(you)</span>' : ''}</span>` +
          `<span class="user-row-email muted">${escHtml(u.email || '')}</span>` +
        `</div>` +
        `<div class="user-row-actions">` +
          `<select class="form-control user-role-select" data-uid="${u.id}"${isMe ? ' disabled' : ''}>${opts}</select>` +
          (isMe ? '' : `<button class="btn btn-outline btn-sm user-del" data-uid="${u.id}">Delete</button>`) +
        `</div>`;
      list.appendChild(row);
    });
    // Wire role selects and delete buttons; names come from the lookup map, not
    // from a data- attribute, so no user-supplied text is interpolated anywhere.
    list.querySelectorAll('.user-role-select').forEach((sel) => {
      sel.addEventListener('change', () => changeRole(sel.dataset.uid, sel.value));
    });
    list.querySelectorAll('.user-del').forEach((btn) => {
      const u = byId[btn.dataset.uid];
      btn.addEventListener('click', () => deleteUser(btn.dataset.uid, u ? u.username : ''));
    });
  } catch (e) {
    list.innerHTML = `<p class="muted">Could not load users: ${escHtml(e.message)}</p>`;
  }
}

async function createUser() {
  const username = document.getElementById('nu-username').value.trim();
  const email = document.getElementById('nu-email').value.trim();
  const password = document.getElementById('nu-password').value;
  const role = document.getElementById('nu-role').value;
  const result = document.getElementById('nu-result');
  result.textContent = '';
  if (!username || !password) {
    result.textContent = 'Username and password are required.';
    return;
  }
  try {
    await apiJson('/api/v1/accounts/users/', {
      method: 'POST',
      body: JSON.stringify({ username, email, password, role }),
    });
    showToast(`Created ${username}`, 'success');
    document.getElementById('nu-username').value = '';
    document.getElementById('nu-email').value = '';
    document.getElementById('nu-password').value = '';
    document.getElementById('nu-role').value = 'viewer';
    loadUsers();
  } catch (e) {
    // 402 (operator without a Business license) and 400 (dupe / missing) land here.
    result.textContent = e.message;
  }
}

async function changeRole(uid, role) {
  try {
    await apiJson(`/api/v1/accounts/users/${uid}/role/`, {
      method: 'PATCH',
      body: JSON.stringify({ role }),
    });
    showToast('Role updated', 'success');
    loadUsers();
  } catch (e) {
    // e.g. Operator needs a Business license — surface it and snap the select back.
    showToast(e.message, 'error');
    loadUsers();
  }
}

async function deleteUser(uid, username) {
  const ok = await confirmModal(`Delete user "${username}"? This cannot be undone.`, { danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/accounts/users/${uid}/`, { method: 'DELETE' });
    showToast(`Deleted ${username}`, 'success');
    loadUsers();
  } catch (e) {
    // Last-admin / self guards return 400 with a detail message.
    showToast(e.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('users-list')) loadUsers();
});
