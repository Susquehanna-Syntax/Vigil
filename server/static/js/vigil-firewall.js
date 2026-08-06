// vigil-firewall.js
// Owns: Firewall page — host picker, tri-state enable/disable + default
//   policies, rule table with protected-port guard, unparsed-rule callout,
//   add-rule form, 2FA-gated writes.
// HTML: templates/pages/_firewall.html
// Depends on: vigil-utils.js (apiJson, showToast).
//
// SECURITY: every value rendered here that came from the API — tool name,
// rule fields, source, interface, unparsed raw lines — was reported by the
// monitored host (or a compromised/malicious agent impersonating one). It
// is written to the DOM exclusively via textContent / element properties,
// never innerHTML or template-literal HTML. Same discipline vigil-vulns.js
// uses throughout; this project has already shipped one XSS from this
// exact shape (vigil-pickers.js), so there is no exception here.
//
// API: GET  /api/v1/hosts/<id>/firewall/
//      POST /api/v1/hosts/<id>/firewall/refresh/
//      POST /api/v1/hosts/<id>/firewall/apply/

let firewallHostsLoaded = false;
let firewallCurrentHostId = null;

// Port 22 is always protected; 3389 joins it only on a Windows snapshot.
// Mirrors apps/hosts/firewall_guard.py::_protected_ports exactly — the UI
// must not offer a remove/deny action the server is guaranteed to refuse.
function _fwIsProtected(port, tool) {
  if (port === 22) return true;
  if (port === 3389 && tool === 'windows') return true;
  return false;
}

function _fwAge(iso) {
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

/* ── Host picker ─────────────────────────────────────────────────────── */

async function loadFirewall() {
  if (!firewallHostsLoaded) {
    await _fwPopulateHosts();
    firewallHostsLoaded = true;
  }
  const sel = document.getElementById('firewall-host-select');
  if (sel && sel.value) await loadFirewallSnapshot(sel.value);
}

async function _fwPopulateHosts() {
  const sel = document.getElementById('firewall-host-select');
  const noHosts = document.getElementById('firewall-no-hosts');
  const refreshBtn = document.getElementById('firewall-refresh-btn');
  if (!sel) return;

  let hosts = [];
  try {
    hosts = await apiJson('/api/v1/hosts/');
  } catch (e) {
    showToast('Failed to load hosts: ' + e.message, 'error');
  }

  // Monitor-mode hosts report metrics only and never execute tasks — the
  // firewall endpoints refuse them (see host.mode == Host.Mode.MONITOR
  // checks in apps/hosts/views.py). Leaving them out of the picker, with
  // an explanation, beats listing hosts that can only ever fail here.
  const eligible = (Array.isArray(hosts) ? hosts : []).filter(h => h.mode !== 'monitor');

  sel.replaceChildren();
  for (const h of eligible) {
    const opt = document.createElement('option');
    opt.value = h.id;
    opt.textContent = h.hostname; // host-reported string — textContent only
    sel.appendChild(opt);
  }

  const hasHosts = eligible.length > 0;
  sel.style.display = hasHosts ? '' : 'none';
  if (refreshBtn) refreshBtn.style.display = hasHosts ? '' : 'none';
  if (noHosts) noHosts.style.display = hasHosts ? 'none' : 'block';

  if (!hasHosts) {
    document.getElementById('firewall-empty').style.display = 'none';
    document.getElementById('firewall-state').style.display = 'none';
    document.getElementById('firewall-rules').style.display = 'none';
    const ageEl = document.getElementById('firewall-snapshot-age');
    if (ageEl) ageEl.textContent = '';
    return;
  }

  sel.addEventListener('change', () => loadFirewallSnapshot(sel.value));
  // The browser auto-selects the first <option>, so sel.value is already
  // set — loadFirewall()'s caller reads it next; no extra fetch here.
}

/* ── Snapshot fetch + render ─────────────────────────────────────────── */

async function loadFirewallSnapshot(hostId) {
  firewallCurrentHostId = hostId;
  let data;
  try {
    data = await apiJson(`/api/v1/hosts/${hostId}/firewall/`);
  } catch (e) {
    showToast('Failed to load firewall state: ' + e.message, 'error');
    return;
  }
  _fwRender(hostId, data);
}

function _fwRender(hostId, data) {
  const ageEl = document.getElementById('firewall-snapshot-age');
  const emptyEl = document.getElementById('firewall-empty');
  const stateEl = document.getElementById('firewall-state');
  const rulesEl = document.getElementById('firewall-rules');

  if (ageEl) ageEl.textContent = data.fetched_at ? 'Last read ' + _fwAge(data.fetched_at) : '';

  // fetched_at is null before any read has ever landed — that is normal
  // (reads are dispatched, not polled), not an error.
  if (!data.fetched_at) {
    emptyEl.style.display = 'block';
    stateEl.style.display = 'none';
    rulesEl.style.display = 'none';
    return;
  }
  emptyEl.style.display = 'none';
  stateEl.style.display = 'block';
  rulesEl.style.display = 'block';

  _fwRenderState(hostId, data);
  _fwRenderRules(hostId, data);
}

function _fwRenderState(hostId, data) {
  const el = document.getElementById('firewall-state');
  el.replaceChildren();

  const unsupported = data.supported === false;

  const card = document.createElement('div');
  card.style.cssText = 'background:var(--s1);border-radius:var(--r-lg);padding:24px;margin-bottom:20px;';

  const toolRow = document.createElement('div');
  toolRow.style.cssText = 'display:flex;align-items:center;gap:10px;margin-bottom:14px;';
  const toolLabel = document.createElement('span');
  toolLabel.style.cssText = 'font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--text-3);min-width:110px;';
  toolLabel.textContent = 'Tool';
  const toolVal = document.createElement('span');
  toolVal.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);';
  // data.tool is host-reported (always a non-null string per the ingest
  // contract, but treated as untrusted regardless) — textContent only.
  toolVal.textContent = unsupported ? 'No supported firewall tool found' : (data.tool || 'unknown');
  toolRow.append(toolLabel, toolVal);
  card.appendChild(toolRow);

  // ``enabled`` is tri-state: true / false / null. null means Vigil could
  // not read the state and MUST render as "unknown", never "disabled" —
  // that exact collapse has already been the bug twice in this feature.
  const enabledRow = document.createElement('div');
  enabledRow.style.cssText = 'display:flex;align-items:center;gap:10px;margin-bottom:14px;';
  const enabledLabel = document.createElement('span');
  enabledLabel.style.cssText = 'font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--text-3);min-width:110px;';
  enabledLabel.textContent = 'State';
  const enabledVal = document.createElement('span');
  enabledVal.className = 'mono';
  enabledVal.style.fontWeight = '600';
  let enabledText, enabledColor;
  if (data.enabled === true) { enabledText = 'enabled'; enabledColor = 'var(--mint)'; }
  else if (data.enabled === false) { enabledText = 'disabled'; enabledColor = 'var(--rose)'; }
  else { enabledText = 'unknown'; enabledColor = 'var(--peach)'; }
  enabledVal.style.color = enabledColor;
  enabledVal.textContent = enabledText;
  enabledRow.append(enabledLabel, enabledVal);
  card.appendChild(enabledRow);

  // Default policies — values can be "allow"/"deny"/"reject", or the
  // literal string "unknown"; all host-reported, all textContent.
  const defaults = (data.defaults && typeof data.defaults === 'object') ? data.defaults : {};
  const policyWrap = document.createElement('div');
  policyWrap.style.cssText = 'display:flex;flex-direction:column;gap:10px;margin-bottom:16px;';
  for (const direction of ['incoming', 'outgoing']) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;';

    const label = document.createElement('span');
    label.style.cssText = 'font-size:12px;color:var(--text-3);min-width:110px;';
    label.textContent = direction === 'incoming' ? 'Default incoming' : 'Default outgoing';

    const current = document.createElement('span');
    current.className = 'mono';
    current.style.cssText = 'font-size:12px;color:var(--text-1);min-width:60px;';
    const val = defaults[direction];
    current.textContent = (typeof val === 'string' && val) ? val : 'unknown';

    row.append(label, current);

    if (!unsupported) {
      const sel = document.createElement('select');
      sel.className = 'form-control';
      sel.style.cssText = 'width:auto;padding:4px 8px;font-size:12px;';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Change to…';
      sel.appendChild(placeholder);
      for (const p of ['allow', 'deny', 'reject']) {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        sel.appendChild(opt);
      }
      const setBtn = document.createElement('button');
      setBtn.className = 'btn btn-outline btn-sm';
      setBtn.textContent = 'Set';
      setBtn.addEventListener('click', () => {
        if (!sel.value) { showToast('Choose a policy first', 'error'); return; }
        applyFirewallChange(hostId, 'set_firewall_policy', { direction, policy: sel.value }, setBtn);
      });
      row.append(sel, setBtn);
    }

    policyWrap.appendChild(row);
  }
  card.appendChild(policyWrap);

  // Windows profiles — only present on a Windows snapshot; each profile's
  // enabled flag is a plain bool (unlike the top-level tri-state).
  if (Array.isArray(data.profiles) && data.profiles.length) {
    const profWrap = document.createElement('div');
    profWrap.style.cssText = 'display:flex;flex-direction:column;gap:6px;margin-bottom:16px;';
    const profTitle = document.createElement('div');
    profTitle.style.cssText = 'font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--text-3);margin-bottom:4px;';
    profTitle.textContent = 'Profiles';
    profWrap.appendChild(profTitle);
    for (const p of data.profiles) {
      if (!p || typeof p !== 'object') continue;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;font-size:12px;';
      const name = document.createElement('span');
      name.style.color = 'var(--text-1)';
      name.textContent = String(p.name ?? ''); // host-reported — textContent
      const state = document.createElement('span');
      state.className = 'mono';
      state.style.color = p.enabled ? 'var(--mint)' : 'var(--rose)';
      state.textContent = p.enabled ? 'on' : 'off';
      row.append(name, state);
      profWrap.appendChild(row);
    }
    card.appendChild(profWrap);
  }

  // Enable / disable are never treated as lockouts by the guard, so both
  // stay available regardless of the current (possibly unknown) state.
  const actionsRow = document.createElement('div');
  actionsRow.style.cssText = 'display:flex;gap:10px;';
  const enableBtn = document.createElement('button');
  enableBtn.className = 'btn btn-outline btn-sm';
  enableBtn.textContent = 'Enable firewall';
  enableBtn.addEventListener('click', () => applyFirewallChange(hostId, 'enable_firewall', {}, enableBtn));
  const disableBtn = document.createElement('button');
  disableBtn.className = 'btn btn-outline btn-sm';
  disableBtn.textContent = 'Disable firewall';
  disableBtn.addEventListener('click', () => applyFirewallChange(hostId, 'disable_firewall', {}, disableBtn));
  actionsRow.append(enableBtn, disableBtn);
  card.appendChild(actionsRow);

  el.appendChild(card);
}

function _fwRenderRules(hostId, data) {
  const el = document.getElementById('firewall-rules');
  el.replaceChildren();

  const tool = typeof data.tool === 'string' ? data.tool : '';
  const rules = Array.isArray(data.rules) ? data.rules : [];

  /* ── Rule table ──────────────────────────────────────────────────── */
  const tableWrap = document.createElement('div');
  tableWrap.style.cssText = 'background:var(--s1);border-radius:var(--r-lg);padding:20px;margin-bottom:20px;';

  const title = document.createElement('div');
  title.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);margin-bottom:12px;';
  title.textContent = 'Rules';
  tableWrap.appendChild(title);

  if (!rules.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'color:var(--text-3);font-size:12px;padding:8px 0;';
    empty.textContent = 'No parsed rules on this host.';
    tableWrap.appendChild(empty);
  } else {
    const table = document.createElement('table');
    table.className = 'vuln-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of ['Port', 'Protocol', 'Action', 'Source', 'Interface', '']) {
      const th = document.createElement('th');
      th.textContent = label;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    for (const r of rules) {
      if (!r || typeof r !== 'object') continue;
      const tr = document.createElement('tr');

      const portTd = document.createElement('td');
      portTd.className = 'mono';
      portTd.textContent = (r.port === undefined || r.port === null) ? '—' : String(r.port);
      tr.appendChild(portTd);

      const protoTd = document.createElement('td');
      protoTd.textContent = r.protocol || '—'; // host-reported — textContent
      tr.appendChild(protoTd);

      const actionTd = document.createElement('td');
      const actionSpan = document.createElement('span');
      actionSpan.style.color = r.action === 'deny' ? 'var(--rose)' : 'var(--mint)';
      actionSpan.textContent = r.action || '—'; // host-reported — textContent
      actionTd.appendChild(actionSpan);
      tr.appendChild(actionTd);

      const sourceTd = document.createElement('td');
      sourceTd.textContent = r.source || 'any'; // host-reported — textContent
      tr.appendChild(sourceTd);

      const ifaceTd = document.createElement('td');
      ifaceTd.textContent = r.interface || '—'; // host-reported — textContent
      tr.appendChild(ifaceTd);

      const actTd = document.createElement('td');
      actTd.style.textAlign = 'right';
      const portNum = typeof r.port === 'number' ? r.port : parseInt(r.port, 10);
      if (_fwIsProtected(portNum, tool)) {
        // The server would refuse this change (firewall_guard.check_change)
        // — do not offer an action known to fail.
        const label = document.createElement('span');
        label.style.cssText = 'font-size:11px;color:var(--text-3);';
        label.textContent = '[protected]';
        actTd.appendChild(label);
      } else {
        const rmBtn = document.createElement('button');
        rmBtn.className = 'btn btn-outline btn-sm';
        rmBtn.textContent = 'Remove';
        rmBtn.addEventListener('click', () => {
          applyFirewallChange(hostId, 'remove_firewall_rule', { port: r.port, protocol: r.protocol }, rmBtn);
        });
        actTd.appendChild(rmBtn);
      }
      tr.appendChild(actTd);

      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
  }
  el.appendChild(tableWrap);

  /* ── Unparsed rules — never hidden just because it's usually empty ── */
  const unparsed = Array.isArray(data.unparsed) ? data.unparsed : [];
  if (unparsed.length) {
    const uWrap = document.createElement('div');
    uWrap.style.cssText = 'background:var(--s1);border-radius:var(--r-lg);padding:20px;margin-bottom:20px;border:1px solid var(--lemon);';

    const uTitle = document.createElement('div');
    uTitle.style.cssText = 'font-weight:600;font-size:13px;color:var(--lemon);margin-bottom:6px;';
    uTitle.textContent = 'Unparsed rules';
    uWrap.appendChild(uTitle);

    const uSub = document.createElement('div');
    uSub.style.cssText = 'color:var(--text-3);font-size:12px;margin-bottom:10px;line-height:1.6;';
    uSub.textContent = 'These rules exist on the host but Vigil could not interpret them, so they cannot be edited here. A firewall view that silently omitted them would be worse than one that admits it.';
    uWrap.appendChild(uSub);

    const list = document.createElement('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:4px;';
    for (const item of unparsed) {
      const line = document.createElement('div');
      line.className = 'mono';
      line.style.cssText = 'font-size:12px;color:var(--text-2);white-space:pre-wrap;word-break:break-all;';
      // Raw host output — may be a bare string or an {raw: "..."} object
      // depending on which backend/path produced it. Either way this is
      // untrusted text and is set via textContent only, never innerHTML.
      line.textContent = typeof item === 'string' ? item
        : (item && typeof item === 'object' && typeof item.raw === 'string') ? item.raw
        : JSON.stringify(item);
      list.appendChild(line);
    }
    uWrap.appendChild(list);
    el.appendChild(uWrap);
  }

  /* ── Add-rule form ───────────────────────────────────────────────── */
  if (data.supported !== false) {
    const formWrap = document.createElement('div');
    formWrap.style.cssText = 'background:var(--s1);border-radius:var(--r-lg);padding:20px;';

    const fTitle = document.createElement('div');
    fTitle.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);margin-bottom:12px;';
    fTitle.textContent = 'Add a rule';
    formWrap.appendChild(fTitle);

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:10px;flex-wrap:wrap;align-items:center;';

    const portInput = document.createElement('input');
    portInput.type = 'number';
    portInput.min = '1';
    portInput.max = '65535';
    portInput.placeholder = 'Port';
    portInput.className = 'form-control';
    portInput.style.cssText = 'width:100px;';

    const protoSel = document.createElement('select');
    protoSel.className = 'form-control';
    protoSel.style.cssText = 'width:auto;';
    for (const p of ['tcp', 'udp']) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p;
      protoSel.appendChild(opt);
    }

    const dispSel = document.createElement('select');
    dispSel.className = 'form-control';
    dispSel.style.cssText = 'width:auto;';
    for (const p of ['allow', 'deny']) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p;
      dispSel.appendChild(opt);
    }

    const sourceInput = document.createElement('input');
    sourceInput.type = 'text';
    sourceInput.placeholder = 'Source (optional, default any)';
    sourceInput.className = 'form-control';
    sourceInput.style.cssText = 'width:220px;';

    const addBtn = document.createElement('button');
    addBtn.className = 'btn btn-outline btn-sm';
    addBtn.textContent = 'Add rule';
    addBtn.addEventListener('click', () => {
      const port = parseInt(portInput.value, 10);
      if (!port || port < 1 || port > 65535) {
        showToast('Enter a valid port (1-65535)', 'error');
        return;
      }
      const params = { port, protocol: protoSel.value, action: dispSel.value };
      const source = sourceInput.value.trim();
      if (source) params.source = source;
      applyFirewallChange(hostId, 'add_firewall_rule', params, addBtn);
    });

    row.append(portInput, protoSel, dispSel, sourceInput, addBtn);
    formWrap.appendChild(row);
    el.appendChild(formWrap);
  }
}

/* ── Writes ──────────────────────────────────────────────────────────── */

async function applyFirewallChange(hostId, action, params, btn) {
  const totp = (window.prompt('Enter your TOTP code to apply this firewall change:') || '').trim();
  if (!totp) return;
  const prevOpacity = btn ? btn.style.opacity : '';
  if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
  try {
    await apiJson(`/api/v1/hosts/${hostId}/firewall/apply/`, {
      method: 'POST',
      body: JSON.stringify({ action, params, totp }),
    });
    showToast("Change queued — it applies on the host's next check-in", 'success');
    setTimeout(() => loadFirewallSnapshot(hostId), 600);
  } catch (e) {
    // Covers both the lockout guard's 400 refusal and a 401 bad-TOTP —
    // apiJson already extracts body.error, so the operator reads why
    // rather than a generic "Request failed".
    showToast('Firewall change failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = prevOpacity || '1'; }
  }
}

document.getElementById('firewall-refresh-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('firewall-refresh-btn');
  const hostId = document.getElementById('firewall-host-select')?.value;
  if (!hostId) return;
  btn.disabled = true; btn.style.opacity = '0.5';
  try {
    await apiJson(`/api/v1/hosts/${hostId}/firewall/refresh/`, { method: 'POST' });
    showToast("Read queued — it lands on the host's next check-in", 'success');
  } catch (e) {
    showToast('Refresh failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
});

// Auto-load the first time the Firewall tab is opened, following the
// wrap-navigateTo pattern documented atop vigil-nav.js.
const _fwNavigateTo = navigateTo;
navigateTo = function (page) {
  _fwNavigateTo(page);
  if (page === 'firewall') loadFirewall();
};
