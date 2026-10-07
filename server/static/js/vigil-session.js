// vigil-session.js — sign-in session timeout (QA-14): a heartbeat on real use,
// a warning two minutes before the server signs this browser out, and the
// redirect to the login page once it has. The server is the authority; this
// file only keeps the person informed. Several tabs share one session, so
// before warning or leaving, a tab asks the server instead of trusting its timer.
//
// The one-second tick runs on pollingInterval rather than a bare setInterval,
// which the polling guard in apps/hosts/test_polling_guard.py requires; a
// hidden tab skips its ticks and the visibility catch-up re-checks the server
// the moment it is in front of someone again.
const SESSION_WARN_SECONDS = 120;
const SESSION_HEARTBEAT_MS = 60 * 1000;
const sessionState = { deadline: 0, limit: 'idle', lastBeat: 0, warning: null, timer: null };

function _sessionApply(body) {
  const idle = Number(body.idle_seconds_left);
  const max = Number(body.max_seconds_left);
  if (!Number.isFinite(idle) || !Number.isFinite(max)) return;
  sessionState.deadline = Date.now() + 1000 * Math.min(idle, max);
  sessionState.limit = max <= idle ? 'max' : 'idle';
}

async function _sessionFetch(method) {
  let resp;
  try {
    resp = await fetch('/api/v1/accounts/session/' + (method === 'POST' ? 'activity/' : ''), {
      method,
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': getCsrf() },
    });
  } catch (_e) {
    return;  // a flaky network must never be the reason someone signs out
  }
  const body = await resp.json().catch(() => ({}));
  if (resp.status === 401) {
    _sessionLeave(body.reason === 'session_max' ? 'max' : 'idle');
    return;
  }
  if (resp.ok) _sessionApply(body);
}

function _sessionLeave(reason) {
  if (sessionState.timer) {
    clearPollingInterval(sessionState.timer);
    sessionState.timer = null;
  }
  location.href = '/login/?expired=' + reason + '&next='
    + encodeURIComponent(location.pathname + location.search + location.hash);
}

function _sessionOnUse() {
  if (Date.now() - sessionState.lastBeat < SESSION_HEARTBEAT_MS) return;
  sessionState.lastBeat = Date.now();
  _sessionFetch('POST');
}

function _sessionMMSS(seconds) {
  const s = Math.max(0, seconds | 0);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function _sessionMessage(left) {
  return sessionState.limit === 'max'
    ? 'This sign-in reaches its time limit in ' + _sessionMMSS(left)
      + ". Save your work — you'll need to sign in again."
    : "You'll be signed out in " + _sessionMMSS(left) + ' for inactivity.';
}

function _sessionWarn() {
  const m = mountModal('session-warn', { variant: 'm-pop' });
  m.setBody(`
    <div class="modal-title"><span id="session-warn-title"></span></div>
    <div class="confirm-msg session-warn-msg" id="session-warn-msg"></div>
    <div class="confirm-actions" id="session-warn-actions"></div>`);
  m.modal.querySelector('#session-warn-title').textContent =
    sessionState.limit === 'max' ? 'Your sign-in is ending' : 'Still there?';
  const actions = m.modal.querySelector('#session-warn-actions');
  const signOut = document.createElement('button');
  signOut.className = 'btn btn-outline btn-sm';
  signOut.textContent = 'Sign out now';
  signOut.addEventListener('click', () => { location.href = '/logout/'; });
  actions.appendChild(signOut);
  // "Stay signed in" is an idle-only offer: the hard cap cannot be extended,
  // and a button that could not deliver would be a lie.
  if (sessionState.limit === 'idle') {
    const stay = document.createElement('button');
    stay.className = 'btn btn-sm btn-mint';
    stay.textContent = 'Stay signed in';
    stay.addEventListener('click', () => {
      sessionState.lastBeat = 0;
      _sessionFetch('POST');
      m.close();
    });
    actions.appendChild(stay);
  }
  // mountModal wires the overlay to close(); a stray click must not dismiss the
  // warning without extending the session, so it does nothing here.
  m.overlay.onclick = () => {};
  sessionState.warning = m;
  _sessionSetMessage();
  requestAnimationFrame(m.open);
  return m;
}

function _sessionSetMessage() {
  if (!sessionState.warning) return;
  const msg = sessionState.warning.modal.querySelector('#session-warn-msg');
  if (msg) {
    msg.textContent = _sessionMessage(Math.ceil((sessionState.deadline - Date.now()) / 1000));
  }
}

function _sessionCloseWarn() {
  if (!sessionState.warning) return;
  sessionState.warning.close();
  sessionState.warning = null;
}

let _sessionTickBusy = false;

async function _sessionTick() {
  if (_sessionTickBusy) return;
  _sessionTickBusy = true;
  try {
    let left = Math.ceil((sessionState.deadline - Date.now()) / 1000);
    if (left > SESSION_WARN_SECONDS) {
      _sessionCloseWarn();
      return;
    }
    if (left > 0) {
      if (!sessionState.warning) {
        // Another tab's heartbeat extends this session for every tab, so ask
        // before alarming anyone.
        await _sessionFetch('GET');
        left = Math.ceil((sessionState.deadline - Date.now()) / 1000);
        if (left <= 0) {
          _sessionLeave(sessionState.limit);
          return;
        }
        if (left > SESSION_WARN_SECONDS) return;
        _sessionWarn();
      }
      _sessionSetMessage();
      return;
    }
    await _sessionFetch('GET');
    if (Math.ceil((sessionState.deadline - Date.now()) / 1000) <= 0) {
      _sessionLeave(sessionState.limit);
    }
  } finally {
    _sessionTickBusy = false;
  }
}

document.addEventListener('pointerdown', _sessionOnUse, { passive: true });
document.addEventListener('keydown', _sessionOnUse, { passive: true });
document.addEventListener('wheel', _sessionOnUse, { passive: true });
document.addEventListener('touchstart', _sessionOnUse, { passive: true });

document.addEventListener('DOMContentLoaded', () => {
  _sessionFetch('GET');
  sessionState.timer = pollingInterval(_sessionTick, 1000);
});
