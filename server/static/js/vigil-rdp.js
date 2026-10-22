/* ── Web RDP (QA-59) ────────────────────────────────────────────────────────
   A Windows desktop in the browser. Not loaded by base.html: loadRdpAssets() in
   vigil-host-cards.js appends guacamole-common.js and this file the first time
   an operator picks Web RDP, so nobody pays for a megabyte of vendor JS they
   never use.

   What runs the desktop is Apache Guacamole's guacd, which Vigil reaches through
   apps/remote/rdp.py. The pipe is a WebSocket carrying Guacamole instructions as
   text, so this file contributes exactly two things: a tunnel that speaks
   Vigil's socket instead of Guacamole's HTTP one, and the sign-in form whose
   values are handed over once and then dropped.

   Those credentials are why three rules below hold. They go in the first frame
   and nowhere else — not the URL, not a variable kept for the session, not an
   input's value left in the DOM after connecting. And the desktop's own drawing
   never becomes markup: Guacamole paints it into a canvas, and this file writes
   none of it into the page. */

let _rdpSession = null;

async function openRdpSession(hostId) {
  const code = await totpPrompt('Open a remote desktop on this host',
    { title: 'Start a session', confirmText: 'Continue' });
  if (!code) return;

  let started;
  try {
    started = await apiJson('/api/v1/sessions/', {
      method: 'POST',
      body: JSON.stringify({ host: hostId, kind: 'rdp', totp: code }),
    });
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }
  _signInThenConnect(started);
}

/* The sign-in form. Its fields are read once into a local object and the form is
   removed at connect, so an operator who closes the tab mid-way leaves no
   password in a DOM node another page could still find. */
function _signInThenConnect(started) {
  const m = mountModal('rdp-login', { variant: 'm-pop' });
  m.setBody(`<div class="modal-title"><span id="rdp-login-title"></span>
      <button class="modal-close" type="button" id="rdp-login-x" aria-label="Cancel">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button></div>
    <form id="rdp-login-form" autocomplete="off">
      <div class="setting-row"><label for="rdp-user">Windows username</label>
        <input class="form-control" id="rdp-user" name="username" type="text"
               autocomplete="username" required></div>
      <div class="setting-row"><label for="rdp-pass">Password</label>
        <input class="form-control" id="rdp-pass" name="password" type="password"
               autocomplete="new-password"></div>
      <div class="setting-row"><label for="rdp-domain">Domain (optional)</label>
        <input class="form-control" id="rdp-domain" name="domain" type="text"
               autocomplete="off"></div>
      <div class="modal-actions">
        <button class="btn btn-outline btn-sm" type="button" id="rdp-login-cancel">Cancel</button>
        <button class="btn btn-sm" type="submit">Connect</button>
      </div>
    </form>`);
  m.modal.querySelector('#rdp-login-title').textContent =
    `Sign in to ${started.host || 'the host'}`;

  const drop = () => {
    // Blank the password before the element goes away: otherwise the field
    // holds it until the node itself is collected.
    const pass = m.modal.querySelector('#rdp-pass');
    if (pass) pass.value = '';
    m.close();
  };
  const form = m.modal.querySelector('#rdp-login-form');
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const login = {
      username: m.modal.querySelector('#rdp-user').value,
      password: m.modal.querySelector('#rdp-pass').value,
      domain: m.modal.querySelector('#rdp-domain').value,
    };
    drop();
    if (!login.username) { showToast('A Windows username is required.', 'error'); return; }
    _connectRdp(started, login);
  });
  m.modal.querySelector('#rdp-login-cancel').addEventListener('click', drop);
  m.modal.querySelector('#rdp-login-x').addEventListener('click', drop);
  m.overlay.addEventListener('click', drop);

  m.open();
}

function _connectRdp(started, login) {
  const m = mountModal('rdp', { xwide: true });
  m.modal.classList.add('rdp-modal');
  m.setBody(`<div class="modal-title"><span id="rdp-title"></span>
      <button class="modal-close" type="button" id="rdp-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button></div>
    <div class="rdp-bar">
      <span class="run-state t-lemon" id="rdp-state">Connecting…</span>
      ${started.recorded ? '<span class="chip chip-muted" title="guacd records this session">Recorded</span>' : ''}
      <button class="btn btn-outline btn-rose-ghost btn-xs" type="button" id="rdp-end">End session</button>
    </div>
    <div class="rdp-display" id="rdp-display"></div>`);
  m.modal.querySelector('#rdp-title').textContent = `Desktop — ${started.host || 'the host'}`;

  const stateEl = m.modal.querySelector('#rdp-state');
  const displayEl = m.modal.querySelector('#rdp-display');
  const setState = (text, cls) => {
    stateEl.textContent = text;
    stateEl.className = 'run-state ' + cls;
  };

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // login is handed to the tunnel and not referenced again after this line: it
  // is the password's last address in this file.
  const tunnel = new VigilRdpTunnel(`${proto}//${location.host}${started.ws}`, login);
  const client = new Guacamole.Client(tunnel);

  tunnel.onstatechange = (state) => {
    if (state === Guacamole.Tunnel.State.CONNECTING) setState('Connecting…', 't-lemon');
    else if (state === Guacamole.Tunnel.State.OPEN) setState('Connected', 't-mint');
    else setState('Closed', 't-rose');
  };
  client.onerror = (status) => {
    showToast(`Remote desktop: ${(status && status.message) || 'the connection failed'}`, 'error');
  };

  displayEl.appendChild(client.getDisplay().getElement());

  const mouse = new Guacamole.Mouse(client.getDisplay().getElement());
  // onmousemove covers pointer motion, the button pair covers clicks; sending
  // the whole state each time is what makes a drag look like a drag remotely.
  mouse.onmousedown = (data) => { client.sendMouseState(data.state, true); data.preventDefault(); };
  mouse.onmouseup = (data) => { client.sendMouseState(data.state, true); data.preventDefault(); };
  mouse.onmousemove = (data) => { client.sendMouseState(data.state, true); };

  /* Guacamole's own guidance is a document-wide keyboard: a key pressed while
     the canvas has focus still has to reach Windows, and losing focus must not
     leave a key held down on the far desktop. */
  const keyboard = new Guacamole.Keyboard(document);
  keyboard.onkeydown = (keysym) => { client.sendKeyEvent(1, keysym); return false; };
  keyboard.onkeyup = (keysym) => { client.sendKeyEvent(0, keysym); };

  let observer = null;
  let debounce = null;
  if (window.ResizeObserver) {
    // Debounced: a drag on the dialog's grip would otherwise ask guacd to
    // re-display the desktop once per frame, and each of those is a round trip.
    observer = new ResizeObserver(() => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        const w = Math.floor(displayEl.clientWidth);
        const h = Math.floor(displayEl.clientHeight);
        if (w > 0 && h > 0) client.sendSize(w, h);
      }, 250);
    });
    observer.observe(displayEl);
  }

  const teardown = () => {
    if (_rdpSession && _rdpSession.teardownDone) return;
    if (_rdpSession) _rdpSession.teardownDone = true;
    clearTimeout(debounce);
    if (observer) observer.disconnect();
    // Guacamole's keyboard listeners live on the document. Left attached, they
    // would swallow this page's keystrokes for whatever session came next.
    keyboard.onkeydown = null;
    keyboard.onkeyup = null;
    keyboard.reset();
    try { client.disconnect(); } catch (e) {}
    _rdpSession = null;
  };

  const end = () => { teardown(); _endRdpSession(started.id); m.close(); };
  m.modal.querySelector('#rdp-end').addEventListener('click', end);
  m.modal.querySelector('#rdp-x').addEventListener('click', end);
  // addEventListener, never an inline handler: the CSP has no 'unsafe-inline',
  // so generated markup carrying one is a button that silently does nothing.
  m.overlay.addEventListener('click', end);

  _rdpSession = { teardown: teardown, teardownDone: false };
  client.connect('');
  m.open();
}

//: End it server-side as well: closing the socket ends the pipe, and this is
//: what puts "closed" on the row the sessions list shows.
async function _endRdpSession(sessionId) {
  try {
    await apiJson(`/api/v1/sessions/${sessionId}/close/`, { method: 'POST' });
  } catch (e) {
    // The socket we just closed is the authoritative end; a 404 here means it
    // had already finished, which is what the operator asked for.
  }
}

/* ── The tunnel ─────────────────────────────────────────────────────────────
   Guacamole.Client takes any object shaped like a Guacamole.Tunnel: connect,
   sendMessage, disconnect, state, and the handlers the client sets on it. This
   one's transport is a WebSocket to Vigil rather than Guacamole's HTTP tunnel —
   which is also why the vendored HTTP tunnels are not used here: they put their
   parameters in a query string, and an access log would keep a Windows password. */
function VigilRdpTunnel(url, login) {
  const tunnel = this;
  let socket = null;

  this.state = Guacamole.Tunnel.State.CONNECTING;

  /* The Guacamole parser buffers partial instructions across WebSocket frames:
     a desktop's drawing is far larger than one message and arrives in pieces. */
  const parser = new Guacamole.Parser();
  parser.oninstruction = (opcode, args) => {
    if (tunnel.oninstruction) tunnel.oninstruction(opcode, args);
  };

  this.connect = function connect() {
    socket = new WebSocket(url, 'guacamole');
    socket.onopen = () => {
      // The credentials in the first frame — the only place they ever appear —
      // plus the space we have for the desktop. devicePixelRatio is what makes
      // a HiDPI laptop's remote desktop readable rather than blurry.
      const dpr = window.devicePixelRatio || 1;
      socket.send(JSON.stringify({
        t: 'rdp_login',
        username: login.username,
        password: login.password,
        domain: login.domain || '',
        width: Math.floor(window.innerWidth * 0.8),
        height: Math.floor(window.innerHeight * 0.72),
        dpi: Math.round(96 * dpr),
      }));
      // Not "the object is small so it hardly matters": the field that holds a
      // password is exactly the one worth proving is gone.
      login = null;
      tunnel.setState(Guacamole.Tunnel.State.OPEN);
    };
    socket.onmessage = (event) => {
      if (typeof event.data === 'string') parser.receive(event.data);
    };
    socket.onerror = () => {
      if (tunnel.onerror) {
        tunnel.onerror(new Guacamole.Status(512, 'the connection failed'));
      }
    };
    socket.onclose = () => tunnel.setState(Guacamole.Tunnel.State.CLOSED);
  };

  this.sendMessage = function sendMessage(...elements) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(_encodeInstruction(elements));
  };

  this.disconnect = function disconnect() {
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    tunnel.setState(Guacamole.Tunnel.State.CLOSED);
  };

  /* setState is the Tunnel contract's own notifier: onstatechange fires only on
     a change, which is what keeps the status pill from re-painting per frame. */
  this.setState = function setState(state) {
    if (state === tunnel.state) return;
    tunnel.state = state;
    if (tunnel.onstatechange) tunnel.onstatechange(state);
  };
}

//: Element lengths count Unicode code points — which is what Array.from counts,
//: and not what .length counts once a value leaves the BMP. An instruction whose
//: length lies does not fail loudly: it truncates a desktop's drawing.
function _encodeInstruction(elements) {
  let out = '';
  for (const element of elements) {
    const value = String(element);
    out += `${Array.from(value).length}.${value},`;
  }
  return `${out};`;
}
